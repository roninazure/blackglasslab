"""Sanitized, local-only exports of completed unattended sports scans."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse


SCHEMA_VERSION = "parallax.public.v1"
FREE_VISIBILITY_DELAY = timedelta(minutes=15)
STALE_AFTER = timedelta(minutes=90)
PUBLIC_ACTIONS = {"BUY", "WATCH", "PASS"}
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
            _first(row, "model_probability", "parallax_fair_value", "nfl_v1_probability")
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
        "resolution_time": _timestamp(_first(row, "resolution_time", "game_start")),
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
    if normalized_lane not in {"nfl", "mlb"}:
        raise ValueError("Public feed supports only NFL and MLB lanes")
    decoded = json.loads(completed_stdout)
    if not isinstance(decoded, Mapping):
        raise ValueError("Completed scan output must be a JSON object")
    now = (generated_at or datetime.now(UTC)).astimezone(UTC)
    rows = _source_rows(decoded, normalized_lane)
    plays = [play for row in rows if (play := _public_play(row, normalized_lane))]
    source_as_of = _source_as_of(decoded, rows)
    counts = {action: sum(play["action"] == action for play in plays) for action in PUBLIC_ACTIONS}
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "source_as_of": source_as_of,
        "lane": normalized_lane.upper(),
        "runtime_state": _runtime_state(decoded, source_as_of, now),
        "health_state": _health_state(decoded),
        "read_only": True,
        "summary": {
            "plays": len(plays),
            "buy": counts["BUY"],
            "watch": counts["WATCH"],
            "pass": counts["PASS"],
        },
        "plays": plays,
    }


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
