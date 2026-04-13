from __future__ import annotations

import json
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://polymarket.com/",
}

# Bypass system proxies (VPN/corporate proxies cause CONNECT tunnel 403 with curl)
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class PolymarketAdapter:
    """
    Phase 1.9 adapter:
    - Fetch exact market by slug using direct slug endpoint
    - Avoid fuzzy search mismatch for runtime infer
    """

    VENUE = "polymarket"
    BASE_URL = "https://gamma-api.polymarket.com/markets"
    EVENTS_URL = "https://gamma-api.polymarket.com/events"

    def venue(self) -> str:
        return self.VENUE

    def _fetch_json(self, url: str, *, context: str) -> Dict[str, Any]:
        req = urllib.request.Request(url, headers=_HEADERS)
        try:
            with _OPENER.open(req, timeout=20) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code} fetching {context}: {e.reason}")
        except Exception as e:
            raise RuntimeError(f"fetch failed for {context}: {str(e)[:300]}")

        try:
            data = json.loads(raw)
        except Exception as e:
            raise RuntimeError(f"gamma-api returned non-json for {context}: {str(e)}")

        if not isinstance(data, dict):
            raise LookupError(f"gamma-api did not return an object for {context}")
        return data

    def _is_not_found(self, data: Dict[str, Any]) -> bool:
        err = str(data.get("error") or "").strip().lower()
        typ = str(data.get("type") or "").strip().lower()
        return "not found" in err or "not found" in typ

    def _parse_json_list(self, value: Any) -> Optional[List[Any]]:
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except Exception:
                return None
            return parsed if isinstance(parsed, list) else None
        return None

    def _is_yes_no_market(self, market: Dict[str, Any]) -> bool:
        outcomes = self._parse_json_list(market.get("outcomes"))
        if not outcomes or len(outcomes) < 2:
            return False
        normalized = [str(x).strip().lower() for x in outcomes]
        return "yes" in normalized and "no" in normalized

    def _select_event_market(self, *, slug: str, event: Dict[str, Any]) -> Dict[str, Any]:
        markets = event.get("markets")
        if not isinstance(markets, list) or not markets:
            raise LookupError(f"event slug resolved but contains no markets for slug={slug}")

        exact = [
            m for m in markets
            if isinstance(m, dict) and str(m.get("slug") or "").strip() == slug
        ]
        if exact:
            return exact[0]

        binary = [
            m for m in markets
            if isinstance(m, dict) and self._is_yes_no_market(m)
        ]
        if len(binary) == 1:
            return binary[0]
        if len(binary) > 1:
            raise LookupError(
                f"event slug resolved but multiple YES/NO markets found for slug={slug}; "
                f"cannot choose unambiguously"
            )

        raise LookupError(f"event slug resolved but no YES/NO market found for slug={slug}")

    def get_market(self, slug: str) -> Dict[str, Any]:
        slug = (slug or "").strip()
        if not slug:
            raise ValueError("get_market: slug is empty")

        market_url = f"{self.BASE_URL}/slug/{slug}"
        market_data = self._fetch_json(market_url, context=f"market slug={slug}")
        if not self._is_not_found(market_data):
            got_slug = str(market_data.get("slug") or "").strip()
            if got_slug and got_slug != slug:
                raise LookupError(f"market slug mismatch: requested={slug} returned={got_slug}")
            return market_data

        event_url = f"{self.EVENTS_URL}/slug/{slug}"
        event_data = self._fetch_json(event_url, context=f"event slug={slug}")
        if self._is_not_found(event_data):
            raise LookupError(f"slug not found in Polymarket markets or events: {slug}")

        market = self._select_event_market(slug=slug, event=event_data)
        got_slug = str(market.get("slug") or "").strip()
        if got_slug and got_slug != slug:
            market = dict(market)
            market["event_slug"] = slug
        if not market.get("question") and event_data.get("title"):
            market = dict(market)
            market["question"] = event_data.get("title")

        return market
