from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .config import RevenueConfig
from .economics import adaptive_threshold, evaluate_execution
from .repository import apply_schema, json_object


OFFICIAL_TAKER_FEE_RATES = {
    "crypto": 0.07,
    "sports": 0.05,
    "politics": 0.04,
    "legal": 0.04,
    "macro/fed": 0.05,
    "macro/econ": 0.05,
    "geopolitics": 0.0,
    "novelty/other": 0.05,
}


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


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _horizon_bucket(days: float | None) -> str:
    if days is None:
        return "UNKNOWN"
    if days <= 3:
        return "FAST"
    if days <= 14:
        return "WEEKLY"
    if days <= 45:
        return "MONTHLY"
    return "LONG"


def _execution_sources(
    snapshot: dict[str, Any],
    *,
    category: str,
    fallback_timestamp: str,
    fallback_fee_bps: float,
) -> dict[str, Any]:
    bid = snapshot.get("best_bid", snapshot.get("bestBid"))
    ask = snapshot.get("best_ask", snapshot.get("bestAsk"))
    quote_timestamp = snapshot.get("quote_timestamp_utc") or snapshot.get("updatedAt")
    fee_rate = _float_or_none(
        snapshot.get("taker_fee_rate", snapshot.get("feeRate"))
    )
    fees_enabled = snapshot.get("fees_enabled", snapshot.get("feesEnabled"))
    if fees_enabled is False:
        fee_rate, fee_source = 0.0, "venue_market_fee_flag"
    elif fee_rate is not None:
        fee_source = "venue_market_fee_rate"
    elif fees_enabled is True:
        fee_rate = OFFICIAL_TAKER_FEE_RATES.get(category, 0.05)
        fee_source = "official_category_schedule_assumption"
    else:
        fee_rate = None
        fee_source = "configured_fee_bps_assumption"
    return {
        "bid": _float_or_none(bid),
        "ask": _float_or_none(ask),
        "bid_source": "venue_top_of_book" if bid is not None else "derived_mid_spread_assumption",
        "ask_source": "venue_top_of_book" if ask is not None else "derived_mid_spread_assumption",
        "quote_timestamp_utc": str(quote_timestamp or fallback_timestamp),
        "quote_timestamp_source": "venue" if quote_timestamp else "forecast_timestamp_assumption",
        "fee_rate": fee_rate,
        "fee_source": fee_source,
        "fallback_fee_bps": fallback_fee_bps,
    }


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

    def remaining_api_budget(self, date_utc: str) -> float | None:
        row = self.conn.execute(
            "SELECT estimated_cost_usd,unknown_cost_calls FROM revenue_poc_api_daily WHERE date_utc=?",
            (date_utc,),
        ).fetchone()
        if row and int(row[1] or 0) > 0:
            return None
        spent = float(row[0] or 0.0) if row else 0.0
        return max(0.0, self.config.daily_api_budget_usd - spent)

    def can_spend_api(self, date_utc: str, estimated_call_cost_usd: float) -> bool:
        remaining = self.remaining_api_budget(date_utc)
        return remaining is not None and max(0.0, estimated_call_cost_usd) <= remaining

    def ingest_shadow_forecasts(
        self,
        *,
        execution_quote_provider: Any = None,
        source_run_id: str | None = None,
    ) -> dict[str, int]:
        """Convert immutable shadow observations into executable paper decisions."""
        self.initialize()

        if source_run_id is None:
            rows = self.conn.execute(
                """
                SELECT id,run_id,timestamp_utc,venue,market_id,question,category,
                       market_probability,model_probability,absolute_edge,
                       production_decision,rejection_reason,time_to_resolution_days,
                       market_end_date,llm_used,metadata
                FROM shadow_forecasts
                ORDER BY timestamp_utc,id
                """
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT id,run_id,timestamp_utc,venue,market_id,question,category,
                       market_probability,model_probability,absolute_edge,
                       production_decision,rejection_reason,time_to_resolution_days,
                       market_end_date,llm_used,metadata
                FROM shadow_forecasts
                WHERE run_id=?
                ORDER BY timestamp_utc,id
                """,
                (source_run_id,),
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
            sources = _execution_sources(
                snapshot,
                category=str(row[6]),
                fallback_timestamp=str(row[2]),
                fallback_fee_bps=self.config.fee_bps,
            )
            depth = float(
                snapshot.get("depth_usd")
                or snapshot.get("liquidity")
                or scoring_raw.get("liquidity")
                or 0.0
            )
            depth_source = str(
                snapshot.get("depth_source")
                or (
                    "venue_order_book"
                    if snapshot.get("depth_usd") is not None
                    else "liquidity_proxy_assumption"
                )
            )
            economics = evaluate_execution(
                model_probability=float(row[8]),
                market_probability=float(row[7]),
                stake_usd=self.config.position_size_usd,
                best_bid=sources["bid"],
                best_ask=sources["ask"],
                spread=spread,
                depth_usd=depth,
                fee_rate=sources["fee_rate"],
                fee_bps=self.config.fee_bps,
                slippage_bps=self.config.slippage_bps,
                expected_holding_days=float(row[12]) if row[12] is not None else None,
            )
            execution_validation_failure: str | None = None

            if execution_quote_provider is not None:
                try:
                    quote = execution_quote_provider(
                        market_id=str(row[4]),
                        category=str(row[6]),
                        model_probability=float(row[8]),
                        expected_holding_days=(
                            float(row[12]) if row[12] is not None else None
                        ),
                        position_size_usd=self.config.position_size_usd,
                    )

                    if not isinstance(quote, dict):
                        raise ValueError(
                            "execution quote provider returned no usable quote"
                        )

                    bid = float(quote["best_bid"])
                    ask = float(quote["best_ask"])
                    depth = float(quote["depth_usd"])

                    fresh_market_probability = (bid + ask) / 2.0

                    economics = evaluate_execution(
                        model_probability=float(row[8]),
                        market_probability=fresh_market_probability,
                        stake_usd=self.config.position_size_usd,
                        best_bid=bid,
                        best_ask=ask,
                        depth_usd=depth,
                        fee_rate=_float_or_none(quote.get("fee_rate")),
                        fee_bps=self.config.fee_bps,
                        slippage_bps=self.config.slippage_bps,
                        expected_holding_days=(
                            float(row[12]) if row[12] is not None else None
                        ),
                    )

                    validated_side = str(
                        quote.get("validated_side") or ""
                    ).upper()

                    if validated_side and validated_side != economics.side:
                        raise ValueError(
                            "validated execution side does not match fresh economics"
                        )

                    spread = economics.spread
                    depth_source = str(
                        quote.get("depth_source")
                        or "venue_clob_top_level"
                    )

                    sources = {
                        "bid": bid,
                        "ask": ask,
                        "quote_timestamp_utc": str(
                            quote.get("quote_timestamp_utc") or ""
                        ),
                        "quote_timestamp_source": "venue",
                        "bid_source": "venue_top_of_book",
                        "ask_source": "venue_top_of_book",
                        "fee_source": str(
                            quote.get("fee_source") or "unknown"
                        ),
                        "fee_rate": _float_or_none(
                            quote.get("fee_rate")
                        ),
                        "fallback_fee_bps": self.config.fee_bps,
                    }

                    metadata["execution_validation"] = {
                        "status": "FRESH_CLOB_VALIDATED",
                        "quote_source": quote.get("quote_source"),
                        "quote_timestamp_utc": quote.get(
                            "quote_timestamp_utc"
                        ),
                        "quote_age_seconds": quote.get(
                            "quote_age_seconds"
                        ),
                        "book_state_age_seconds": quote.get(
                            "book_state_age_seconds"
                        ),
                        "depth_source": depth_source,
                        "depth_usd": depth,
                        "side": economics.side,
                        "entry_price": economics.entry_price,
                        "executable_edge": economics.executable_edge,
                        "expected_value_usd": economics.expected_value_usd,
                    }

                except Exception as exc:
                    execution_validation_failure = (
                        f"{type(exc).__name__}:{exc}"
                    )
                    metadata["execution_validation"] = {
                        "status": "FAILED",
                        "error": execution_validation_failure,
                    }

            state = {
                "venue": row[3], "market_id": row[4],
                "model_probability": round(float(row[8]), 8),
                "market_probability": round(float(row[7]), 8),
                "bid": round(economics.executable_bid, 8),
                "ask": round(economics.executable_ask, 8),
                "depth": round(depth, 2),
                "fee_rate": sources["fee_rate"],
                "fallback_fee_bps": self.config.fee_bps,
                "slippage_bps": self.config.slippage_bps,
                "expected_holding_days": economics.expected_holding_days,
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
                         depth_source,quote_timestamp_utc,quote_timestamp_source,
                         bid_source,ask_source,fee_source,fee_rate,
                         side,entry_price,raw_edge,executable_edge,
                         expected_value_usd,fee_usd,slippage_usd,spread_cost_usd,
                         capital_required_usd,expected_holding_days,fixed_threshold,
                         adaptive_threshold,adaptive_qualifies,llm_used,production_decision,
                         production_rejection_reason,metadata)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            evaluation_key, fingerprint, row[0], row[1], row[2], row[3],
                            row[4], row[5], row[6], economics.model_probability,
                            economics.market_probability, economics.executable_bid,
                            economics.executable_ask, economics.spread, economics.depth_usd,
                            depth_source, sources["quote_timestamp_utc"],
                            sources["quote_timestamp_source"], sources["bid_source"],
                            sources["ask_source"], sources["fee_source"],
                            sources["fee_rate"], economics.side, economics.entry_price,
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
                    self._record_attribution_entry(evaluation_id, row, economics, metadata)
                    self._record_threshold_experiments(evaluation_id, economics, threshold)
                    self._api_increment(date_utc, markets_evaluated=1)
                    if int(row[14]):
                        usage_value = metadata.get("anthropic_usage")
                        usage_events = (
                            usage_value if isinstance(usage_value, list) else [usage_value]
                        )
                        for usage_event in usage_events:
                            self._record_api_call(
                                source_shadow_forecast_id=int(row[0]),
                                timestamp_utc=str(row[2]),
                                usage=usage_event,
                            )
                counts["evaluated"] += 1
            except sqlite3.IntegrityError as exc:
                if "state_fingerprint" not in str(exc) and "UNIQUE constraint" not in str(exc):
                    raise
                counts["cache_hits"] += 1
                with self.conn:
                    self._api_increment(date_utc, cache_hits=1, calls_avoided=int(row[14]))
                continue

            if execution_validation_failure is not None:
                self._decision(
                    evaluation_id,
                    row[2],
                    "REJECT",
                    f"execution_validation_failed:{execution_validation_failure}",
                    economics.expected_value_usd,
                    details={
                        "execution_validation_status": "FAILED",
                        "execution_validation_error": execution_validation_failure,
                    },
                )
                counts["rejected"] += 1
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

    def _record_api_call(
        self,
        *,
        source_shadow_forecast_id: int,
        timestamp_utc: str,
        usage: Any,
    ) -> None:
        telemetry = usage if isinstance(usage, dict) else {}
        known = bool(telemetry)
        call_key = _fingerprint(
            {"source_shadow_forecast_id": source_shadow_forecast_id, "operation": telemetry.get("operation", "forecast")}
        )
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO revenue_poc_api_calls
            (call_key,source_shadow_forecast_id,timestamp_utc,operation,model,
             input_tokens,output_tokens,cache_creation_input_tokens,
             cache_read_input_tokens,estimated_cost_usd,pricing_source,telemetry_status,
             routing_tier,decision_changed,estimated_cache_savings_usd)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                call_key,
                source_shadow_forecast_id,
                timestamp_utc,
                str(telemetry.get("operation") or "forecast"),
                telemetry.get("model"),
                telemetry.get("input_tokens"),
                telemetry.get("output_tokens"),
                telemetry.get("cache_creation_input_tokens"),
                telemetry.get("cache_read_input_tokens"),
                telemetry.get("estimated_cost_usd"),
                str(telemetry.get("pricing_source") or "historical_unknown"),
                "observed" if known else "historical_unknown",
                str(telemetry.get("routing_tier") or "legacy"),
                int(bool(telemetry.get("decision_changed", False))),
                telemetry.get("estimated_cache_savings_usd"),
            ),
        )
        if cursor.rowcount != 1:
            return
        self._api_increment(
            timestamp_utc[:10],
            api_calls=1,
            input_tokens=int(telemetry.get("input_tokens") or 0),
            output_tokens=int(telemetry.get("output_tokens") or 0),
            cache_creation_input_tokens=int(
                telemetry.get("cache_creation_input_tokens") or 0
            ),
            cache_read_input_tokens=int(telemetry.get("cache_read_input_tokens") or 0),
            estimated_cost_usd=telemetry.get("estimated_cost_usd"),
            unknown_cost_calls=0 if telemetry.get("estimated_cost_usd") is not None else 1,
        )

    def _api_increment(self, date_utc: str, **values: float | int | None) -> None:
        columns = (
            "api_calls", "input_tokens", "output_tokens",
            "cache_creation_input_tokens", "cache_read_input_tokens",
            "estimated_cost_usd",
            "unknown_cost_calls", "cache_hits", "calls_avoided", "markets_evaluated",
            "candidates", "admitted_trades",
        )
        data = {column: values.get(column, 0) for column in columns}
        if "estimated_cost_usd" not in values:
            data["estimated_cost_usd"] = None
        self.conn.execute(
            """
            INSERT INTO revenue_poc_api_daily
            (date_utc,api_calls,input_tokens,output_tokens,
             cache_creation_input_tokens,cache_read_input_tokens,estimated_cost_usd,
             unknown_cost_calls,cache_hits,calls_avoided,markets_evaluated,candidates,admitted_trades)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(date_utc) DO UPDATE SET
              api_calls=api_calls+excluded.api_calls,
              input_tokens=input_tokens+excluded.input_tokens,
              output_tokens=output_tokens+excluded.output_tokens,
              cache_creation_input_tokens=cache_creation_input_tokens+excluded.cache_creation_input_tokens,
              cache_read_input_tokens=cache_read_input_tokens+excluded.cache_read_input_tokens,
              estimated_cost_usd=CASE
                WHEN excluded.estimated_cost_usd IS NULL THEN estimated_cost_usd
                ELSE COALESCE(estimated_cost_usd,0)+excluded.estimated_cost_usd END,
              unknown_cost_calls=unknown_cost_calls+excluded.unknown_cost_calls,
              cache_hits=cache_hits+excluded.cache_hits,
              calls_avoided=calls_avoided+excluded.calls_avoided,
              markets_evaluated=markets_evaluated+excluded.markets_evaluated,
              candidates=candidates+excluded.candidates,
              admitted_trades=admitted_trades+excluded.admitted_trades
            """,
            (date_utc, *(data[column] for column in columns)),
        )

    def _decision(
        self,
        evaluation_id: int,
        timestamp: str,
        decision: str,
        reason: str,
        lost_ev: float,
        details: dict[str, Any] | None = None,
    ) -> None:
        row = self.conn.execute(
            "SELECT model_probability,executable_bid,executable_ask,executable_edge,"
            "expected_value_usd,capital_required_usd,expected_holding_days,metadata "
            "FROM revenue_poc_evaluations WHERE id=?",
            (evaluation_id,),
        ).fetchone()
        payload = {
            "model_probability": float(row[0]) if row else None,
            "executable_price": float(row[2]) if row else None,
            "net_executable_edge": float(row[3]) if row else None,
            "modeled_ev_usd": float(row[4]) if row else None,
            "capital_required_usd": float(row[5]) if row else None,
            "expected_holding_days": float(row[6]) if row and row[6] is not None else None,
            "estimated_api_cost_usd": None,
            "skip_classification": reason,
        }
        if details:
            payload.update(details)
        with self.conn:
            self.conn.execute(
                "INSERT INTO revenue_poc_decisions (evaluation_id,timestamp_utc,decision,reason,expected_lost_pnl_usd,details) VALUES (?,?,?,?,?,?)",
                (evaluation_id, timestamp, decision, reason, max(0.0, float(lost_ev)), json.dumps(payload, sort_keys=True)),
            )

    def _record_attribution_entry(
        self, evaluation_id: int, source_row: Any, economics: Any, metadata: dict[str, Any]
    ) -> None:
        source = metadata.get("source_metadata")
        source = source if isinstance(source, dict) else {}
        source_type = str(source.get("source_type") or metadata.get("source_type") or "shadow_forecast")
        source_id = str(source.get("source_id") or metadata.get("source_id") or f"shadow_forecast:{int(source_row[0])}")
        event_family = str(source.get("event_family_id") or metadata.get("event_family_id") or source.get("series") or f"market:{source_row[4]}")
        side = str(economics.side)
        benchmark = float(source_row[7]) if side == "YES" else 1.0 - float(source_row[7])
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO revenue_poc_attribution_entries
                (evaluation_id,recorded_at_utc,strategy_id,strategy_version,source_id,
                 source_type,event_family_id,category,horizon_bucket,entry_benchmark_price,
                 entry_benchmark_source,attribution_basis_version,metadata)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (evaluation_id, str(source_row[2]), "revenue_poc", "revenue-poc-v1",
                 source_id, source_type, event_family, str(source_row[6]),
                 _horizon_bucket(economics.expected_holding_days), benchmark,
                 "market_probability_normalized_to_side", "alpha-attribution-v1",
                 json.dumps({"proxy_labels": ["source_id", "event_family_id"]}, sort_keys=True)),
            )

    def _record_attribution_completion(
        self, position_id: int, outcome: str, resolved_at: str, pnl: float,
        realized_fee: float, realized_slippage: float,
    ) -> None:
        row = self.conn.execute(
            """
            SELECT p.evaluation_id,p.opened_at_utc,p.entry_price,p.size_usd,p.side,
                   p.model_probability,a.id,a.entry_benchmark_price
            FROM revenue_poc_positions p
            LEFT JOIN revenue_poc_attribution_entries a ON a.evaluation_id=p.evaluation_id
            WHERE p.id=?
            """, (position_id,),
        ).fetchone()
        if row is None or row[6] is None:
            return
        mark = self.conn.execute(
            "SELECT mark_price FROM revenue_poc_marks WHERE position_id=? ORDER BY quote_timestamp_utc DESC,id DESC LIMIT 1",
            (position_id,),
        ).fetchone()
        exit_benchmark = float(mark[0]) if mark else None
        shares = float(row[3]) / float(row[2])
        settlement = 1.0 if str(row[4]) == outcome else 0.0
        model = float(row[5]) if str(row[4]) == "YES" else 1.0 - float(row[5])
        benchmark = float(row[7])
        forecast = (model - benchmark) * shares
        resolution = (settlement - model) * shares
        structural = (benchmark - float(row[2])) * shares - realized_fee - realized_slippage
        elapsed = max(0.0, ((_datetime(resolved_at) or self.now) - (_datetime(row[1]) or self.now)).total_seconds() / 86400.0)
        self.conn.execute(
            """
            INSERT OR IGNORE INTO revenue_poc_attribution_completions
            (entry_id,position_id,recorded_at_utc,exit_benchmark_price,exit_benchmark_source,
             settlement_outcome,settlement_price,resolved_at_utc,capital_days,fees_usd,
             slippage_usd,close_classification,forecast_alpha_usd,event_alpha_usd,
             resolution_alpha_usd,structural_alpha_usd,realized_net_pnl_usd,
             attribution_status,metadata)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (int(row[6]), position_id, _utc_now(), exit_benchmark,
             "last_executable_mark" if exit_benchmark is not None else "settlement_proxy",
             outcome, settlement, resolved_at, float(row[3]) * elapsed,
             realized_fee, realized_slippage, "RESOLUTION", forecast, 0.0,
             resolution, structural, pnl,
             "COMPLETE" if exit_benchmark is not None else "PARTIAL_PROXY",
             json.dumps({"event_alpha": "unavailable_without_peer_benchmark"}, sort_keys=True)),
        )

    def _record_threshold_experiments(self, evaluation_id: int, economics: Any, adaptive: float) -> None:
        thresholds = (("0.5%", 0.005), ("1.0%", 0.01), ("1.5%", 0.015),
                      ("2.0%", 0.02), ("2.5%", 0.025), ("3.0%", 0.03),
                      ("adaptive", float(adaptive)))
        with self.conn:
            for label, threshold in thresholds:
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO revenue_poc_shadow_thresholds
                    (evaluation_id,threshold_label,threshold,qualifies,modeled_ev_usd,
                     capital_required_usd,expected_holding_days,adaptive_threshold)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        evaluation_id, label, threshold,
                        int(float(economics.executable_edge) >= threshold),
                        float(economics.expected_value_usd),
                        float(economics.capital_required_usd),
                        economics.expected_holding_days,
                        float(adaptive),
                    ),
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
                position_id = int(self.conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                self._record_equity_point(str(row[0]), "OPEN", position_id)
        except sqlite3.IntegrityError:
            return False, "one_position_per_contract"
        return True, "admitted_executable_edge"

    def _portfolio_state(self) -> dict[str, float]:
        account = self.conn.execute(
            "SELECT starting_balance_usd FROM revenue_poc_accounts WHERE id=1"
        ).fetchone()
        starting = float(account[0])
        realized = float(
            self.conn.execute(
                "SELECT COALESCE(SUM(realized_pnl_usd),0) FROM revenue_poc_positions WHERE status='RESOLVED'"
            ).fetchone()[0]
        )
        open_rows = self.conn.execute(
            "SELECT id,size_usd,fee_usd,slippage_usd FROM revenue_poc_positions WHERE status='OPEN'"
        ).fetchall()
        deployed = sum(float(row[1]) for row in open_rows)
        entry_costs = sum(float(row[2]) + float(row[3]) for row in open_rows)
        unrealized = 0.0
        for row in open_rows:
            mark = self.conn.execute(
                "SELECT unrealized_pnl_usd FROM revenue_poc_marks WHERE position_id=? ORDER BY quote_timestamp_utc DESC,id DESC LIMIT 1",
                (row[0],),
            ).fetchone()
            unrealized += float(mark[0]) if mark else 0.0
        cash = starting + realized - deployed - entry_costs
        equity = cash + deployed + unrealized
        return {
            "cash_usd": cash,
            "deployed_capital_usd": deployed,
            "realized_pnl_usd": realized,
            "unrealized_pnl_usd": unrealized,
            "equity_usd": equity,
        }

    def _record_equity_point(
        self, timestamp_utc: str, event_type: str, reference_id: int
    ) -> None:
        state = self._portfolio_state()
        prior_peak = self.conn.execute(
            "SELECT MAX(equity_usd) FROM revenue_poc_equity_points"
        ).fetchone()[0]
        peak = max(float(prior_peak or self.config.starting_balance_usd), state["equity_usd"])
        drawdown = max(0.0, peak - state["equity_usd"])
        point_key = _fingerprint(
            {"timestamp": timestamp_utc, "event": event_type, "reference_id": reference_id}
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO revenue_poc_equity_points
            (point_key,timestamp_utc,event_type,reference_id,cash_usd,
             deployed_capital_usd,realized_pnl_usd,unrealized_pnl_usd,
             equity_usd,drawdown_usd)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                point_key, timestamp_utc, event_type, reference_id,
                state["cash_usd"], state["deployed_capital_usd"],
                state["realized_pnl_usd"], state["unrealized_pnl_usd"],
                state["equity_usd"], drawdown,
            ),
        )

    def mark_position(self, position_id: int, quote: dict[str, Any]) -> bool:
        row = self.conn.execute(
            "SELECT side,entry_price,size_usd,status FROM revenue_poc_positions WHERE id=?",
            (position_id,),
        ).fetchone()
        if row is None or row[3] != "OPEN":
            return False
        bid = float(quote["best_bid"])
        ask = float(quote["best_ask"])
        if not 0 < bid <= ask < 1:
            raise ValueError("mark quote must satisfy 0 < bid <= ask < 1")
        timestamp = str(quote["quote_timestamp_utc"])
        fee_rate = _float_or_none(quote.get("fee_rate"))
        mark_price = bid if row[0] == "YES" else 1.0 - ask
        shares = float(row[2]) / float(row[1])
        market_value = shares * mark_price
        gross_unrealized = market_value - float(row[2])
        if fee_rate is not None:
            exit_fee = shares * fee_rate * mark_price * (1.0 - mark_price)
        else:
            exit_fee = market_value * self.config.fee_bps / 10_000.0
        exit_slippage = market_value * self.config.slippage_bps / 10_000.0
        unrealized = gross_unrealized - exit_fee - exit_slippage
        mark_key = _fingerprint(
            {"position_id": position_id, "timestamp": timestamp, "bid": bid, "ask": ask}
        )
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT OR IGNORE INTO revenue_poc_marks
                (mark_key,position_id,quote_timestamp_utc,recorded_at_utc,
                 executable_bid,executable_ask,bid_depth_usd,ask_depth_usd,
                 depth_source,fee_rate,fee_source,quote_source,mark_price,
                 market_value_usd,gross_unrealized_pnl_usd,
                 estimated_exit_fee_usd,estimated_exit_slippage_usd,
                 unrealized_pnl_usd,assumptions_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    mark_key, position_id, timestamp, _utc_now(), bid, ask,
                    quote.get("bid_depth_usd"), quote.get("ask_depth_usd"),
                    str(quote.get("depth_source") or "unavailable_assumption"),
                    fee_rate, str(quote.get("fee_source") or "configured_assumption"),
                    str(quote.get("quote_source") or "fixture"), mark_price,
                    market_value, gross_unrealized, exit_fee, exit_slippage,
                    unrealized, json.dumps(quote.get("assumptions") or {}, sort_keys=True),
                ),
            )
            if cursor.rowcount != 1:
                return False
            mark_id = int(cursor.lastrowid)
            self._record_equity_point(timestamp, "MARK", mark_id)
        return True

    def resolve_position(
        self,
        position_id: int,
        outcome: str,
        resolved_at_utc: str | None = None,
        *,
        resolution_fee_usd: float = 0.0,
        resolution_slippage_usd: float = 0.0,
    ) -> bool:
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
        gross_pnl = float(row[2]) * (1.0 / float(row[1]) - 1.0) if won else -float(row[2])
        realized_fee = float(row[3]) + max(0.0, resolution_fee_usd)
        realized_slippage = float(row[4]) + max(0.0, resolution_slippage_usd)
        pnl = gross_pnl - realized_fee - realized_slippage
        resolved_at = resolved_at_utc or _utc_now()
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE revenue_poc_positions
                SET status='RESOLVED',resolved_outcome=?,resolved_at_utc=?,
                    realized_pnl_usd=?,gross_realized_pnl_usd=?,
                    realized_fee_usd=?,realized_slippage_usd=?
                WHERE id=? AND status='OPEN'
                """,
                (
                    outcome, resolved_at, pnl, gross_pnl, realized_fee,
                    realized_slippage, position_id,
                ),
            )
            if cursor.rowcount != 1:
                return False
            self._record_attribution_completion(
                position_id, outcome, resolved_at, pnl, realized_fee, realized_slippage
            )
            self._record_equity_point(resolved_at, "RESOLUTION", position_id)
        return True

    def resolve_from_shadow(self) -> int:
        resolved = 0
        rows = self.conn.execute(
            "SELECT id,venue,market_id FROM revenue_poc_positions WHERE status='OPEN'"
        ).fetchall()
        for position_id, venue, market_id in rows:
            outcome = self.conn.execute(
                """
                SELECT eventual_outcome,resolved_at_utc FROM shadow_forecasts
                WHERE venue=? AND market_id=? AND status='RESOLVED'
                  AND eventual_outcome IN ('YES','NO')
                ORDER BY resolved_at_utc DESC,id DESC LIMIT 1
                """,
                (venue, market_id),
            ).fetchone()
            if outcome and self.resolve_position(int(position_id), outcome[0], outcome[1]):
                resolved += 1
        return resolved
