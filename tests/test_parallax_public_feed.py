from __future__ import annotations

import json
import os
import hashlib
from datetime import UTC, datetime, timedelta

from parallax.public_feed import export_completed_scan, sanitize_completed_scan


NOW = datetime(2026, 9, 24, 12, 30, tzinfo=UTC)
ISSUED = "2026-09-24T12:00:00+00:00"


def completed_mlb_stdout() -> str:
    plays = []
    for index, action in enumerate(("BUY", "WATCH", "PASS")):
        plays.append(
            {
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
                "status": "OPEN",
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
        )
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
            "environment": {"TOKEN": "environment-secret"},
        }
    )


def test_sanitizer_counts_actions_and_maps_only_public_fields():
    result = sanitize_completed_scan("mlb", completed_mlb_stdout(), generated_at=NOW)

    assert result["schema_version"] == "parallax.public.v1"
    assert result["lane"] == "MLB"
    assert result["runtime_state"] == "LIVE"
    assert result["health_state"] == "HEALTHY"
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
            "summary": {"rows": []},
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
