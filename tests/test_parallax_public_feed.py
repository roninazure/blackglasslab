from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from parallax.public_feed import (
    export_completed_scan,
    publish_incremental_nfl_game,
    sanitize_completed_scan,
)


NOW = datetime(2026, 9, 24, 12, 30, tzinfo=UTC)
ISSUED = "2026-09-24T12:00:00+00:00"


def completed_mlb_stdout() -> str:
    plays = []
    for index, action in enumerate(("BUY", "WATCH", "PASS")):
        row = {
                "id": f"internal-uuid-{index}",
                "sport": "MLB",
                "venue": "POLYMARKET" if index == 0 else "KALSHI",
                "market_id": f"market-{index}",
                "market_title": f"Example market {index}",
                "matchup": "Dodgers at Mets",
                "side": "NO" if index == 0 else "YES",
                "side_description": "Los Angeles Dodgers" if index == 0 else "New York Mets",
                "suggested_action": action,
                "executable_price": 0.42 + index / 100,
                "current_price": 0.99,
                "model_probability": 0.61,
                "parallax_fair_value": 0.01,
                "edge_points": 19.0,
                "confidence_band": "HIGH",
                "data_freshness": "FRESH",
                "status": "CURRENT",
                "created_at": ISSUED,
                "updated_at": "2026-09-24T12:05:00Z",
                "expires_at": "2026-09-24T13:00:00Z",
                "resolution_time": "2026-09-25T00:00:00Z",
                "reason_summary": "Clears the public value gates.",
                "failed_gates": ["EXAMPLE_GATE"],
                "market_url": (
                    "https://polymarket.com/event/example" if index == 1 else "https://evil.example/contract"
                ),
                "retail_examples": [
                    {
                        "stake": stake,
                        "available": True,
                        "contracts_or_shares": 2.0,
                        "total_cost": 25.0,
                        "estimated_payout_if_correct": 50.0,
                        "net_profit_if_correct": 25.0,
                        "maximum_loss_including_fees": 25.0,
                        "secret_calculation": "do-not-export",
                    }
                    for stake in range(10)
                ],
                "evidence": {"api_key": "secret-api-key", "source_url": "https://statsapi.mlb.com/private"},
                "independent_sources": ["private-provider"],
                "validation_reference": "private-validation",
                "review_reference": "private-review",
                "rules_digest": "private-rules",
                "market_reference": "private-reference",
                "risk_factors": ["private-risk"],
                "confidence_method": "private-method",
                "source_url": "https://api.kalshi.com/private-source",
                "authorization": "Bearer private-token",
                "runtime_path": "/private/runtime/path",
            }
        if index == 0:
            row.update(
                {
                    "publication_eligible": True,
                    "game_id": "mlb-game-0",
                    "game_start": "2026-09-25T00:00:00Z",
                    "away_team": "Los Angeles Dodgers",
                    "home_team": "New York Mets",
                    "selected_team": "Los Angeles Dodgers",
                    "economic_key": "MLB:mlb-game-0:losangelesdodgers",
                    "mapping_status": "MAPPED_GAME_WINNER",
                    "acquisition_status": "ACQUIRED",
                    "market_status": "OPEN",
                    "fee_status": "VERIFIED_SCHEDULE",
                    "fees_estimate": 0.50,
                    "expected_value": 3.0,
                    "expected_return": 0.12,
                    "executable_size": 100.0,
                    "failed_gates": [],
                    "verdict": {"failed_gates": []},
                    "evidence": {
                        "source": "official-mlb-statsapi",
                        "model_version": "mlb-division-series-conservative-v1",
                        "validation_status": "CALIBRATED",
                        "validation_reference": "validation:mlb-division-series-conservative-v1:2024-features-2025-holdout-n18",
                        "forecast_metadata": {"official_game_type": "D"},
                    },
                }
            )
        plays.append(row)
    return json.dumps(
        {
            "health": {
                "mode": "live",
                "state": "healthy",
                "recent_errors": ["raw private exception"],
            },
            "plays": {
                "mode": "live",
                "as_of": "2026-09-24T12:05:00Z",
                "items": plays,
            },
            "slate": {
                "market_data_complete": True,
                "dates": [
                    {
                        "date": "2026-09-24",
                        "games": [
                            {
                                "game_id": "mlb-game-0",
                                "away_team": "Los Angeles Dodgers",
                                "home_team": "New York Mets",
                                "status": "BUY",
                            }
                        ],
                    }
                ],
            },
            "environment": {"TOKEN": "environment-secret"},
        }
    )


def completed_cfb_stdout() -> str:
    return json.dumps(
        {
            "read_only": True,
            "orders": 0,
            "alerts": 0,
            "published": 0,
            "prospective_captured": 3,
            "cfbd_calls": 17,
            "validation_ece": 0.08,
            "venues": {
                "PMUS": {
                    "coverage": {"state": "COMPLETE", "pages": 1},
                    "discovered": 4,
                    "WATCH": 1,
                    "PASS": 0,
                    "BUY": 1,
                    "status_counts": {"WATCH": 1, "BUY": 1},
                },
                "KALSHI": {
                    "coverage": {"state": "PARTIAL", "failed_scopes": 1},
                    "endpoint_status": "UNAVAILABLE",
                    "discovered": 2,
                    "WATCH": 0,
                    "PASS": 1,
                    "BUY": 0,
                    "status_counts": {"PASS": 1},
                },
            },
            "rows": [
                {
                    "venue": "PMUS",
                    "market_id": "cfb-buy",
                    "matchup": "Georgia at Alabama",
                    "kickoff_utc": "2026-09-26T19:30:00Z",
                    "side": "YES",
                    "cfb_v1_probability": 0.72,
                    "executable_price": 0.49,
                    "raw_edge": 23.0,
                    "verdict": "BUY",
                },
                {
                    "venue": "PMUS",
                    "market_id": "cfb-watch",
                    "matchup": "Georgia at Alabama",
                    "kickoff_utc": "2026-09-26T19:30:00Z",
                    "side": "NO",
                    "cfb_v1_probability": 0.28,
                    "executable_price": 0.31,
                    "raw_edge": -3.0,
                    "verdict": "WATCH",
                },
                {
                    "venue": "KALSHI",
                    "market_id": "cfb-pass",
                    "matchup": "Texas at Auburn",
                    "kickoff_utc": "2026-09-27T00:00:00Z",
                    "side": "YES",
                    "cfb_v1_probability": 0.44,
                    "executable_price": 0.55,
                    "raw_edge": -11.0,
                    "verdict": "PASS",
                },
            ],
            "mapping_failure_reasons": {"DATE_MISMATCH": 1},
        }
    )


def eligible_cfb_buy() -> dict:
    return {
        "venue": "PMUS",
        "market_id": "cfb-certified-buy",
        "matchup": "Georgia at Alabama",
        "game_id": "cfb-game-1",
        "away_team": "Georgia",
        "home_team": "Alabama",
        "selected_team": "Alabama",
        "economic_key": "CFB:cfb-game-1:alabama",
        "kickoff_utc": "2026-09-26T19:30:00Z",
        "game_start": "2026-09-26T19:30:00Z",
        "side": "YES",
        "cfb_v1_probability": 0.72,
        "executable_price": 0.49,
        "raw_edge": 23.0,
        "cfb_safety": True,
        "fee": 0.50,
        "fee_status": "VERIFIED_UPPER_BOUND",
        "net_ev_25": 3.0,
        "expected_return": 0.12,
        "liquidity": 100.0,
        "verdict": "BUY",
        "failed_gates": [],
        "mapping_status": "MAPPED",
        "acquisition_status": "ACQUIRED",
        "market_status": "OPEN",
        "data_freshness": "FRESH",
        "status": "CURRENT",
        "updated_at": "2026-09-24T12:29:30+00:00",
        "expires_at": "2026-09-24T12:30:30+00:00",
        "evidence_source": "CollegeFootballData",
        "evidence_model_version": "cfb-v1-rolling-elo",
        "evidence_validation_status": "CALIBRATED",
        "evidence_validation_reference": "CFB V1 validated 2025 holdout",
        "publication_eligible": True,
    }


def eligible_nfl_buy(
    *,
    game_id="2026_03_ARI_NYG",
    market_id="KXNFLGAME-ARI-NYG-ARI",
    venue="KALSHI",
    economic_team="ARI",
    selected_team="Arizona Cardinals",
    price=0.41,
):
    return {
        "venue": venue,
        "market_id": market_id,
        "market": "ARI at NYG",
        "game_id": game_id,
        "side": "YES",
        "side_description": selected_team,
        "verdict": "BUY",
        "executable_price": price,
        "nfl_v1_probability": 0.63,
        "raw_edge": 22.0,
        "safety_margin": 0.144535,
        "fee": 0.01,
        "net_ev_25": 3.25,
        "expected_return": 0.13,
        "liquidity": 100.0,
        "failed_gates": [],
        "mapping_status": "MAPPED_GAME_WINNER",
        "acquisition_status": "ACQUIRED",
        "economic_key": f"NFL:{game_id}:{economic_team}",
        "economic_team": economic_team,
        "selected_team": selected_team,
        "created_at": "2026-09-24T12:29:20+00:00",
        "updated_at": "2026-09-24T12:29:30+00:00",
        "expires_at": "2026-09-24T12:30:30+00:00",
        "data_freshness": "FRESH",
        "status": "CURRENT",
        "game_start": "2026-09-27T17:00:00+00:00",
        "publication_eligible": True,
    }


def nfl_slate_for(
    game_id="2026_03_ARI_NYG",
    status="BUY",
    *,
    away_team="ARI",
    home_team="NYG",
    complete=True,
):
    return {
        "market_data_complete": complete,
        "expected_games": 1,
        "accounted_games": 1,
        "all_games_accounted": True,
        "dates": [
            {
                "date": "2026-09-27",
                "games": [
                    {
                        "game_id": game_id,
                        "away_team": away_team,
                        "home_team": home_team,
                        "status": status,
                    }
                ],
            }
        ],
    }


def incremental_nfl_game(
    buy,
    *,
    finalized_at="2026-09-24T12:29:40+00:00",
    status="BUY",
    away_team="ARI",
    home_team="NYG",
):
    game_id = buy["game_id"]
    return {
        "read_only": True,
        "orders": 0,
        "published": 0,
        "finalized_at": finalized_at,
        "slate": nfl_slate_for(
            game_id,
            status,
            away_team=away_team,
            home_team=home_team,
            complete=False,
        ),
        "summary": {"rows": [buy]},
    }


def test_sanitizer_counts_actions_and_maps_only_public_fields():
    result = sanitize_completed_scan("mlb", completed_mlb_stdout(), generated_at=NOW)

    assert result["schema_version"] == "parallax.public.v1"
    assert result["lane"] == "MLB"
    assert result["runtime_state"] == "LIVE"
    assert result["health_state"] == "HEALTHY"
    assert result["data_quality_state"] == "COMPLETE"
    assert result["market_data_complete"] is True
    assert result["buy_publication_eligible"] is True
    assert result["read_only"] is True
    assert result["summary"] == {"plays": 3, "buy": 1, "watch": 1, "pass": 1}
    assert [play["action"] for play in result["plays"]] == ["BUY", "WATCH", "PASS"]
    assert result["plays"][0]["price"] == 0.42
    assert result["plays"][0]["model_probability"] == 0.61
    assert result["plays"][0]["edge_pp"] == 19.0
    assert len(result["plays"][0]["retail_examples"]) == 8
    assert "secret_calculation" not in result["plays"][0]["retail_examples"][0]


def test_prohibited_fields_internal_uuid_and_provider_urls_cannot_leak():
    result = sanitize_completed_scan("mlb", completed_mlb_stdout(), generated_at=NOW)
    serialized = json.dumps(result, sort_keys=True)

    for prohibited in (
        "internal-uuid",
        "secret-api-key",
        "statsapi.mlb.com",
        "api.kalshi.com/private-source",
        "private-provider",
        "private-validation",
        "private-review",
        "private-rules",
        "private-reference",
        "private-risk",
        "private-method",
        "private-token",
        "/private/runtime/path",
        "raw private exception",
        "environment-secret",
    ):
        assert prohibited not in serialized


def test_public_signal_id_is_deterministic_and_not_internal_id():
    first = sanitize_completed_scan("mlb", completed_mlb_stdout(), generated_at=NOW)
    second = sanitize_completed_scan(
        "mlb", completed_mlb_stdout(), generated_at=NOW + timedelta(minutes=1)
    )

    first_id = first["plays"][0]["signal_id"]
    assert first_id == second["plays"][0]["signal_id"]
    expected = hashlib.sha256(
        b"mlbPOLYMARKETmarket-0NOinternal-uuid-0"
    ).hexdigest()[:20]
    assert first_id == f"px1_{expected}"
    assert "internal-uuid-0" not in first_id


def test_no_side_semantics_are_preserved_without_opponent_inference():
    play = sanitize_completed_scan("mlb", completed_mlb_stdout(), generated_at=NOW)["plays"][0]

    assert play["contract_side"] == "NO"
    assert play["contract_label"] == "Los Angeles Dodgers"
    assert play["position_label"] == "NO — Los Angeles Dodgers"
    assert "selected_team" not in play


def test_free_visibility_is_exactly_fifteen_minutes_after_issue():
    play = sanitize_completed_scan("mlb", completed_mlb_stdout(), generated_at=NOW)["plays"][0]

    issued = datetime.fromisoformat(play["issued_at"])
    visible = datetime.fromisoformat(play["free_visible_at"])
    assert visible - issued == timedelta(minutes=15)


def test_contract_url_requires_allowlisted_https_host():
    plays = sanitize_completed_scan("mlb", completed_mlb_stdout(), generated_at=NOW)["plays"]

    assert "contract_url" not in plays[0]
    assert plays[1]["contract_url"] == "https://polymarket.com/event/example"


def test_export_write_uses_atomic_replace_and_private_file(tmp_path, monkeypatch):
    calls = []
    real_replace = os.replace

    def replace(source, destination):
        calls.append((source, destination))
        real_replace(source, destination)

    monkeypatch.setattr("parallax.public_feed.os.replace", replace)
    destination = export_completed_scan("mlb", completed_mlb_stdout(), tmp_path)

    assert destination == tmp_path / "public_feed" / "mlb.json"
    assert len(calls) == 1
    assert calls[0][1] == destination
    assert json.loads(destination.read_text())["schema_version"] == "parallax.public.v1"
    assert destination.stat().st_mode & 0o777 == 0o600
    assert not list(destination.parent.glob(".mlb.json.*"))


def test_nfl_existing_verdict_rows_are_exported_without_guessing_fields():
    stdout = json.dumps(
        {
            "read_only": True,
            "summary": {
                "rows": [
                    {
                        "venue": "KALSHI",
                        "market_id": "KXNFLGAME-EXAMPLE",
                        "market": "NFL game winner",
                        "side": "NO",
                        "verdict": "WATCH",
                        "executable_price": 0.48,
                        "nfl_v1_probability": 0.55,
                        "raw_edge": 7.0,
                        "game_start": "2026-09-25T20:00:00Z",
                    },
                    {"venue": "KALSHI", "market_id": "mapping-only", "status": "MAPPED_GAME_WINNER"},
                ]
            },
        }
    )

    result = sanitize_completed_scan("nfl", stdout, generated_at=NOW)
    assert result["summary"] == {"plays": 1, "buy": 0, "watch": 1, "pass": 0}
    assert result["plays"][0]["contract_side"] == "NO"
    assert "contract_label" not in result["plays"][0]
    assert "selected_team" not in result["plays"][0]


def test_complete_nfl_scan_retains_existing_buy_publication():
    stdout = json.dumps(
        {
            "slate": nfl_slate_for(),
            "summary": {
                "rows": [
                    eligible_nfl_buy(market_id="KXNFLGAME-COMPLETE")
                ]
            },
        }
    )

    result = sanitize_completed_scan("nfl", stdout, generated_at=NOW)

    assert result["buy_publication_eligible"] is True
    assert result["summary"] == {"plays": 1, "buy": 1, "watch": 0, "pass": 0}
    assert result["plays"][0]["action"] == "BUY"
    assert result["plays"][0]["publication_eligible"] is True


@pytest.mark.parametrize(
    ("failed_game_id", "failure_status"),
    [
        ("2026_03_GB_TB", "MAPPING_FAILURE"),
        ("2026_03_JAX_CIN", "DATA_UNAVAILABLE"),
    ],
)
def test_unrelated_nfl_game_failure_does_not_suppress_eligible_buy(
    failed_game_id, failure_status
):
    payload = {
        "slate": {
            "market_data_complete": False,
            "expected_games": 2,
            "accounted_games": 2,
            "all_games_accounted": True,
            "dates": [
                {
                    "date": "2026-09-27",
                    "games": [
                        {
                            "game_id": "2026_03_ARI_NYG",
                            "away_team": "ARI",
                            "home_team": "NYG",
                            "status": "BUY",
                        },
                        {"game_id": failed_game_id, "status": failure_status},
                    ],
                }
            ],
        },
        "summary": {"rows": [eligible_nfl_buy()]},
    }

    result = sanitize_completed_scan("nfl", json.dumps(payload), generated_at=NOW)

    assert result["data_quality_state"] == "DEGRADED"
    assert result["market_data_complete"] is False
    assert result["buy_publication_eligible"] is True
    assert result["summary"]["buy"] == 1
    assert [game["status"] for game in result["slate"]["dates"][0]["games"]] == [
        "BUY",
        failure_status,
    ]


def test_economic_duplicate_nfl_contracts_publish_once():
    expensive = eligible_nfl_buy(market_id="contract-expensive", price=0.42)
    canonical = eligible_nfl_buy(market_id="contract-canonical", price=0.39)
    payload = {
        "slate": nfl_slate_for(complete=False),
        "summary": {"rows": [expensive, canonical]},
    }

    result = sanitize_completed_scan("nfl", json.dumps(payload), generated_at=NOW)

    assert result["summary"]["buy"] == 1
    assert result["plays"][0]["market_id"] == "contract-canonical"


def test_incremental_nfl_publication_withholds_buy_expired_at_write(tmp_path):
    payload = incremental_nfl_game(eligible_nfl_buy())

    destination = publish_incremental_nfl_game(
        payload, tmp_path, generated_at=NOW + timedelta(minutes=1)
    )

    public = json.loads(destination.read_text())
    assert public["summary"]["buy"] == 0
    assert public["plays"] == []


def test_incremental_nfl_publication_deduplicates_equivalent_venues(tmp_path):
    expensive = eligible_nfl_buy(
        market_id="pmus-expensive", venue="PMUS", price=0.43
    )
    canonical = eligible_nfl_buy(
        market_id="kalshi-canonical", venue="KALSHI", price=0.39
    )
    payload = incremental_nfl_game(expensive)
    payload["summary"]["rows"].append(canonical)

    destination = publish_incremental_nfl_game(
        payload, tmp_path, generated_at=NOW
    )

    public = json.loads(destination.read_text())
    assert public["summary"]["buy"] == 1
    assert public["plays"][0]["market_id"] == "kalshi-canonical"


def test_incremental_game_merge_preserves_other_unexpired_buy(tmp_path):
    game_a = eligible_nfl_buy()
    game_b = eligible_nfl_buy(
        game_id="2026_03_KC_BUF",
        market_id="game-b",
        economic_team="KC",
        selected_team="Kansas City Chiefs",
    )
    publish_incremental_nfl_game(
        incremental_nfl_game(game_a), tmp_path, generated_at=NOW
    )
    destination = publish_incremental_nfl_game(
        incremental_nfl_game(
            game_b,
            finalized_at="2026-09-24T12:29:50+00:00",
            away_team="KC",
            home_team="BUF",
        ),
        tmp_path,
        generated_at=NOW,
    )

    public = json.loads(destination.read_text())
    assert public["summary"]["buy"] == 2
    assert {play["market_id"] for play in public["plays"]} == {
        game_a["market_id"],
        game_b["market_id"],
    }


def test_incremental_write_removes_another_games_expired_buy(tmp_path):
    game_a = eligible_nfl_buy()
    game_b = eligible_nfl_buy(
        game_id="2026_03_KC_BUF",
        market_id="game-b-current",
        economic_team="KC",
        selected_team="Kansas City Chiefs",
    )
    game_b["updated_at"] = "2026-09-24T12:30:45+00:00"
    game_b["expires_at"] = "2026-09-24T12:31:45+00:00"
    publish_incremental_nfl_game(
        incremental_nfl_game(game_a), tmp_path, generated_at=NOW
    )

    destination = publish_incremental_nfl_game(
        incremental_nfl_game(
            game_b,
            finalized_at="2026-09-24T12:30:50+00:00",
            away_team="KC",
            home_team="BUF",
        ),
        tmp_path,
        generated_at=NOW + timedelta(minutes=1),
    )

    public = json.loads(destination.read_text())
    assert public["summary"]["buy"] == 1
    assert [play["market_id"] for play in public["plays"]] == ["game-b-current"]


def test_older_incremental_game_cannot_overwrite_newer_state(tmp_path):
    newer = eligible_nfl_buy(market_id="newer")
    older = eligible_nfl_buy(market_id="older")
    destination = publish_incremental_nfl_game(
        incremental_nfl_game(
            newer, finalized_at="2026-09-24T12:29:50+00:00"
        ),
        tmp_path,
        generated_at=NOW,
    )
    before = destination.read_text()

    publish_incremental_nfl_game(
        incremental_nfl_game(
            older, finalized_at="2026-09-24T12:29:40+00:00"
        ),
        tmp_path,
        generated_at=NOW,
    )

    assert destination.read_text() == before
    assert json.loads(before)["plays"][0]["market_id"] == "newer"


def test_completed_nfl_export_reconciles_and_removes_incremental_buy(tmp_path):
    publish_incremental_nfl_game(
        incremental_nfl_game(eligible_nfl_buy()), tmp_path, generated_at=NOW
    )
    completed = {
        "read_only": True,
        "slate": nfl_slate_for(status="PASS", complete=True),
        "summary": {"rows": []},
    }

    destination = export_completed_scan("nfl", json.dumps(completed), tmp_path)

    public = json.loads(destination.read_text())
    assert public["market_data_complete"] is True
    assert public["summary"]["buy"] == 0
    assert public["plays"] == []


def test_incremental_nfl_legacy_buy_without_attestation_is_withheld(tmp_path):
    legacy = eligible_nfl_buy()
    legacy.pop("publication_eligible")

    destination = publish_incremental_nfl_game(
        incremental_nfl_game(legacy), tmp_path, generated_at=NOW
    )

    assert json.loads(destination.read_text())["summary"]["buy"] == 0


@pytest.mark.parametrize("own_status", ["MAPPING_FAILURE", "DATA_UNAVAILABLE"])
def test_nfl_publisher_blocks_buy_when_own_game_has_localized_failure(own_status):
    payload = {
        "slate": nfl_slate_for(status=own_status, complete=False),
        "summary": {"rows": [eligible_nfl_buy()]},
    }

    result = sanitize_completed_scan("nfl", json.dumps(payload), generated_at=NOW)

    assert result["buy_publication_eligible"] is False
    assert result["summary"]["buy"] == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("publication_eligible", False),
        ("acquisition_status", "PARTIAL"),
        ("mapping_status", "MAPPING_FAILURE"),
        ("data_freshness", "STALE_OR_UNKNOWN"),
        ("safety_margin", 0.0),
        ("expected_return", 0.049),
        ("economic_team", "NYG"),
        ("selected_team", "New York Giants"),
    ],
)
def test_nfl_publisher_rechecks_individual_buy_attestation(field, value):
    buy = eligible_nfl_buy()
    buy[field] = value
    payload = {
        "slate": nfl_slate_for(),
        "summary": {"rows": [buy]},
    }

    result = sanitize_completed_scan("nfl", json.dumps(payload), generated_at=NOW)

    assert result["buy_publication_eligible"] is False
    assert result["summary"]["buy"] == 0


def test_nfl_publisher_blocks_internally_consistent_team_outside_matchup():
    buy = eligible_nfl_buy(
        economic_team="KC",
        selected_team="Kansas City Chiefs",
    )
    payload = {"slate": nfl_slate_for(), "summary": {"rows": [buy]}}

    result = sanitize_completed_scan("nfl", json.dumps(payload), generated_at=NOW)

    assert result["buy_publication_eligible"] is False
    assert result["summary"]["buy"] == 0


def test_explicit_incomplete_aggregate_does_not_override_certified_mlb_buy():
    payload = json.loads(completed_mlb_stdout())
    payload["market_data_complete"] = True
    payload["slate"]["market_data_complete"] = False

    result = sanitize_completed_scan("mlb", json.dumps(payload), generated_at=NOW)

    assert result["market_data_complete"] is False
    assert result["data_quality_state"] == "DEGRADED"
    assert result["buy_publication_eligible"] is True
    assert result["summary"]["buy"] == 1


def test_public_feed_preserves_only_sanitized_dynamic_slate_fields():
    stdout = json.dumps(
        {
            "read_only": True,
            "slate": {
                "schedule_state": "COMPLETE",
                "expected_games": 2,
                "accounted_games": 2,
                "all_games_accounted": True,
                "market_data_complete": True,
                "status_counts": {"BUY": 1, "PASS": 1},
                "private_debug": "do-not-export",
                "dates": [
                    {
                        "date": "2026-09-27",
                        "expected_games": 2,
                        "accounted_games": 2,
                        "all_games_accounted": True,
                        "market_data_complete": True,
                        "status_counts": {"BUY": 1, "PASS": 1},
                        "games": [
                            {
                                "game_id": "g1",
                                "date": "2026-09-27",
                                "start_time": "2026-09-27T17:00:00+00:00",
                                "away_team": "KC",
                                "home_team": "MIA",
                                "schedule_status": "SCHEDULED",
                                "status": "BUY",
                                "raw_provider_payload": "secret",
                            },
                            {
                                "game_id": "g2",
                                "date": "2026-09-27",
                                "start_time": "2026-09-27T17:00:00+00:00",
                                "away_team": "CAR",
                                "home_team": "CLE",
                                "schedule_status": "SCHEDULED",
                                "status": "PASS",
                            },
                        ],
                    }
                ],
            },
            "summary": {
                "rows": [
                    eligible_nfl_buy(
                        game_id="g1",
                        economic_team="KC",
                        selected_team="Kansas City Chiefs",
                    )
                ]
            },
        }
    )

    result = sanitize_completed_scan("nfl", stdout, generated_at=NOW)

    assert result["slate"]["expected_games"] == 2
    assert result["slate"]["accounted_games"] == 2
    assert result["slate"]["all_games_accounted"] is True
    assert [row["status"] for row in result["slate"]["dates"][0]["games"]] == [
        "BUY",
        "PASS",
    ]
    serialized = json.dumps(result["slate"], sort_keys=True)
    assert "private_debug" not in serialized
    assert "raw_provider_payload" not in serialized
    assert "secret" not in serialized


@pytest.mark.parametrize("lane", ["nfl", "mlb"])
def test_incomplete_scan_is_current_and_degraded_without_public_buy(lane):
    buy = {
        "venue": "KALSHI",
        "market_id": f"{lane}-buy",
        "side": "YES",
        "verdict" if lane == "nfl" else "suggested_action": "BUY",
    }
    watch = {
        "venue": "KALSHI",
        "market_id": f"{lane}-watch",
        "side": "NO",
        "verdict" if lane == "nfl" else "suggested_action": "WATCH",
    }
    payload = {
        "health": {"mode": "live", "state": "healthy"},
        "slate": {
            "market_data_complete": False,
            "status_counts": {"BUY": 1, "WATCH": 1},
            "dates": [
                {
                    "date": "2026-09-24",
                    "market_data_complete": False,
                    "status_counts": {"BUY": 1, "WATCH": 1},
                    "games": [{"game_id": "g1", "status": "BUY"}],
                }
            ],
        },
    }
    if lane == "nfl":
        payload["summary"] = {"rows": [buy, watch]}
    else:
        payload["plays"] = {"mode": "live", "as_of": ISSUED, "items": [buy, watch]}

    result = sanitize_completed_scan(lane, json.dumps(payload), generated_at=NOW)

    assert result["generated_at"] == NOW.isoformat()
    assert result["market_data_complete"] is False
    assert result["data_quality_state"] == "DEGRADED"
    assert result["health_state"] == "DEGRADED"
    assert result["buy_publication_eligible"] is False
    assert result["summary"] == {"plays": 1, "buy": 0, "watch": 1, "pass": 0}
    assert [play["action"] for play in result["plays"]] == ["WATCH"]
    assert "BUY" not in result["slate"]["status_counts"]
    assert result["slate"]["dates"][0]["games"][0]["status"] == (
        "WITHHELD_INCOMPLETE_DATA"
    )


def test_cfb_real_scanner_contract_exports_without_fabricating_completeness_or_buy():
    payload = json.loads(completed_cfb_stdout())
    payload["market_data_complete"] = True
    result = sanitize_completed_scan("cfb", json.dumps(payload), generated_at=NOW)

    assert result["lane"] == "CFB"
    assert result["generated_at"] == NOW.isoformat()
    assert result["source_as_of"] is None
    assert result["data_quality_state"] == "UNVERIFIED"
    assert result["buy_publication_eligible"] is False
    assert "market_data_complete" not in result
    assert result["summary"] == {"plays": 2, "buy": 0, "watch": 1, "pass": 1}
    assert [play["action"] for play in result["plays"]] == ["WATCH", "PASS"]
    assert result["plays"][0]["model_probability"] == 0.28
    assert result["plays"][0]["resolution_time"] == "2026-09-26T19:30:00+00:00"
    assert result["venues"]["PMUS"]["coverage_state"] == "COMPLETE"
    assert result["venues"]["KALSHI"]["endpoint_status"] == "UNAVAILABLE"
    assert "BUY" not in result["venues"]["PMUS"]
    assert "BUY" not in result["venues"]["PMUS"]["status_counts"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.pop("rows"),
        lambda payload: payload.__setitem__("read_only", False),
        lambda payload: payload.__setitem__("orders", 1),
        lambda payload: payload["rows"][0].pop("side"),
        lambda payload: payload["rows"][0].__setitem__("verdict", "UNKNOWN"),
    ],
)
def test_malformed_or_untrustworthy_cfb_output_fails_closed(mutation):
    payload = json.loads(completed_cfb_stdout())
    mutation(payload)

    with pytest.raises(ValueError):
        sanitize_completed_scan("cfb", json.dumps(payload), generated_at=NOW)


def test_cfb_export_is_local_postprocessing_only(tmp_path):
    destination = export_completed_scan("cfb", completed_cfb_stdout(), tmp_path)

    assert destination == tmp_path / "public_feed" / "cfb.json"
    assert json.loads(destination.read_text())["lane"] == "CFB"


def test_certified_cfb_buy_publishes_while_lane_remains_unverified():
    payload = json.loads(completed_cfb_stdout())
    payload["rows"] = [eligible_cfb_buy()]
    payload["venues"]["PMUS"]["coverage"]["state"] = "BOUNDED"

    result = sanitize_completed_scan("cfb", json.dumps(payload), generated_at=NOW)

    assert result["data_quality_state"] == "UNVERIFIED"
    assert result["buy_publication_eligible"] is True
    assert result["summary"]["buy"] == 1
    assert result["plays"][0]["publication_eligible"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("publication_eligible", False),
        ("mapping_status", "TEAM_PAIR_MISMATCH"),
        ("data_freshness", "STALE_OR_UNKNOWN"),
        ("expires_at", "2026-09-24T12:29:59+00:00"),
        ("selected_team", "Texas"),
        ("fee_status", "UNVERIFIED"),
        ("expected_return", 0.049),
    ],
)
def test_uncertified_malformed_or_stale_cfb_buy_is_withheld(field, value):
    payload = json.loads(completed_cfb_stdout())
    buy = eligible_cfb_buy()
    buy[field] = value
    payload["rows"] = [buy]

    result = sanitize_completed_scan("cfb", json.dumps(payload), generated_at=NOW)

    assert result["buy_publication_eligible"] is False
    assert result["summary"]["buy"] == 0
    assert result["plays"] == []


def test_certified_mlb_postseason_buy_survives_unrelated_slate_degradation():
    payload = json.loads(completed_mlb_stdout())
    payload["slate"]["market_data_complete"] = False
    payload["slate"]["dates"][0]["games"].append(
        {
            "game_id": "unrelated-broken-game",
            "away_team": "Boston Red Sox",
            "home_team": "New York Yankees",
            "status": "DATA_UNAVAILABLE",
        }
    )

    result = sanitize_completed_scan("mlb", json.dumps(payload), generated_at=NOW)

    assert result["data_quality_state"] == "DEGRADED"
    assert result["market_data_complete"] is False
    assert result["buy_publication_eligible"] is True
    assert result["summary"]["buy"] == 1


def test_current_13_5_point_division_series_edge_clears_frozen_safety():
    payload = json.loads(completed_mlb_stdout())
    buy = payload["plays"]["items"][0]
    buy["executable_price"] = 0.33
    buy["model_probability"] = 0.465
    buy["edge_points"] = 13.5

    result = sanitize_completed_scan("mlb", json.dumps(payload), generated_at=NOW)

    assert result["buy_publication_eligible"] is True
    assert result["summary"]["buy"] == 1


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("evidence", "validation_status"), "UNVALIDATED"),
        (("evidence", "validation_reference"), "unvalidated:mlb-v2:official-postseason"),
        (("evidence", "forecast_metadata", "official_game_type"), "L"),
        (("selected_team",), "Ambiguous Baseball Club"),
        (("mapping_status",), "AMBIGUOUS"),
    ],
)
def test_unvalidated_or_ambiguous_mlb_postseason_buy_is_withheld(path, value):
    payload = json.loads(completed_mlb_stdout())
    target = payload["plays"]["items"][0]
    nested = target
    for key in path[:-1]:
        nested = nested[key]
    nested[path[-1]] = value

    result = sanitize_completed_scan("mlb", json.dumps(payload), generated_at=NOW)

    assert result["buy_publication_eligible"] is False
    assert result["summary"]["buy"] == 0
