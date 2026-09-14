"""Bounded, auditable market-universe discovery and classification.

This module deliberately does not qualify markets or produce probabilities.  It
keeps malformed rows visible as UNSUPPORTED and makes pagination limits part of
the returned result.
"""
from __future__ import annotations

import statistics
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable, Iterable


class Coverage(StrEnum):
    COMPLETE = "COMPLETE"
    BOUNDED = "BOUNDED"
    PARTIAL = "PARTIAL"


class Routing(StrEnum):
    SUPPORTED = "SUPPORTED"
    CANDIDATE = "CANDIDATE"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class CanonicalMarket:
    venue: str
    venue_market_id: str
    venue_event_id: str | None
    venue_series_id: str | None
    title: str
    subtitle: str
    category_raw: str
    tags_raw: tuple[str, ...]
    rules_text: str
    rules_reference: str | None
    yes_semantics: str
    no_semantics: str
    market_type: str
    scheduled_start: str | None
    close_time: str | None
    resolution_time: str | None
    status: str
    yes_price: float | None
    no_price: float | None
    best_bid: float | None
    best_ask: float | None
    liquidity: float | None
    depth: float | None
    volume: float | None
    observation_timestamp: str
    source_provenance: dict[str, Any] = field(default_factory=dict)
    classification: str = "OTHER_UNSUPPORTED"
    subcategory: str | None = None
    classification_reason: str = "no recognized venue metadata"
    routing: Routing = Routing.UNSUPPORTED
    routing_reason: str = "no validated provider"


@dataclass(frozen=True)
class CoverageReport:
    state: Coverage
    pages: int
    rows_returned: int
    unique_rows: int
    page_limit: int
    max_pages: int
    reason: str
    upstream_empty: bool = False


MAX_ACTIVE_MARKETS_PER_VENUE = 10_000


def _s(*values: Any) -> str:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(x.strip() for x in value.split(",") if x.strip())
    if isinstance(value, list):
        return tuple(_s(x.get("name") if isinstance(x, dict) else x) for x in value if _s(x.get("name") if isinstance(x, dict) else x))
    return ()


def _nested(raw: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        value: Any = raw
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if value not in (None, "", []):
            return value
    return None


def _num(value: Any) -> float | None:
    try:
        result = float(value)
        return result if result == result and result not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def _metadata(raw: dict[str, Any], event: dict[str, Any], series: dict[str, Any]) -> tuple[str, tuple[str, ...], str, str]:
    category = _s(raw.get("category"), raw.get("subcategory"), event.get("category"), series.get("category"), _nested(raw, ("sports", "category"), ("metadata", "category")))
    tags = _list(raw.get("tags")) + _list(event.get("tags")) + _list(series.get("tags")) + _list(_nested(raw, ("metadata", "tags"), ("sports", "tags")))
    sport = _s(raw.get("sport"), raw.get("league"), event.get("sport"), event.get("league"), series.get("sport"), series.get("league"), _nested(raw, ("sports", "sport"), ("sports", "league")))
    market_type = _s(raw.get("marketType"), raw.get("market_type"), raw.get("sportsMarketType"), raw.get("sportsMarketTypeV2"), event.get("market_type"), series.get("market_type"), _nested(raw, ("sports", "marketType"), ("sports", "moneyline", "marketType")))
    return category, tuple(dict.fromkeys(tags)), sport, market_type


def _ticker_sport(ticker: str) -> str:
    """Resolve only unambiguous Kalshi family prefixes."""
    value = ticker.upper()
    families = (("KXMLBGAME", "MLB"), ("KXMLB", "MLB"), ("KXNFL", "NFL"), ("KXNCAAF", "CFB"), ("KXCFB", "CFB"), ("KXNBA", "NBA"), ("KXLNB", "NBA"), ("KXNCAAB", "NCAAB"), ("KXNHL", "NHL"), ("KXELH", "NHL"), ("KXTENNIS", "TENNIS"), ("KXTT", "TENNIS"), ("KXATP", "TENNIS"), ("KXWTA", "TENNIS"), ("KXGOLF", "GOLF"), ("KXLIGA", "SOCCER"), ("KXEPL", "SOCCER"), ("KXLALIGA", "SOCCER"), ("KXSOCCER", "SOCCER"), ("KXUFC", "OTHER_SPORTS"), ("KXMMA", "OTHER_SPORTS"), ("KXT20", "OTHER_SPORTS"))
    return next((sport for prefix, sport in families if value.startswith(prefix)), "")


def normalize_market(raw: dict[str, Any], *, venue: str, event: dict[str, Any] | None = None, series: dict[str, Any] | None = None, observed_at: str | None = None) -> CanonicalMarket:
    """Normalize PMUS/Kalshi-ish public rows without dropping unknown fields."""
    if not isinstance(raw, dict):
        raw = {}
    event = event or {}
    series = series or {}
    category, tags, sport, market_type = _metadata(raw, event, series)
    sport = sport or _ticker_sport(_s(raw.get("ticker"), raw.get("id")))
    # PMUS sports metadata is often nested and has no useful top-level
    # category; retain the explicit league as the raw category signal.
    category = sport or category
    title = _s(raw.get("title"), raw.get("question"), raw.get("subtitle"), raw.get("ticker"))
    rules = _s(raw.get("rules_primary"), raw.get("rules_secondary"), raw.get("description"), raw.get("rules"))
    rid = _s(raw.get("id"), raw.get("ticker"), raw.get("slug"))
    status = _s(raw.get("status"), "OPEN" if raw.get("active") and not raw.get("closed") else "UNKNOWN").upper()
    route = Routing.CANDIDATE
    # Reuse the existing audited MLB semantic parser for routing only. This
    # does not alter MLB normalization, evidence, gates, or execution.
    mlb_validated = False
    if venue.upper() == "PMUS":
        try:
            from .normalization import _mlb_metadata
            mlb_validated = _mlb_metadata(raw, event=event) is not None
        except (ImportError, TypeError, AttributeError):
            mlb_validated = False
    if mlb_validated or (sport.upper() == "MLB" and ("moneyline" in market_type.lower() or "full_game_winner" in market_type.lower()) and ("baseball" in title.lower() or " vs " in title.lower() or "baseball" in rules.lower())):
        route = Routing.SUPPORTED
    if venue.upper() == "KALSHI" and _s(raw.get("ticker"), raw.get("id")).upper().startswith("KXMLBGAME"):
        route = Routing.SUPPORTED
    if not rid or not title or not rules:
        route = Routing.UNSUPPORTED
    return CanonicalMarket(
        venue=venue, venue_market_id=rid, venue_event_id=_s(raw.get("event_ticker"), raw.get("event_id"), raw.get("eventSlug"), event.get("id"), event.get("ticker")) or None,
        venue_series_id=_s(raw.get("series_ticker"), raw.get("series_id"), series.get("ticker"), event.get("series_ticker"), _ticker_sport(_s(raw.get("ticker"), raw.get("id")))) or None,
        title=title, subtitle=_s(raw.get("subtitle"), raw.get("question")), category_raw=category or "UNKNOWN", tags_raw=tags,
        rules_text=rules, rules_reference=_s(raw.get("url"), raw.get("slug"), raw.get("ticker")) or None,
        yes_semantics=_s(raw.get("yes_sub_title"), raw.get("yesSemantics"), "YES outcome"), no_semantics=_s(raw.get("no_sub_title"), raw.get("noSemantics"), "NO outcome"),
        market_type=market_type or _s(raw.get("market_type"), "binary"), scheduled_start=_s(raw.get("gameStartTime"), raw.get("open_time"), raw.get("scheduled_start")) or None,
        close_time=_s(raw.get("close_time"), raw.get("endDate"), raw.get("expiration_time"), raw.get("expected_expiration_time")) or None, resolution_time=_s(raw.get("resolution_time"), raw.get("expiration_time")) or None,
        status=status, yes_price=_num(raw.get("yes_price")), no_price=_num(raw.get("no_price")), best_bid=_num(raw.get("best_bid")), best_ask=_num(raw.get("best_ask")), liquidity=_num(raw.get("liquidity")), depth=_num(raw.get("depth")), volume=_num(raw.get("volume_24h_fp", raw.get("volume24hr", raw.get("volume")))),
        observation_timestamp=observed_at or datetime.now(timezone.utc).isoformat(), source_provenance={"venue": venue, "raw_keys": sorted(raw.keys()), "event_present": bool(event), "series_present": bool(series)},
        routing=route, routing_reason="validated MLB provider" if route is Routing.SUPPORTED else ("objective class with no validated provider" if route is Routing.CANDIDATE else "malformed or ambiguous market"),
    )


def classify_market(market: CanonicalMarket) -> CanonicalMarket:
    """Metadata-first deterministic taxonomy; title/rules are a documented fallback."""
    explicit = " ".join((market.category_raw, *market.tags_raw, market.venue_series_id or "", market.market_type)).lower()
    fallback = " ".join((market.title, market.subtitle, market.rules_text)).lower()
    # Evaluate explicit metadata first; only if it yields no match do the same
    # ordered rules against title/subtitle/rules fallback text.
    text = explicit
    groups = [
        ("SPORTS", {"mlb":"MLB", "baseball":"MLB", "nfl":"NFL", "football":"NFL", "cfb":"CFB", "college football":"CFB", "nba":"NBA", "basketball":"NBA", "ncaab":"NCAAB", "nhl":"NHL", "hockey":"NHL", "soccer":"SOCCER", "tennis":"TENNIS", "golf":"GOLF", "combat":"COMBAT", "other_sports":"OTHER_SPORTS"}),
        ("WEATHER_PHYSICAL", {"temperature":"temperature", "precipitation":"precipitation", "snow":"snowfall", "snowfall":"snowfall", "hurricane":"storm/hurricane", "storm":"storm/hurricane"}),
        ("MACRO_ECONOMICS", {"fed":"Fed/rates", "interest rate":"Fed/rates", "cpi":"CPI/inflation", "inflation":"CPI/inflation", "jobs":"jobs/unemployment", "unemployment":"jobs/unemployment", "gdp":"GDP", "recession":"recession"}),
        ("FINANCE", {"s&p":"index/asset thresholds", "nasdaq":"index/asset thresholds", "commodity":"commodities", "oil":"energy", "energy":"energy", "treasury":"rates/finance outcomes", "yield":"rates/finance outcomes"}),
        ("CRYPTO", {"bitcoin":"crypto", "btc":"crypto", "ethereum":"crypto", "crypto":"crypto"}),
        ("POLITICS_ELECTIONS", {"election":"politics/elections", "president":"politics/elections", "senate":"politics/elections", "governor":"politics/elections"}),
        ("TECH_AI", {"ai":"other measurable tech", "model":"model rankings", "benchmark":"benchmark outcomes", "launch":"launches/releases"}),
        ("ENTERTAINMENT_CULTURE", {"oscar":"entertainment/culture", "grammy":"entertainment/culture", "movie":"entertainment/culture", "music":"entertainment/culture"}),
        ("CORPORATE_BUSINESS", {"company":"corporate/business", "ceo":"corporate/business", "revenue":"corporate/business"}),
        ("GEOPOLITICS_NEWS", {"war":"geopolitics/news", "ukraine":"geopolitics/news", "news":"geopolitics/news"}),
    ]
    def has(text_value: str, needle: str) -> bool:
        return bool(re.search(r"\b" + re.escape(needle) + r"\b", text_value)) if needle.isalnum() else needle in text_value

    for category, labels in groups:
        for needle, subcategory in labels.items():
            if has(text, needle):
                reason = f"metadata match: {needle}" if needle in explicit else f"title/rules fallback: {needle}"
                return CanonicalMarket(**{**market.__dict__, "classification": category, "subcategory": subcategory, "classification_reason": reason, "routing": market.routing if category != "OTHER_UNSUPPORTED" else Routing.UNSUPPORTED})
    text = fallback
    for category, labels in groups:
        for needle, subcategory in labels.items():
            if has(text, needle):
                return CanonicalMarket(**{**market.__dict__, "classification": category, "subcategory": subcategory, "classification_reason": f"title/rules fallback: {needle}"})
    category = "MEASURED_DATA_OTHER" if market.category_raw.lower() not in {"sports", "sport"} and any(x in text for x in ("how many", "total", "measured", "data")) else "OTHER_UNSUPPORTED"
    return CanonicalMarket(**{**market.__dict__, "classification": category, "classification_reason": "fallback measured-data test" if category != "OTHER_UNSUPPORTED" else "no deterministic taxonomy match", "routing": market.routing if category != "OTHER_UNSUPPORTED" else Routing.UNSUPPORTED, "routing_reason": market.routing_reason if category != "OTHER_UNSUPPORTED" else "no defensible current forecasting route"})


def deduplicate(rows: Iterable[CanonicalMarket]) -> list[CanonicalMarket]:
    result: dict[tuple[str, str], CanonicalMarket] = {}
    for row in rows:
        if row.venue_market_id:
            result.setdefault((row.venue, row.venue_market_id), row)
    return list(result.values())


def paginate(fetch_page: Callable[..., Any], *, page_size: int = 100, max_pages: int = 10, cursor_mode: bool = False, max_rows: int = MAX_ACTIVE_MARKETS_PER_VENUE) -> tuple[list[dict[str, Any]], CoverageReport]:
    """Fetch a bounded deterministic window and expose truncation explicitly."""
    rows: list[dict[str, Any]] = []
    cursor = ""
    for page_number in range(max_pages):
        payload = fetch_page(limit=page_size, **({"cursor": cursor} if cursor_mode else {"offset": page_number * page_size}))
        page = payload.get("markets", payload) if isinstance(payload, dict) else payload
        if not isinstance(page, list):
            return rows, CoverageReport(Coverage.PARTIAL, page_number + 1, len(rows), len(deduplicate(normalize_market(x, venue="UNKNOWN") for x in rows)), page_size, max_pages, "upstream response omitted a market list")
        rows.extend(x for x in page if isinstance(x, dict))
        if len(rows) >= max_rows:
            return rows[:max_rows], CoverageReport(Coverage.BOUNDED, page_number + 1, len(rows), len(rows[:max_rows]), page_size, max_pages, f"hard safety ceiling {max_rows} reached")
        next_cursor = str(payload.get("cursor") or payload.get("next_cursor") or "") if isinstance(payload, dict) else ""
        if len(page) < page_size or (cursor_mode and not next_cursor):
            state = Coverage.COMPLETE
            reason = "upstream page ended before bounded limit"
            return rows, CoverageReport(state, page_number + 1, len(rows), len({str(x.get('id') or x.get('ticker') or x.get('slug')) for x in rows}), page_size, max_pages, reason, not rows)
        cursor = next_cursor
    return rows, CoverageReport(Coverage.BOUNDED, max_pages, len(rows), len({str(x.get('id') or x.get('ticker') or x.get('slug')) for x in rows}), page_size, max_pages, "bounded page limit reached; upstream may contain more", not rows)


def paginate_collection(fetch_page: Callable[..., Any], *, key: str, page_size: int = 100, max_pages: int = 100, max_rows: int = MAX_ACTIVE_MARKETS_PER_VENUE) -> tuple[list[dict[str, Any]], CoverageReport]:
    """Paginate arbitrary public scope collections with a hard row ceiling."""
    rows: list[dict[str, Any]] = []
    cursor = ""
    for page_number in range(max_pages):
        payload = fetch_page(limit=page_size, cursor=cursor)
        page = payload.get(key, []) if isinstance(payload, dict) else []
        if not isinstance(page, list):
            return rows, CoverageReport(Coverage.PARTIAL, page_number + 1, len(rows), len(rows), page_size, max_pages, f"upstream response omitted {key}")
        rows.extend(x for x in page if isinstance(x, dict))
        if len(rows) >= max_rows:
            return rows[:max_rows], CoverageReport(Coverage.BOUNDED, page_number + 1, len(rows), len(rows[:max_rows]), page_size, max_pages, f"hard safety ceiling {max_rows} reached")
        cursor = str(payload.get("cursor") or payload.get("next_cursor") or "")
        if len(page) < page_size or not cursor:
            return rows, CoverageReport(Coverage.COMPLETE, page_number + 1, len(rows), len(rows), page_size, max_pages, f"{key} scope ended upstream", not rows)
    return rows, CoverageReport(Coverage.BOUNDED, max_pages, len(rows), len(rows), page_size, max_pages, f"{key} pagination limit reached", not rows)


def discover_kalshi_scoped(
    series_rows: Iterable[dict[str, Any]],
    events_for_series: Callable[[str], Iterable[dict[str, Any]]],
    markets_for_event: Callable[[str], Iterable[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collect markets through series → event → market hierarchy.

    Callers provide already-paged API iterators, keeping this function usable
    with both the current client and deterministic test doubles.
    """
    rows: list[dict[str, Any]] = []
    series_count = event_count = 0
    failures: list[dict[str, str]] = []
    for series in series_rows:
        series_id = _s(series.get("ticker"), series.get("series_ticker"), series.get("id"))
        if not series_id:
            continue
        series_count += 1
        try:
            events = events_for_series(series_id)
            event_iter = iter(events)
        except Exception as exc:
            failures.append({"scope": series_id, "stage": "events", "error": type(exc).__name__})
            continue
        for event in event_iter:
            event_id = _s(event.get("ticker"), event.get("event_ticker"), event.get("id"))
            if not event_id:
                continue
            event_count += 1
            try:
                market_iter = iter(markets_for_event(event_id))
            except Exception as exc:
                failures.append({"scope": event_id, "stage": "markets", "error": type(exc).__name__})
                continue
            for market in market_iter:
                if isinstance(market, dict):
                    rows.append({**market, "_discovery_event": event, "_discovery_series": series})
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = _s(row.get("ticker"), row.get("id"))
        if key:
            unique.setdefault(key, row)
    return list(unique.values()), {"series_observed": series_count, "events_observed": event_count, "markets_returned": len(rows), "markets_unique": len(unique), "failed_scopes": failures, "coverage": Coverage.PARTIAL.value if failures else Coverage.BOUNDED.value}


def inventory(markets: Iterable[CanonicalMarket]) -> dict[str, Any]:
    rows = list(markets)
    groups: dict[tuple[str, str, str, str], list[CanonicalMarket]] = {}
    for row in rows:
        groups.setdefault((row.venue, row.classification, row.subcategory or "", row.routing.value), []).append(row)
    result = []
    for (venue, category, subcategory, routing), values in sorted(groups.items()):
        volumes = [x.volume for x in values if x.volume is not None]
        liquidity = [x.liquidity for x in values if x.liquidity is not None]
        result.append({"venue": venue, "category": category, "subcategory": subcategory or None, "routing": routing, "count": len(values), "aggregate_volume": sum(volumes) if volumes else None, "median_volume": statistics.median(volumes) if volumes else None, "median_liquidity": statistics.median(liquidity) if liquidity else None, "examples": [{"id": x.venue_market_id, "title": x.title} for x in values[:3]]})
    return {"market_count": len(rows), "groups": result, "routing_counts": {r.value: sum(x.routing is r for x in rows) for r in Routing}}
