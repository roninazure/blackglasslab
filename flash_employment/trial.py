from __future__ import annotations

import asyncio
import json
import os
import platform
import signal
import socket
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from adapters.polymarket_adapter import PolymarketAdapter
from census.stream import BookState, StreamStats, consume_market_stream

from .bls import (
    SOURCE_NAMES,
    BLSHttpClient,
    ReleaseEvidence,
    SourceArbiter,
    SourceAttempt,
    evidence_from_attempt,
)
from .core import (
    BRACKETS,
    Contract,
    KnownPayoutEconomics,
    MarketBundle,
    evaluate_known_payout,
    resolve_payroll_change,
    validate_market_event,
)
from .operations import (
    EXPECTED_EVENT_DATE,
    REQUIRED_STATIC_GATES,
    SOURCE_PHASE_OFFSETS_SECONDS,
    GateResult,
    ReadinessFailure,
    TrialLock,
    clocks_function,
    coarse_http_clock_delta_ms,
    source_poll_sleep_seconds,
)
from .storage import TrialStore

EASTERN = ZoneInfo("America/New_York")
DEFAULT_EVENT_SLUG = "how-many-jobs-added-in-august-1786116586124"
PRE_RELEASE_RACE_LEAD_SECONDS = 2.0


def iso_utc(wall_ns: int) -> str:
    return datetime.fromtimestamp(wall_ns / 1e9, UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class TrialConfig:
    rehearsal: bool
    db_path: Path
    event_date: date
    arm_at: datetime
    release_at: datetime
    duration_minutes: float
    event_slug: str = DEFAULT_EVENT_SLUG
    slippage_bps: float = 10.0
    rehearsal_seconds: float = 8.0
    preflight_only: bool = False
    preflight_timeout_seconds: float = 30.0

    @property
    def reference_period(self) -> tuple[int, int]:
        first = self.event_date.replace(day=1)
        previous = first - timedelta(days=1)
        return previous.year, previous.month


def discover_bundle(config: TrialConfig) -> MarketBundle:
    event = PolymarketAdapter().get_event(config.event_slug)
    year, month = config.reference_period
    return validate_market_event(
        event,
        expected_slug=config.event_slug,
        reference_year=year,
        reference_month=month,
        release_at=config.release_at,
    )


def _aggregate(rows: tuple[KnownPayoutEconomics, ...]) -> dict[str, float] | None:
    if not rows:
        return None
    shares = sum(row.available_shares for row in rows)
    purchase = sum(row.price * row.available_shares for row in rows)
    fees = sum(row.fees for row in rows)
    slippage = sum(row.conservative_slippage for row in rows)
    capital = sum(row.capital_required for row in rows)
    net = sum(row.net_executable_dollars for row in rows)
    return {
        "shares": shares,
        "price": purchase / shares,
        "capital": capital,
        "gross_edge_per_share": sum(
            row.gross_edge_per_share * row.available_shares for row in rows
        )
        / shares,
        "fees": fees,
        "slippage": slippage,
        "net_edge_per_share": net / shares,
        "net": net,
        "return": net / capital,
        "gross_dollars": shares - purchase,
        "costs": fees + slippage,
    }


class FlashTrial:
    def __init__(
        self,
        config: TrialConfig,
        bundle: MarketBundle,
        store: TrialStore,
        run_id: int,
        *,
        readiness_session_id: str = "legacy",
    ) -> None:
        self.config, self.bundle, self.store, self.run_id = (
            config,
            bundle,
            store,
            run_id,
        )
        self.book = BookState()
        self.books_ready = asyncio.Event()
        self.stats = StreamStats(iso_utc(time.time_ns()))
        self.stop = asyncio.Event()
        self.full_books: set[str] = set()
        self.asset_info: dict[str, tuple[Contract, str]] = {}
        for contract in bundle.contracts:
            self.asset_info[contract.yes_token] = (contract, "YES")
            self.asset_info[contract.no_token] = (contract, "NO")
        self.claims_by_bracket = {
            bracket: bundle.known_one_dollar_claims(bracket) for bracket in BRACKETS
        }
        self.winning_claims: dict[str, tuple[Contract, str]] = {}
        self.release: ReleaseEvidence | None = None
        self.arbiter = SourceArbiter()
        self.request_counts = {source: 0 for source in SOURCE_NAMES}
        self.contact = os.environ.get("FLASH_CONTACT", "").strip() or None
        self.source_clients = {
            source: BLSHttpClient(source, contact=self.contact)
            for source in SOURCE_NAMES
        }
        self.winner: str | None = None
        self.resolution_wall_ns: int | None = None
        self.resolution_mono_ns: int | None = None
        self.decision_wall_ns: int | None = None
        self.decision_mono_ns: int | None = None
        self.economics_wall_ns: int | None = None
        self.economics_mono_ns: int | None = None
        self.economics_timing: dict[str, float | int] = {}
        self.paper_decisions: tuple[
            tuple[str, Contract, str, dict[str, float]], ...
        ] = ()
        self.decision_tops: dict[
            str, tuple[float | None, float | None, float, float]
        ] = {}
        self.t2_wall_ns: int | None = None
        self.t2_mono_ns: int | None = None
        self.t3_wall_ns: int | None = None
        self.t3_mono_ns: int | None = None
        self.had_profitable = False
        self.max_shares = self.max_capital = self.max_profit = 0.0
        self.executed = False
        self.readiness_session_id = readiness_session_id
        self.gates: dict[str, GateResult] = {}
        self.preflight_attempts: dict[str, SourceAttempt] = {}
        self.armed = False
        self.disarmed_reason: str | None = None
        self.armed_wall_ns: int | None = None

    def record_gate(self, name: str, passed: bool, detail: str) -> None:
        result = GateResult(name, passed, detail)
        self.gates[name] = result
        wall_ns, mono_ns = time.time_ns(), time.monotonic_ns()
        self.store.readiness_check(
            run_id=self.run_id,
            session_id=self.readiness_session_id,
            gate_name=name,
            passed=passed,
            detail=detail,
            wall_ns=wall_ns,
            mono_ns=mono_ns,
            iso_utc=iso_utc(wall_ns),
        )

    def _runtime_readiness(self) -> list[str]:
        for gate_name in REQUIRED_STATIC_GATES:
            if gate_name not in self.gates:
                self.record_gate(gate_name, False, f"mandatory gate {gate_name} not evaluated")
        self.record_gate(
            "WEBSOCKET_CONNECTED",
            self.stats.connection_count > 0,
            f"connections={self.stats.connection_count}",
        )
        self.record_gate(
            "CLOB_BOOKS",
            self.complete() and len(self.full_books) == 12,
            f"books={len(self.full_books)}/12",
        )
        for source in SOURCE_NAMES:
            attempt = self.preflight_attempts.get(source)
            passed = bool(
                attempt
                and attempt.http_status == 200
                and attempt.valid
                and attempt.reference_year == 2026
                and attempt.reference_month == 7
            )
            if attempt is None:
                detail = "no preflight response"
            else:
                advisory = coarse_http_clock_delta_ms(
                    attempt.http_date, attempt.body_complete_wall_ns
                )
                clock = (
                    "; coarse local-minus-HTTP-Date="
                    f"{advisory:.0f}ms (advisory only)"
                    if advisory is not None
                    else ""
                )
                detail = (
                    f"http={attempt.http_status} validation={attempt.validation_result} "
                    f"period={attempt.reference_year}-{attempt.reference_month:02d}"
                    f"{clock}"
                    if attempt.reference_month is not None
                    else f"http={attempt.http_status} {attempt.rejection_reason or ''}"
                )
            self.record_gate(f"BLS_{source.upper()}", passed, detail)
        valid = [attempt for attempt in self.preflight_attempts.values() if attempt.valid]
        values = {attempt.parsed_value for attempt in valid}
        historical_ok = len(valid) == 3 and len(values) == 1
        self.record_gate(
            "BLS_HISTORICAL_JULY",
            historical_ok,
            f"recognized=3/3 agreement={len(values) == 1} "
            f"value={next(iter(values)) if len(values) == 1 else 'n/a'}",
        )
        non_actionable = len(valid) == 3 and all(
            not self._target_attempt(attempt) for attempt in valid
        )
        self.record_gate(
            "BLS_PRERELEASE_NON_ACTIONABLE",
            non_actionable,
            "July 2026 content rejected as actionable for 2026-09-04",
        )
        return [result.detail for result in self.gates.values() if not result.passed]

    def print_readiness(self, *, preflight_only: bool) -> None:
        print("=" * 66)
        print("PARALLAX FLASH — SEPTEMBER 4 EMPLOYMENT SITUATION PREFLIGHT")
        print("=" * 66)
        for result in self.gates.values():
            status = "PASS" if result.passed else "FAIL"
            print(f"{result.name:<36} {status:<4} {result.detail}")
        blockers = [result.detail for result in self.gates.values() if not result.passed]
        if blockers:
            print(f"FLASH NOT ARMED — {'; '.join(blockers)}", flush=True)
        elif preflight_only:
            print("FLASH PREFLIGHT — PASS", flush=True)

    def arm(self) -> None:
        blockers = self._runtime_readiness()
        if blockers:
            self.print_readiness(preflight_only=False)
            raise ReadinessFailure("; ".join(blockers))
        self.armed = True
        self.armed_wall_ns = time.time_ns()
        self.record_gate("ARMED_STATE", True, "all mandatory gates passed")
        print("FLASH ARMED — READY FOR RELEASE", flush=True)
        print(
            f"event={self.bundle.event_slug} event_date={self.config.event_date} "
            f"scheduled_release={self.config.release_at.isoformat()} "
            "winning_source_policy=first-independently-valid "
            f"books=12/12 BLS_sources=3/3 mode=PAPER db={self.config.db_path} "
            f"armed_at={iso_utc(self.armed_wall_ns)}",
            flush=True,
        )

    def disarm(self, reason: str) -> None:
        if not self.armed:
            return
        self.armed = False
        self.disarmed_reason = reason
        self.record_gate("ARMED_STATE", False, reason)
        print(f"FLASH DISARMED — {reason}", flush=True)
        self.stop.set()

    async def on_stream_gap(self, reason: str, _mono_ns: int) -> None:
        if self.armed and datetime.now(EASTERN) < self.config.release_at:
            detail = f"WebSocket invariant lost before release: {reason}"
            self.record_gate("WEBSOCKET_CONNECTED", False, detail)
            self.disarm(detail)

    def complete(self) -> bool:
        return self.full_books == set(self.bundle.assets)

    def _claim(self, asset: str) -> tuple[Contract, str] | None:
        return self.winning_claims.get(asset)

    def _economics(self, asset: str) -> tuple[KnownPayoutEconomics, ...]:
        claim = self._claim(asset)
        if claim is None:
            return ()
        contract, outcome = claim
        asks = self.book.books.get(asset, {}).get("asks", {})
        return evaluate_known_payout(
            token=asset,
            outcome_side=outcome,
            ask_levels=asks.items(),
            fee_rate=contract.fee_rate,
            fee_exponent=contract.fee_exponent,
            slippage_bps=self.config.slippage_bps,
        )

    def _record_asset(
        self, asset: str, *, phase: str, wall_ns: int, mono_ns: int
    ) -> dict[str, float] | None:
        contract, outcome = self.asset_info[asset]
        top = self.book.top(asset)
        aggregate = _aggregate(self._economics(asset))
        reason = (
            "known-$1 displayed asks are net profitable"
            if aggregate
            else (
                "not a known-$1 claim"
                if self._claim(asset) is None
                else "no displayed ask has positive net edge"
            )
        )
        self.store.observation(
            (
                self.run_id,
                phase,
                wall_ns,
                mono_ns,
                iso_utc(wall_ns),
                asset,
                contract.market_id,
                contract.bracket,
                outcome,
                top["bid"],
                top["ask"],
                top["bid_size"],
                top["ask_size"],
                1.0 if self._claim(asset) is not None else None,
                f"BUY_{outcome}" if aggregate else None,
                aggregate["price"] if aggregate else None,
                aggregate["shares"] if aggregate else None,
                aggregate["capital"] if aggregate else None,
                aggregate["gross_edge_per_share"] if aggregate else None,
                aggregate["fees"] if aggregate else None,
                aggregate["slippage"] if aggregate else None,
                aggregate["net_edge_per_share"] if aggregate else None,
                aggregate["net"] if aggregate else None,
                aggregate["return"] if aggregate else None,
                reason,
            )
        )
        return aggregate

    def _evaluate_state(
        self,
        *,
        phase: str,
        wall_ns: int,
        mono_ns: int,
        assets: tuple[str, ...] | None = None,
    ) -> None:
        changed_assets = assets or self.bundle.assets
        for asset in changed_assets:
            self._record_asset(asset, phase=phase, wall_ns=wall_ns, mono_ns=mono_ns)
        totals = [_aggregate(self._economics(asset)) for asset in self.bundle.assets]
        current = [row for row in totals if row is not None]
        shares = sum(row["shares"] for row in current)
        capital = sum(row["capital"] for row in current)
        profit = sum(row["net"] for row in current)
        self.max_shares = max(self.max_shares, shares)
        self.max_capital = max(self.max_capital, capital)
        self.max_profit = max(self.max_profit, profit)
        if profit > 0:
            self.had_profitable = True
        elif self.had_profitable and self.t3_mono_ns is None:
            self.t3_wall_ns, self.t3_mono_ns = wall_ns, mono_ns
        self.store.commit()

    def _paper_execute(self) -> None:
        if (
            self.executed
            or self.decision_wall_ns is None
            or self.decision_mono_ns is None
        ):
            return
        for asset, contract, outcome, aggregate in self.paper_decisions:
            execution_wall_ns, execution_mono_ns = time.time_ns(), time.monotonic_ns()
            reason = (
                "REHEARSAL COUNTERFACTUAL: latest historical BLS result applied to current displayed depth"
                if self.config.rehearsal
                else "PAPER ONLY: authoritative BLS result makes this token a known-$1 claim"
            )
            self.store.paper_execution(
                (
                    self.run_id,
                    int(self.config.rehearsal),
                    asset,
                    contract.market_id,
                    contract.bracket,
                    f"BUY_{outcome}",
                    self.decision_wall_ns,
                    self.decision_mono_ns,
                    iso_utc(self.decision_wall_ns),
                    execution_wall_ns,
                    execution_mono_ns,
                    iso_utc(execution_wall_ns),
                    aggregate["price"],
                    aggregate["shares"],
                    aggregate["shares"],
                    aggregate["capital"],
                    aggregate["gross_dollars"],
                    aggregate["costs"],
                    aggregate["net"],
                    aggregate["return"],
                    reason,
                )
            )
        self.executed = True
        self.store.commit()

    def _maybe_decide(self) -> None:
        if (
            self.release is None
            or self.decision_mono_ns is not None
            or not self.complete()
        ):
            return
        assert self.winner is not None
        assert self.resolution_mono_ns is not None
        requests_at_t1 = sum(self.request_counts.values())
        self.decision_mono_ns = time.monotonic_ns()
        self.decision_wall_ns = time.time_ns()

        lookup_start_ns = time.monotonic_ns()
        claims = self.claims_by_bracket[self.winner]
        self.winning_claims = {
            asset: (contract, outcome) for contract, outcome, asset in claims
        }
        lookup_end_ns = time.monotonic_ns()

        book_start_ns = lookup_end_ns
        book_inputs = tuple(
            (
                asset,
                contract,
                outcome,
                tuple(self.book.books[asset]["asks"].items()),
            )
            for contract, outcome, asset in claims
        )
        book_end_ns = time.monotonic_ns()

        arithmetic_start_ns = book_end_ns
        evaluated = tuple(
            (
                asset,
                contract,
                outcome,
                _aggregate(
                    evaluate_known_payout(
                        token=asset,
                        outcome_side=outcome,
                        ask_levels=ask_levels,
                        fee_rate=contract.fee_rate,
                        fee_exponent=contract.fee_exponent,
                        slippage_bps=self.config.slippage_bps,
                    )
                ),
            )
            for asset, contract, outcome, ask_levels in book_inputs
        )
        arithmetic_end_ns = time.monotonic_ns()

        paper_start_ns = arithmetic_end_ns
        self.paper_decisions = tuple(
            (asset, contract, outcome, aggregate)
            for asset, contract, outcome, aggregate in evaluated
            if aggregate is not None
        )
        self.economics_mono_ns = time.monotonic_ns()
        self.economics_wall_ns = time.time_ns()
        self.economics_timing = {
            "precompiled_claim_lookup_us": (lookup_end_ns - lookup_start_ns) / 1_000.0,
            "current_book_retrieval_us": (book_end_ns - book_start_ns) / 1_000.0,
            "deterministic_arithmetic_us": (arithmetic_end_ns - arithmetic_start_ns)
            / 1_000.0,
            "paper_decision_creation_us": (self.economics_mono_ns - paper_start_ns)
            / 1_000.0,
            "resolution_ready_wait_us": (lookup_start_ns - self.resolution_mono_ns)
            / 1_000.0,
            "market_discovery_us": 0.0,
            "fee_metadata_fetch_us": 0.0,
            "rest_clob_requests_us": 0.0,
            "http_requests_started_between_t1_t2": (
                sum(self.request_counts.values()) - requests_at_t1
            ),
            "sqlite_before_t2_us": 0.0,
            "logging_before_t2_us": 0.0,
            "other_sync_io_before_t2_us": 0.0,
            "candidate_token_count": len(self.bundle.assets),
            "known_one_dollar_claim_count": len(claims),
            "paper_decision_count": len(self.paper_decisions),
            "resolution_to_economics_us": (
                self.economics_mono_ns - self.resolution_mono_ns
            )
            / 1_000.0,
        }

        # T2 is established above. Top snapshots, audit rows, commits, and output
        # intentionally follow the economics-ready clock.
        self.decision_tops = {
            asset: tuple(
                self.book.top(asset)[key]
                for key in ("bid", "ask", "bid_size", "ask_size")
            )
            for asset in self.bundle.assets
        }
        self._evaluate_state(
            phase="REHEARSAL_DECISION"
            if self.config.rehearsal
            else "POST_RELEASE_DECISION",
            wall_ns=self.decision_wall_ns,
            mono_ns=self.decision_mono_ns,
        )
        self._paper_execute()
        persistence_end_ns = time.monotonic_ns()
        self.economics_timing["post_t2_audit_persistence_us"] = (
            persistence_end_ns - self.economics_mono_ns
        ) / 1_000.0
        label = "REHEARSAL" if self.config.rehearsal else "LIVE_OBSERVATION"
        logging_start_ns = time.monotonic_ns()
        print(
            f"{label} decision winner={self.winner} change_jobs={self.release.change_jobs} "
            f"net=${self.max_profit:.4f} capital=${self.max_capital:.4f} shares={self.max_shares:.4f}",
            flush=True,
        )
        self.economics_timing["post_t2_logging_us"] = (
            time.monotonic_ns() - logging_start_ns
        ) / 1_000.0

    async def on_message(self, message: dict[str, object], mono_ns: int) -> None:
        wall_ns = time.time_ns()
        changed = self.book.apply(message)
        event = message.get("event_type") or message.get("type")
        if event == "book":
            asset = str(message.get("asset_id") or message.get("assetId") or "")
            if asset in self.asset_info:
                self.full_books.add(asset)
                if asset not in self.decision_tops:
                    self._record_asset(
                        asset, phase="INITIAL_BOOK", wall_ns=wall_ns, mono_ns=mono_ns
                    )
                    self.store.commit()
                if self.complete():
                    self.books_ready.set()
        self._maybe_decide()
        relevant = tuple(asset for asset in changed if asset in self.asset_info)
        if self.decision_mono_ns is None or not relevant:
            return
        if self.t2_mono_ns is None:
            for asset in relevant:
                current = tuple(
                    self.book.top(asset)[key]
                    for key in ("bid", "ask", "bid_size", "ask_size")
                )
                if current != self.decision_tops.get(asset):
                    self.t2_wall_ns, self.t2_mono_ns = wall_ns, mono_ns
                    break
        self._evaluate_state(
            phase="POST_RELEASE_CHANGE",
            wall_ns=wall_ns,
            mono_ns=mono_ns,
            assets=relevant,
        )

    def accept_release(self, evidence: ReleaseEvidence) -> None:
        self.release = evidence
        self.winner = resolve_payroll_change(evidence.change_jobs)
        self.resolution_wall_ns, self.resolution_mono_ns = (
            time.time_ns(),
            time.monotonic_ns(),
        )
        self._maybe_decide()
        kind = (
            "historical rehearsal fetch"
            if self.config.rehearsal
            else "authoritative local receipt"
        )
        print(
            f"BLS {kind}: period={evidence.period_name} {evidence.reference_year} "
            f"change={evidence.change_jobs:+d} sha256={evidence.payload_sha256}",
            flush=True,
        )
        self.store.release(
            self.run_id, evidence, rehearsal=self.config.rehearsal, winner=self.winner
        )

    def _target_attempt(self, attempt: SourceAttempt) -> bool:
        year, month = self.config.reference_period
        return (
            attempt.reference_year == year
            and attempt.reference_month == month
            and attempt.published_at_utc is not None
            and datetime.fromisoformat(attempt.published_at_utc)
            .astimezone(EASTERN)
            .date()
            == self.config.event_date
        )

    def _accept_attempt(self, attempt: SourceAttempt, *, actionable: bool) -> None:
        if attempt.phase == "PREFLIGHT":
            self.preflight_attempts[attempt.source_name] = attempt
        if attempt.valid and not actionable:
            attempt = replace(
                attempt,
                validation_result="STALE",
                rejection_reason=(
                    f"preflight content is reference_month={attempt.reference_month} "
                    f"reference_year={attempt.reference_year}, not live August 2026"
                ),
            )
        outcome = self.arbiter.submit(attempt) if actionable else "REJECTED"
        if outcome == "WINNER":
            self.accept_release(evidence_from_attempt(attempt))
            print(
                f"official source race winner={attempt.source_name} "
                f"value={attempt.parsed_value:+d} parser_us={attempt.parser_runtime_us:.1f}",
                flush=True,
            )
        elif outcome == "CONFIRMED":
            assert self.arbiter.winner is not None
            delay = (
                attempt.parse_complete_monotonic_ns
                - self.arbiter.winner.parse_complete_monotonic_ns
            ) / 1e6
            print(
                f"official source confirmed source={attempt.source_name} "
                f"value={attempt.parsed_value:+d} delay_ms={delay:.3f}",
                flush=True,
            )
        elif outcome == "OFFICIAL_SOURCE_CONFLICT":
            self.store.invalidate_executions(self.run_id)
            winner_value = (
                self.arbiter.winner.parsed_value if self.arbiter.winner else None
            )
            print(
                f"OFFICIAL_SOURCE_CONFLICT winner={winner_value} "
                f"source={attempt.source_name} value={attempt.parsed_value}",
                flush=True,
            )
        self.store.source_attempt(self.run_id, attempt)

    async def _fetch_source(
        self,
        source: str,
        *,
        phase: str,
        expected: bool,
        revalidate: bool,
    ) -> SourceAttempt:
        self.request_counts[source] += 1
        year, month = self.config.reference_period
        return await asyncio.to_thread(
            self.source_clients[source].fetch,
            phase=phase,
            request_number=self.request_counts[source],
            expected_year=year if expected else None,
            expected_month=month if expected else None,
            expected_release_date=(
                self.config.event_date.isoformat() if expected else None
            ),
            revalidate=revalidate,
        )

    async def _preflight(self) -> None:
        tasks = [
            asyncio.create_task(
                self._fetch_source(
                    source, phase="PREFLIGHT", expected=False, revalidate=False
                ),
                name=f"flash-preflight-{source}",
            )
            for source in SOURCE_NAMES
        ]
        for completed in asyncio.as_completed(tasks):
            attempt = await completed
            actionable = self.config.rehearsal or self._target_attempt(attempt)
            self._accept_attempt(attempt, actionable=actionable)
            status = "reachable" if attempt.http_status == 200 else "unreachable"
            displayed_validation = (
                "STALE"
                if attempt.valid and not actionable
                else attempt.validation_result
            )
            print(
                f"BLS preflight source={attempt.source_name} status={status} "
                f"http={attempt.http_status} validation={displayed_validation} "
                f"sha256={attempt.payload_sha256}",
                flush=True,
            )

    async def _live_source_worker(self, source: str) -> None:
        start = self.config.release_at - timedelta(
            seconds=PRE_RELEASE_RACE_LEAD_SECONDS
        ) + timedelta(seconds=SOURCE_PHASE_OFFSETS_SECONDS[source])
        delay = (start - datetime.now(EASTERN)).total_seconds()
        if delay > 0:
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=delay)
                return
            except TimeoutError:
                pass
        end = self.config.release_at + timedelta(minutes=self.config.duration_minutes)
        while not self.stop.is_set() and datetime.now(EASTERN) < end:
            if source in self.arbiter.valid_by_source:
                return
            attempt = await self._fetch_source(
                source, phase="RELEASE_RACE", expected=True, revalidate=True
            )
            self._accept_attempt(attempt, actionable=True)
            if attempt.valid:
                return
            elapsed = (datetime.now(EASTERN) - self.config.release_at).total_seconds()
            sleep_seconds = source_poll_sleep_seconds(
                request_started_mono_ns=attempt.request_started_monotonic_ns,
                now_mono_ns=time.monotonic_ns(),
                after_release_seconds=elapsed,
            )
            await asyncio.sleep(sleep_seconds)

    async def _countdown(self) -> None:
        for minute, second in ((29, 0), (29, 50), (29, 59)):
            target = self.config.release_at.replace(minute=minute, second=second)
            delay = (target - datetime.now(EASTERN)).total_seconds()
            if delay > 0:
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=delay)
                    return
                except TimeoutError:
                    pass
            if not self.stop.is_set() and datetime.now(EASTERN) < self.config.release_at:
                print(
                    f"FLASH COUNTDOWN {target.strftime('%H:%M:%S')} ET "
                    f"ARMED={self.armed} books={len(self.full_books)}/12 "
                    f"BLS={len(self.preflight_attempts)}/3",
                    flush=True,
                )

    async def source_task(self) -> None:
        if self.config.rehearsal:
            await self.books_ready.wait()
        await self._preflight()
        if self.config.rehearsal:
            return
        await self.books_ready.wait()
        self.arm()
        print(
            "BLS race cadence: one request/source/second through +30s, "
            "then one request/source/5s",
            flush=True,
        )
        await asyncio.gather(
            self._countdown(),
            *(self._live_source_worker(source) for source in SOURCE_NAMES),
        )

    async def run_preflight_only(self) -> dict[str, object]:
        stream = asyncio.create_task(
            consume_market_stream(
                self.bundle.assets,
                self.on_message,
                stop=self.stop,
                stats=self.stats,
                on_stream_gap=self.on_stream_gap,
            ),
            name="flash-polymarket-preflight-stream",
        )
        try:
            await asyncio.wait_for(
                asyncio.gather(self._preflight(), self.books_ready.wait()),
                timeout=self.config.preflight_timeout_seconds,
            )
            blockers = self._runtime_readiness()
            self.print_readiness(preflight_only=True)
            if blockers:
                raise ReadinessFailure("; ".join(blockers))
            return {
                "preflight": "PASS",
                "books": len(self.full_books),
                "bls_sources": len(self.preflight_attempts),
                "database": str(self.config.db_path),
            }
        except TimeoutError as exc:
            detail = (
                f"preflight timed out after {self.config.preflight_timeout_seconds:.1f}s; "
                f"books={len(self.full_books)}/12 BLS={len(self.preflight_attempts)}/3"
            )
            self.record_gate("PREFLIGHT_BOUNDS", False, detail)
            self.print_readiness(preflight_only=True)
            raise ReadinessFailure(detail) from exc
        finally:
            self.stop.set()
            await asyncio.gather(stream, return_exceptions=True)

    async def run(self) -> dict[str, object]:
        stream = asyncio.create_task(
            consume_market_stream(
                self.bundle.assets,
                self.on_message,
                stop=self.stop,
                stats=self.stats,
                on_stream_gap=self.on_stream_gap,
            ),
            name="flash-polymarket-stream",
        )
        source = asyncio.create_task(self.source_task(), name="flash-bls-source")
        if self.config.rehearsal:
            deadline = time.monotonic() + self.config.rehearsal_seconds
        else:
            deadline = (
                self.config.release_at
                + timedelta(minutes=self.config.duration_minutes)
                - datetime.now(EASTERN)
            ).total_seconds() + time.monotonic()
        try:
            while time.monotonic() < deadline and not self.stop.is_set():
                if stream.done():
                    await stream
                if source.done():
                    await source
                    if self.config.rehearsal and self.decision_mono_ns is not None:
                        await asyncio.sleep(
                            min(1.0, max(0.0, deadline - time.monotonic()))
                        )
                        break
                await asyncio.sleep(0.05)
        except Exception as exc:
            if self.armed and datetime.now(EASTERN) < self.config.release_at:
                self.disarm(f"mandatory local failure: {type(exc).__name__}: {exc}")
            raise
        finally:
            self.stop.set()
            await asyncio.gather(stream, source, return_exceptions=True)
        if self.release is None:
            raise RuntimeError("no matching authoritative BLS release was observed")
        if self.config.rehearsal and len(self.arbiter.valid_by_source) != len(
            SOURCE_NAMES
        ):
            raise RuntimeError(
                f"rehearsal did not validate all official sources: "
                f"{len(self.arbiter.valid_by_source)}/{len(SOURCE_NAMES)}"
            )
        if not self.complete():
            raise RuntimeError(
                f"incomplete Polymarket books: {len(self.full_books)}/{len(self.bundle.assets)}"
            )
        if self.decision_mono_ns is None or self.economics_mono_ns is None:
            raise RuntimeError("release and complete books never produced a decision")
        return self.summary()

    def summary(self) -> dict[str, object]:
        assert self.release is not None
        assert self.decision_mono_ns is not None
        assert self.resolution_mono_ns is not None
        assert self.economics_mono_ns is not None
        live = not self.config.rehearsal
        source_to_decision = (
            (self.decision_mono_ns - self.release.valid_monotonic_ns) / 1e6
            if live
            else None
        )
        source_to_move = (
            (self.t2_mono_ns - self.release.valid_monotonic_ns) / 1e6
            if live and self.t2_mono_ns
            else None
        )
        window = (
            (self.t3_mono_ns - self.decision_mono_ns) / 1e6
            if live and self.t3_mono_ns
            else None
        )
        limitation = None
        if self.t2_mono_ns is None:
            limitation = "no relevant post-decision top-of-book movement observed"
        if self.t3_mono_ns is None:
            suffix = (
                "profitable liquidity did not fully disappear during the bounded window"
            )
            limitation = f"{limitation}; {suffix}" if limitation else suffix
        if self.arbiter.official_source_conflict:
            suffix = "OFFICIAL_SOURCE_CONFLICT invalidated paper execution validity"
            limitation = f"{limitation}; {suffix}" if limitation else suffix
        valid_at = {
            source: (
                iso_utc(attempt.parse_complete_wall_ns)
                if (attempt := self.arbiter.valid_by_source.get(source))
                else None
            )
            for source in SOURCE_NAMES
        }
        source_valid_to_resolution_us = (
            self.resolution_mono_ns - self.release.valid_monotonic_ns
        ) / 1_000.0
        resolution_to_economics_us = (
            self.economics_mono_ns - self.resolution_mono_ns
        ) / 1_000.0
        source_valid_to_economics_us = (
            self.economics_mono_ns - self.release.valid_monotonic_ns
        ) / 1_000.0
        return {
            "winning_bracket": self.winner,
            "winning_source": self.release.source_name,
            "source_value": self.release.change_jobs,
            "rss_valid_at": valid_at["rss"],
            "summary_valid_at": valid_at["summary"],
            "table_b1_valid_at": valid_at["table_b1"],
            "rss_minus_winner_ms": self.arbiter.valid_delta_ms("rss"),
            "summary_minus_winner_ms": self.arbiter.valid_delta_ms("summary"),
            "table_b1_minus_winner_ms": self.arbiter.valid_delta_ms("table_b1"),
            "confirmation_count": len(self.arbiter.confirmations),
            "official_source_conflict": self.arbiter.official_source_conflict,
            "execution_validity": not self.arbiter.official_source_conflict,
            "source_valid_to_resolution_us": source_valid_to_resolution_us,
            "resolution_to_economics_us": resolution_to_economics_us,
            "source_valid_to_economics_us": source_valid_to_economics_us,
            "economics_timing": self.economics_timing,
            "request_count_rss": self.request_counts["rss"],
            "request_count_summary": self.request_counts["summary"],
            "request_count_table_b1": self.request_counts["table_b1"],
            "source_to_decision_ms": source_to_decision,
            "source_to_first_market_move_ms": source_to_move,
            "profitable_window_ms": window,
            "maximum_executable_stale_depth_shares": self.max_shares,
            "maximum_deployable_capital": self.max_capital,
            "maximum_modeled_net_profit": self.max_profit,
            "complete_books": self.complete(),
            "limitation": limitation,
        }

    def summary_row(self, result: dict[str, object]) -> dict[str, object]:
        live = not self.config.rehearsal
        assert self.release is not None
        return {
            "run_id": self.run_id,
            "is_rehearsal": int(self.config.rehearsal),
            "winning_bracket": self.winner,
            "t0_wall_time_ns": self.release.valid_wall_time_ns if live else None,
            "t0_monotonic_ns": self.release.valid_monotonic_ns if live else None,
            "t1_wall_time_ns": self.decision_wall_ns,
            "t1_monotonic_ns": self.decision_mono_ns,
            "t2_wall_time_ns": self.t2_wall_ns if live else None,
            "t2_monotonic_ns": self.t2_mono_ns if live else None,
            "t3_wall_time_ns": self.t3_wall_ns if live else None,
            "t3_monotonic_ns": self.t3_mono_ns if live else None,
            "source_to_decision_ms": result["source_to_decision_ms"],
            "source_to_first_market_move_ms": result["source_to_first_market_move_ms"],
            "profitable_window_ms": result["profitable_window_ms"],
            "maximum_executable_stale_depth_shares": self.max_shares,
            "maximum_deployable_capital": self.max_capital,
            "maximum_modeled_net_profit": self.max_profit,
            "complete_books": int(self.complete()),
            "stream_stats_json": json.dumps(asdict(self.stats), sort_keys=True),
            "limitation": result["limitation"],
            "winning_source": result["winning_source"],
            "source_value": result["source_value"],
            "rss_valid_at": result["rss_valid_at"],
            "summary_valid_at": result["summary_valid_at"],
            "table_b1_valid_at": result["table_b1_valid_at"],
            "rss_minus_winner_ms": result["rss_minus_winner_ms"],
            "summary_minus_winner_ms": result["summary_minus_winner_ms"],
            "table_b1_minus_winner_ms": result["table_b1_minus_winner_ms"],
            "confirmation_count": result["confirmation_count"],
            "official_source_conflict": int(bool(result["official_source_conflict"])),
            "execution_validity": int(bool(result["execution_validity"])),
            "source_valid_to_resolution_us": result["source_valid_to_resolution_us"],
            "resolution_to_economics_us": result["resolution_to_economics_us"],
            "source_valid_to_economics_us": result["source_valid_to_economics_us"],
            "economics_timing_json": json.dumps(
                result["economics_timing"], sort_keys=True
            ),
            "request_count_rss": result["request_count_rss"],
            "request_count_summary": result["request_count_summary"],
            "request_count_table_b1": result["request_count_table_b1"],
            "t0_source_valid_wall_ns": self.release.valid_wall_time_ns,
            "t0_source_valid_monotonic_ns": self.release.valid_monotonic_ns,
            "t1_resolution_wall_ns": self.resolution_wall_ns,
            "t1_resolution_monotonic_ns": self.resolution_mono_ns,
            "t2_economics_wall_ns": self.economics_wall_ns,
            "t2_economics_monotonic_ns": self.economics_mono_ns,
        }


async def run_config(config: TrialConfig) -> tuple[int, dict[str, object]]:
    if not config.rehearsal and not config.preflight_only:
        now = datetime.now(EASTERN)
        if now < config.arm_at:
            print(f"waiting until arm time {config.arm_at.isoformat()}", flush=True)
            await asyncio.sleep((config.arm_at - now).total_seconds())
        if datetime.now(EASTERN) >= config.release_at + timedelta(
            minutes=config.duration_minutes
        ):
            raise ValueError("configured live observation window has already ended")
    store = TrialStore(config.db_path)
    session_id = uuid.uuid4().hex
    lock = TrialLock(config.event_slug, config.event_date)

    def preliminary_gate(name: str, passed: bool, detail: str) -> None:
        wall_ns, mono_ns = time.time_ns(), time.monotonic_ns()
        store.readiness_check(
            session_id=session_id,
            gate_name=name,
            passed=passed,
            detail=detail,
            wall_ns=wall_ns,
            mono_ns=mono_ns,
            iso_utc=iso_utc(wall_ns),
        )

    preliminary_gate("TRIAL_DATABASE", True, f"opened {config.db_path}")
    contact_configured = bool(os.environ.get("FLASH_CONTACT", "").strip())
    preliminary_gate(
        "FLASH_CONTACT",
        contact_configured,
        "configured" if contact_configured else "FLASH_CONTACT is missing or empty",
    )
    correct_date = config.event_date == EXPECTED_EVENT_DATE
    preliminary_gate(
        "EVENT_DATE",
        correct_date,
        f"requested={config.event_date} expected={EXPECTED_EVENT_DATE}",
    )
    clocks_ok = clocks_function()
    preliminary_gate("HIGH_RESOLUTION_CLOCKS", clocks_ok, "time_ns and monotonic_ns")
    preliminary_gate("PAPER_ONLY_MODE", True, "mode=PAPER; no live mode option exists")
    preliminary_gate(
        "NO_ORDER_AUTH_WALLET_PATH",
        True,
        "runner imports public market data and SQLite paper persistence only",
    )
    if not contact_configured or not correct_date or not clocks_ok:
        blockers = []
        if not contact_configured:
            blockers.append("FLASH_CONTACT is missing or empty")
        if not correct_date:
            blockers.append(f"wrong event date {config.event_date}")
        if not clocks_ok:
            blockers.append("high-resolution clocks failed")
        print(f"FLASH NOT ARMED — {'; '.join(blockers)}", flush=True)
        store.close()
        raise ReadinessFailure("; ".join(blockers))
    try:
        lock.acquire()
    except ReadinessFailure as exc:
        preliminary_gate("DUPLICATE_RUNNER", False, str(exc))
        print(f"FLASH NOT ARMED — {exc}", flush=True)
        store.close()
        raise
    preliminary_gate("DUPLICATE_RUNNER", True, f"lock={lock.path}")
    try:
        try:
            bundle = await asyncio.to_thread(discover_bundle, config)
        except Exception as exc:
            preliminary_gate(
                "POLYMARKET_EVENT_AND_RULES", False, f"{type(exc).__name__}: {exc}"
            )
            print(f"FLASH NOT ARMED — Polymarket validation failed: {exc}", flush=True)
            raise ReadinessFailure(f"Polymarket validation failed: {exc}") from exc
        wall_ns, mono_ns = time.time_ns(), time.monotonic_ns()
        run_id = store.start_run(
            mode="REHEARSAL" if config.rehearsal else "LIVE_OBSERVATION",
            wall_ns=wall_ns,
            mono_ns=mono_ns,
            iso_utc=iso_utc(wall_ns),
            event_date=config.event_date.isoformat(),
            reference_year=config.reference_period[0],
            reference_month=config.reference_period[1],
            bundle=bundle,
            slippage_bps=config.slippage_bps,
        )
        trial = FlashTrial(
            config,
            bundle,
            store,
            run_id,
            readiness_session_id=session_id,
        )
        for name, passed, detail in (
            ("FLASH_CONTACT", True, "configured"),
            ("EVENT_DATE", True, str(config.event_date)),
            ("POLYMARKET_EVENT", bool(bundle.event_id), f"event={bundle.event_slug}"),
            ("RESOLUTION_RULES", bool(bundle.rules), "deterministically validated"),
            ("PAYROLL_BRACKETS", len(bundle.contracts) == 6, f"{len(bundle.contracts)}/6"),
            ("TOKEN_MAP", len(bundle.assets) == 12 and len(set(bundle.assets)) == 12, f"{len(set(bundle.assets))}/12"),
            (
                "FEE_METADATA",
                all(contract.fee_source == "gamma.feeSchedule" for contract in bundle.contracts),
                f"prepared={len(bundle.contracts)}/6",
            ),
            (
                "KNOWN_CLAIM_MAPPINGS",
                len(trial.claims_by_bracket) == 6
                and all(len(claims) == 6 for claims in trial.claims_by_bracket.values()),
                f"winning-bracket maps={len(trial.claims_by_bracket)}/6",
            ),
            ("TRIAL_DATABASE", True, f"opened {config.db_path}"),
            ("HIGH_RESOLUTION_CLOCKS", True, "time_ns and monotonic_ns"),
            ("DUPLICATE_RUNNER", True, f"lock={lock.path}"),
            ("PAPER_ONLY_MODE", True, "mode=PAPER"),
            (
                "NO_ORDER_AUTH_WALLET_PATH",
                True,
                "no order, auth, wallet, or live execution object is constructed",
            ),
        ):
            trial.record_gate(name, passed, detail)
        static_blockers = [gate.detail for gate in trial.gates.values() if not gate.passed]
        if static_blockers:
            trial.print_readiness(preflight_only=config.preflight_only)
            raise ReadinessFailure("; ".join(static_blockers))

        precheck_wall_ns, precheck_mono_ns = time.time_ns(), time.monotonic_ns()
        store.arm_precheck(
            run_id,
            local_utc=iso_utc(precheck_wall_ns),
            wall_ns=precheck_wall_ns,
            mono_ns=precheck_mono_ns,
            hostname=socket.gethostname(),
            platform=platform.platform(),
            contact_configured=contact_configured,
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, trial.stop.set)
            except NotImplementedError:
                pass
        try:
            if config.preflight_only:
                result = await trial.run_preflight_only()
                store.finish(
                    run_id, status="PREFLIGHT_PASS", error=None, summary=None
                )
                return run_id, result
            result = await trial.run()
            status = (
                "OFFICIAL_SOURCE_CONFLICT"
                if trial.arbiter.official_source_conflict
                else "COMPLETE"
            )
            store.finish(
                run_id, status=status, error=None, summary=trial.summary_row(result)
            )
            return run_id, result
        except Exception as exc:
            store.invalidate_executions(
                run_id, reason=f"TRIAL_FAILED: {type(exc).__name__}"
            )
            store.finish(
                run_id,
                status="FAILED",
                error=f"{type(exc).__name__}: {exc}",
                summary=None,
            )
            raise
    finally:
        lock.release()
        store.close()
