from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .engine import qualify
from .models import Action, Evidence, NormalizedMarket, Side, timestamp, utcnow


class TrackRecord:
    """Append-only publication, settlement, and prospective records in one product DB."""

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
                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS prospective_plays (
                    observation_id TEXT PRIMARY KEY,
                    captured_at TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    snapshot TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS prospective_settlements (
                    observation_id TEXT PRIMARY KEY REFERENCES prospective_plays(observation_id),
                    settled_at TEXT NOT NULL,
                    settlement_state TEXT NOT NULL CHECK(settlement_state IN ('RESOLVED', 'VOID')),
                    result TEXT NOT NULL CHECK(result IN ('WIN', 'LOSS', 'VOID')),
                    snapshot TEXT NOT NULL,
                    CHECK(
                        (settlement_state = 'RESOLVED' AND result IN ('WIN', 'LOSS')) OR
                        (settlement_state = 'VOID' AND result = 'VOID')
                    )
                );
                CREATE TRIGGER IF NOT EXISTS candidates_no_update BEFORE UPDATE ON candidates
                    BEGIN SELECT RAISE(ABORT, 'Immutable candidate'); END;
                CREATE TRIGGER IF NOT EXISTS candidates_no_delete BEFORE DELETE ON candidates
                    BEGIN SELECT RAISE(ABORT, 'Immutable candidate'); END;
                CREATE TRIGGER IF NOT EXISTS prospective_plays_no_update
                    BEFORE UPDATE ON prospective_plays
                    BEGIN SELECT RAISE(ABORT, 'Immutable prospective play'); END;
                CREATE TRIGGER IF NOT EXISTS prospective_plays_no_delete
                    BEFORE DELETE ON prospective_plays
                    BEGIN SELECT RAISE(ABORT, 'Immutable prospective play'); END;
                CREATE TRIGGER IF NOT EXISTS prospective_settlements_no_update
                    BEFORE UPDATE ON prospective_settlements
                    BEGIN SELECT RAISE(ABORT, 'Immutable prospective settlement'); END;
                CREATE TRIGGER IF NOT EXISTS prospective_settlements_no_delete
                    BEFORE DELETE ON prospective_settlements
                    BEGIN SELECT RAISE(ABORT, 'Immutable prospective settlement'); END;
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

    def candidate(self, candidate_id, market, side, evidence, *, now=None) -> dict:
        """Freeze a dry run without calling publish or consuming public identities."""
        now = now or utcnow()
        if not re.fullmatch(r"PX-CANDIDATE-\d{8}-\d{3}", candidate_id):
            raise ValueError("Provisional candidate ID required")
        play = qualify(market, side, evidence, now=now)
        if play.demo or play.suggested_action != Action.BUY:
            raise ValueError("Only freshly qualified real BUY candidates can be frozen")
        view = play.as_dict()
        view.update(id=candidate_id, status="PRE-PUBLICATION VALIDATION CANDIDATE")
        snapshot = {
            "play_id": candidate_id,
            "published_at": None,
            "candidate_at": now.isoformat(),
            "dry_run": True,
            "status": "NOT YET PUBLISHED",
            "venue": play.venue,
            "market": play.market_id,
            "side": play.side,
            "entry_price_at_publication": play.executable_price,
            "fair_value_at_publication": play.parallax_fair_value,
            "edge_at_publication": play.edge_points,
            "confidence": play.confidence_band,
            "verdict": play.suggested_action,
            "resolution_time": play.resolution_time,
            "play": view,
            "market_snapshot": asdict(market),
        }
        with self.connect() as db:
            db.execute(
                "INSERT INTO candidates VALUES (?, ?)",
                (candidate_id, json.dumps(snapshot, allow_nan=False)),
            )
        return snapshot

    def capture_prospective(
        self, market: NormalizedMarket, side: Side, evidence: Evidence | None, *, now=None
    ) -> dict:
        """Freeze one real, non-publishing prospective evaluation observation."""
        now = (now or utcnow()).astimezone(UTC)
        if market.demo or (evidence is not None and evidence.demo):
            raise ValueError("Synthetic demo inputs cannot be prospectively captured")
        play = qualify(market, side, evidence, now=now)
        if play.demo:
            raise ValueError("Synthetic demo inputs cannot be prospectively captured")

        captured_at = now.isoformat()
        observation_identity = (
            f"prospective:v1:{play.id}:{play.venue}:{play.market_id}:{play.side}:{captured_at}"
        )
        observation_id = f"PX-{uuid5(NAMESPACE_URL, observation_identity)}"
        snapshot = {
            "observation_id": observation_id,
            "captured_at": captured_at,
            "play_id": play.id,
            "venue": play.venue,
            "market_id": play.market_id,
            "event": market.event,
            "event_title": market.event_title,
            "title": play.market_title,
            "side": play.side,
            "selected_outcome": play.side_description,
            "verdict": play.suggested_action,
            "executable_price": play.executable_price,
            "current_price": play.current_price,
            "model_probability": play.model_probability,
            "fair_value": play.parallax_fair_value,
            "edge": play.edge_points,
            "confidence": play.confidence_band,
            "failed_gates": play.verdict.failed_gates,
            "invalidation_conditions": play.invalidation_conditions,
            "resolution_time": play.resolution_time,
            "expected_value": play.expected_value,
            "retail_examples": [asdict(example) for example in play.retail_examples],
            "play": play.as_dict(),
            "market_snapshot": asdict(market),
            "evidence_snapshot": asdict(evidence) if evidence is not None else None,
        }
        serialized = json.dumps(snapshot, allow_nan=False)
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO prospective_plays "
                "(observation_id, captured_at, venue, market_id, side, verdict, snapshot) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    observation_id,
                    captured_at,
                    play.venue,
                    play.market_id,
                    play.side,
                    play.suggested_action,
                    serialized,
                ),
            )
            stored = db.execute(
                "SELECT snapshot FROM prospective_plays WHERE observation_id=?",
                (observation_id,),
            ).fetchone()
        assert stored is not None
        return json.loads(stored[0])

    def prospective_records(self) -> list[dict]:
        """Return immutable prospective observations in capture order."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT snapshot FROM prospective_plays ORDER BY captured_at, observation_id"
            ).fetchall()
        return [json.loads(snapshot) for (snapshot,) in rows]

    def prospective_settlements(self) -> list[dict]:
        """Return authoritative prospective settlements in observation order."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT s.snapshot FROM prospective_settlements s "
                "JOIN prospective_plays p USING(observation_id) "
                "ORDER BY p.captured_at, p.observation_id"
            ).fetchall()
        return [json.loads(snapshot) for (snapshot,) in rows]

    def pending_prospective(self, *, limit: int | None = None) -> list[dict]:
        """Return observations with no appended authoritative settlement."""
        if limit is not None and limit < 1:
            raise ValueError("Pending limit must be positive")
        query = (
            "SELECT p.snapshot FROM prospective_plays p "
            "LEFT JOIN prospective_settlements s USING(observation_id) "
            "WHERE s.observation_id IS NULL ORDER BY p.captured_at, p.observation_id"
        )
        parameters: tuple[int, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            parameters = (limit,)
        with self.connect() as db:
            rows = db.execute(query, parameters).fetchall()
        return [json.loads(snapshot) for (snapshot,) in rows]

    def settle_prospective(
        self,
        observation_id: str,
        *,
        settlement_state: str,
        result: str,
        authoritative_source: str,
        authoritative_source_id: str,
        authoritative_winner: str | None,
        settled_at: str,
        source_resolved_at: str | None = None,
        sport: str | None = None,
    ) -> bool:
        """Append one auditable final outcome; identical repeats are idempotent."""
        if settlement_state not in {"RESOLVED", "VOID"}:
            raise ValueError("Prospective settlement must be RESOLVED or VOID")
        if not authoritative_source.strip() or not authoritative_source_id.strip():
            raise ValueError("Authoritative source and identifier are required")
        if settlement_state == "RESOLVED":
            if result not in {"WIN", "LOSS"} or not authoritative_winner:
                raise ValueError("Resolved settlement requires WIN/LOSS and a winner")
        elif result != "VOID" or authoritative_winner is not None:
            raise ValueError("Void settlement requires VOID and no winner")
        date = timestamp(settled_at)
        resolved_date = timestamp(source_resolved_at) if source_resolved_at else None
        if (
            date is None
            or date > utcnow()
            or (source_resolved_at and resolved_date is None)
            or (resolved_date is not None and resolved_date > date)
        ):
            raise ValueError("Invalid settlement timestamp")

        with self.connect() as db:
            # Serialize check-and-append so concurrent identical attempts remain idempotent.
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT captured_at, snapshot FROM prospective_plays WHERE observation_id=?",
                (observation_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown prospective observation")
            captured_at, serialized_observation = row
            if date < timestamp(captured_at):
                raise ValueError("Settlement predates prospective observation")
            observation = json.loads(serialized_observation)
            probability = observation.get("model_probability")
            binary_outcome = None if result == "VOID" else int(result == "WIN")
            brier_score = None
            log_loss = None
            if binary_outcome is not None and isinstance(probability, (int, float)):
                probability = float(probability)
                if 0.0 <= probability <= 1.0:
                    brier_score = (probability - binary_outcome) ** 2
                if 0.0 < probability < 1.0:
                    log_loss = -math.log(
                        probability if binary_outcome else 1.0 - probability
                    )
            executable_price = observation.get("executable_price")
            hypothetical_return = None
            if result == "VOID":
                hypothetical_return = 0.0
            elif isinstance(executable_price, (int, float)) and 0 < executable_price <= 1:
                hypothetical_return = (
                    1.0 / float(executable_price) - 1.0 if result == "WIN" else -1.0
                )
            authority = {
                "settlement_state": settlement_state,
                "result": result,
                "authoritative_winner": authoritative_winner,
                "authoritative_source": authoritative_source,
                "authoritative_source_id": authoritative_source_id,
                "source_resolved_at": source_resolved_at,
                "sport": sport,
            }
            existing = db.execute(
                "SELECT snapshot FROM prospective_settlements WHERE observation_id=?",
                (observation_id,),
            ).fetchone()
            if existing is not None:
                stored = json.loads(existing[0])
                stored_authority = {key: stored.get(key) for key in authority}
                if stored_authority == authority:
                    return False
                raise ValueError("Conflicting prospective settlement")
            snapshot = {
                "observation_id": observation_id,
                "settled_at": settled_at,
                **authority,
                "selected_outcome": observation.get("selected_outcome"),
                "selected_side": observation.get("side"),
                "venue": observation.get("venue"),
                "market_id": observation.get("market_id"),
                "frozen_executable_price": executable_price,
                "frozen_current_price": observation.get("current_price"),
                "frozen_model_probability": probability,
                "frozen_edge": observation.get("edge"),
                "frozen_verdict": observation.get("verdict"),
                "binary_outcome": binary_outcome,
                "brier_score": brier_score,
                "log_loss": log_loss,
                "accuracy": binary_outcome,
                "hypothetical_standardized_return_at_frozen_price": hypothetical_return,
            }
            db.execute(
                "INSERT INTO prospective_settlements "
                "(observation_id, settled_at, settlement_state, result, snapshot) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    observation_id,
                    settled_at,
                    settlement_state,
                    result,
                    json.dumps(snapshot, allow_nan=False, sort_keys=True),
                ),
            )
        return True

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
