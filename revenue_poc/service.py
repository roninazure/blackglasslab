from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .config import RevenueConfig
from .economics import adaptive_threshold, evaluate_execution
from .repository import apply_schema, json_object


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class RevenuePOCService:
    """Paper-only portfolio service. It has no adapter or order-execution dependency."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        config: RevenueConfig | None = None,
        *,
        now: datetime | None = None,
    ) -> None:
        self.conn = conn
        self.config = config or RevenueConfig.from_env()
        self.now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self.config.validate()

    def initialize(self) -> None:
        apply_schema(self.conn)
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO revenue_poc_accounts
                (id,created_at_utc,starting_balance_usd,position_size_usd,
                 max_open_positions,max_capital_deployed_usd,max_category_positions,
                 min_executable_edge,daily_api_budget_usd,config_json)
                VALUES (1,?,?,?,?,?,?,?,?,?)
                """,
                (
                    _utc_now(),
                    self.config.starting_balance_usd,
                    self.config.position_size_usd,
                    self.config.max_open_positions,
                    self.config.max_capital_deployed_usd,
                    self.config.max_category_positions,
                    self.config.min_executable_edge,
                    self.config.daily_api_budget_usd,
                    json.dumps(self.config.as_dict(), sort_keys=True),
                ),
            )

    def remaining_api_budget(self, date_utc: str) -> float:
        row = self.conn.execute(
            "SELECT estimated_cost_usd FROM revenue_poc_api_daily WHERE date_utc=?",
            (date_utc,),
        ).fetchone()
        spent = float(row[0]) if row else 0.0
        return max(0.0, self.config.daily_api_budget_usd - spent)

    def can_spend_api(self, date_utc: str, estimated_call_cost_usd: float) -> bool:
        return max(0.0, estimated_call_cost_usd) <= self.remaining_api_budget(date_utc)

    def ingest_shadow_forecasts(self) -> dict[str, int]:
        """Convert immutable shadow observations into executable paper decisions."""
        self.initialize()
        rows = self.conn.execute(
            """
            SELECT id,run_id,timestamp_utc,venue,market_id,question,category,
                   market_probability,model_probability,absolute_edge,
                   production_decision,rejection_reason,time_to_resolution_days,
                   market_end_date,llm_used,metadata
            FROM shadow_forecasts ORDER BY timestamp_utc,id
            """
        ).fetchall()
        counts = {"source": len(rows), "evaluated": 0, "cache_hits": 0, "admitted": 0, "rejected": 0}
        candidates: list[tuple[float, int, Any, str]] = []
        for row in rows:
            metadata = json_object(row[15])
            scoring_raw = (
                metadata.get("scoring_components", {}).get("raw", {})
                if isinstance(metadata.get("scoring_components"), dict)
                else {}
            )
            snapshot = metadata.get("market_snapshot", {})
            if not isinstance(snapshot, dict):
                snapshot = {}
            spread = float(metadata.get("spread") or 0.0)
            depth = float(
                snapshot.get("depth_usd")
                or snapshot.get("liquidity")
                or scoring_raw.get("liquidity")
                or 0.0
            )
            bid = snapshot.get("best_bid", snapshot.get("bestBid"))
            ask = snapshot.get("best_ask", snapshot.get("bestAsk"))
            economics = evaluate_execution(
                model_probability=float(row[8]),
                market_probability=float(row[7]),
                stake_usd=self.config.position_size_usd,
                best_bid=float(bid) if bid is not None else None,
                best_ask=float(ask) if ask is not None else None,
                spread=spread,
                depth_usd=depth,
                fee_bps=self.config.fee_bps,
                slippage_bps=self.config.slippage_bps,
                expected_holding_days=float(row[12]) if row[12] is not None else None,
            )
            state = {
                "venue": row[3], "market_id": row[4],
                "market_probability": round(float(row[7]), 8),
                "bid": round(economics.executable_bid, 8),
                "ask": round(economics.executable_ask, 8),
                "depth": round(depth, 2),
            }
            fingerprint = _fingerprint(state)
            evaluation_key = _fingerprint({"source_shadow_forecast_id": row[0], "state": state})
            date_utc = str(row[2])[:10]
            threshold = adaptive_threshold(
                spread=economics.spread,
                depth_usd=economics.depth_usd,
                holding_days=economics.expected_holding_days,
            )
            try:
                with self.conn:
                    cursor = self.conn.execute(
                        """
                        INSERT INTO revenue_poc_evaluations
                        (evaluation_key,state_fingerprint,source_shadow_forecast_id,run_id,
                         timestamp_utc,venue,market_id,question,category,model_probability,
                         market_probability,executable_bid,executable_ask,spread,depth_usd,
                         depth_source,side,entry_price,raw_edge,executable_edge,
                         expected_value_usd,fee_usd,slippage_usd,spread_cost_usd,
                         capital_required_usd,expected_holding_days,fixed_threshold,
                         adaptive_threshold,adaptive_qualifies,llm_used,production_decision,
                         production_rejection_reason,metadata)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            evaluation_key, fingerprint, row[0], row[1], row[2], row[3],
                            row[4], row[5], row[6], economics.model_probability,
                            economics.market_probability, economics.executable_bid,
                            economics.executable_ask, economics.spread, economics.depth_usd,
                            economics.depth_source, economics.side, economics.entry_price,
                            economics.raw_edge, economics.executable_edge,
                            economics.expected_value_usd, economics.fee_usd,
                            economics.slippage_usd, economics.spread_cost_usd,
                            economics.capital_required_usd, economics.expected_holding_days,
                            self.config.min_executable_edge, threshold,
                            int(economics.executable_edge >= threshold), int(row[14]),
                            row[10], row[11], json.dumps(metadata, sort_keys=True),
                        ),
                    )
                    evaluation_id = int(cursor.lastrowid)
                    self._api_increment(
                        date_utc,
                        api_calls=int(row[14]),
                        estimated_cost=float(row[14]) * self.config.estimated_api_cost_per_call_usd,
                        markets_evaluated=1,
                    )
                counts["evaluated"] += 1
            except sqlite3.IntegrityError as exc:
                if "state_fingerprint" not in str(exc) and "UNIQUE constraint" not in str(exc):
                    raise
                counts["cache_hits"] += 1
                with self.conn:
                    self._api_increment(date_utc, cache_hits=1, calls_avoided=int(row[14]))
                continue

            end_date = _datetime(row[13])
            if end_date is not None and end_date <= self.now:
                self._decision(evaluation_id, row[2], "REJECT", "market_expired", 0.0)
                counts["rejected"] += 1
                continue
            if row[11] in {"skeptic_reject", "temporal_inconsistency"}:
                self._decision(
                    evaluation_id,
                    row[2],
                    "REJECT",
                    f"source_safety_rejection:{row[11]}",
                    0.0,
                )
                counts["rejected"] += 1
                continue
            if economics.executable_edge < self.config.min_executable_edge:
                self._decision(evaluation_id, row[2], "REJECT", "executable_edge_below_2pct", economics.expected_value_usd)
                counts["rejected"] += 1
                continue
            if economics.expected_value_usd <= 0:
                self._decision(evaluation_id, row[2], "REJECT", "non_positive_expected_value", 0.0)
                counts["rejected"] += 1
                continue
            if economics.depth_usd < self.config.position_size_usd:
                self._decision(evaluation_id, row[2], "REJECT", "insufficient_depth", economics.expected_value_usd)
                counts["rejected"] += 1
                continue
            efficiency = economics.expected_value_usd / max(
                economics.capital_required_usd * max(economics.expected_holding_days or 30.0, 1.0),
                0.01,
            )
            candidates.append((efficiency, evaluation_id, row, date_utc))

        for _, evaluation_id, row, date_utc in sorted(candidates, reverse=True, key=lambda item: item[0]):
            admitted, reason = self._admit(evaluation_id)
            self._decision(
                evaluation_id,
                row[2],
                "ADMIT" if admitted else "REJECT",
                reason,
                0.0 if admitted else self._evaluation_ev(evaluation_id),
            )
            if admitted:
                counts["admitted"] += 1
                with self.conn:
                    self._api_increment(date_utc, candidates=1, admitted_trades=1)
            else:
                counts["rejected"] += 1
                with self.conn:
                    self._api_increment(date_utc, candidates=1)
        return counts

    def _api_increment(self, date_utc: str, **values: float | int) -> None:
        columns = ("api_calls", "input_tokens", "output_tokens", "estimated_cost_usd", "cache_hits", "calls_avoided", "markets_evaluated", "candidates", "admitted_trades")
        data = {column: values.get(column, 0) for column in columns}
        self.conn.execute(
            """
            INSERT INTO revenue_poc_api_daily
            (date_utc,api_calls,input_tokens,output_tokens,estimated_cost_usd,
             cache_hits,calls_avoided,markets_evaluated,candidates,admitted_trades)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(date_utc) DO UPDATE SET
              api_calls=api_calls+excluded.api_calls,
              input_tokens=input_tokens+excluded.input_tokens,
              output_tokens=output_tokens+excluded.output_tokens,
              estimated_cost_usd=estimated_cost_usd+excluded.estimated_cost_usd,
              cache_hits=cache_hits+excluded.cache_hits,
              calls_avoided=calls_avoided+excluded.calls_avoided,
              markets_evaluated=markets_evaluated+excluded.markets_evaluated,
              candidates=candidates+excluded.candidates,
              admitted_trades=admitted_trades+excluded.admitted_trades
            """,
            (date_utc, *(data[column] for column in columns)),
        )

    def _decision(self, evaluation_id: int, timestamp: str, decision: str, reason: str, lost_ev: float) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO revenue_poc_decisions (evaluation_id,timestamp_utc,decision,reason,expected_lost_pnl_usd,details) VALUES (?,?,?,?,?,?)",
                (evaluation_id, timestamp, decision, reason, max(0.0, float(lost_ev)), "{}"),
            )

    def _evaluation_ev(self, evaluation_id: int) -> float:
        return float(self.conn.execute("SELECT expected_value_usd FROM revenue_poc_evaluations WHERE id=?", (evaluation_id,)).fetchone()[0])

    def _admit(self, evaluation_id: int) -> tuple[bool, str]:
        row = self.conn.execute(
            "SELECT timestamp_utc,venue,market_id,question,category,side,entry_price,model_probability,fee_usd,slippage_usd,spread_cost_usd,expected_value_usd,expected_holding_days FROM revenue_poc_evaluations WHERE id=?",
            (evaluation_id,),
        ).fetchone()
        open_count, deployed = self.conn.execute(
            "SELECT COUNT(*),COALESCE(SUM(size_usd),0) FROM revenue_poc_positions WHERE status='OPEN'"
        ).fetchone()
        if open_count >= self.config.max_open_positions:
            return False, "max_open_positions"
        if float(deployed) + self.config.position_size_usd > self.config.max_capital_deployed_usd:
            return False, "max_capital_deployed"
        category_count = self.conn.execute(
            "SELECT COUNT(*) FROM revenue_poc_positions WHERE status='OPEN' AND category=?", (row[4],)
        ).fetchone()[0]
        if int(category_count) >= self.config.max_category_positions:
            return False, "max_category_exposure"
        if self.conn.execute("SELECT 1 FROM revenue_poc_positions WHERE venue=? AND market_id=?", (row[1], row[2])).fetchone():
            return False, "one_position_per_contract"
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO revenue_poc_positions
                    (evaluation_id,opened_at_utc,venue,market_id,question,category,side,
                     entry_price,model_probability,size_usd,fee_usd,slippage_usd,
                     spread_cost_usd,expected_value_usd,expected_holding_days)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (evaluation_id,row[0],row[1],row[2],row[3],row[4],row[5],row[6],row[7],
                     self.config.position_size_usd,row[8],row[9],row[10],row[11],row[12]),
                )
        except sqlite3.IntegrityError:
            return False, "one_position_per_contract"
        return True, "admitted_executable_edge"

    def resolve_position(self, position_id: int, outcome: str, resolved_at_utc: str | None = None) -> bool:
        outcome = outcome.upper()
        if outcome not in {"YES", "NO"}:
            raise ValueError("outcome must be YES or NO")
        row = self.conn.execute(
            "SELECT side,entry_price,size_usd,fee_usd,slippage_usd,status FROM revenue_poc_positions WHERE id=?",
            (position_id,),
        ).fetchone()
        if row is None or row[5] != "OPEN":
            return False
        won = row[0] == outcome
        pnl = float(row[2]) * (1.0 / float(row[1]) - 1.0) if won else -float(row[2])
        pnl -= float(row[3]) + float(row[4])
        with self.conn:
            self.conn.execute(
                "UPDATE revenue_poc_positions SET status='RESOLVED',resolved_outcome=?,resolved_at_utc=?,realized_pnl_usd=? WHERE id=? AND status='OPEN'",
                (outcome, resolved_at_utc or _utc_now(), pnl, position_id),
            )
        return True
