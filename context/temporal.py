from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple


_DATE_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_RELATIVE_TIME_RE = re.compile(
    r"\b(?:in|within|next)\s+(?:about\s+)?"
    r"(\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|couple|few|several)\s*"
    r"(hour|hours|day|days|week|weeks|month|months|year|years)\b"
)
_ALREADY_RE = re.compile(
    r"\b(?:already\s+(?:released|launched|happened|occurred|out|resolved)|has\s+already|"
    r"already\s+occurred|already\s+happened|already\s+launched|already\s+released)\b"
)
_FUTURE_CUES = ("release", "released", "launch", "launched", "expected", "scheduled", "before", "by", "will")
_GTA_VI_RE = re.compile(r"\b(?:gta\s*vi|gta\s*6|grand theft auto\s*vi|grand theft auto\s*6|before\s+gta\s+vi)\b", re.I)


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        s = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _fmt_dt(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _extract_first_datetime(market_snapshot: Dict[str, Any], fields: Tuple[str, ...]) -> Optional[datetime]:
    for field in fields:
        dt = _parse_dt(market_snapshot.get(field))
        if dt is not None:
            return dt
    return None


def _format_time_remaining(hours: Optional[float]) -> str:
    if hours is None:
        return "unknown"
    if hours < 0:
        return f"{abs(hours):.1f} hours overdue"
    if hours < 24:
        return f"{hours:.1f} hours"
    days = hours / 24.0
    if days < 7:
        return f"{days:.1f} days"
    if days < 30:
        return f"{days:.1f} days (~{days / 7.0:.1f} weeks)"
    return f"{days:.0f} days (~{days / 30.4:.1f} months)"


def _market_status(
    *,
    market_snapshot: Dict[str, Any],
    now: datetime,
    start_dt: Optional[datetime],
    end_dt: Optional[datetime],
    resolved_dt: Optional[datetime],
) -> str:
    active = market_snapshot.get("active")
    closed = bool(market_snapshot.get("closed"))
    archived = bool(market_snapshot.get("archived"))
    uma_status = str(market_snapshot.get("umaResolutionStatus") or "").strip().lower()

    if resolved_dt is not None or closed or archived or active is False or "resolved" in uma_status or "final" in uma_status:
        return "RESOLVED"
    if end_dt is None and start_dt is None:
        return "UNKNOWN"
    if end_dt is not None and now >= end_dt:
        return "ONGOING" if active else "UNKNOWN"
    if start_dt is not None and now < start_dt:
        return "UPCOMING"
    if end_dt is not None:
        return "ONGOING"
    if start_dt is not None:
        return "ONGOING" if now >= start_dt else "UPCOMING"
    return "UNKNOWN"


def is_gta_vi_market(question: str = "", slug: str = "") -> bool:
    return bool(_GTA_VI_RE.search(f"{question} {slug}"))


def build_temporal_context(
    market_snapshot: Dict[str, Any],
    *,
    question: str = "",
    slug: str = "",
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start_dt = _extract_first_datetime(
        market_snapshot,
        ("startDate", "start_date", "openDate", "openedAt", "createdAt", "created_at"),
    )
    end_dt = _extract_first_datetime(
        market_snapshot,
        ("endDate", "end_date", "resolutionDate", "resolution_date"),
    )
    resolved_dt = _extract_first_datetime(
        market_snapshot,
        ("resolvedAt", "resolved_at", "resolvedAtUtc", "resolved_at_utc"),
    )

    hours_remaining: Optional[float] = None
    if end_dt is not None:
        hours_remaining = (end_dt - now_dt).total_seconds() / 3600.0

    status = _market_status(
        market_snapshot=market_snapshot,
        now=now_dt,
        start_dt=start_dt,
        end_dt=end_dt,
        resolved_dt=resolved_dt,
    )

    sources = []
    if end_dt is not None:
        sources.append("endDate")
    if start_dt is not None:
        sources.append("startDate")
    if resolved_dt is not None:
        sources.append("resolvedAt")
    if not sources:
        sources.append("none")

    return {
        "current_utc": _fmt_dt(now_dt),
        "current_date": now_dt.date().isoformat(),
        "market_end_date": _fmt_dt(end_dt),
        "market_start_date": _fmt_dt(start_dt),
        "market_resolution_date": _fmt_dt(resolved_dt or end_dt),
        "time_remaining_hours": hours_remaining,
        "time_remaining": _format_time_remaining(hours_remaining) if hours_remaining is not None else "unknown",
        "event_status": status,
        "temporal_source": ",".join(sources),
        "requires_verified_temporal_context": is_gta_vi_market(question=question, slug=slug),
    }


def format_temporal_context_block(temporal_context: Dict[str, Any]) -> str:
    lines = [
        "Temporal context:",
        f"current_utc: {temporal_context.get('current_utc') or 'unknown'}",
        f"current_date_utc: {temporal_context.get('current_date') or 'unknown'}",
        f"market_end_date: {temporal_context.get('market_end_date') or 'unknown'}",
        f"market_resolution_date: {temporal_context.get('market_resolution_date') or 'unknown'}",
        f"time_remaining: {temporal_context.get('time_remaining') or 'unknown'}",
        f"event_status: {temporal_context.get('event_status') or 'UNKNOWN'}",
        f"temporal_source: {temporal_context.get('temporal_source') or 'none'}",
    ]
    if temporal_context.get("requires_verified_temporal_context"):
        lines.append("verified_context_required: true")
    lines.append(
        "instruction: verify every date and relative-time claim against current_utc; "
        "reject stale, impossible, or contradictory chronology."
    )
    return "\n".join(lines)


def _contains_stale_date_claim(text: str, current_year: int) -> bool:
    years = [int(y) for y in _DATE_YEAR_RE.findall(text)]
    if not years:
        return False
    if not any(cue in text for cue in _FUTURE_CUES):
        return False
    return any(year < current_year for year in years)


def _contains_already_claim(text: str) -> bool:
    return bool(_ALREADY_RE.search(text))


def _contains_relative_future_claim(text: str) -> bool:
    return bool(_RELATIVE_TIME_RE.search(text))


def validate_temporal_rationale(
    rationale: str,
    temporal_context: Dict[str, Any],
    *,
    question: str = "",
    now: Optional[datetime] = None,
) -> Tuple[bool, str, Dict[str, Any]]:
    text = (rationale or "").strip().lower()
    now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    current_year = now_dt.year
    end_dt = _parse_dt(temporal_context.get("market_end_date"))
    hours_remaining = temporal_context.get("time_remaining_hours")
    event_status = str(temporal_context.get("event_status") or "UNKNOWN").upper()
    requires_verified = bool(temporal_context.get("requires_verified_temporal_context"))

    details: Dict[str, Any] = {
        "event_status": event_status,
        "market_end_date": temporal_context.get("market_end_date"),
        "market_resolution_date": temporal_context.get("market_resolution_date"),
        "time_remaining": temporal_context.get("time_remaining"),
        "temporal_source": temporal_context.get("temporal_source"),
        "requires_verified_temporal_context": requires_verified,
    }

    if requires_verified and not temporal_context.get("market_end_date"):
        return (False, "missing_verified_temporal_context", details)

    if not text:
        return (True, "ok", details)

    if _contains_stale_date_claim(text, current_year):
        details["temporal_signal"] = "stale_date_claim"
        return (False, "stale_date_claim", details)

    if _contains_already_claim(text) and event_status in {"UPCOMING", "ONGOING", "UNKNOWN"}:
        details["temporal_signal"] = "already_claim"
        return (False, "contradictory_chronology", details)

    if _contains_relative_future_claim(text):
        details["temporal_signal"] = "relative_future_claim"
        if end_dt is not None and hours_remaining is not None and hours_remaining <= 0:
            return (False, "impossible_relative_time_claim", details)
        if event_status == "RESOLVED":
            return (False, "impossible_relative_time_claim", details)

    if end_dt is not None and now_dt > end_dt and event_status in {"UPCOMING", "ONGOING"} and _contains_relative_future_claim(text):
        details["temporal_signal"] = "future_claim_after_end"
        return (False, "impossible_relative_time_claim", details)

    return (True, "ok", details)
