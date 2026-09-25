from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo
from urllib.request import Request, urlopen

from maker_spread_economics.polymarket_us import PolymarketUSPublicClient

from .fair_value import assess_value
from .fees import attach_fees
from .direct_contracts import normalized_for_direct_contract
from .discovery import MAX_ACTIVE_MARKETS_PER_VENUE, paginate, paginate_collection
from .discovery import classify_market as classify_inventory_market
from .discovery import normalize_market as normalize_inventory_market
from .event_discovery import discover_event_candidate
from .models import Venue
from .mlb import MLBEvidenceProvider, MLBStatsAPI
from .models import NormalizedMarket, utcnow
from .normalization import normalize_kalshi, normalize_pmus

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
MLB_TZ = ZoneInfo("America/New_York")


def _is_mlb_moneyline(row: dict) -> bool:
    """Recognize current MLB full-game winners from venue metadata and rules."""
    raw = row.get("raw", row)
    if not isinstance(raw, dict):
        return False
    market_type = " ".join(
        str(raw.get(key) or "").lower()
        for key in ("marketType", "market_type", "sportsMarketType", "sportsMarketTypeV2")
    )
    teams = raw.get("marketSides")
    nested_mlb = bool(
        isinstance(teams, list)
        and len(teams) == 2
        and all(
            isinstance(side, dict)
            and isinstance(side.get("team"), dict)
            and str(side["team"].get("league") or "").lower() == "mlb"
            for side in teams
        )
    )
    text = " ".join(
        str(raw.get(key) or "").lower()
        for key in ("question", "title", "description", "rules_primary", "rules_secondary")
    )
    return (
        "moneyline" in market_type
        and (nested_mlb or "mlb" in text or "baseball" in text)
        and any(term in text for term in ("winner", "wins", "win"))
        and not any(term in market_type + " " + text for term in ("spread", "total", "prop", "future"))
    )


def _is_fed_rates_market(row: dict) -> bool:
    """Scope generic live enrichment to the existing Fed/rates taxonomy."""
    raw = row.get("raw", row)
    if not isinstance(raw, dict):
        return False
    classified = classify_inventory_market(
        normalize_inventory_market(raw, venue="PMUS")
    )
    return (
        classified.classification == "MACRO_ECONOMICS"
        and classified.subcategory == "Fed/rates"
    )


def _mlb_game_id(review_reference: object) -> str | None:
    prefix = "official-mlb-statsapi:"
    value = str(review_reference or "")
    game_id = value[len(prefix):] if value.startswith(prefix) else ""
    return game_id if game_id.isdigit() else None


def _dedupe_rows(rows: list[dict], key: str) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for row in rows:
        identity = str(row.get(key) or row.get("id") or row.get("slug") or "")
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        result.append(row)
    return result


class KalshiPublicClient:
    """GET-only transport, adapted from the existing cross-venue research script.

    No credentials, account paths, order methods or environment loading.
    """

    def get(self, path: str, **params: str | int) -> dict:
        parts = path.split("/")
        if (
            ".." in path
            or len(parts) not in (2, 3, 4)
            or parts[1] not in {"markets", "events", "series"}
            or (len(parts) == 4 and (parts[1] != "markets" or parts[3] != "orderbook"))
        ):
            raise ValueError("Only public market paths are supported")
        request = Request(
            f"{KALSHI_BASE}{path}?{urlencode(params)}",
            headers={"User-Agent": "PARALLAX-read-only/1"},
            method="GET",
        )
        with urlopen(request, timeout=8) as response:
            return json.load(response)

    def markets_page(self, *, limit: int, cursor: str = "") -> dict:
        return self.get(
            "/markets", status="open", limit=limit, cursor=cursor, mve_filter="exclude"
        )

    def series_page(self, *, limit: int, cursor: str = "") -> dict:
        return self.get("/series", status="open", limit=limit, cursor=cursor)

    def events_page(self, *, series_ticker: str, limit: int, cursor: str = "") -> dict:
        return self.get("/events", status="open", series_ticker=series_ticker, limit=limit, cursor=cursor)

    def event_markets_page(self, *, event_ticker: str, limit: int, cursor: str = "") -> dict:
        return self.get("/markets", status="open", event_ticker=event_ticker, limit=limit, cursor=cursor, mve_filter="exclude")

    def mlb_markets_page(self, *, limit: int = 100, cursor: str = "") -> dict:
        return self.get("/markets", status="open", limit=limit, cursor=cursor, series_ticker="KXMLBGAME")

    def book(self, ticker: str) -> dict:
        return self.get(f"/markets/{quote(ticker, safe='')}/orderbook", depth=20)

    def trades(self, ticker: str) -> list:
        return self.get("/markets/trades", ticker=ticker, limit=100).get("trades", [])

    def event(self, ticker: str) -> dict:
        return self.get(f"/events/{quote(ticker, safe='')}").get("event", {})

    def series(self, ticker: str) -> dict:
        return self.get(f"/series/{quote(ticker, safe='')}").get("series", {})


def collect_markets(limit: int = 12) -> tuple[list[NormalizedMarket], dict]:
    if not 1 <= limit <= 100:
        raise ValueError("Scan limit must be between 1 and 100 per venue")
    markets: list[NormalizedMarket] = []
    collected_evidence = {}
    market_game_ids: dict[str, str] = {}
    data_unavailable_game_ids: set[str] = set()
    metrics: Counter = Counter()
    errors: list[dict] = []
    pmus_discovery_complete = False
    kalshi_discovery_complete = False

    def failure(venue: str, stage: str, exc: Exception) -> None:
        metrics[f"{venue}.failures"] += 1
        # Do not serialize arbitrary exception messages, headers or account details.
        errors.append(
            {"venue": venue, "stage": stage, "error_type": type(exc).__name__}
        )

    pmus = None
    try:
        pmus = PolymarketUSPublicClient()
        # The venue orders globally by volume; low-volume MLB games are not
        # reliably near the front. Page until upstream completion or the shared
        # hard safety ceiling, and expose bounded coverage explicitly.
        rows, pmus_coverage = paginate(
            pmus.markets_page,
            page_size=100,
            max_pages=100,
            max_rows=MAX_ACTIVE_MARKETS_PER_VENUE,
        )
        pmus_discovery_complete = pmus_coverage.state.value == "COMPLETE"
        all_rows = [
            r for r in rows
            if r.get("active") and not r.get("closed") and r.get("accepting_orders")
        ]
        all_rows = _dedupe_rows(all_rows, "slug")
        rows = [r for r in all_rows if _is_mlb_moneyline(r)]
        discovery_time = utcnow()
        discovery_at = discovery_time.isoformat()
        metrics["POLYMARKET.markets_discovered"] = len(all_rows)
        provider = MLBEvidenceProvider()
        for row in rows:
            mapped_game_id = None
            try:
                # Mapping/evidence is deliberately attempted before the book.
                candidate = normalize_pmus(row, {}, discovery_at)
                proof = provider.assess(candidate)
                if proof is None:
                    failure("POLYMARKET", "mapping_or_evidence", ValueError("no exact MLB match or evidence"))
                    continue
                mapped_game_id = _mlb_game_id(proof.review_reference)
                book = pmus.book(row["slug"])
                market = normalize_pmus(row, book, utcnow().isoformat())
                market = attach_fees(market, utcnow())
                markets.append(market)
                collected_evidence[(market.venue, market.venue_market_id)] = proof
                if mapped_game_id:
                    market_game_ids[f"{market.venue.value}:{market.venue_market_id}"] = mapped_game_id
                metrics["POLYMARKET.markets_observed"] += 1
                metrics["POLYMARKET.evidence_produced"] += 1
            except (ValueError, TypeError, KeyError) as exc:
                if mapped_game_id:
                    data_unavailable_game_ids.add(mapped_game_id)
                failure("POLYMARKET", "market", exc)
            except Exception as exc:  # noqa: BLE001 - isolate one live market
                if mapped_game_id:
                    data_unavailable_game_ids.add(mapped_game_id)
                failure("POLYMARKET", "market", exc)

        # The generic/direct seam is intentionally evidence-neutral.  It keeps
        # exact non-sports contracts in the same normalized market universe;
        # EvidenceEngine remains the only source allowed to provide a forecast.
        for row in all_rows:
            if _is_mlb_moneyline(row) or not _is_fed_rates_market(row):
                if not _is_mlb_moneyline(row):
                    metrics["POLYMARKET.generic_scope_skipped"] += 1
                continue
            try:
                candidate = discover_event_candidate(
                    row, venue=Venue.POLYMARKET, discovered_at=discovery_time
                )
                if candidate.candidate is None:
                    metrics["POLYMARKET.generic_rejected"] += 1
                    continue
                book = pmus.book(candidate.candidate.slug or candidate.candidate.market_id)
                candidate = discover_event_candidate(
                    row,
                    venue=Venue.POLYMARKET,
                    book=book,
                    discovered_at=discovery_time,
                )
                if candidate.candidate is None:
                    metrics["POLYMARKET.generic_rejected"] += 1
                    continue
                market = attach_fees(
                    normalized_for_direct_contract(candidate.candidate), utcnow()
                )
                markets.append(market)
                metrics["POLYMARKET.generic_markets_observed"] += 1
            except (ValueError, TypeError, KeyError) as exc:
                failure("POLYMARKET", "generic_market", exc)
            except Exception as exc:  # noqa: BLE001 - isolate one live market
                failure("POLYMARKET", "generic_market", exc)
    except Exception as exc:  # noqa: BLE001 - isolate SDK discovery failures by venue
        failure("POLYMARKET", "discovery", exc)
    finally:
        if pmus:
            pmus.close()
    kalshi = KalshiPublicClient()
    events, series = {}, {}
    try:
        rows, kalshi_coverage = paginate_collection(
            kalshi.mlb_markets_page,
            key="markets",
            page_size=100,
            max_pages=100,
            max_rows=MAX_ACTIVE_MARKETS_PER_VENUE,
        )
        kalshi_discovery_complete = kalshi_coverage.state.value == "COMPLETE"
        discovery_at = utcnow().isoformat()
        metrics["KALSHI.markets_discovered"] = len(rows)
        rows = _dedupe_rows(rows, "ticker")
        provider = MLBEvidenceProvider()
        for row in rows:
            book, trades = {}, None
            event, fee_series = None, None
            mapped_game_id = None
            try:
                event_id = row["event_ticker"]
                if event_id not in events:
                    events[event_id] = kalshi.event(event_id)
                event = events[event_id]
                series_id = event["series_ticker"]
                if series_id not in series:
                    series[series_id] = kalshi.series(series_id)
                fee_series = series[series_id]
            except (OSError, ValueError, KeyError) as exc:
                failure("KALSHI", "fees", exc)
            observed_at = utcnow().isoformat()
            try:
                # Exact mapping/evidence precedes executable-book retrieval.
                candidate = normalize_kalshi(row, {}, discovery_at, None, event)
                proof = provider.assess(candidate)
                if proof is None:
                    failure("KALSHI", "mapping_or_evidence", ValueError("no exact MLB match or evidence"))
                    continue
                mapped_game_id = _mlb_game_id(proof.review_reference)
                book = kalshi.book(row["ticker"])
                observed_at = utcnow().isoformat()
                market = normalize_kalshi(row, book, observed_at, trades, event)
                market = replace(market, data_timestamp=discovery_at)
                market = attach_fees(market, utcnow(), event=event, series=fee_series)
                markets.append(market)
                collected_evidence[(market.venue, market.venue_market_id)] = proof
                if mapped_game_id:
                    market_game_ids[f"{market.venue.value}:{market.venue_market_id}"] = mapped_game_id
                metrics["KALSHI.markets_observed"] += 1
                metrics["KALSHI.evidence_produced"] += 1
            except (ValueError, TypeError, KeyError) as exc:
                if mapped_game_id:
                    data_unavailable_game_ids.add(mapped_game_id)
                failure("KALSHI", "market", exc)
            except Exception as exc:  # noqa: BLE001 - isolate one live market
                if mapped_game_id:
                    data_unavailable_game_ids.add(mapped_game_id)
                failure("KALSHI", "market", exc)
    except (OSError, ValueError, KeyError) as exc:
        failure("KALSHI", "discovery", exc)
    now = utcnow()
    target_date = now.astimezone(MLB_TZ).date().isoformat()
    slate_schedule: list[dict] = []
    slate_schedule_state = "COMPLETE"
    try:
        slate_schedule = MLBStatsAPI().scheduled_games_for_date(target_date)
    except Exception as exc:  # Official schedule failure must stay explicit.
        slate_schedule_state = "DATA_UNAVAILABLE"
        failure("OFFICIAL_MLB", "schedule", exc)
    markets = [
        replace(
            m,
            original_metadata={
                **m.original_metadata,
                "fair_value_provenance": assess_value(m, markets, now),
            },
        )
        for m in markets
    ]
    return markets, {
        "metrics": dict(metrics),
        "errors": errors,
        "scope": "complete-or-explicitly-bounded current MLB moneyline discovery across PMUS and Kalshi KXMLBGAME",
        "_slate_schedule": slate_schedule,
        "_slate_schedule_state": slate_schedule_state,
        "_slate_discovery_complete": (
            pmus_discovery_complete
            and kalshi_discovery_complete
            and not any(error["stage"] == "mapping_or_evidence" for error in errors)
        ),
        "_market_game_ids": market_game_ids,
        "_slate_data_unavailable_game_ids": sorted(data_unavailable_game_ids),
        # Request-local transport for the normal service scorer.  This is
        # consumed immediately by PlayService and is never persisted.
        "_evidence": collected_evidence,
    }
