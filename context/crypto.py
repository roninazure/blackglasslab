"""
BlackGlassLab — live crypto price feed (Phase 3.5)

Uses CoinGecko public API — free, no key required.
Prices are cached for 5 minutes to avoid hammering the API.
"""
from __future__ import annotations

import time
import urllib.request
import urllib.error
import json
from typing import Dict, Optional

# ---------------------------------------------------------------------------
# In-process cache: {coin_id: (price_usd, fetched_at)}
# ---------------------------------------------------------------------------
_cache: Dict[str, tuple[float, float]] = {}
_CACHE_TTL = 300  # 5 minutes

# CoinGecko IDs for the coins we care about
COIN_IDS = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "sol": "solana",
}

_COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=bitcoin,ethereum,solana&vs_currencies=usd"
)


def _fetch_prices() -> Dict[str, float]:
    """Fetch current USD prices from CoinGecko. Returns {} on failure."""
    try:
        req = urllib.request.Request(
            _COINGECKO_URL,
            headers={"User-Agent": "BlackGlassLab/3.5 (paper-trading-research)"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {
            "btc": float(data.get("bitcoin", {}).get("usd", 0)),
            "eth": float(data.get("ethereum", {}).get("usd", 0)),
            "sol": float(data.get("solana", {}).get("usd", 0)),
        }
    except Exception:
        return {}


def get_price(coin: str) -> Optional[float]:
    """
    Return current USD price for a coin symbol (btc, eth, sol).
    Returns None if unavailable. Cached for 5 minutes.
    """
    coin = coin.lower().strip()
    now = time.time()
    cached = _cache.get(coin)
    if cached and (now - cached[1]) < _CACHE_TTL:
        return cached[0]

    prices = _fetch_prices()
    for k, v in prices.items():
        if v > 0:
            _cache[k] = (v, now)

    result = _cache.get(coin)
    return result[0] if result else None


def get_crypto_context(question: str) -> str:
    """
    If the question mentions a crypto asset, return a context string
    with the current price. Returns empty string if not relevant.
    """
    q = question.lower()
    lines = []

    if "bitcoin" in q or "btc" in q:
        price = get_price("btc")
        if price:
            lines.append(f"Current BTC price: ${price:,.0f} USD")

    if "ethereum" in q or " eth " in q:
        price = get_price("eth")
        if price:
            lines.append(f"Current ETH price: ${price:,.0f} USD")

    if "solana" in q or " sol " in q:
        price = get_price("sol")
        if price:
            lines.append(f"Current SOL price: ${price:,.2f} USD")

    return "\n".join(lines)
