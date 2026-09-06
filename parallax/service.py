from __future__ import annotations

import math
import sqlite3
from collections import Counter
from dataclasses import asdict, replace
from threading import RLock
from typing import Any

from .engine import qualify
from .entitlements import Feature, Plan, entitlement
from .models import (
    Action,
    Confidence,
    Evidence,
    NormalizedMarket,
    PlayType,
    Side,
    Venue,
    utcnow,
)
from .track_record import TrackRecord


class PlayService:
    def __init__(self, store: TrackRecord):
        self.store = store
        self.lock = RLock()
        self.markets: list[NormalizedMarket] = []
        self.evidence: dict[tuple[Venue, str], Evidence] = {}
        self.collection: dict[str, Any] = {}
        self.failures = 0
        self.mode = "live"
        self.last_refresh = None
        self.first_seen: dict[str, str] = {}

    def replace_inputs(self, markets, evidence=None, collection=None, *, mode="live"):
        if mode not in {"live", "demo"} or any(
            m.demo != (mode == "demo") for m in markets
        ):
            raise ValueError("Live and synthetic inputs must never be mixed")
        with self.lock:
            self.markets = list(
                {(m.venue, m.venue_market_id): m for m in markets}.values()
            )
            self.evidence = dict(evidence or {})
            self.collection = collection or {}
            self.mode = mode
            self.last_refresh = utcnow().isoformat()

    def _plays(self):
        now = utcnow()
        result = []
        with self.lock:
            for market in self.markets:
                evidence = self.evidence.get((market.venue, market.venue_market_id))
                for side in Side:
                    try:
                        play = qualify(market, side, evidence, now=now)
                        created = self.first_seen.setdefault(play.id, play.created_at)
                        play = replace(play, created_at=created)
                        if play.suggested_action == Action.BUY and not play.demo:
                            # Persist before returning an actionable play to any consumer.
                            self.store.publish(market, side, evidence, now=now)
                        result.append(play)
                    except (ValueError, TypeError, ArithmeticError, sqlite3.Error):
                        self.failures += 1
                        # Fail closed: never return an unrecorded live BUY.
            return result

    @staticmethod
    def _view(play, plan):
        view = play.as_dict()
        if not entitlement(plan).permits(Feature.DETAILS):
            for key in (
                "evidence",
                "expected_value",
                "expected_return",
                "decision_reasons",
                "reason_factors",
            ):
                view.pop(key, None)
            view["verdict"].pop("supporting_reasons", None)
        return view

    def plays(self, plan: Plan = Plan.EXPLORER, **filters):
        allowed = {
            "venue",
            "confidence",
            "action",
            "play_type",
            "resolution_horizon",
            "minimum_edge",
        }
        if set(filters) - allowed:
            raise ValueError("Unknown filter")
        advanced = {
            k: v for k, v in filters.items() if k != "venue" and v not in (None, "")
        }
        if advanced and not entitlement(plan).permits(Feature.ADVANCED_FILTERS):
            raise PermissionError("Advanced filters require PRO")
        for key, enum in (
            ("venue", Venue),
            ("confidence", Confidence),
            ("action", Action),
            ("play_type", PlayType),
        ):
            if filters.get(key):
                enum(filters[key])
        for key in ("resolution_horizon", "minimum_edge"):
            if filters.get(key) not in (None, ""):
                value = float(filters[key])
                if not math.isfinite(value) or value < 0:
                    raise ValueError(f"{key} must be finite and nonnegative")
                filters[key] = value
        rows = self._plays()
        for key, attr in (
            ("venue", "venue"),
            ("confidence", "confidence_band"),
            ("action", "suggested_action"),
            ("play_type", "play_type"),
        ):
            if filters.get(key):
                rows = [p for p in rows if getattr(p, attr) == filters[key]]
        horizon, edge = filters.get("resolution_horizon"), filters.get("minimum_edge")
        if horizon not in (None, ""):
            rows = [
                p
                for p in rows
                if p.estimated_time_to_resolution is not None
                and 0 < p.estimated_time_to_resolution <= float(horizon) * 3600
            ]
        if edge not in (None, ""):
            rows = [
                p
                for p in rows
                if p.edge_points is not None and p.edge_points >= float(edge)
            ]
        rows.sort(
            key=lambda p: (
                {Action.BUY: 0, Action.WATCH: 1, Action.PASS: 2}[p.suggested_action],
                p.venue,
                p.market_id,
                p.side,
            )
        )
        # Interleave venues so the limited Explorer feed visibly covers both.
        first = [p for p in rows if p.venue == Venue.POLYMARKET]
        second = [p for p in rows if p.venue == Venue.KALSHI]
        interleaved = [
            p
            for i in range(max(len(first), len(second)))
            for group in (first, second)
            if i < len(group)
            for p in [group[i]]
        ]
        limited = interleaved[: entitlement(plan).play_limit]
        return {
            "mode": self.mode,
            "total": len(rows),
            "limit": entitlement(plan).play_limit,
            "items": [self._view(p, plan) for p in limited],
            "as_of": utcnow().isoformat(),
        }

    def play(self, play_id: str, plan: Plan = Plan.EXPLORER):
        if not entitlement(plan).permits(Feature.DETAILS):
            raise PermissionError("Full play details require PRO")
        for item in self._plays():
            if item.id == play_id:
                return self._view(item, plan)
        raise KeyError(play_id)

    def market_views(self, plan: Plan = Plan.EXPLORER):
        with self.lock:
            rows = [asdict(m) for m in self.markets]
        if not entitlement(plan).permits(Feature.DETAILS):
            keys = {
                "venue",
                "venue_market_id",
                "title",
                "outcomes",
                "status",
                "yes_bid",
                "yes_ask",
                "no_bid",
                "no_ask",
                "data_timestamp",
                "book_timestamp",
                "resolution_time",
                "demo",
            }
            rows = [{k: v for k, v in row.items() if k in keys} for row in rows]
        return rows

    def health(self):
        plays = self._plays()
        metrics = Counter(
            {
                "plays_generated": len(plays),
                "play_generation_failures": self.failures,
                "BUY": 0,
                "WATCH": 0,
                "PASS": 0,
                "stale_data_rejections": 0,
                "liquidity_rejections": 0,
                "confidence_rejections": 0,
            }
        )
        for play in plays:
            metrics[play.suggested_action] += 1
            for code in play.verdict.failed_gates:
                if code in {"stale_data", "liquidity", "confidence"}:
                    metrics[f"{code}_rejections"] += 1
        summary = self.store.summary()
        metrics.update(
            {f"published_outcomes_{k}": summary[k] for k in ("wins", "losses", "voids")}
        )
        metrics.update(
            {
                f"{v}.markets_observed": sum(m.venue == v for m in self.markets)
                for v in Venue
            }
        )
        return {
            "status": "ok"
            if self.markets
            and not self.collection.get("errors")
            and not self.failures
            and not metrics["stale_data_rejections"]
            else "degraded",
            "mode": self.mode,
            "last_refresh": self.last_refresh,
            "metrics": dict(metrics),
            "collection": self.collection,
            "live_orders": 0,
            "execution_enabled": False,
        }

    def venues(self):
        return [
            {
                "venue": v,
                "label": "Polymarket US" if v == Venue.POLYMARKET else "Kalshi",
                "markets_observed": sum(m.venue == v for m in self.markets),
                "intelligence": True,
                "execution_enabled": False,
            }
            for v in Venue
        ]

    def alert_candidates(self, plan: Plan):
        if not entitlement(plan).permits(Feature.ALERTS):
            raise PermissionError("Alerts require PRO")
        return self.plays(plan, action="BUY")["items"]
