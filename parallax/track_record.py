from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .engine import qualify
from .models import Action, Evidence, NormalizedMarket, Side, timestamp, utcnow


class TrackRecord:
    """Append-only publication and settlement records in a separate product DB."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS publications (
                    play_id TEXT PRIMARY KEY, published_at TEXT NOT NULL, snapshot TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settlements (
                    play_id TEXT PRIMARY KEY REFERENCES publications(play_id),
                    settled_at TEXT NOT NULL, snapshot TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS publications_no_update BEFORE UPDATE ON publications
                    BEGIN SELECT RAISE(ABORT, 'Immutable publication'); END;
                CREATE TRIGGER IF NOT EXISTS publications_no_delete BEFORE DELETE ON publications
                    BEGIN SELECT RAISE(ABORT, 'Immutable publication'); END;
                CREATE TRIGGER IF NOT EXISTS settlements_no_update BEFORE UPDATE ON settlements
                    BEGIN SELECT RAISE(ABORT, 'Immutable settlement'); END;
                CREATE TRIGGER IF NOT EXISTS settlements_no_delete BEFORE DELETE ON settlements
                    BEGIN SELECT RAISE(ABORT, 'Immutable settlement'); END;
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    def publish(
        self, market: NormalizedMarket, side: Side, evidence: Evidence, *, now=None
    ) -> bool:
        now = now or utcnow()
        play = qualify(market, side, evidence, now=now)
        if play.demo or play.suggested_action != Action.BUY:
            raise ValueError(
                "Only freshly qualified, non-demo BUY plays can be published"
            )
        # Freeze $100 outcome math even when the displayed book only supports $25.
        # A capacity-limited $100 example is not a claimed executable $100 trade.
        snapshot = {
            "play_id": play.id,
            "published_at": now.isoformat(),
            "venue": play.venue,
            "market": play.market_id,
            "side": play.side,
            "entry_price_at_publication": play.executable_price,
            "fair_value_at_publication": play.parallax_fair_value,
            "edge_at_publication": play.edge_points,
            "confidence": play.confidence_band,
            "verdict": play.suggested_action,
            "resolution_time": play.resolution_time,
            "play": play.as_dict(),
            "market_snapshot": asdict(market),
        }
        with self.connect() as db:
            # One publication per venue / market / side for v1, regardless of refreshes.
            cursor = db.execute(
                "INSERT OR IGNORE INTO publications VALUES (?, ?, ?)",
                (play.id, now.isoformat(), json.dumps(snapshot, allow_nan=False)),
            )
            return cursor.rowcount == 1

    def settle(
        self,
        play_id: str,
        *,
        venue: str,
        market_id: str,
        winning_side: str | None,
        resolution: str,
        source_reference: str,
        settled_at: str,
    ) -> None:
        if resolution not in {"RESOLVED", "VOID"} or not source_reference.strip():
            raise ValueError(
                "Settlement requires an explicit resolution and authoritative reference"
            )
        if (resolution == "RESOLVED" and winning_side not in {"YES", "NO"}) or (
            resolution == "VOID" and winning_side is not None
        ):
            raise ValueError("Invalid winning side")
        date = timestamp(settled_at)
        if date is None or date > utcnow():
            raise ValueError("Invalid settlement timestamp")
        with self.connect() as db:
            row = db.execute(
                "SELECT snapshot FROM publications WHERE play_id=?", (play_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Unknown publication")
            publication = json.loads(row[0])
            published_at = timestamp(publication["published_at"])
            if (
                publication["venue"] != venue
                or publication["market"] != market_id
                or published_at is None
                or date < published_at
            ):
                raise ValueError("Settlement does not match publication")
            result = (
                "VOID"
                if resolution == "VOID"
                else "WIN"
                if winning_side == publication["side"]
                else "LOSS"
            )
            # Price-only standardized equal-$100 benchmark; no execution or fee claims.
            gross_return = (
                0
                if result == "VOID"
                else (1 / publication["entry_price_at_publication"] - 1)
                if result == "WIN"
                else -1
            )
            scenario = publication["play"]["retail_examples"][3]
            net_pnl = None
            if (
                result != "VOID"
                and scenario["available"]
                and scenario["total_cost"] is not None
            ):
                net_pnl = (
                    scenario["estimated_payout_if_correct"] if result == "WIN" else 0
                ) - scenario["total_cost"]
            snapshot = {
                "resolution": resolution,
                "winning_side": winning_side,
                "result": result,
                "realized_return_at_published_price": gross_return,
                "hypothetical_net_pnl_100": net_pnl,
                "source_reference": source_reference,
                "settled_at": settled_at,
            }
            db.execute(
                "INSERT INTO settlements VALUES (?, ?, ?)",
                (play_id, settled_at, json.dumps(snapshot)),
            )

    def summary(self) -> dict:
        with self.connect() as db:
            rows = db.execute(
                "SELECT p.snapshot, s.snapshot FROM publications p LEFT JOIN settlements s USING(play_id) ORDER BY published_at"
            ).fetchall()
        published = [json.loads(p) for p, _ in rows]
        settled = [json.loads(s) for _, s in rows if s]
        wins = sum(s["result"] == "WIN" for s in settled)
        losses = sum(s["result"] == "LOSS" for s in settled)
        voids = sum(s["result"] == "VOID" for s in settled)
        nonvoid = [s for s in settled if s["result"] != "VOID"]
        net = [
            s["hypothetical_net_pnl_100"]
            for s in nonvoid
            if s["hypothetical_net_pnl_100"] is not None
        ]
        return {
            "published_plays": len(published),
            "wins": wins,
            "losses": losses,
            "voids": voids,
            "pending": len(published) - len(settled),
            "win_rate": wins / (wins + losses) if wins + losses else None,
            "roi_at_published_price_before_costs": sum(
                s["realized_return_at_published_price"] for s in nonvoid
            )
            / len(nonvoid)
            if nonvoid
            else None,
            "net_pnl_per_100_hypothetical_equal_stake": sum(net) / len(net)
            if net and len(net) == len(nonvoid)
            else None,
            "net_pnl_total": sum(net) if net and len(net) == len(nonvoid) else None,
            "net_pnl_eligible_plays": len(net),
            "calibration_metrics": None,
            "calibration_note": "No calibrated probability model or statistically justified sample has been established.",
            "method": "Hypothetical results, not executed trades. Price-only ROI excludes costs and quantity rounding. Net P/L uses the frozen $100 contract-budget example, venue quantity rounding and estimated fees. Void costs are unknown and excluded. Pending plays excluded. No research trials imported.",
            "publications": [
                {k: v for k, v in p.items() if k not in {"play", "market_snapshot"}}
                for p in published
            ],
            "outcomes": settled,
        }
