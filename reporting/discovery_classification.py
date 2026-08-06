"""Deterministic, source-backed discovery reporting classification."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

REPORTING_CLASSES = (
    "sports", "esports", "entertainment", "television", "movies", "music",
    "awards", "celebrity", "pop culture", "memes", "weather", "crypto",
    "politics", "economics/macro", "geopolitics", "technology",
    "unsupported market type", "unknown/other",
)

_SOURCE_FIELDS = (
    "category", "subcategory", "tags", "series", "seriesSlug", "series_id",
    "type", "marketType", "sportsMeta", "sport", "league", "gameStartTime",
)


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        if isinstance(value, Mapping):
            return {str(key): _json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [_json_safe(item) for item in value]
        return str(value)


def _values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        result: list[str] = []
        for key in ("label", "name", "slug", "category", "type", "sport", "league"):
            if value.get(key) is not None:
                result.extend(_values(value[key]))
        return result
    if isinstance(value, (list, tuple)):
        result: list[str] = []
        for item in value:
            result.extend(_values(item))
        return result
    return [str(value)]


def source_metadata(market: Mapping[str, Any]) -> dict[str, Any]:
    """Extract audit-safe source fields without using titles or slugs."""
    event = market.get("_discovery_event")
    event = dict(event) if isinstance(event, Mapping) else {}
    market_fields = {
        field: _json_safe(market[field])
        for field in _SOURCE_FIELDS
        if market.get(field) is not None
    }
    event_fields = {
        field: _json_safe(event[field])
        for field in _SOURCE_FIELDS
        if event.get(field) is not None
    }
    return {
        "event": event_fields,
        "market": market_fields,
        "event_id": event.get("id"),
        "event_slug": event.get("slug"),
        "event_title": event.get("title"),
        "event_category": event.get("category") or market.get("category"),
        "tags": event.get("tags") or market.get("tags") or [],
        "series": event.get("series") or market.get("series") or event.get("seriesSlug"),
        "source_type": event.get("type") or market.get("type") or market.get("marketType"),
    }


def _normalized_source_values(metadata: Mapping[str, Any]) -> set[str]:
    values: list[str] = []
    for key in ("event_category", "tags", "series", "source_type"):
        values.extend(_values(metadata.get(key)))
    event = metadata.get("event")
    market = metadata.get("market")
    if isinstance(event, Mapping):
        values.extend(_values(event.get("category")))
    if isinstance(market, Mapping):
        values.extend(_values(market.get("category")))
    return {
        re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
        for value in values
        if value.strip()
    }


def classify_reporting_class(
    metadata: Mapping[str, Any],
    *,
    policy_market_class: str | None = None,
    policy_category: str | None = None,
) -> tuple[str, str]:
    """Return ``(normalized_class, classification_source)``.

    Source metadata has priority. Existing policy classification is the only
    fallback. Contract question/title/slug text is never inspected here.
    """
    source = _normalized_source_values(metadata)
    has_source_metadata = bool(source)
    event = metadata.get("event")
    market = metadata.get("market")
    if (
        isinstance(market, Mapping) and market.get("gameStartTime")
    ) or (isinstance(event, Mapping) and event.get("gameStartTime")):
        return "sports", "source_metadata"
    ordered = (
        ("esports", {"esports", "e sports", "e sports market"}),
        ("sports", {"sports", "sport", "sports betting"}),
        ("entertainment", {"entertainment"}),
        ("television", {"television", "tv", "tv show"}),
        ("movies", {"movies", "movie", "film", "box office"}),
        ("music", {"music", "album", "spotify"}),
        ("awards", {"awards", "grammy", "oscar", "emmy"}),
        ("celebrity", {"celebrity"}),
        ("pop culture", {"pop culture", "popculture"}),
        ("memes", {"memes", "meme", "memecoin"}),
        ("weather", {"weather"}),
        ("crypto", {"crypto", "cryptocurrency"}),
        ("politics", {"politics", "political", "elections", "election"}),
        ("economics/macro", {"economics", "macro", "macroeconomics", "finance"}),
        ("geopolitics", {"geopolitics", "international relations"}),
        ("technology", {"technology", "tech", "science"}),
    )
    for category, aliases in ordered:
        if source.intersection(aliases):
            return category, "source_metadata"

    policy_map = {
        "sports_prop": "sports",
        "entertainment_celebrity": "entertainment",
        "novelty_meme": "memes",
        "product_release": "technology",
        "product_release_comparison": "technology",
        "thin_local_primary": "politics",
        "malformed_market": "unsupported market type",
    }
    if policy_market_class in policy_map:
        return policy_map[policy_market_class], "policy_classification"
    if policy_category in {"crypto", "politics", "geopolitics", "technology"}:
        return str(policy_category), "policy_classification"
    if policy_category and str(policy_category).startswith("macro/"):
        return "economics/macro", "policy_classification"
    if policy_market_class:
        return "unknown/other", "policy_classification"
    return "unknown/other", "source_metadata" if has_source_metadata else "unknown_metadata"
