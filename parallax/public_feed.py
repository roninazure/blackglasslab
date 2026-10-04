"""Sanitized, local-only exports of completed unattended sports scans."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import fcntl

from .nfl import NFL_TEAM_NAMES
from .cfb import MODEL_VERSION as CFB_MODEL_VERSION, VALIDATION_ECE as CFB_VALIDATION_ECE, VALIDATION_REFERENCE as CFB_VALIDATION_REFERENCE, _canonical_team
from .mlb import DIVISION_SERIES_VALIDATION_ECE, SOURCE_ID as MLB_SOURCE_ID, _team_key as _mlb_team_key, mlb_target_model_version, mlb_target_validation_reference


SCHEMA_VERSION = "parallax.public.v1"
NFL_INCREMENTAL_SCHEMA_VERSION = "parallax.nfl.incremental.v1"
FREE_VISIBILITY_DELAY = timedelta(minutes=15)
STALE_AFTER = timedelta(minutes=90)
PUBLIC_ACTIONS = {"BUY", "WATCH", "PASS"}
PUBLIC_LANES = {"nfl", "cfb", "mlb"}
PUBLIC_PLAY_FIELDS = (
    "sport",
    "venue",
    "market_id",
    "market_title",
    "matchup",
    "contract_side",
    "contract_label",
    "position_label",
    "action",
    "price",
    "model_probability",
    "edge_pp",
    "confidence_band",
    "freshness",
    "status",
    "issued_at",
    "updated_at",
    "expires_at",
    "resolution_time",
    "free_visible_at",
    "reason",
    "failed_gates",
    "contract_url",
    "retail_examples",
    "publication_eligible",
)
RETAIL_FIELDS = (
    "stake",
    "available",
    "contracts_or_shares",
    "total_cost",
    "estimated_payout_if_correct",
    "net_profit_if_correct",
    "maximum_loss_including_fees",
)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _timestamp(value: object) -> str | None:
    parsed = _parse_timestamp(value)
    return parsed.isoformat() if parsed is not None else None


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _contract_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    allowed = any(
        host == root or host.endswith(f".{root}")
        for root in ("polymarket.com", "polymarket.us", "kalshi.com")
    )
    if parsed.scheme != "https" or not allowed:
        return None
    return value


def _retail_examples(value: object) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    result: list[dict[str, Any]] = []
    for example in value[:8]:
        if not isinstance(example, Mapping):
            continue
        public: dict[str, Any] = {}
        for field in RETAIL_FIELDS:
            item = example.get(field)
            if field == "available" and isinstance(item, bool):
                public[field] = item
            elif (number := _number(item)) is not None:
                public[field] = number
        result.append(public)
    return result


def _failed_gates(value: object) -> list[str] | None:
    if not isinstance(value, (list, tuple)):
        return None
    return [item for item in value if isinstance(item, str)][:32]


def _reason(row: Mapping[str, Any]) -> str | None:
    direct = _text(row.get("reason_summary"))
    if direct:
        return direct
    verdict = row.get("verdict")
    if isinstance(verdict, Mapping):
        return _text(verdict.get("primary_reason"))
    return None


def _action(row: Mapping[str, Any]) -> str | None:
    value = _first(row, "suggested_action", "action")
    if value is None and isinstance(row.get("verdict"), str):
        # The existing NFL scan names its already-computed action ``verdict``.
        value = row["verdict"]
    if hasattr(value, "value"):
        value = value.value
    normalized = str(value).upper() if value is not None else None
    return normalized if normalized in PUBLIC_ACTIONS else None


def _source_rows(payload: Mapping[str, Any], lane: str) -> list[Mapping[str, Any]]:
    if lane == "mlb":
        plays = payload.get("plays")
        rows = plays.get("items") if isinstance(plays, Mapping) else None
    elif lane == "cfb":
        rows = payload.get("rows")
    else:
        summary = payload.get("summary")
        rows = summary.get("rows") if isinstance(summary, Mapping) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def _source_as_of(payload: Mapping[str, Any], rows: list[Mapping[str, Any]]) -> str | None:
    plays = payload.get("plays")
    candidates: list[object] = [payload.get("source_as_of")]
    if isinstance(plays, Mapping):
        candidates.append(plays.get("as_of"))
    candidates.extend(
        _first(row, "updated_at", "issued_at", "created_at", "data_timestamp")
        for row in rows
    )
    parsed = [value for value in (_parse_timestamp(item) for item in candidates) if value]
    return max(parsed).isoformat() if parsed else None


def _mode(payload: Mapping[str, Any]) -> str | None:
    health = payload.get("health")
    plays = payload.get("plays")
    value = health.get("mode") if isinstance(health, Mapping) else None
    if value is None and isinstance(plays, Mapping):
        value = plays.get("mode")
    if value is None:
        value = payload.get("mode")
    return str(value).casefold() if value is not None else None


def _runtime_state(
    payload: Mapping[str, Any], source_as_of: str | None, generated_at: datetime
) -> str:
    mode = _mode(payload)
    if mode in {"paused", "disabled"}:
        return "PAUSED"
    source_time = _parse_timestamp(source_as_of)
    if source_time is not None and generated_at - source_time > STALE_AFTER:
        return "STALE"
    if mode == "live" and source_time is not None:
        return "LIVE"
    return "UNKNOWN"


def _health_state(payload: Mapping[str, Any]) -> str:
    if _market_data_complete(payload) is False:
        return "DEGRADED"
    health = payload.get("health")
    value = None
    if isinstance(health, Mapping):
        value = _first(health, "state", "status", "health_state")
    if value is None:
        value = _first(payload, "health_state", "status")
    normalized = str(value).casefold() if value is not None else ""
    if normalized in {"healthy", "ok"}:
        return "HEALTHY"
    if normalized == "degraded":
        return "DEGRADED"
    return "UNKNOWN"


def _market_data_complete(payload: Mapping[str, Any]) -> bool | None:
    values = [payload.get("market_data_complete")]
    slate = payload.get("slate")
    if isinstance(slate, Mapping):
        values.append(slate.get("market_data_complete"))
    reported = [value for value in values if isinstance(value, bool)]
    if False in reported:
        return False
    return True if True in reported else None


def _validate_cfb_payload(payload: Mapping[str, Any]) -> None:
    """Require the current CFB scanner's fail-closed, read-only output contract."""
    if payload.get("read_only") is not True:
        raise ValueError("CFB scan output must attest read_only=true")
    for field in ("orders", "published"):
        value = payload.get(field)
        if isinstance(value, bool) or value != 0:
            raise ValueError(f"CFB scan output must attest {field}=0")
    alerts = payload.get("alerts")
    if isinstance(alerts, bool) or not isinstance(alerts, int) or alerts < 0:
        raise ValueError("CFB scan output must report a non-negative alert count")
    venues = payload.get("venues")
    if not isinstance(venues, Mapping) or not all(
        isinstance(name, str) and isinstance(value, Mapping)
        for name, value in venues.items()
    ):
        raise ValueError("CFB scan output must contain venue diagnostics")
    rows = payload.get("rows")
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        raise ValueError("CFB scan output must contain a row list")
    for row in rows:
        if not all(_text(row.get(field)) for field in ("venue", "market_id", "side")):
            raise ValueError("CFB public rows require venue, market_id, and side")
        if _action(row) is None:
            raise ValueError("CFB public rows require a recognized verdict")


def _public_count_map(value: object, *, allow_buy: bool) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        str(key): int(count)
        for key, count in value.items()
        if isinstance(count, int)
        and not isinstance(count, bool)
        and (allow_buy or str(key).upper() != "BUY")
    }


def _public_venues(
    value: object, *, allow_buy: bool
) -> dict[str, dict[str, Any]] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, dict[str, Any]] = {}
    numeric_fields = (
        "discovered",
        "eligible",
        "mapped",
        "evidence",
        "model_probability",
        "executable_book",
        "fee",
        "edge",
        "economics",
        "fully_scored_sides",
        "WATCH",
        "PASS",
    )
    for venue, diagnostic in value.items():
        if not isinstance(venue, str) or not isinstance(diagnostic, Mapping):
            continue
        public: dict[str, Any] = {}
        endpoint_status = _text(diagnostic.get("endpoint_status"))
        if endpoint_status:
            public["endpoint_status"] = endpoint_status
        coverage = diagnostic.get("coverage")
        if isinstance(coverage, Mapping):
            coverage_state = _text(coverage.get("state"))
            if coverage_state:
                public["coverage_state"] = coverage_state
        for field in numeric_fields:
            number = _number(diagnostic.get(field))
            if number is not None:
                public[field] = number
        if allow_buy:
            number = _number(diagnostic.get("BUY"))
            if number is not None:
                public["BUY"] = number
        counts = _public_count_map(diagnostic.get("status_counts"), allow_buy=allow_buy)
        if counts is not None:
            public["status_counts"] = counts
        result[venue.upper()] = public
    return result


def _public_slate(
    value: object,
    *,
    allow_buy: bool,
    eligible_nfl_game_ids: set[str] | None = None,
    expired_nfl_signal_game_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    public: dict[str, Any] = {}
    for key in (
        "schedule_state",
        "expected_games",
        "accounted_games",
        "all_games_accounted",
        "market_data_complete",
    ):
        item = value.get(key)
        if isinstance(item, (str, int, bool)) or item is None:
            public[key] = item
    status_counts = value.get("status_counts")
    counts = _public_count_map(status_counts, allow_buy=allow_buy)
    if counts is not None:
        public["status_counts"] = counts
    dates_out = []
    dates = value.get("dates")
    if isinstance(dates, list):
        for date_row in dates:
            if not isinstance(date_row, Mapping):
                continue
            date_public = {
                key: date_row.get(key)
                for key in (
                    "date",
                    "expected_games",
                    "accounted_games",
                    "all_games_accounted",
                    "market_data_complete",
                )
                if isinstance(date_row.get(key), (str, int, bool))
            }
            counts = date_row.get("status_counts")
            public_counts = _public_count_map(counts, allow_buy=allow_buy)
            if public_counts is not None:
                date_public["status_counts"] = public_counts
            games_out = []
            games = date_row.get("games")
            if isinstance(games, list):
                for game in games:
                    if not isinstance(game, Mapping):
                        continue
                    game_public = {
                        key: game.get(key)
                        for key in (
                            "game_id",
                            "date",
                            "start_time",
                            "away_team",
                            "home_team",
                            "schedule_status",
                            "status",
                        )
                        if isinstance(game.get(key), str)
                    }
                    if (
                        (
                            not allow_buy
                            or (
                                eligible_nfl_game_ids is not None
                                and game_public.get("game_id")
                                not in eligible_nfl_game_ids
                            )
                        )
                        and game_public.get("status", "").upper() == "BUY"
                    ):
                        game_public["status"] = (
                            "WATCH"
                            if expired_nfl_signal_game_ids is not None
                            and game_public.get("game_id")
                            in expired_nfl_signal_game_ids
                            else "WITHHELD_INCOMPLETE_DATA"
                        )
                    games_out.append(game_public)
            date_public["games"] = games_out
            if eligible_nfl_game_ids is not None:
                date_public["status_counts"] = dict(
                    Counter(game.get("status") for game in games_out)
                )
            dates_out.append(date_public)
    public["dates"] = dates_out
    if eligible_nfl_game_ids is not None:
        public["status_counts"] = dict(
            Counter(
                game.get("status")
                for date_row in dates_out
                for game in date_row.get("games", [])
            )
        )
    return public


def _nfl_buy_certificate_valid(row: Mapping[str, Any]) -> bool:
    """Cross-check the immutable parts of an NFL BUY certificate."""
    if (
        row.get("publication_eligible") is not True
        or _action(row) != "BUY"
        or row.get("mapping_status") != "MAPPED_GAME_WINNER"
        or row.get("acquisition_status") != "ACQUIRED"
        or row.get("data_freshness") != "FRESH"
        or row.get("status") != "CURRENT"
        or row.get("failed_gates") not in ([], ())
    ):
        return False
    required_text = {
        key: _text(row.get(key))
        for key in (
            "venue",
            "market_id",
            "side",
            "game_id",
            "economic_key",
            "economic_team",
            "selected_team",
        )
    }
    if not all(required_text.values()):
        return False
    team = required_text["economic_team"].upper()
    if (
        required_text["venue"].upper() not in {"PMUS", "KALSHI"}
        or required_text["side"].upper() not in {"YES", "NO"}
        or team not in NFL_TEAM_NAMES
        or required_text["selected_team"] != NFL_TEAM_NAMES[team]
        or required_text["economic_key"]
        != f"NFL:{required_text['game_id']}:{team}"
    ):
        return False
    numeric = {
        "price": _number(row.get("executable_price")),
        "probability": _number(row.get("nfl_v1_probability")),
        "fee": _number(row.get("fee")),
        "edge": _number(row.get("raw_edge")),
        "safety_margin": _number(row.get("safety_margin")),
        "liquidity": _number(row.get("liquidity")),
        "net_ev": _number(row.get("net_ev_25")),
        "expected_return": _number(row.get("expected_return")),
    }
    if not all(value is not None and math.isfinite(value) for value in numeric.values()):
        return False
    if not (
        0 < numeric["price"] < 1
        and 0 < numeric["probability"] < 1
        and numeric["fee"] >= 0
        and numeric["edge"] > 0
        and numeric["safety_margin"] > 0
        and numeric["liquidity"] > 0
        and numeric["net_ev"] > 0
        and numeric["expected_return"] >= 0.05
    ):
        return False
    return True


def _nfl_buy_source_eligible(row: Mapping[str, Any], now: datetime) -> bool:
    """Require a valid certificate backed by a currently executable quote."""
    if not _nfl_buy_certificate_valid(row):
        return False
    updated_at = _parse_timestamp(row.get("updated_at"))
    expires_at = _parse_timestamp(row.get("expires_at"))
    game_start = _parse_timestamp(row.get("game_start"))
    return bool(
        updated_at
        and expires_at
        and game_start
        and updated_at <= now < expires_at
        and now < game_start
    )


def _cfb_buy_source_eligible(row: Mapping[str, Any], now: datetime) -> bool:
    if (
        row.get("publication_eligible") is not True
        or _action(row) != "BUY"
        or row.get("mapping_status") != "MAPPED"
        or row.get("acquisition_status") != "ACQUIRED"
        or row.get("market_status") != "OPEN"
        or row.get("data_freshness") != "FRESH"
        or row.get("status") != "CURRENT"
        or row.get("failed_gates") not in ([], ())
        or row.get("cfb_safety") is not True
        or row.get("fee_status")
        not in {"REVIEWED", "VERIFIED_SCHEDULE", "VERIFIED_UPPER_BOUND"}
        or row.get("evidence_source") != "CollegeFootballData"
        or row.get("evidence_model_version") != CFB_MODEL_VERSION
        or row.get("evidence_validation_status") != "CALIBRATED"
        or row.get("evidence_validation_reference") != CFB_VALIDATION_REFERENCE
    ):
        return False
    required = {
        key: _text(row.get(key))
        for key in (
            "venue", "market_id", "side", "game_id", "economic_key",
            "selected_team", "away_team", "home_team",
        )
    }
    if not all(required.values()):
        return False
    selected_key = _canonical_team(required["selected_team"])
    participants = {
        _canonical_team(required["away_team"]),
        _canonical_team(required["home_team"]),
    }
    if (
        required["venue"].upper() not in {"PMUS", "KALSHI"}
        or required["side"].upper() not in {"YES", "NO"}
        or selected_key not in participants
        or required["economic_key"]
        != f"CFB:{required['game_id']}:{selected_key}"
    ):
        return False
    numeric = {
        "price": _number(row.get("executable_price")),
        "probability": _number(row.get("cfb_v1_probability")),
        "fee": _number(row.get("fee")),
        "edge": _number(row.get("raw_edge")),
        "liquidity": _number(row.get("liquidity")),
        "net_ev": _number(row.get("net_ev_25")),
        "expected_return": _number(row.get("expected_return")),
    }
    if not all(value is not None and math.isfinite(value) for value in numeric.values()):
        return False
    if not (
        0 < numeric["price"] < 1
        and 0 < numeric["probability"] < 1
        and numeric["fee"] >= 0
        and abs((numeric["probability"] - numeric["price"]) * 100 - numeric["edge"]) <= 1e-6
        and numeric["edge"] / 100 > CFB_VALIDATION_ECE + 1e-9
        and numeric["liquidity"] > 0
        and numeric["net_ev"] > 0
        and numeric["expected_return"] >= 0.05
    ):
        return False
    updated_at = _parse_timestamp(row.get("updated_at"))
    expires_at = _parse_timestamp(row.get("expires_at"))
    game_start = _parse_timestamp(_first(row, "game_start", "kickoff_utc"))
    return bool(
        updated_at and expires_at and game_start
        and updated_at <= now < expires_at
        and now < game_start
    )


def _mlb_buy_source_eligible(
    row: Mapping[str, Any], now: datetime, game: Mapping[str, Any] | None
) -> bool:
    evidence = row.get("evidence")
    verdict = row.get("verdict")
    failed_gates = verdict.get("failed_gates") if isinstance(verdict, Mapping) else None
    game_type = (
        evidence.get("forecast_metadata", {}).get("official_game_type")
        if isinstance(evidence, Mapping)
        and isinstance(evidence.get("forecast_metadata"), Mapping)
        else None
    )
    if (
        row.get("publication_eligible") is not True
        or _action(row) != "BUY"
        or row.get("mapping_status") != "MAPPED_GAME_WINNER"
        or row.get("acquisition_status") != "ACQUIRED"
        or row.get("market_status") != "OPEN"
        or row.get("data_freshness") != "FRESH"
        or row.get("status") != "CURRENT"
        or failed_gates not in ([], ())
        or row.get("fee_status")
        not in {"REVIEWED", "VERIFIED_SCHEDULE", "VERIFIED_UPPER_BOUND"}
        or not isinstance(evidence, Mapping)
        or evidence.get("source") != MLB_SOURCE_ID
        or evidence.get("model_version") != mlb_target_model_version(game_type)
        or evidence.get("validation_status") != "CALIBRATED"
        or evidence.get("validation_reference") != mlb_target_validation_reference(game_type)
        or game is None
        or game.get("status") != "BUY"
    ):
        return False
    required = {
        key: _text(row.get(key))
        for key in (
            "venue", "market_id", "side", "game_id", "economic_key",
            "selected_team", "away_team", "home_team",
        )
    }
    if not all(required.values()) or required["game_id"] != str(game.get("game_id") or ""):
        return False
    selected_key = _mlb_team_key(required["selected_team"])
    row_participants = {
        _mlb_team_key(required["away_team"]),
        _mlb_team_key(required["home_team"]),
    }
    slate_participants = {
        _mlb_team_key(game.get("away_team")),
        _mlb_team_key(game.get("home_team")),
    }
    if (
        required["venue"].upper() not in {"POLYMARKET", "PMUS", "KALSHI"}
        or required["side"].upper() not in {"YES", "NO"}
        or selected_key not in row_participants
        or row_participants != slate_participants
        or required["economic_key"] != f"MLB:{required['game_id']}:{selected_key}"
    ):
        return False
    numeric = {
        "price": _number(row.get("executable_price")),
        "probability": _number(row.get("model_probability")),
        "fee": _number(row.get("fees_estimate")),
        "edge": _number(row.get("edge_points")),
        "liquidity": _number(row.get("executable_size")),
        "net_ev": _number(row.get("expected_value")),
        "expected_return": _number(row.get("expected_return")),
    }
    if not all(value is not None and math.isfinite(value) for value in numeric.values()):
        return False
    if not (
        0 < numeric["price"] < 1
        and 0 < numeric["probability"] < 1
        and numeric["fee"] >= 0
        and abs((numeric["probability"] - numeric["price"]) * 100 - numeric["edge"]) <= 1e-6
        and numeric["edge"] >= 5.0 - 1e-9
        and (
            game_type != "D"
            or numeric["edge"] / 100 > DIVISION_SERIES_VALIDATION_ECE + 1e-9
        )
        and numeric["liquidity"] > 0
        and numeric["net_ev"] > 0
        and numeric["expected_return"] >= 0.05
    ):
        return False
    updated_at = _parse_timestamp(row.get("updated_at"))
    expires_at = _parse_timestamp(row.get("expires_at"))
    game_start = _parse_timestamp(_first(row, "game_start", "resolution_time"))
    return bool(
        updated_at and expires_at and game_start
        and updated_at <= now < expires_at
        and now < game_start
    )


def _nfl_buy_rank(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        float(row["executable_price"]),
        -float(row["raw_edge"]),
        -float(row["liquidity"]),
        str(row["venue"]),
        str(row["market_id"]),
        str(row["side"]),
    )


def _nfl_slate_games(value: object) -> dict[str, Mapping[str, Any]]:
    """Return unambiguous authoritative game rows from the scanner slate."""
    if not isinstance(value, Mapping) or not isinstance(value.get("dates"), list):
        return {}
    games_by_id: dict[str, Mapping[str, Any]] = {}
    ambiguous: set[str] = set()
    for date_row in value["dates"]:
        games = date_row.get("games") if isinstance(date_row, Mapping) else None
        if not isinstance(games, list):
            continue
        for game in games:
            if not isinstance(game, Mapping):
                continue
            game_id = _text(game.get("game_id"))
            if not game_id:
                continue
            if game_id in games_by_id:
                ambiguous.add(game_id)
            else:
                games_by_id[game_id] = game
    for game_id in ambiguous:
        games_by_id.pop(game_id, None)
    return games_by_id


def _eligible_nfl_buy_indexes(
    rows: list[Mapping[str, Any]], now: datetime, slate: object
) -> set[int]:
    slate_games = _nfl_slate_games(slate)
    grouped: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for index, row in enumerate(rows):
        game = slate_games.get(str(row.get("game_id") or ""))
        participants = (
            {
                str(game.get("away_team") or "").upper(),
                str(game.get("home_team") or "").upper(),
            }
            if game is not None
            else set()
        ) - {""}
        if (
            _nfl_buy_source_eligible(row, now)
            and _text(game.get("status") if game is not None else None) == "BUY"
            and str(row["economic_team"]).upper() in participants
        ):
            grouped.setdefault(str(row["economic_key"]), []).append((index, row))
    return {
        min(equivalents, key=lambda item: _nfl_buy_rank(item[1]))[0]
        for equivalents in grouped.values()
    }


def _expired_nfl_buy_indexes(
    rows: list[Mapping[str, Any]],
    now: datetime,
    slate: object,
    eligible_indexes: set[int],
) -> set[int]:
    """Select certified BUYs whose executable quote, but not game, expired."""
    slate_games = _nfl_slate_games(slate)
    current_keys = {
        str(rows[index]["economic_key"]) for index in eligible_indexes
    }
    observed_keys = {
        str(row["economic_key"])
        for row in rows
        if _text(row.get("economic_key")) and _action(row) != "BUY"
    }
    grouped: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    terminal_game_states = {
        "CANCELLED", "CANCELED", "POSTPONED", "SUSPENDED", "DELAYED",
        "IN_PROGRESS", "PAST_START", "FINAL",
    }
    for index, row in enumerate(rows):
        economic_key = str(row.get("economic_key") or "")
        game = slate_games.get(str(row.get("game_id") or ""))
        participants = (
            {
                str(game.get("away_team") or "").upper(),
                str(game.get("home_team") or "").upper(),
            }
            if game is not None
            else set()
        ) - {""}
        updated_at = _parse_timestamp(row.get("updated_at"))
        expires_at = _parse_timestamp(row.get("expires_at"))
        game_start = _parse_timestamp(row.get("game_start"))
        game_state = str(
            _first(game, "schedule_status", "status") if game is not None else ""
        ).upper()
        if (
            economic_key
            and economic_key not in current_keys
            and economic_key not in observed_keys
            and _nfl_buy_certificate_valid(row)
            and game is not None
            and str(row["economic_team"]).upper() in participants
            and game_state not in terminal_game_states
            and updated_at is not None
            and expires_at is not None
            and game_start is not None
            and updated_at <= expires_at <= now < game_start
        ):
            grouped.setdefault(economic_key, []).append((index, row))
    return {
        min(equivalents, key=lambda item: _nfl_buy_rank(item[1]))[0]
        for equivalents in grouped.values()
    }


def _eligible_cfb_buy_indexes(
    rows: list[Mapping[str, Any]], now: datetime
) -> set[int]:
    return {
        index for index, row in enumerate(rows)
        if _cfb_buy_source_eligible(row, now)
    }


def _eligible_mlb_buy_indexes(
    rows: list[Mapping[str, Any]], now: datetime, slate: object
) -> set[int]:
    slate_games = _nfl_slate_games(slate)
    grouped: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for index, row in enumerate(rows):
        game = slate_games.get(str(row.get("game_id") or ""))
        if _mlb_buy_source_eligible(row, now, game):
            grouped.setdefault(str(row["economic_key"]), []).append((index, row))
    return {
        min(
            equivalents,
            key=lambda item: (
                float(item[1]["executable_price"]),
                -float(item[1]["edge_points"]),
                -float(item[1]["executable_size"]),
                str(item[1]["venue"]),
                str(item[1]["market_id"]),
                str(item[1]["side"]),
            ),
        )[0]
        for equivalents in grouped.values()
    }


def _public_play(row: Mapping[str, Any], lane: str) -> dict[str, Any] | None:
    action = _action(row)
    if action is None:
        return None

    venue = _text(row.get("venue"))
    market_id = _text(row.get("market_id"))
    side = _text(_first(row, "side", "contract_side"))
    internal_id = _text(_first(row, "id", "signal_id")) or ""
    opaque_input = f"{lane}{venue or ''}{market_id or ''}{side or ''}{internal_id}"
    public: dict[str, Any] = {
        "signal_id": f"px1_{hashlib.sha256(opaque_input.encode()).hexdigest()[:20]}",
        "sport": (_text(row.get("sport")) or lane).upper(),
        "action": action,
    }

    simple_text = {
        "venue": venue,
        "market_id": market_id,
        "market_title": _text(_first(row, "market_title", "market")),
        "matchup": _text(row.get("matchup")),
        "contract_side": side,
        "contract_label": _text(_first(row, "side_description", "contract_label")),
        "confidence_band": _text(row.get("confidence_band")),
        "freshness": _text(_first(row, "freshness", "data_freshness")),
        "status": _text(row.get("status")),
    }
    for key, value in simple_text.items():
        if value is not None:
            public[key] = value

    label = simple_text["contract_label"]
    if side and label:
        public["position_label"] = f"{side} — {label}"

    numeric = {
        "price": _number(_first(row, "executable_price", "current_price")),
        "model_probability": _number(
            _first(
                row,
                "model_probability",
                "parallax_fair_value",
                "nfl_v1_probability",
                "cfb_v1_probability",
            )
        ),
        "edge_pp": _number(_first(row, "edge_points", "raw_edge")),
    }
    for key, value in numeric.items():
        if value is not None:
            public[key] = value

    timestamps = {
        "issued_at": _timestamp(_first(row, "issued_at", "created_at")),
        "updated_at": _timestamp(row.get("updated_at")),
        "expires_at": _timestamp(row.get("expires_at")),
        "resolution_time": _timestamp(
            _first(row, "resolution_time", "game_start", "kickoff_utc")
        ),
    }
    for key, value in timestamps.items():
        if value is not None:
            public[key] = value
    issued = _parse_timestamp(timestamps["issued_at"])
    if issued is not None:
        public["free_visible_at"] = (issued + FREE_VISIBILITY_DELAY).isoformat()

    reason = _reason(row)
    if reason:
        public["reason"] = reason
    failed_gates = row.get("failed_gates")
    if failed_gates is None:
        verdict = row.get("verdict")
        if isinstance(verdict, Mapping):
            failed_gates = verdict.get("failed_gates")
    gates = _failed_gates(failed_gates)
    if gates is not None:
        public["failed_gates"] = gates
    url = _contract_url(_first(row, "market_url", "contract_url"))
    if url:
        public["contract_url"] = url
    examples = _retail_examples(row.get("retail_examples"))
    if examples is not None:
        public["retail_examples"] = examples
    if action == "BUY" and row.get("publication_eligible") is True:
        public["publication_eligible"] = True

    # Defense in depth: the returned keys are fixed even if this function changes.
    allowed = {"signal_id", *PUBLIC_PLAY_FIELDS}
    return {key: value for key, value in public.items() if key in allowed}


def _public_expired_nfl_signal(
    row: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Render a prior BUY certificate without presenting its quote as current."""
    public = _public_play(row, "nfl")
    if public is None:
        return None
    checked_at = _timestamp(row.get("updated_at")) or "an unknown time"
    expired_at = _timestamp(row.get("expires_at")) or "an unknown time"
    price = _number(row.get("executable_price"))
    price_text = f" at ${price:.4f}" if price is not None else ""
    public.update(
        {
            "action": "WATCH",
            "freshness": "STALE_OR_UNKNOWN",
            "status": "STALE",
            "reason": (
                f"PARALLAX previously certified a BUY{price_text} at {checked_at}; "
                f"that executable quote expired at {expired_at}. Revalidation is "
                "required before acting."
            ),
            "failed_gates": ["stale_data"],
        }
    )
    for field in (
        "price",
        "model_probability",
        "edge_pp",
        "retail_examples",
        "publication_eligible",
    ):
        public.pop(field, None)
    return public


def sanitize_completed_scan(
    lane: str,
    completed_stdout: str,
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the public schema from a completed scan's captured JSON stdout."""
    normalized_lane = lane.casefold()
    if normalized_lane not in PUBLIC_LANES:
        raise ValueError("Public feed supports only NFL, CFB, and MLB lanes")
    decoded = json.loads(completed_stdout)
    if not isinstance(decoded, Mapping):
        raise ValueError("Completed scan output must be a JSON object")
    if normalized_lane == "cfb":
        _validate_cfb_payload(decoded)
    now = (generated_at or datetime.now(UTC)).astimezone(UTC)
    rows = _source_rows(decoded, normalized_lane)
    market_data_complete = (
        None if normalized_lane == "cfb" else _market_data_complete(decoded)
    )
    nfl_buy_indexes = (
        _eligible_nfl_buy_indexes(rows, now, decoded.get("slate"))
        if normalized_lane == "nfl"
        else set()
    )
    expired_nfl_buy_indexes = (
        _expired_nfl_buy_indexes(
            rows, now, decoded.get("slate"), nfl_buy_indexes
        )
        if normalized_lane == "nfl"
        else set()
    )
    cfb_buy_indexes = (
        _eligible_cfb_buy_indexes(rows, now) if normalized_lane == "cfb" else set()
    )
    mlb_buy_indexes = (
        _eligible_mlb_buy_indexes(rows, now, decoded.get("slate"))
        if normalized_lane == "mlb"
        else set()
    )
    eligible_buy_indexes = (
        nfl_buy_indexes if normalized_lane == "nfl"
        else cfb_buy_indexes if normalized_lane == "cfb"
        else mlb_buy_indexes
    )
    allow_buy = bool(eligible_buy_indexes)
    plays = []
    for index, row in enumerate(rows):
        if index in expired_nfl_buy_indexes:
            play = _public_expired_nfl_signal(row)
        else:
            play = _public_play(row, normalized_lane)
        if play is not None and (
            play["action"] != "BUY" or index in eligible_buy_indexes
        ):
            plays.append(play)
    source_as_of = _source_as_of(decoded, rows)
    counts = {action: sum(play["action"] == action for play in plays) for action in PUBLIC_ACTIONS}
    result = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "source_as_of": source_as_of,
        "lane": normalized_lane.upper(),
        "runtime_state": _runtime_state(decoded, source_as_of, now),
        "health_state": _health_state(decoded),
        "data_quality_state": (
            "COMPLETE"
            if market_data_complete is True
            else "DEGRADED"
            if market_data_complete is False
            else "UNVERIFIED"
        ),
        "buy_publication_eligible": allow_buy,
        "read_only": True,
        "summary": {
            "plays": len(plays),
            "buy": counts["BUY"],
            "watch": counts["WATCH"],
            "pass": counts["PASS"],
        },
        "plays": plays,
    }
    if market_data_complete is not None:
        result["market_data_complete"] = market_data_complete
    eligible_nfl_game_ids = (
        {str(rows[index]["game_id"]) for index in nfl_buy_indexes}
        if normalized_lane == "nfl"
        else None
    )
    expired_nfl_signal_game_ids = (
        {str(rows[index]["game_id"]) for index in expired_nfl_buy_indexes}
        if normalized_lane == "nfl"
        else None
    )
    slate = _public_slate(
        decoded.get("slate"),
        allow_buy=allow_buy,
        eligible_nfl_game_ids=eligible_nfl_game_ids,
        expired_nfl_signal_game_ids=expired_nfl_signal_game_ids,
    )
    if slate is not None:
        result["slate"] = slate
    venues = _public_venues(
        decoded.get("venues"),
        allow_buy=normalized_lane in {"cfb", "mlb"} and allow_buy,
    )
    if venues is not None:
        result["venues"] = venues
    return result


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        raise


@contextmanager
def _nfl_publication_lock(state_dir: Path):
    """Serialize NFL reconciliation and per-game publication in one process tree."""
    directory = Path(state_dir) / "public_feed"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = directory / ".nfl.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _nfl_incremental_state_path(state_dir: Path) -> Path:
    return Path(state_dir) / "public_feed" / ".nfl_incremental.json"


def _read_nfl_incremental_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": NFL_INCREMENTAL_SCHEMA_VERSION,
            "reconciled_at": None,
            "baseline": None,
            "updates": {},
        }
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("NFL incremental state is unreadable") from exc
    if (
        not isinstance(decoded, dict)
        or decoded.get("schema_version") != NFL_INCREMENTAL_SCHEMA_VERSION
        or decoded.get("baseline") is not None
        and not isinstance(decoded.get("baseline"), Mapping)
        or not isinstance(decoded.get("updates"), Mapping)
    ):
        raise ValueError("NFL incremental state is malformed")
    for game_id, update in decoded["updates"].items():
        if (
            not isinstance(game_id, str)
            or not isinstance(update, Mapping)
            or _parse_timestamp(update.get("finalized_at")) is None
            or not isinstance(update.get("payload"), Mapping)
        ):
            raise ValueError("NFL incremental state contains a malformed update")
    return decoded


def _single_nfl_game(payload: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    games = _nfl_slate_games(payload.get("slate"))
    if len(games) != 1:
        raise ValueError("Incremental NFL publication requires exactly one slate game")
    game_id, game = next(iter(games.items()))
    rows = _source_rows(payload, "nfl")
    if any(str(row.get("game_id") or "") != game_id for row in rows):
        raise ValueError("Incremental NFL rows must belong to the finalized game")
    return game_id, game


def _aggregate_nfl_incremental_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Overlay newer per-game snapshots on the last authoritative scan."""
    baseline = state.get("baseline")
    baseline = baseline if isinstance(baseline, Mapping) else {}
    updates = state["updates"]
    overridden = set(updates)
    rows = [
        row
        for row in _source_rows(baseline, "nfl")
        if str(row.get("game_id") or "") not in overridden
    ]
    games = {
        game_id: dict(game)
        for game_id, game in _nfl_slate_games(baseline.get("slate")).items()
        if game_id not in overridden
    }
    for game_id, update in updates.items():
        payload = update["payload"]
        rows.extend(_source_rows(payload, "nfl"))
        update_game_id, game = _single_nfl_game(payload)
        if update_game_id != game_id:
            raise ValueError("NFL incremental state game key does not match its payload")
        games[game_id] = dict(game)

    dates: dict[str, list[dict[str, Any]]] = {}
    for game in games.values():
        date = _text(game.get("date"))
        if date is None:
            start = _parse_timestamp(game.get("start_time"))
            date = start.date().isoformat() if start is not None else "unknown"
        dates.setdefault(date, []).append(game)
    return {
        "read_only": True,
        "orders": 0,
        "published": 0,
        # An incremental view is intentionally honest about not being a new
        # completed-slate reconciliation. Individual NFL BUYs remain eligible.
        "slate": {
            "market_data_complete": False,
            "dates": [
                {"date": date, "market_data_complete": False, "games": games_for_date}
                for date, games_for_date in sorted(dates.items())
            ],
        },
        "summary": {"rows": rows},
    }


def _reconcile_nfl_signal_lifecycle(
    previous: Mapping[str, Any],
    completed: Mapping[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Carry missing certified positions only as expiring customer signals.

    A current observation for the same economic position always wins, including
    WATCH/PASS and failed-gate rows. Missing venue data is not itself evidence
    that the prior signal was false, so its certificate remains available for
    the sanitizer to render as a non-actionable expired-quote lifecycle record.
    """
    current_rows = list(_source_rows(completed, "nfl"))
    current_keys = {
        str(row["economic_key"])
        for row in current_rows
        if _text(row.get("economic_key"))
    }
    current_games = _nfl_slate_games(completed.get("slate"))
    market_data_complete = _market_data_complete(completed)
    candidates: dict[str, list[Mapping[str, Any]]] = {}
    terminal_game_states = {
        "CANCELLED", "CANCELED", "POSTPONED", "SUSPENDED", "DELAYED",
        "IN_PROGRESS", "PAST_START", "FINAL",
    }
    for row in _source_rows(previous, "nfl"):
        economic_key = str(row.get("economic_key") or "")
        game_id = str(row.get("game_id") or "")
        game = current_games.get(game_id)
        participants = (
            {
                str(game.get("away_team") or "").upper(),
                str(game.get("home_team") or "").upper(),
            }
            if game is not None
            else set()
        ) - {""}
        game_start = _parse_timestamp(
            _first(game, "start_time") if game is not None else None
        ) or _parse_timestamp(row.get("game_start"))
        schedule_state = str(
            _first(game, "schedule_status", "status") if game is not None else ""
        ).upper()
        updated_at = _parse_timestamp(row.get("updated_at"))
        expires_at = _parse_timestamp(row.get("expires_at"))
        if (
            economic_key
            and economic_key not in current_keys
            and game is not None
            and schedule_state not in terminal_game_states
            and schedule_state not in {"WATCH", "PASS"}
            and not (
                schedule_state == "NO_MARKET" and market_data_complete is True
            )
            and game_start is not None
            and now < game_start
            and updated_at is not None
            and expires_at is not None
            and updated_at <= expires_at
            and updated_at <= now
            and _nfl_buy_certificate_valid(row)
            and str(row["economic_team"]).upper() in participants
        ):
            candidates.setdefault(economic_key, []).append(row)

    carried = [
        min(equivalents, key=_nfl_buy_rank)
        for equivalents in candidates.values()
    ]
    reconciled = dict(completed)
    summary = completed.get("summary")
    reconciled_summary = dict(summary) if isinstance(summary, Mapping) else {}
    reconciled_summary["rows"] = current_rows + carried
    reconciled["summary"] = reconciled_summary
    return reconciled


def publish_incremental_nfl_game(
    finalized_game: Mapping[str, Any],
    state_dir: Path,
    *,
    generated_at: datetime | None = None,
) -> Path:
    """Atomically merge one finalized NFL game into the sanitized local feed."""
    now = (generated_at or datetime.now(UTC)).astimezone(UTC)
    if finalized_game.get("read_only") is not True:
        raise ValueError("Incremental NFL publication must attest read_only=true")
    finalized_at = _parse_timestamp(finalized_game.get("finalized_at"))
    if finalized_at is None or finalized_at > now:
        raise ValueError("Incremental NFL publication requires a valid finalization time")
    game_id, _game = _single_nfl_game(finalized_game)
    # Exercise the same full BUY attestation, economic deduplication, game-start,
    # and freshness checks before this fragment can enter merge state.
    sanitize_completed_scan("nfl", json.dumps(finalized_game), generated_at=now)

    state_path = _nfl_incremental_state_path(state_dir)
    destination = Path(state_dir) / "public_feed" / "nfl.json"
    with _nfl_publication_lock(state_dir):
        state = _read_nfl_incremental_state(state_path)
        reconciled_at = _parse_timestamp(state.get("reconciled_at"))
        current = state["updates"].get(game_id)
        current_at = (
            _parse_timestamp(current.get("finalized_at"))
            if isinstance(current, Mapping)
            else None
        )
        newest_at = max(
            (value for value in (reconciled_at, current_at) if value is not None),
            default=None,
        )
        if newest_at is not None and finalized_at <= newest_at:
            return destination

        state["updates"][game_id] = {
            "finalized_at": finalized_at.isoformat(),
            "payload": dict(finalized_game),
        }
        aggregate = _aggregate_nfl_incremental_state(state)
        public = sanitize_completed_scan(
            "nfl", json.dumps(aggregate), generated_at=now
        )
        _atomic_write(state_path, state)
        _atomic_write(destination, public)
    return destination


def export_completed_scan(
    lane: str,
    completed_stdout: str,
    state_dir: Path,
    *,
    generated_at: datetime | None = None,
) -> Path:
    """Sanitize one already-completed scan and atomically publish it locally."""
    normalized_lane = lane.casefold()
    destination = Path(state_dir) / "public_feed" / f"{normalized_lane}.json"
    if normalized_lane != "nfl":
        payload = sanitize_completed_scan(
            normalized_lane, completed_stdout, generated_at=generated_at
        )
        _atomic_write(destination, payload)
        return destination

    try:
        decoded = json.loads(completed_stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("Completed scan output must be valid JSON") from exc
    if not isinstance(decoded, Mapping):
        raise ValueError("Completed scan output must be a JSON object")
    now = (generated_at or datetime.now(UTC)).astimezone(UTC)
    with _nfl_publication_lock(state_dir):
        prior_state = _read_nfl_incremental_state(
            _nfl_incremental_state_path(state_dir)
        )
        previous = _aggregate_nfl_incremental_state(prior_state)
        reconciled = _reconcile_nfl_signal_lifecycle(
            previous, decoded, now=now
        )
        payload = sanitize_completed_scan(
            "nfl", json.dumps(reconciled), generated_at=now
        )
        state = {
            "schema_version": NFL_INCREMENTAL_SCHEMA_VERSION,
            "reconciled_at": now.isoformat(),
            "baseline": reconciled,
            "updates": {},
        }
        # State goes first: interruption can delay a new feed, but cannot leave
        # stale merge state capable of resurrecting a reconciled-away BUY.
        _atomic_write(_nfl_incremental_state_path(state_dir), state)
        _atomic_write(destination, payload)
    return destination
