"""Conservative discovery and exact matching for non-sports binary events.

This module discovers contracts; it never forecasts, qualifies, captures, or
publishes them.  Exactness is decided from structured resolution attributes,
not title similarity.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .generic_events import EventFamily, normalize_binary_event
from .models import NormalizedMarket, Venue, utcnow


class DiscoveryStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    UNSUPPORTED = "UNSUPPORTED"


class MatchStatus(StrEnum):
    EXACT = "EXACT"
    MISMATCH = "MISMATCH"
    AMBIGUOUS = "AMBIGUOUS"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class EventIdentity:
    event_family: EventFamily
    action: str | None
    subject: str | None
    actor: str | None
    body: str | None
    stage: str | None
    outcome: str | None
    deadline: str | None
    temporal_scope: str | None
    threshold: str | None
    resolution_authority: str | None

    def fingerprint_fields(self) -> dict[str, str | None]:
        return {
            "event_family": self.event_family.value,
            "action": self.action,
            "subject": self.subject,
            "actor": self.actor,
            "body": self.body,
            "stage": self.stage,
            "outcome": self.outcome,
            "deadline": self.deadline,
            "temporal_scope": self.temporal_scope,
            "threshold": self.threshold,
            "resolution_authority": self.resolution_authority,
        }

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.fingerprint_fields(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class EventCandidate:
    venue: Venue
    market_id: str
    slug: str | None
    event_title: str
    contract_question: str
    outcomes: dict[str, str]
    executable_prices: dict[str, float | None]
    liquidity: float | None
    open_time: str | None
    close_time: str | None
    resolution_time: str | None
    source_timestamp: str | None
    resolution_text: str
    source_reference: str | None
    event_family: EventFamily
    identity: EventIdentity
    ambiguity_flags: tuple[str, ...]
    derivative_flags: tuple[str, ...]
    discovery_timestamp: str
    raw_provenance: dict[str, Any] = field(repr=False)

    @property
    def event_id(self) -> str:
        return f"generic-{self.identity.fingerprint[:24]}"


@dataclass(frozen=True)
class DiscoveryDecision:
    status: DiscoveryStatus
    reasons: tuple[str, ...]
    candidate: EventCandidate | None = None


@dataclass(frozen=True)
class MatchResult:
    status: MatchStatus
    reasons: tuple[str, ...]
    compared_dimensions: tuple[str, ...] = ()


_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7,
    "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_SPORT_TERMS = re.compile(
    r"\b(sports?|mlb|nfl|nba|nhl|ncaaf|ncaab|cfb|baseball|basketball|football|"
    r"hockey|soccer|tennis|golf|boxing|ufc|mma|touchdown|world series|super bowl|"
    r"most valuable player|\bmvp\b)\b", re.IGNORECASE
)
_SPORT_TICKER = re.compile(
    r"^KX(?:MLB|NFL|NBA|NHL|NCAA|CFB|TENNIS|ATP|WTA|GOLF|UFC|MMA|EPL|SOCCER|T20)",
    re.IGNORECASE,
)
_DERIVATIVE_TERMS = (
    (re.compile(r"\bhow many\b|\bnumber of (?:votes|seats|points)\b|\bvote count\b", re.IGNORECASE), "count_contract"),
    (re.compile(r"\bspread\b|\bmargin of victory\b|\bexactly \d+\b", re.IGNORECASE), "derivative_contract"),
    (re.compile(r"\bprice (?:above|below)\b|\btrading above\b|\bmarket cap\b", re.IGNORECASE), "financial_derivative"),
)


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, Mapping):
        value = value.get("value")
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _canon(value: Any) -> str | None:
    text = re.sub(r"[^a-z0-9]+", "_", _text(value).casefold()).strip("_")
    return text or None


def _semantic_value(dimension: str, value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    if dimension == "subject":
        hr = re.search(r"\bh\s*\.?\s*r\s*\.?\s*(\d+)\b", text, re.IGNORECASE)
        if hr:
            return f"hr:{int(hr.group(1))}"
    if dimension == "deadline":
        return _date(text) or text
    if dimension == "threshold":
        votes = re.search(r"\b(\d{1,3})\s+votes?\b", text, re.IGNORECASE)
        if votes:
            return f"votes:{int(votes.group(1))}"
    return _canon(text)


def _raw_row(raw: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    source = dict(raw)
    nested = source.get("raw")
    return source, dict(nested) if isinstance(nested, Mapping) else source


def _all_text(row: Mapping[str, Any], event: Mapping[str, Any]) -> str:
    values = (
        _first(row, "title", "question", "subtitle"), row.get("description"),
        row.get("rules_primary"), row.get("rules_secondary"), row.get("category"),
        event.get("title"), event.get("category"), row.get("series_ticker"),
    )
    return " ".join(_text(value) for value in values if _text(value))


def is_sports_market(raw: Mapping[str, Any], *, event: Mapping[str, Any] | None = None) -> bool:
    source, row = _raw_row(raw)
    parent = dict(event or source.get("_discovery_event") or {})
    explicit = " ".join(
        _text(value) for value in (
            row.get("sport"), row.get("league"), row.get("category"),
            parent.get("sport"), parent.get("league"), parent.get("category"),
            row.get("series_ticker"), row.get("ticker"),
        ) if _text(value)
    )
    ticker = _text(row.get("ticker") or row.get("series_ticker"))
    side_leagues = " ".join(
        _text(side.get("team", {}).get("league"))
        for side in row.get("marketSides", [])
        if isinstance(side, Mapping) and isinstance(side.get("team"), Mapping)
    )
    if _SPORT_TERMS.search(f"{explicit} {side_leagues}") or _SPORT_TICKER.search(ticker):
        return True
    return bool(_SPORT_TERMS.search(_all_text(row, parent)))


def classify_event_family(raw: Mapping[str, Any], *, event: Mapping[str, Any] | None = None) -> EventFamily:
    source, row = _raw_row(raw)
    parent = dict(event or source.get("_discovery_event") or {})
    text = _all_text(row, parent).casefold()
    explicit = " ".join(
        _text(value).casefold() for value in (
            row.get("category"), row.get("marketType"), row.get("market_type"),
            parent.get("category"), parent.get("market_type"),
        ) if _text(value)
    )
    if re.search(r"\b(election|electoral|primary)\b", explicit):
        return EventFamily.ELECTION_POLITICAL
    rules: tuple[tuple[EventFamily, str], ...] = (
        (EventFamily.ELECTION_POLITICAL, r"\b(election|electoral|primary|nominee|president|governor|mayor|win the senate|win the house)\b"),
        (EventFamily.LEGISLATIVE_REGULATORY, r"\b(cloture|motion to proceed|bill|h\.?\s*r\.?\s*\d+|legislation|regulation|regulatory|become law|signed into law|congress)\b"),
        (EventFamily.MACRO_MONETARY, r"\b(federal reserve|fed funds|interest rate|cpi|inflation|gdp|unemployment|payrolls|recession|monetary policy)\b"),
        (EventFamily.LEGAL_JUDICIAL, r"\b(supreme court|court of appeals|district court|judge|jury|verdict|convicted|indicted|lawsuit|injunction|ruling)\b"),
        (EventFamily.CORPORATE, r"\b(earnings|revenue|ceo|company|acquire|acquisition|merger|bankruptcy|ipo|board of directors)\b"),
        (EventFamily.GEOPOLITICAL, r"\b(ceasefire|invasion|war|treaty|sanction|diplomatic|territory|border|nato|united nations)\b"),
    )
    for family, pattern in rules:
        if re.search(pattern, text, re.IGNORECASE):
            return family
    return EventFamily.OTHER_BINARY_EVENT


def _outcomes(row: Mapping[str, Any], venue: Venue) -> dict[str, str] | None:
    if venue is Venue.POLYMARKET:
        sides = row.get("marketSides")
        if not isinstance(sides, list):
            return None
        result = {
            "YES" if side.get("long") is True else "NO": _text(side.get("description"))
            for side in sides
            if isinstance(side, Mapping) and side.get("long") in (True, False)
        }
    else:
        market_type = _text(row.get("market_type") or row.get("marketType")).casefold()
        if any(term in market_type for term in ("multi", "scalar", "range", "count", "mve")):
            return None
        result = {
            "YES": _text(row.get("yes_sub_title") or row.get("yes_title") or "YES"),
            "NO": _text(row.get("no_sub_title") or row.get("no_title") or "NO"),
        }
    if len(result) != 2 or any(not value for value in result.values()):
        return None
    if len({value.casefold() for value in result.values()}) != 2:
        return None
    return result


def _date(text: str) -> str | None:
    iso = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", text)
    if iso:
        year, month, day = map(int, iso.groups())
        try:
            return datetime(year, month, day, tzinfo=UTC).date().isoformat()
        except ValueError:
            return None
    names = "|".join(sorted(_MONTHS, key=len, reverse=True))
    named = re.search(rf"\b({names})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(20\d{{2}})\b", text, re.IGNORECASE)
    if named:
        month, day, year = named.groups()
        try:
            return datetime(int(year), _MONTHS[month.casefold()], int(day), tzinfo=UTC).date().isoformat()
        except ValueError:
            return None
    return None


def _extract_one(text: str, family: EventFamily) -> dict[str, str | None]:
    lower = text.casefold()
    action = stage = outcome = None
    if re.search(r"\binvoke(?:s|d)? cloture\b|\bcloture\b", lower):
        action, stage, outcome = "invoke_cloture", "cloture", "succeeds"
    elif re.search(r"\b(?:signed into law|become law|becomes law|enacted)\b", lower):
        action, stage, outcome = "enact", "enactment", "succeeds"
    elif re.search(r"\b(?:final passage|pass(?:es|ed)?)\b", lower):
        action, stage, outcome = "pass", "final_passage", "succeeds"
    elif re.search(r"\b(?:vote occurs|hold(?:s)? a vote|vote on)\b", lower):
        action, stage, outcome = "hold_vote", "vote_occurrence", "occurs"
    elif re.search(r"\bsenator\s+[a-z][a-z .'-]+\s+vote(?:s)?\s+(?:for|against)\b", lower):
        action, stage = "individual_vote", "member_vote"
        outcome = "supports" if re.search(r"\bvotes?\s+for\b", lower) else "opposes"
    elif family is EventFamily.MACRO_MONETARY and re.search(r"\b(?:raise|increase|cut|lower|hold)\b", lower):
        action, outcome = "set_rate", "occurs"
    elif family is EventFamily.ELECTION_POLITICAL and re.search(r"\b(?:win|wins|elected)\b", lower):
        action, outcome = "win_election", "succeeds"
    elif family is EventFamily.LEGAL_JUDICIAL and re.search(r"\b(?:rule|rules|ruling|verdict|convicted)\b", lower):
        action, outcome = "judicial_decision", "occurs"
    elif family is EventFamily.CORPORATE and re.search(r"\b(?:acquire|acquisition|merger)\b", lower):
        action, outcome = "corporate_transaction", "completes"
    elif family is EventFamily.GEOPOLITICAL and "ceasefire" in lower:
        action, outcome = "ceasefire", "occurs"

    hr = re.search(r"\bh\s*\.?\s*r\s*\.?\s*(\d+)\b", lower)
    act = re.search(r"\b([a-z][a-z0-9 '-]{2,50}\s+act)\b", lower)
    if hr:
        subject = f"hr:{int(hr.group(1))}"
    elif act:
        subject = _canon(act.group(1))
    else:
        subject = None
    body = "senate" if re.search(r"\b(?:u\.s\.\s+)?senate\b", lower) else "house" if re.search(r"\b(?:u\.s\.\s+)?house\b", lower) else None
    senator = re.search(r"\bsenator\s+([a-z][a-z .'-]{1,60}?)(?=\s+vote|\s+will|[,?.])", lower)
    actor = f"senator:{_canon(senator.group(1))}" if senator else body
    if "motion to proceed" in lower:
        stage = "motion_to_proceed_cloture" if action == "invoke_cloture" else "motion_to_proceed"
    threshold_match = re.search(r"\b(\d{1,3})\s+votes?\b", lower)
    threshold = f"votes:{int(threshold_match.group(1))}" if threshold_match else ("majority" if "majority" in lower else None)
    if re.search(r"official (?:u\.s\.\s+)?senate roll call|senate\.gov", lower):
        authority = "us_senate_roll_call"
    elif re.search(r"official (?:u\.s\.\s+)?house roll call|house\.gov", lower):
        authority = "us_house_roll_call"
    elif "federal reserve" in lower or "federalreserve.gov" in lower:
        authority = "federal_reserve"
    elif re.search(r"official court (?:order|docket)|supremecourt\.gov", lower):
        authority = "official_court_record"
    elif "official company" in lower or "investor relations" in lower:
        authority = "official_company_record"
    else:
        authority = None
    temporal_scope = None
    if _date(lower):
        if re.search(r"\bbefore\b", lower):
            temporal_scope = "before"
        elif re.search(r"\bby\b|\bon or before\b", lower):
            temporal_scope = "through"
        elif re.search(r"\bon\b", lower):
            temporal_scope = "on"
    return {
        "action": action, "subject": subject, "actor": actor, "body": body,
        "stage": stage, "outcome": outcome, "deadline": _date(lower),
        "temporal_scope": temporal_scope, "threshold": threshold,
        "resolution_authority": authority,
    }


def extract_event_identity(
    question: str,
    resolution_text: str,
    event_family: EventFamily,
    *,
    explicit: Mapping[str, Any] | None = None,
) -> tuple[EventIdentity, tuple[str, ...]]:
    """Extract only bounded, auditable attributes and report uncertainty."""
    q_fields = _extract_one(question, event_family)
    r_fields = _extract_one(resolution_text, event_family)
    supplied = dict(explicit or {})
    flags: list[str] = []
    values: dict[str, str | None] = {}
    for key in q_fields:
        explicit_value = _semantic_value(key, supplied.get(key))
        q_value, r_value = q_fields[key], r_fields[key]
        if q_value and r_value and q_value != r_value:
            flags.append(f"conflicting_{key}")
        values[key] = explicit_value or r_value or q_value
    if not resolution_text.strip() or not any(r_fields.values()):
        flags.append("incomplete_resolution_semantics")
    for key in ("action", "subject", "outcome", "deadline", "temporal_scope", "resolution_authority"):
        if not values[key]:
            flags.append(f"missing_{key}")
    identity = EventIdentity(event_family=event_family, **values)
    return identity, tuple(dict.fromkeys(flags))


def discover_event_candidate(
    raw: Mapping[str, Any],
    *,
    venue: Venue,
    event: Mapping[str, Any] | None = None,
    book: Mapping[str, Any] | None = None,
    discovered_at: datetime | None = None,
) -> DiscoveryDecision:
    """Turn one public inventory row into a non-sports binary candidate."""
    if not isinstance(raw, Mapping):
        return DiscoveryDecision(DiscoveryStatus.UNSUPPORTED, ("malformed_market_record",))
    source, row = _raw_row(raw)
    parent = dict(event or source.get("_discovery_event") or {})
    if is_sports_market(source, event=parent):
        return DiscoveryDecision(DiscoveryStatus.UNSUPPORTED, ("sports_market_excluded",))
    market_id = _text(_first(row, "id", "ticker", "slug") or _first(source, "id", "ticker", "slug"))
    question = _text(_first(row, "question", "title", "subtitle"))
    rules = _text(_first(row, "rules_primary", "rules_secondary", "description", "rules"))
    if not market_id or not question or not rules:
        return DiscoveryDecision(DiscoveryStatus.UNSUPPORTED, ("missing_market_id_question_or_rules",))
    outcomes = _outcomes(row, venue)
    if outcomes is None:
        return DiscoveryDecision(DiscoveryStatus.UNSUPPORTED, ("not_explicitly_binary",))
    combined = f"{question}\n{rules}"
    derivatives = tuple(reason for pattern, reason in _DERIVATIVE_TERMS if pattern.search(combined))
    if derivatives:
        return DiscoveryDecision(DiscoveryStatus.UNSUPPORTED, derivatives)
    status = _text(row.get("status") or ("OPEN" if row.get("active") and not row.get("closed") else "UNKNOWN")).upper()
    if row.get("closed") is True or status in {"CLOSED", "SETTLED", "RESOLVED", "FINALIZED"}:
        return DiscoveryDecision(DiscoveryStatus.UNSUPPORTED, ("stale_or_closed_market",))
    family = classify_event_family(source, event=parent)
    explicit = row.get("semantic_identity") if isinstance(row.get("semantic_identity"), Mapping) else None
    identity, ambiguity = extract_event_identity(question, rules, family, explicit=explicit)
    if outcomes["YES"].casefold().startswith("no") or outcomes["NO"].casefold().startswith("yes"):
        ambiguity = (*ambiguity, "reversed_binary_semantics")

    book = dict(book or {})
    slug = _text(_first(source, "slug") or _first(row, "slug") or market_id)
    if venue is Venue.POLYMARKET:
        yes_book = book.get(f"{slug}::YES", {})
        no_book = book.get(f"{slug}::NO", {})
        yes_price = _number(yes_book.get("best_ask")) if isinstance(yes_book, Mapping) else None
        no_price = _number(no_book.get("best_ask")) if isinstance(no_book, Mapping) else None
    else:
        fp = book.get("orderbook_fp") if isinstance(book.get("orderbook_fp"), Mapping) else {}
        legacy = book.get("orderbook") if isinstance(book.get("orderbook"), Mapping) else {}
        yes_rows = fp.get("yes_dollars", []) or legacy.get("yes", [])
        no_rows = fp.get("no_dollars", []) or legacy.get("no", [])
        yes_bid = _number(yes_rows[0][0]) if yes_rows else None
        no_bid = _number(no_rows[0][0]) if no_rows else None
        if not fp:
            yes_bid = yes_bid / 100 if yes_bid is not None else None
            no_bid = no_bid / 100 if no_bid is not None else None
        yes_price = 1 - no_bid if no_bid is not None else None
        no_price = 1 - yes_bid if yes_bid is not None else None
    direct_yes = _number(_first(row, "yes_ask", "yes_price"))
    direct_no = _number(_first(row, "no_ask", "no_price"))
    yes_price = yes_price if yes_price is not None else direct_yes
    no_price = no_price if no_price is not None else direct_no
    for price in (yes_price, no_price):
        if price is not None and not 0 <= price <= 1:
            return DiscoveryDecision(DiscoveryStatus.UNSUPPORTED, ("malformed_price",))
    now = (discovered_at or utcnow()).astimezone(UTC).isoformat()
    candidate = EventCandidate(
        venue=venue, market_id=market_id, slug=slug or None,
        event_title=_text(_first(parent, "title", "name") or question),
        contract_question=question, outcomes=outcomes,
        executable_prices={"YES": yes_price, "NO": no_price},
        liquidity=_number(
            _first(row, "liquidity", "liquidity_dollars")
            if _first(row, "liquidity", "liquidity_dollars") is not None
            else _first(source, "liquidity", "liquidity_dollars")
        ),
        open_time=_text(_first(row, "open_time", "startDate", "scheduled_start")) or None,
        close_time=_text(_first(row, "close_time", "endDate", "expiration_time")) or None,
        resolution_time=_text(_first(row, "resolution_time", "expected_expiration_time", "expiration_time")) or None,
        source_timestamp=_text(
            _first(row, "updated_at", "updatedAt", "last_updated", "transact_time")
            or _first(source, "updated_at", "updatedAt", "last_updated", "transact_time")
            or book.get("transact_time")
        ) or None,
        resolution_text=rules,
        source_reference=_text(_first(row, "url", "rules_url") or _first(source, "url") or _first(row, "slug", "ticker")) or None,
        event_family=family, identity=identity, ambiguity_flags=ambiguity,
        derivative_flags=(), discovery_timestamp=now,
        raw_provenance={
            "market": dict(row), "event": parent, "inventory_wrapper": source,
            "book": dict(book),
        },
    )
    return DiscoveryDecision(DiscoveryStatus.CANDIDATE, (), candidate)


_IDENTITY_DIMENSIONS = (
    "event_family", "action", "subject", "actor", "body", "stage", "outcome",
    "deadline", "temporal_scope", "threshold", "resolution_authority",
)
_REQUIRED_EXACT = (
    "action", "subject", "outcome", "deadline", "temporal_scope",
    "resolution_authority",
)
_CONDITIONAL_EXACT = ("actor", "body", "stage", "threshold")


def match_event_contract(target: EventCandidate, contract: EventCandidate) -> MatchResult:
    """Prove exact identity or fail closed with dimension-specific reasons."""
    if target.derivative_flags or contract.derivative_flags:
        return MatchResult(MatchStatus.UNSUPPORTED, ("derivative_contract",))
    if target.ambiguity_flags or contract.ambiguity_flags:
        conflicting = tuple(flag for flag in (*target.ambiguity_flags, *contract.ambiguity_flags) if flag.startswith("conflicting_") or flag == "reversed_binary_semantics")
        if conflicting:
            return MatchResult(MatchStatus.AMBIGUOUS, conflicting)
    compared: list[str] = []
    mismatches: list[str] = []
    left = target.identity.fingerprint_fields()
    right = contract.identity.fingerprint_fields()
    for dimension in _IDENTITY_DIMENSIONS:
        a, b = left[dimension], right[dimension]
        if a is not None and b is not None:
            compared.append(dimension)
            if a != b:
                mismatches.append(f"{dimension}_mismatch:{a}!={b}")
    if mismatches:
        return MatchResult(MatchStatus.MISMATCH, tuple(mismatches), tuple(compared))
    missing = tuple(
        dimension for dimension in _REQUIRED_EXACT
        if left[dimension] is None or right[dimension] is None
    )
    unproven = tuple(
        dimension for dimension in _CONDITIONAL_EXACT
        if (left[dimension] is None) != (right[dimension] is None)
    )
    uncertainty = tuple(dict.fromkeys((*target.ambiguity_flags, *contract.ambiguity_flags)))
    if missing or unproven or uncertainty:
        reasons = tuple(f"missing_exact_dimension:{item}" for item in missing) + uncertainty
        reasons += tuple(f"unproven_exact_dimension:{item}" for item in unproven)
        return MatchResult(MatchStatus.AMBIGUOUS, reasons, tuple(compared))
    return MatchResult(MatchStatus.EXACT, ("all required semantic dimensions match",), tuple(compared))


def normalized_for_exact_match(candidate: EventCandidate, match: MatchResult) -> NormalizedMarket:
    """Adapt only a proven exact candidate to the existing forecast binding seam."""
    if match.status is not MatchStatus.EXACT:
        raise ValueError(f"Cannot normalize non-exact candidate: {match.status.value}")
    raw = dict(candidate.raw_provenance["market"])
    raw.setdefault("id", candidate.market_id)
    raw.setdefault("question", candidate.contract_question)
    raw.setdefault("description", candidate.resolution_text)
    if candidate.venue is Venue.POLYMARKET:
        raw.setdefault("marketSides", [
            {"long": True, "description": candidate.outcomes["YES"]},
            {"long": False, "description": candidate.outcomes["NO"]},
        ])
    reference = candidate.source_reference or f"inventory://{candidate.venue.value}/{candidate.market_id}"
    return normalize_binary_event(
        raw,
        venue=candidate.venue,
        event_id=candidate.event_id,
        event_family=candidate.event_family.value,
        event_question=candidate.contract_question,
        resolution_reference=reference,
        event=candidate.raw_provenance.get("event"),
        observed_at=candidate.discovery_timestamp,
    )


__all__ = [
    "DiscoveryDecision", "DiscoveryStatus", "EventCandidate", "EventIdentity",
    "MatchResult", "MatchStatus", "classify_event_family", "discover_event_candidate",
    "extract_event_identity", "is_sports_market", "match_event_contract",
    "normalized_for_exact_match",
]
