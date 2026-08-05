from __future__ import annotations

from typing import Any, Mapping

from context.temporal import format_temporal_context_block


_CATEGORY_RULES: list[tuple[tuple[str, ...], str]] = [
    (("federal reserve", "fomc", "fed funds", " fed ", "rate cut", "rate cuts", "rate hike", "rate hikes"), "macro/fed"),
    (("recession", "inflation", "gdp", "cpi", "unemployment", "tariff"), "macro/econ"),
    (("election", "ballot", "referendum", "primary", "president", "congress", "senate", "nominee", "vote"), "politics"),
    (("supreme court", "scotus", "court", "verdict", "indictment", "lawsuit", "legal"), "legal"),
    (("bitcoin", " btc", "ethereum", " eth", "crypto", "solana"), "crypto"),
    (("ceasefire", "invasion", "invade", "nuclear", "conflict", "sanction", "coup", "regime"), "geopolitics"),
    (("nba", "nfl", "nhl", "mlb", "fifa", "world cup", "playoff", "super bowl", "match", "game"), "sports"),
]

_FAMILY_INSTRUCTIONS = {
    "macro/fed": "Use the scheduled FOMC calendar, policy reaction function, and current rate path. Distinguish emergency action from scheduled decisions.",
    "macro/econ": "Use release definitions, base rates, survey uncertainty, and revision risk. Match the exact statistic and measurement window.",
    "politics": "Use jurisdiction, ballot rules, polling base rates, candidate field, and procedural deadlines. Separate nomination from election outcomes.",
    "crypto": "Use current market structure, volatility, catalyst timing, and threshold path dependence. Do not treat narrative momentum as evidence.",
    "legal": "Use procedural posture, jurisdiction, remedy, and the exact decision required. Do not equate filing, acceptance, hearing, and final judgment.",
    "geopolitics": "Use observable capabilities, incentives, escalation base rates, and the contract's precise event definition. Discount unsourced breaking claims.",
    "sports": "Use competition format, schedule, injuries only if time-verified, and appropriate historical base rates. Avoid reputation-only forecasts.",
    "novelty/other": "First test whether the question is coherent, resolvable, and temporally anchored. Stay close to the market prior when evidence is weak.",
}

_FAILURE_MODES = {
    "macro/fed": "Common failures: confusing meeting dates, cuts with target ranges, and calendar-year counts.",
    "macro/econ": "Common failures: stale releases, wrong series, revisions, and annualized versus year-over-year rates.",
    "politics": "Common failures: outdated candidate status, wrong jurisdiction, and nomination/election confusion.",
    "crypto": "Common failures: stale spot prices, ignoring intraperiod touch rules, and extrapolating short-term momentum.",
    "legal": "Common failures: procedural-stage confusion, guessed court calendars, and overreading commentary.",
    "geopolitics": "Common failures: rumor reliance, ambiguous verbs, and ignoring resolution-source language.",
    "sports": "Common failures: stale rosters, wrong competition format, and ignoring conditional qualification paths.",
    "novelty/other": "Common failures: malformed premises, entertainment-driven explanations, guessed dates, and non-resolvable wording.",
}


def classify_market(question: str) -> str:
    text = f" {question.lower()} "
    if any(
        term in text
        for term in (
            "before gta vi",
            "before gta 6",
            " alien",
            " album",
            "rapture",
            "second coming",
            " meme",
        )
    ):
        return "novelty/other"
    for terms, category in _CATEGORY_RULES:
        if any(term in text for term in terms):
            return category
    return "novelty/other"


def prompt_family_for_category(category: str) -> str:
    return category if category in _FAMILY_INSTRUCTIONS else "novelty/other"


def build_forecast_prompts(
    *,
    question: str,
    venue: str,
    p_yes_market: float,
    market_snapshot: Mapping[str, Any],
    temporal_context: Mapping[str, Any],
    category: str,
) -> tuple[str, str]:
    family = prompt_family_for_category(category)
    system_prompt = (
        "You are a calibrated prediction-market forecaster. Estimate the true "
        "probability of YES while treating the crowd price as an informative prior. "
        "Return only JSON with p_yes, confidence, and a 1-2 sentence rationale. "
        "Never invent event dates or claim current facts that are not supplied."
    )
    temporal_block = format_temporal_context_block(dict(temporal_context))
    updated = market_snapshot.get("updatedAt") or "unknown"
    volume = market_snapshot.get("volume")
    liquidity = market_snapshot.get("liquidity")
    market_health = (
        f"Market health: volume={volume or 'unknown'}, "
        f"liquidity={liquidity or 'unknown'}"
    )
    user_prompt = "\n".join(
        [
            f"Prompt family: {family}",
            f"Question: {question}",
            f"Venue: {venue}",
            f"Market P(YES): {p_yes_market:.4f}",
            f"Snapshot updated: {updated}",
            market_health,
            temporal_block,
            f"Family instruction: {_FAMILY_INSTRUCTIONS[family]}",
            _FAILURE_MODES[family],
            "Temporal self-check: before answering, verify every date and relative-time claim against current_utc, the deadline, and time remaining.",
            "Only diverge materially from the market when the supplied facts and base rates justify it.",
            'Return: {"p_yes": 0.01-0.99, "confidence": 0.50-0.95, "rationale": "short"}',
        ]
    )
    return system_prompt, user_prompt
