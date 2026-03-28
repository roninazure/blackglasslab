"""
BlackGlassLab — enriched market context builder (Phase 3.5)

Builds a rich context block for Claude before each forecast call.
Adds: time-to-resolution, crypto prices, category hints, market health.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from context.crypto import get_crypto_context


# ---------------------------------------------------------------------------
# Market category detection
# ---------------------------------------------------------------------------

_CATEGORY_KEYWORDS = {
    "crypto":      ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol", "token"],
    "sports_nba":  ["nba", "playoffs", "finals", "lakers", "celtics", "bucks", "warriors",
                    "nuggets", "grizzlies", "bulls", "knicks", "heat", "76ers"],
    "sports_golf": ["masters", "pga", "golf", "open championship", "mcilroy", "koepka",
                    "scheffler", "dechambeau"],
    "geopolitical":["ukraine", "russia", "nato", "ceasefire", "war", "invasion", "taiwan",
                    "china", "netanyahu", "israel", "gaza"],
    "politics_us": ["trump", "biden", "democrat", "republican", "president", "congress",
                    "senate", "election", "2028", "nomination"],
    "macro":       ["fed", "federal reserve", "rate cut", "interest rate", "recession",
                    "gdp", "inflation", "fomc"],
}


def _detect_category(question: str) -> Optional[str]:
    q = question.lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            return category
    return None


# ---------------------------------------------------------------------------
# Time-to-resolution helpers
# ---------------------------------------------------------------------------

def _hours_remaining(end_date: Any) -> Optional[float]:
    if not end_date:
        return None
    try:
        s = str(end_date).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        now = datetime.now(timezone.utc)
        return max(0.0, (dt - now).total_seconds() / 3600.0)
    except Exception:
        return None


def _time_context(hours: Optional[float]) -> str:
    if hours is None:
        return ""
    if hours < 24:
        return f"⚠️  Resolves in {hours:.1f} hours — imminent."
    days = hours / 24.0
    if days < 3:
        return f"⚠️  Resolves in {days:.1f} days — very soon."
    if days < 7:
        return f"Resolves in {days:.1f} days."
    if days < 30:
        return f"Resolves in {days:.0f} days (~{days/7:.1f} weeks)."
    months = days / 30.4
    return f"Resolves in ~{months:.1f} months ({days:.0f} days)."


# ---------------------------------------------------------------------------
# Category-specific context hints
# ---------------------------------------------------------------------------

_SPORTS_NBA_NOTE = (
    "NOTE: My training data has a cutoff. For current NBA standings and "
    "playoff picture, weight the market price heavily — it reflects live data. "
    "Only diverge if you have strong fundamental reasoning."
)

_SPORTS_GOLF_NOTE = (
    "NOTE: Current golf tournament standings may be outside my training data. "
    "Weight the market price heavily for in-progress tournaments."
)

_GEO_NOTE = (
    "Consider: ceasefire/conflict markets are highly uncertain and sensitive "
    "to breaking news. The market price aggregates live intelligence. "
    "Diverge only with strong structural reasoning."
)

_MACRO_NOTE = (
    "Fed policy markets: weight CME FedWatch implied probabilities. "
    "The crowd price on Polymarket typically tracks those closely."
)

_CATEGORY_NOTES = {
    "sports_nba":   _SPORTS_NBA_NOTE,
    "sports_golf":  _SPORTS_GOLF_NOTE,
    "geopolitical": _GEO_NOTE,
    "macro":        _MACRO_NOTE,
}


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build_context_block(
    question: str,
    market_snapshot: Dict[str, Any],
    p_yes_market: float,
    venue: str = "polymarket",
) -> str:
    """
    Build an enriched context block string to prepend to Claude's user prompt.
    Includes: time-to-resolution, crypto prices, category-specific notes.
    """
    lines: list[str] = []

    # Time to resolution
    end_date = market_snapshot.get("endDate") or market_snapshot.get("end_date")
    hours = _hours_remaining(end_date)
    time_str = _time_context(hours)
    if time_str:
        lines.append(time_str)

    # Market health
    volume = market_snapshot.get("volume")
    liquidity = market_snapshot.get("liquidity")
    if volume:
        lines.append(f"Market volume: ${float(volume):,.0f}")
    if liquidity:
        lines.append(f"Market liquidity: ${float(liquidity):,.0f}")

    # Crypto prices (only if relevant)
    crypto_ctx = get_crypto_context(question)
    if crypto_ctx:
        lines.append(crypto_ctx)

    # Category detection + notes
    category = _detect_category(question)
    if category:
        lines.append(f"Market category: {category.replace('_', ' ').upper()}")
        note = _CATEGORY_NOTES.get(category)
        if note:
            lines.append(note)

    return "\n".join(lines)
