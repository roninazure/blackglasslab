from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from flash_employment.bls import (
    SOURCE_RSS,
    SOURCE_SUMMARY,
    SOURCE_TABLE_B1,
    BLSReleaseError,
    BLSStaleReleaseError,
    SourceArbiter,
    SourceAttempt,
    evidence_from_attempt,
    parse_feed,
    parse_summary_payload,
    parse_table_b1_payload,
)
from flash_employment.core import (
    BRACKETS,
    Contract,
    MarketBundle,
    evaluate_known_payout,
    resolve_payroll_change,
)
from flash_employment.operations import (
    MIN_SOURCE_POLL_SECONDS,
    REQUIRED_STATIC_GATES,
    SOURCE_PHASE_OFFSETS_SECONDS,
    DuplicateTrialError,
    ReadinessFailure,
    TrialLock,
    source_poll_sleep_seconds,
)
from flash_employment.storage import TrialStore
from flash_employment.trial import EASTERN, FlashTrial, TrialConfig, run_config


def feed(*, content: str, published: str = "2026-08-07T08:30:00-04:00") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <id>bls.gov:feed:empsit</id>
      <entry>
        <id>empsit-2026_08_07__08_30_00</id>
        <published>{published}</published>
        <content>{content}</content>
        <link href="https://www.bls.gov/news.release/archives/empsit_08072026.htm" />
      </entry>
    </feed>""".encode()


def summary_html(
    *,
    period: str = "July",
    year: int = 2026,
    release_month: str = "August",
    release_day: int = 7,
    sentence: str = "Total nonfarm payroll employment changed little in July (-23,000).",
    extra: str = "",
) -> bytes:
    return f"""<html><body>
    <h1>Employment Situation Summary</h1>
    <pre>8:30 a.m. (ET) Friday, {release_month} {release_day}, 2026
    THE EMPLOYMENT SITUATION - {period.upper()} {year}</pre>
    <h2>Establishment Survey Data</h2><p>{sentence}</p><p>{extra}</p>
    </body></html>""".encode()


def table_b1_html(
    *,
    period: str = "July",
    prior: str = "June",
    release_month: str = "August",
    release_day: int = 7,
    nsa_prior: str = "159,748",
    nsa_target: str = "158,649",
    sa_prior: str = "158,881",
    sa_target: str = "158,858",
    change: str = "-23",
) -> bytes:
    return f"""<html><body>
    <h1>Table B-1. Employees on nonfarm payrolls by industry sector and selected industry detail</h1>
    <p>ESTABLISHMENT DATA [In thousands]</p>
    <table>
      <tr><th>Industry</th><th colspan="4">Not seasonally adjusted</th><th colspan="5">Seasonally adjusted</th></tr>
      <tr><th>Change from: {prior} 2026 - {period} 2026</th></tr>
      <tr><td>Total nonfarm</td><td>158,267</td><td>159,386</td><td>{nsa_prior}</td><td>{nsa_target}</td><td>158,542</td><td>158,861</td><td>{sa_prior}</td><td>{sa_target}</td><td>{change}</td></tr>
      <tr><td>Total private</td><td>1</td><td>2</td><td>3</td><td>4</td><td>5</td><td>6</td><td>7</td><td>8</td><td>99</td></tr>
    </table>
    <p>Last Modified Date: {release_month} {release_day:02d}, 2026</p>
    </body></html>""".encode()


def attempt(
    source: str,
    *,
    value: int | None = -23_000,
    mono_ns: int = 1_000_000,
    result: str = "VALID",
    reason: str | None = None,
) -> SourceAttempt:
    return SourceAttempt(
        source_name=source,
        source_url=f"https://www.bls.gov/{source}",
        phase="TEST",
        request_number=1,
        request_started_wall_ns=10,
        request_started_monotonic_ns=mono_ns - 3_000,
        first_byte_wall_ns=11,
        first_byte_monotonic_ns=mono_ns - 2_000,
        body_complete_wall_ns=12,
        body_complete_monotonic_ns=mono_ns - 1_000,
        parse_complete_wall_ns=13,
        parse_complete_monotonic_ns=mono_ns,
        http_status=200 if result not in {"HTTP_ERROR", "TIMEOUT"} else None,
        http_date="Fri, 04 Sep 2026 12:30:00 GMT",
        age="0",
        etag='"etag"',
        last_modified="Fri, 04 Sep 2026 12:30:00 GMT",
        cache_control="max-age=0",
        payload=b"evidence",
        payload_sha256=hashlib.sha256(b"evidence").hexdigest(),
        parsed_value=value,
        reference_year=2026 if value is not None else None,
        reference_month=8 if value is not None else None,
        published_at_utc="2026-09-04T12:30:00Z" if value is not None else None,
        entry_id=f"{source}-entry" if value is not None else None,
        release_url=f"https://www.bls.gov/{source}",
        provenance_text="fixture" if value is not None else None,
        validation_result=result,
        rejection_reason=reason,
    )


def flash_bundle() -> MarketBundle:
    contracts = tuple(
        Contract(
            bracket=bracket,
            market_id=f"market-{index}",
            condition_id=f"condition-{index}",
            yes_token=str(index * 2 + 1),
            no_token=str(index * 2 + 2),
            fee_rate=0.05,
            fee_exponent=1.0,
            fee_source="gamma.feeSchedule",
        )
        for index, bracket in enumerate(BRACKETS)
    )
    return MarketBundle(
        "event", "slug", "title", "rules", "https://www.bls.gov/x", contracts
    )


def flash_config(db_path: Path) -> TrialConfig:
    event_date = date(2026, 9, 4)
    return TrialConfig(
        rehearsal=True,
        db_path=db_path,
        event_date=event_date,
        arm_at=datetime(2026, 9, 4, 8, 25, tzinfo=EASTERN),
        release_at=datetime(2026, 9, 4, 8, 30, tzinfo=EASTERN),
        duration_minutes=15.0,
    )


def ready_trial(path: Path) -> tuple[FlashTrial, TrialStore]:
    bundle = flash_bundle()
    store = TrialStore(path)
    run_id = store.start_run(
        mode="REHEARSAL",
        wall_ns=1,
        mono_ns=2,
        iso_utc="2026-09-01T00:00:00Z",
        event_date="2026-09-04",
        reference_year=2026,
        reference_month=8,
        bundle=bundle,
        slippage_bps=10,
    )
    trial = FlashTrial(flash_config(path), bundle, store, run_id)
    for asset in bundle.assets:
        trial.book.apply(
            {
                "event_type": "book",
                "asset_id": asset,
                "bids": [{"price": "0.39", "size": "7"}],
                "asks": [{"price": "0.40", "size": "5"}],
            }
        )
        trial.full_books.add(asset)
    trial.books_ready.set()
    return trial, store


def historical_attempt(source: str, *, result: str = "VALID") -> SourceAttempt:
    base = attempt(
        source,
        value=-23_000 if result == "VALID" else None,
        result=result,
        reason=None if result == "VALID" else result.lower(),
    )
    return replace(
        base,
        phase="PREFLIGHT",
        reference_month=7 if result == "VALID" else None,
        published_at_utc="2026-08-07T12:30:00Z" if result == "VALID" else None,
    )


def operationally_ready_trial(path: Path) -> tuple[FlashTrial, TrialStore]:
    trial, store = ready_trial(path)
    trial.stats.connection_count = 1
    trial.preflight_attempts = {
        source: historical_attempt(source)
        for source in (SOURCE_RSS, SOURCE_SUMMARY, SOURCE_TABLE_B1)
    }
    for gate in REQUIRED_STATIC_GATES:
        trial.record_gate(gate, True, "fixture pass")
    return trial, store


class ResolutionTests(unittest.TestCase):
    def test_boundaries_and_extremes(self) -> None:
        cases = {
            -(10**9): "<-50k",
            -75_000: "<-50k",
            -50_001: "<-50k",
            -50_000: "-50k–0",
            -1: "-50k–0",
            0: "0–50k",
            49_999: "0–50k",
            50_000: "50k–100k",
            99_999: "50k–100k",
            100_000: "100k–150k",
            149_999: "100k–150k",
            150_000: "150k+",
            10**9: "150k+",
        }
        self.assertEqual(
            {value: resolve_payroll_change(value) for value in cases}, cases
        )

    def test_non_integer_fails_closed(self) -> None:
        with self.assertRaises(TypeError):
            resolve_payroll_change(50_000.0)  # type: ignore[arg-type]


class BLSTests(unittest.TestCase):
    def test_structured_feed_parses_signed_change_and_timestamps(self) -> None:
        payload = feed(
            content="Both nonfarm payroll employment (-23,000) and the unemployment rate changed little in July."
        )
        evidence = parse_feed(
            payload,
            expected_year=2026,
            expected_month=7,
            expected_release_date="2026-08-07",
            receipt_wall_time_ns=1_725_000_000_000_000_000,
            receipt_monotonic_ns=987_654_321,
        )
        self.assertEqual(evidence.change_jobs, -23_000)
        self.assertEqual(evidence.reference_month, 7)
        self.assertEqual(evidence.payload_sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(evidence.receipt_wall_time_ns, 1_725_000_000_000_000_000)
        self.assertEqual(evidence.receipt_monotonic_ns, 987_654_321)
        self.assertTrue(evidence.receipt_iso_utc.endswith("Z"))

    def test_boundary_style_feed_parses(self) -> None:
        payload = feed(
            content="Total nonfarm payroll employment changed little in August (+50,000).",
            published="2026-09-04T08:30:00-04:00",
        )
        evidence = parse_feed(
            payload,
            expected_year=2026,
            expected_month=8,
            expected_release_date="2026-09-04",
        )
        self.assertEqual(evidence.change_jobs, 50_000)
        self.assertEqual(resolve_payroll_change(evidence.change_jobs), "50k–100k")

    def test_directional_negative_feed_parses(self) -> None:
        payload = feed(
            content="Total nonfarm payroll employment declined by 92,000 in July."
        )
        self.assertEqual(
            parse_feed(payload, expected_year=2026, expected_month=7).change_jobs,
            -92_000,
        )

    def test_wrong_period_fails_closed(self) -> None:
        payload = feed(
            content="Both nonfarm payroll employment (-23,000) and the unemployment rate changed little in July."
        )
        with self.assertRaisesRegex(BLSReleaseError, "found 0"):
            parse_feed(payload, expected_year=2026, expected_month=8)

    def test_malformed_and_ambiguous_response_fail_closed(self) -> None:
        with self.assertRaisesRegex(BLSReleaseError, "malformed"):
            parse_feed(b"not xml", expected_year=2026, expected_month=8)
        payload = feed(
            content="Total nonfarm payroll employment was discussed in July without a value."
        )
        with self.assertRaisesRegex(BLSReleaseError, "deterministic"):
            parse_feed(payload, expected_year=2026, expected_month=7)

    def test_summary_parser_selects_target_change_not_prior_revision(self) -> None:
        payload = summary_html(
            period="August",
            release_month="September",
            release_day=4,
            sentence="Total nonfarm payroll employment increased by 50,000 in August.",
            extra=(
                "The change in total nonfarm payroll employment for June was revised "
                "from +100,000 to +20,000."
            ),
        )
        parsed = parse_summary_payload(
            payload,
            expected_year=2026,
            expected_month=8,
            expected_release_date="2026-09-04",
        )
        self.assertEqual(parsed.change_jobs, 50_000)

    def test_cached_july_summary_is_rejected_for_live_august(self) -> None:
        with self.assertRaises(BLSStaleReleaseError):
            parse_summary_payload(
                summary_html(),
                expected_year=2026,
                expected_month=8,
                expected_release_date="2026-09-04",
            )

    def test_summary_malformed_or_ambiguous_fails_closed(self) -> None:
        with self.assertRaises(BLSReleaseError):
            parse_summary_payload(b"<html>not an employment release</html>")
        payload = summary_html(
            sentence="Total nonfarm payroll employment increased by 50,000 in July.",
            extra="Total nonfarm payroll employment declined by 10,000 in July.",
        )
        with self.assertRaisesRegex(BLSReleaseError, "2 target-period"):
            parse_summary_payload(payload)

    def test_table_b1_uses_sa_monthly_change_not_level_or_nsa(self) -> None:
        payload = table_b1_html(
            period="August",
            prior="July",
            release_month="September",
            release_day=4,
            nsa_prior="170,000",
            nsa_target="160,000",
            sa_prior="158,858",
            sa_target="158,908",
            change="50",
        )
        parsed = parse_table_b1_payload(
            payload,
            expected_year=2026,
            expected_month=8,
            expected_release_date="2026-09-04",
        )
        self.assertEqual(parsed.change_jobs, 50_000)
        self.assertNotEqual(parsed.change_jobs, 158_908_000)
        self.assertNotEqual(parsed.change_jobs, -10_000_000)

    def test_table_b1_rejects_inconsistent_change_column(self) -> None:
        with self.assertRaisesRegex(BLSReleaseError, "inconsistent"):
            parse_table_b1_payload(
                table_b1_html(sa_prior="158,881", sa_target="158,858", change="158,858")
            )

    def test_table_b1_wrong_period_and_malformed_fail_closed(self) -> None:
        with self.assertRaises(BLSStaleReleaseError):
            parse_table_b1_payload(
                table_b1_html(),
                expected_year=2026,
                expected_month=8,
                expected_release_date="2026-09-04",
            )
        with self.assertRaises(BLSReleaseError):
            parse_table_b1_payload(b"<html>malformed table</html>")


class SourceRaceTests(unittest.TestCase):
    def test_each_official_source_can_win_race(self) -> None:
        for source in (SOURCE_RSS, SOURCE_SUMMARY, SOURCE_TABLE_B1):
            with self.subTest(source=source):
                arbiter = SourceArbiter()
                self.assertEqual(arbiter.submit(attempt(source)), "WINNER")
                self.assertEqual(arbiter.winner.source_name, source)

    def test_later_sources_confirm_and_arrival_deltas_are_monotonic(self) -> None:
        arbiter = SourceArbiter()
        self.assertEqual(
            arbiter.submit(attempt(SOURCE_RSS, mono_ns=1_000_000)), "WINNER"
        )
        self.assertEqual(
            arbiter.submit(attempt(SOURCE_SUMMARY, mono_ns=3_000_000)), "CONFIRMED"
        )
        self.assertEqual(
            arbiter.submit(attempt(SOURCE_TABLE_B1, mono_ns=4_500_000)), "CONFIRMED"
        )
        self.assertEqual(len(arbiter.confirmations), 2)
        self.assertEqual(arbiter.valid_delta_ms(SOURCE_RSS), 0.0)
        self.assertEqual(arbiter.valid_delta_ms(SOURCE_SUMMARY), 2.0)
        self.assertEqual(arbiter.valid_delta_ms(SOURCE_TABLE_B1), 3.5)

    def test_conflict_marks_execution_claim_invalid(self) -> None:
        arbiter = SourceArbiter()
        arbiter.submit(attempt(SOURCE_RSS, value=50_000))
        outcome = arbiter.submit(attempt(SOURCE_SUMMARY, value=49_000))
        self.assertEqual(outcome, "OFFICIAL_SOURCE_CONFLICT")
        self.assertTrue(arbiter.official_source_conflict)

    def test_http_error_and_timeout_do_not_kill_another_source(self) -> None:
        for failed_result in ("HTTP_ERROR", "TIMEOUT"):
            with self.subTest(failed_result=failed_result):
                arbiter = SourceArbiter()
                failed = attempt(
                    SOURCE_RSS,
                    value=None,
                    result=failed_result,
                    reason=failed_result.lower(),
                )
                self.assertEqual(arbiter.submit(failed), "REJECTED")
                self.assertEqual(arbiter.submit(attempt(SOURCE_TABLE_B1)), "WINNER")

    def test_all_sources_fail_or_stale_means_no_decision(self) -> None:
        for result in ("INVALID", "STALE", "NOT_MODIFIED"):
            arbiter = SourceArbiter()
            for source in (SOURCE_RSS, SOURCE_SUMMARY, SOURCE_TABLE_B1):
                arbiter.submit(
                    attempt(source, value=None, result=result, reason=result.lower())
                )
            self.assertIsNone(arbiter.winner)

    def test_first_valid_is_available_before_confirmation(self) -> None:
        arbiter = SourceArbiter()
        outcome = arbiter.submit(attempt(SOURCE_SUMMARY))
        self.assertEqual(outcome, "WINNER")
        self.assertIsNotNone(arbiter.winner)
        self.assertEqual(arbiter.confirmations, [])


class EconomicsTests(unittest.TestCase):
    def test_known_payout_economics_and_depth_cap(self) -> None:
        rows = evaluate_known_payout(
            token="token",
            outcome_side="YES",
            ask_levels=((0.40, 5.0), (0.50, 7.0)),
            fee_rate=0.05,
            fee_exponent=1.0,
            slippage_bps=10,
        )
        self.assertEqual(sum(row.available_shares for row in rows), 12.0)
        self.assertTrue(all(row.net_edge_per_share > 0 for row in rows))
        self.assertAlmostEqual(rows[0].fee_per_share, 0.05 * 0.4 * 0.6)
        requested_shares = 100.0
        paper_shares = min(requested_shares, sum(row.available_shares for row in rows))
        self.assertEqual(paper_shares, 12.0)

    def test_zero_or_negative_net_edge_rejects_trade(self) -> None:
        rows = evaluate_known_payout(
            token="token",
            outcome_side="NO",
            ask_levels=((0.999, 10.0),),
            fee_rate=0.05,
            fee_exponent=0.0,
            slippage_bps=10,
        )
        self.assertEqual(rows, ())


class HotPathTests(unittest.TestCase):
    def test_resolution_to_economics_is_in_memory_and_uses_current_book(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            trial, store = ready_trial(Path(td) / "trial.sqlite")
            winning_token = trial.bundle.contracts[1].yes_token
            trial.book.books[winning_token]["asks"] = {0.25: 3.0}
            persisted_after_t2: list[bool] = []
            original_observation = store.observation
            original_execution = store.paper_execution

            def observation(values: tuple[object, ...]) -> None:
                persisted_after_t2.append(trial.economics_mono_ns is not None)
                original_observation(values)

            def execution(values: tuple[object, ...]) -> None:
                persisted_after_t2.append(trial.economics_mono_ns is not None)
                original_execution(values)

            with (
                patch.object(store, "observation", side_effect=observation),
                patch.object(store, "paper_execution", side_effect=execution),
                patch(
                    "flash_employment.trial.PolymarketAdapter.get_event",
                    side_effect=AssertionError("market discovery entered hot path"),
                ) as discovery,
                patch.object(
                    trial.source_clients[SOURCE_RSS],
                    "fetch",
                    side_effect=AssertionError("HTTP entered hot path"),
                ) as http,
            ):
                trial.accept_release(evidence_from_attempt(attempt(SOURCE_RSS)))

            decision = next(
                row for row in trial.paper_decisions if row[0] == winning_token
            )
            aggregate = decision[3]
            self.assertEqual(aggregate["price"], 0.25)
            self.assertEqual(aggregate["shares"], 3.0)
            self.assertLessEqual(aggregate["shares"], 3.0)
            self.assertTrue(persisted_after_t2)
            self.assertTrue(all(persisted_after_t2))
            discovery.assert_not_called()
            http.assert_not_called()
            self.assertEqual(trial.economics_timing["market_discovery_us"], 0.0)
            self.assertEqual(trial.economics_timing["fee_metadata_fetch_us"], 0.0)
            self.assertEqual(trial.economics_timing["rest_clob_requests_us"], 0.0)
            self.assertEqual(
                trial.economics_timing["http_requests_started_between_t1_t2"], 0
            )
            self.assertEqual(trial.economics_timing["sqlite_before_t2_us"], 0.0)
            store.close()


class HotPathStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_rehearsal_source_fetch_waits_for_all_books(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            trial, store = ready_trial(Path(td) / "trial.sqlite")
            trial.books_ready.clear()
            with patch.object(trial, "_preflight", new_callable=AsyncMock) as preflight:
                task = asyncio.create_task(trial.source_task())
                await asyncio.sleep(0)
                preflight.assert_not_awaited()
                trial.books_ready.set()
                await task
                preflight.assert_awaited_once_with()
            store.close()


class OperationalReadinessTests(unittest.TestCase):
    def test_incomplete_books_and_unreachable_bls_do_not_arm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            trial, store = operationally_ready_trial(Path(td) / "trial.sqlite")
            trial.full_books.pop()
            trial.preflight_attempts[SOURCE_RSS] = historical_attempt(
                SOURCE_RSS, result="HTTP_ERROR"
            )
            with self.assertRaises(ReadinessFailure):
                trial.arm()
            self.assertFalse(trial.armed)
            self.assertFalse(trial.gates["CLOB_BOOKS"].passed)
            self.assertFalse(trial.gates["BLS_RSS"].passed)
            store.close()

    def test_current_release_is_not_accepted_as_historical_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            trial, store = operationally_ready_trial(Path(td) / "trial.sqlite")
            trial.preflight_attempts[SOURCE_SUMMARY] = replace(
                attempt(SOURCE_SUMMARY), phase="PREFLIGHT"
            )
            with self.assertRaises(ReadinessFailure):
                trial.arm()
            self.assertFalse(trial.gates["BLS_SUMMARY"].passed)
            self.assertFalse(trial.gates["BLS_PRERELEASE_NON_ACTIONABLE"].passed)
            store.close()

    def test_armed_only_after_all_gates_and_safety_gate_is_mandatory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            trial, store = operationally_ready_trial(Path(td) / "trial.sqlite")
            trial.gates["PAPER_ONLY_MODE"] = replace(
                trial.gates["PAPER_ONLY_MODE"], passed=False, detail="unsafe mode"
            )
            with self.assertRaises(ReadinessFailure):
                trial.arm()
            trial.record_gate("PAPER_ONLY_MODE", True, "mode=PAPER")
            trial.arm()
            self.assertTrue(trial.armed)
            store.close()

    def test_mandatory_invariant_loss_disarms(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            trial, store = operationally_ready_trial(Path(td) / "trial.sqlite")
            trial.arm()
            asyncio.run(trial.on_stream_gap("stream_disconnect", 1))
            self.assertFalse(trial.armed)
            self.assertIn("stream_disconnect", trial.disarmed_reason or "")
            store.close()

    def test_duplicate_lock_rejects_second_owner_and_releases(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            first = TrialLock("event", date(2026, 9, 4), root=Path(td))
            second = TrialLock("event", date(2026, 9, 4), root=Path(td))
            first.acquire()
            with self.assertRaises(DuplicateTrialError):
                second.acquire()
            first.release()
            second.acquire()
            second.release()

    def test_source_phases_and_minimum_per_endpoint_cadence(self) -> None:
        offsets = tuple(SOURCE_PHASE_OFFSETS_SECONDS.values())
        self.assertEqual(offsets, (0.0, 0.33, 0.66))
        self.assertEqual(len(set(offsets)), 3)
        sleep = source_poll_sleep_seconds(
            request_started_mono_ns=1_000_000_000,
            now_mono_ns=1_200_000_000,
            after_release_seconds=10,
        )
        self.assertAlmostEqual(sleep, MIN_SOURCE_POLL_SECONDS - 0.2)
        self.assertEqual(
            source_poll_sleep_seconds(
                request_started_mono_ns=1_000_000_000,
                now_mono_ns=6_000_000_000,
                after_release_seconds=31,
            ),
            0.0,
        )


class PreliminaryGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_contact_is_persisted_and_never_arms(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "missing-contact.sqlite"
            with (
                patch.dict(os.environ, {}, clear=True),
                self.assertRaises(ReadinessFailure),
            ):
                await run_config(flash_config(db))
            conn = sqlite3.connect(db)
            row = conn.execute(
                "SELECT passed,detail FROM readiness_checks WHERE gate_name='FLASH_CONTACT'"
            ).fetchone()
            conn.close()
            self.assertEqual(row[0], 0)
            self.assertIn("missing", row[1])

    async def test_six_brackets_and_twelve_unique_tokens_are_mandatory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = replace(
                flash_config(Path(td) / "invalid-map.sqlite"), preflight_only=True
            )
            bad = replace(flash_bundle(), contracts=flash_bundle().contracts[:5])
            with (
                patch.dict(os.environ, {"FLASH_CONTACT": "ops@example.com"}),
                patch("flash_employment.trial.discover_bundle", return_value=bad),
                self.assertRaises(ReadinessFailure),
            ):
                await run_config(config)
            duplicate_contracts = list(flash_bundle().contracts)
            duplicate_contracts[-1] = replace(
                duplicate_contracts[-1], no_token=duplicate_contracts[0].yes_token
            )
            duplicate_tokens = replace(
                flash_bundle(), contracts=tuple(duplicate_contracts)
            )
            duplicate_config = replace(
                config, db_path=Path(td) / "duplicate-token.sqlite"
            )
            with (
                patch.dict(os.environ, {"FLASH_CONTACT": "ops@example.com"}),
                patch(
                    "flash_employment.trial.discover_bundle",
                    return_value=duplicate_tokens,
                ),
                self.assertRaises(ReadinessFailure),
            ):
                await run_config(duplicate_config)


class StorageTests(unittest.TestCase):
    def test_trial_records_distinguish_rehearsal_and_live(self) -> None:
        bundle = MarketBundle(
            "event", "slug", "title", "rules", "https://www.bls.gov/x", ()
        )
        with tempfile.TemporaryDirectory() as td:
            store = TrialStore(Path(td) / "trial.sqlite")
            rehearsal = store.start_run(
                mode="REHEARSAL",
                wall_ns=1,
                mono_ns=2,
                iso_utc="2026-09-01T00:00:00Z",
                event_date="2026-09-04",
                reference_year=2026,
                reference_month=8,
                bundle=bundle,
                slippage_bps=10,
            )
            live = store.start_run(
                mode="LIVE_OBSERVATION",
                wall_ns=3,
                mono_ns=4,
                iso_utc="2026-09-04T12:25:00Z",
                event_date="2026-09-04",
                reference_year=2026,
                reference_month=8,
                bundle=bundle,
                slippage_bps=10,
            )
            modes = store.conn.execute(
                "SELECT mode FROM trial_runs ORDER BY id"
            ).fetchall()
            release_columns = {
                row[1]
                for row in store.conn.execute("PRAGMA table_info(release_evidence)")
            }
            observation_columns = {
                row[1]
                for row in store.conn.execute("PRAGMA table_info(market_observations)")
            }
            execution_columns = {
                row[1]
                for row in store.conn.execute("PRAGMA table_info(paper_executions)")
            }
            store.close()
        self.assertNotEqual(rehearsal, live)
        self.assertEqual(modes, [("REHEARSAL",), ("LIVE_OBSERVATION",)])
        self.assertTrue(
            {"receipt_wall_time_ns", "receipt_monotonic_ns"}.issubset(release_columns)
        )
        self.assertTrue({"wall_time_ns", "monotonic_ns"}.issubset(observation_columns))
        self.assertTrue(
            {
                "decision_wall_time_ns",
                "decision_monotonic_ns",
                "execution_wall_time_ns",
                "execution_monotonic_ns",
            }.issubset(execution_columns)
        )

    def test_source_timing_request_count_and_parser_runtime_are_persisted(self) -> None:
        bundle = MarketBundle(
            "event", "slug", "title", "rules", "https://www.bls.gov/x", ()
        )
        with tempfile.TemporaryDirectory() as td:
            store = TrialStore(Path(td) / "trial.sqlite")
            run_id = store.start_run(
                mode="REHEARSAL",
                wall_ns=1,
                mono_ns=2,
                iso_utc="2026-09-01T00:00:00Z",
                event_date="2026-09-04",
                reference_year=2026,
                reference_month=8,
                bundle=bundle,
                slippage_bps=10,
            )
            store.source_attempt(run_id, attempt(SOURCE_RSS, mono_ns=10_000))
            row = store.conn.execute(
                """SELECT request_started_wall_ns,request_started_monotonic_ns,
                first_byte_wall_ns,first_byte_monotonic_ns,body_complete_wall_ns,
                body_complete_monotonic_ns,parse_complete_wall_ns,
                parse_complete_monotonic_ns,parser_runtime_us,request_number
                FROM source_attempts WHERE run_id=? AND source_name=?""",
                (run_id, SOURCE_RSS),
            ).fetchone()
            count = store.conn.execute(
                "SELECT COUNT(*) FROM source_attempts WHERE run_id=? AND source_name=?",
                (run_id, SOURCE_RSS),
            ).fetchone()[0]
            store.close()
        self.assertTrue(all(value is not None for value in row[:8]))
        self.assertEqual(row[8], 1.0)
        self.assertEqual(row[9], 1)
        self.assertEqual(count, 1)

    def test_official_conflict_invalidates_existing_paper_execution(self) -> None:
        bundle = MarketBundle(
            "event", "slug", "title", "rules", "https://www.bls.gov/x", ()
        )
        with tempfile.TemporaryDirectory() as td:
            store = TrialStore(Path(td) / "trial.sqlite")
            run_id = store.start_run(
                mode="REHEARSAL",
                wall_ns=1,
                mono_ns=2,
                iso_utc="2026-09-01T00:00:00Z",
                event_date="2026-09-04",
                reference_year=2026,
                reference_month=8,
                bundle=bundle,
                slippage_bps=10,
            )
            store.paper_execution(
                (
                    run_id,
                    1,
                    "token",
                    "market",
                    "0–50k",
                    "BUY_YES",
                    1,
                    2,
                    "2026-09-01T00:00:00Z",
                    3,
                    4,
                    "2026-09-01T00:00:00Z",
                    0.5,
                    5.0,
                    5.0,
                    2.5,
                    2.5,
                    0.1,
                    2.4,
                    0.96,
                    "paper",
                )
            )
            store.invalidate_executions(run_id)
            row = store.conn.execute(
                "SELECT execution_valid,reason FROM paper_executions WHERE run_id=?",
                (run_id,),
            ).fetchone()
            store.close()
        self.assertEqual(row[0], 0)
        self.assertIn("OFFICIAL_SOURCE_CONFLICT", row[1])


if __name__ == "__main__":
    unittest.main()
