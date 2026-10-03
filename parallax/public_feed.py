"""Sanitized, local-only exports of completed unattended sports scans."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .nfl import NFL_TEAM_NAMES


SCHEMA_VERSION = "parallax.public.v1"
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
    for field in ("orders", "alerts", "published"):
        value = payload.get(field)
        if isinstance(value, bool) or value != 0:
            raise ValueError(f"CFB scan output must attest {field}=0")
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
                        game_public["status"] = "WITHHELD_INCOMPLETE_DATA"
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


def _nfl_buy_source_eligible(row: Mapping[str, Any], now: datetime) -> bool:
    """Cross-check an NFL scanner's per-position publication attestation."""
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
    if lane == "nfl" and action == "BUY" and row.get("publication_eligible") is True:
        public["publication_eligible"] = True

    # Defense in depth: the returned keys are fixed even if this function changes.
    allowed = {"signal_id", *PUBLIC_PLAY_FIELDS}
    return {key: value for key, value in public.items() if key in allowed}


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
    allow_buy = (
        market_data_complete is True
        if normalized_lane == "mlb"
        else bool(nfl_buy_indexes)
        if normalized_lane == "nfl"
        else False
    )
    plays = [
        play
        for index, row in enumerate(rows)
        if (play := _public_play(row, normalized_lane))
        and (
            play["action"] != "BUY"
            or (normalized_lane == "mlb" and allow_buy)
            or (normalized_lane == "nfl" and index in nfl_buy_indexes)
        )
    ]
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
    slate = _public_slate(
        decoded.get("slate"),
        allow_buy=allow_buy,
        eligible_nfl_game_ids=eligible_nfl_game_ids,
    )
    if slate is not None:
        result["slate"] = slate
    venues = _public_venues(
        decoded.get("venues"),
        allow_buy=normalized_lane == "mlb" and allow_buy,
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


def export_completed_scan(lane: str, completed_stdout: str, state_dir: Path) -> Path:
    """Sanitize one already-completed scan and atomically publish it locally."""
    normalized_lane = lane.casefold()
    payload = sanitize_completed_scan(normalized_lane, completed_stdout)
    destination = Path(state_dir) / "public_feed" / f"{normalized_lane}.json"
    _atomic_write(destination, payload)
    return destination
