"""Deterministic, read-only replay of persisted revenue evaluations.

This deliberately replays the admission lane only.  Execution economics are
the immutable observations captured in ``revenue_poc_evaluations``; replay
does not fetch quotes, call models, or create ledger records.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import RevenueConfig
from .service import (
    _CATEGORY_VELOCITY_MAX_HOLDING_DAYS,
    _CATEGORY_VELOCITY_MIN_EXECUTABLE_EDGE,
    _threshold_market_theme,
)


@dataclass(frozen=True)
class ReplayPolicy:
    position_size_usd: float
    max_open_positions: int
    max_capital_deployed_usd: float
    max_category_positions: int
    min_executable_edge: float

    @classmethod
    def from_config(cls, config: RevenueConfig) -> "ReplayPolicy":
        return cls(**{key: getattr(config, key) for key in cls.__annotations__})

    def with_overrides(self, **overrides: Any) -> "ReplayPolicy":
        values = asdict(self)
        values.update({key: value for key, value in overrides.items() if value is not None})
        policy = ReplayPolicy(**values)
        RevenueConfig(
            starting_balance_usd=max(policy.max_capital_deployed_usd, 1.0),
            position_size_usd=policy.position_size_usd,
            max_open_positions=policy.max_open_positions,
            max_capital_deployed_usd=policy.max_capital_deployed_usd,
            max_category_positions=policy.max_category_positions,
            min_executable_edge=policy.min_executable_edge,
        ).validate()
        return policy


def open_readonly(path: str | Path) -> sqlite3.Connection:
    """Open a SQLite database without write capability, even accidentally."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def baseline_policy(conn: sqlite3.Connection) -> ReplayPolicy:
    """Use the persisted production account config, falling back to defaults."""
    try:
        row = conn.execute("SELECT config_json FROM revenue_poc_accounts WHERE id=1").fetchone()
    except sqlite3.OperationalError:
        row = None
    if row:
        try:
            saved = json.loads(row[0])
            return ReplayPolicy.from_config(RevenueConfig(**saved))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return ReplayPolicy.from_config(RevenueConfig())


def _rows(conn: sqlite3.Connection, start: str, end: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """SELECT id,timestamp_utc,venue,market_id,question,category,
                  executable_edge,expected_value_usd,depth_usd,
                  capital_required_usd,expected_holding_days,
                  CASE WHEN production_decision LIKE 'execution_validation_failed:%'
                            OR production_rejection_reason LIKE 'execution_validation_failed:%'
                            OR EXISTS (
                                SELECT 1 FROM revenue_poc_decisions d
                                WHERE d.evaluation_id=revenue_poc_evaluations.id
                                  AND d.reason LIKE 'execution_validation_failed:%'
                            )
                       THEN 1 ELSE 0 END AS execution_validation_failed
           FROM revenue_poc_evaluations
           WHERE timestamp_utc >= ? AND timestamp_utc < ?
           ORDER BY timestamp_utc,id""",
        (start, end),
    ).fetchall()


def _historical_actuals(
    conn: sqlite3.Connection, start: str, end: str, min_executable_edge: float
) -> tuple[int, Counter[str]]:
    """Return persisted decisions, rather than inferring old policy from today."""
    admissions = 0
    rejections: Counter[str] = Counter()
    try:
        for decision, reason, count in conn.execute(
            """SELECT d.decision,d.reason,COUNT(*) FROM revenue_poc_decisions d
               JOIN revenue_poc_evaluations e ON e.id=d.evaluation_id
               WHERE e.timestamp_utc >= ? AND e.timestamp_utc < ?
                 AND e.executable_edge >= ?
               GROUP BY d.decision,d.reason""",
            (start, end, min_executable_edge),
        ):
            if str(decision).upper() == "ADMIT":
                admissions += int(count)
            elif str(decision).upper() == "REJECT":
                rejections[str(reason)] += int(count)
    except sqlite3.OperationalError:
        # Older read-only databases may predate the decision ledger.  Their
        # current-policy simulation remains valid, but actuals are unknown.
        pass
    return admissions, rejections


def replay(conn: sqlite3.Connection, *, start: str, end: str, policy: ReplayPolicy) -> dict[str, Any]:
    """Replay the production admission ordering against an immutable time window."""
    rows = _rows(conn, start, end)
    failures: Counter[str] = Counter()
    try:
        for reason, count in conn.execute(
            """SELECT d.reason,COUNT(*) FROM revenue_poc_decisions d
               JOIN revenue_poc_evaluations e ON e.id=d.evaluation_id
               WHERE e.timestamp_utc >= ? AND e.timestamp_utc < ?
                 AND d.reason LIKE 'execution_validation_failed:%'
               GROUP BY d.reason""",
            (start, end),
        ):
            failures[str(reason)] += int(count)
    except sqlite3.OperationalError:
        pass

    historical_actual_admissions, historical_rejections = _historical_actuals(
        conn, start, end, policy.min_executable_edge
    )
    reasons: Counter[str] = Counter()
    eligible: list[sqlite3.Row] = []
    execution_invalid = 0
    for row in rows:
        edge, ev, depth = float(row["executable_edge"]), float(row["expected_value_usd"]), float(row["depth_usd"])
        if row["execution_validation_failed"]:
            # Execution validation is an observed prerequisite, not an
            # admission-policy choice.  Never let a simulated policy admit it.
            if edge >= policy.min_executable_edge:
                execution_invalid += 1
        elif edge < policy.min_executable_edge:
            reasons["executable_edge_below_threshold"] += 1
        elif ev <= 0:
            reasons["non_positive_expected_value"] += 1
        elif depth < policy.position_size_usd:
            reasons["insufficient_depth"] += 1
        else:
            eligible.append(row)

    # This mirrors RevenuePOCService.ingest_shadow_forecasts: admissible rows
    # are ranked globally by EV/capital-day efficiency, then passed to _admit.
    ranked = sorted(
        eligible,
        key=lambda row: float(row["expected_value_usd"]) / max(
            float(row["capital_required_usd"]) * max(float(row["expected_holding_days"] or 30.0), 1.0), .01
        ),
        reverse=True,
    )
    admitted: list[sqlite3.Row] = []
    contracts: set[tuple[str, str]] = set()
    category_questions: dict[str, list[str]] = {}
    deployed = 0.0
    for row in ranked:
        category = str(row["category"])
        if len(admitted) >= policy.max_open_positions:
            reasons["max_open_positions"] += 1
        elif deployed + policy.position_size_usd > policy.max_capital_deployed_usd:
            reasons["max_capital_deployed"] += 1
        elif (str(row["venue"]), str(row["market_id"])) in contracts:
            reasons["one_position_per_contract"] += 1
        elif len(category_questions.get(category, [])) >= policy.max_category_positions:
            holding = row["expected_holding_days"]
            theme = _threshold_market_theme(str(row["question"]))
            if holding is None or float(holding) > _CATEGORY_VELOCITY_MAX_HOLDING_DAYS:
                reasons["max_category_exposure_short_horizon_required"] += 1
            elif float(row["executable_edge"]) < _CATEGORY_VELOCITY_MIN_EXECUTABLE_EDGE:
                reasons["max_category_exposure_velocity_edge_required"] += 1
            elif theme is None:
                reasons["max_category_exposure_unclassified_theme"] += 1
            elif any(_threshold_market_theme(question) == theme for question in category_questions[category]):
                reasons["max_category_exposure_correlated_theme"] += 1
            else:
                admitted.append(row); contracts.add((str(row["venue"]), str(row["market_id"])))
                category_questions.setdefault(category, []).append(str(row["question"])); deployed += policy.position_size_usd
        else:
            admitted.append(row); contracts.add((str(row["venue"]), str(row["market_id"])))
            category_questions.setdefault(category, []).append(str(row["question"])); deployed += policy.position_size_usd

    edges = [float(row["executable_edge"]) for row in rows]
    holding = [float(row["expected_holding_days"]) for row in admitted if row["expected_holding_days"] is not None]
    return {
        "policy": asdict(policy), "evaluations": len(rows), "qualifying_executable_opportunities": len(eligible),
        "qualifying_valid_execution_opportunities": len(eligible),
        "execution_invalid_opportunities_excluded_from_simulation": execution_invalid,
        "historical_actual_admissions": historical_actual_admissions,
        "historical_actual_rejection_reasons": dict(sorted(historical_rejections.items())),
        "executable_edge_counts": {label: sum(edge >= threshold for edge in edges) for label, threshold in (("2pct", .02), ("3pct", .03), ("4pct", .04))},
        "hypothetical_admissions": len(admitted), "unique_contracts_admitted": len(contracts),
        "capital_deployed_usd": round(deployed, 2),
        "average_executable_edge": round(sum(edges) / len(edges), 6) if edges else None,
        "best_executable_edge": round(max(edges), 6) if edges else None,
        "average_holding_days": round(sum(holding) / len(holding), 3) if holding else None,
        "modeled_ev_usd": round(sum(float(row["expected_value_usd"]) for row in admitted), 2),
        "category_exposure": dict(sorted((key, len(value)) for key, value in category_questions.items())),
        "rejection_reasons": dict(sorted(reasons.items())),
        "duplicate_or_correlated_theme_blocks": sum(value for key, value in reasons.items() if key in {"one_position_per_contract", "max_category_exposure_correlated_theme"}),
        "execution_failures": dict(sorted(failures.items())),
    }


def format_report(baseline: dict[str, Any], candidate: dict[str, Any]) -> str:
    lines = [
        "REVENUE REPLAY (READ-ONLY)",
        "Historical actual decisions are persisted outcomes. Current-policy columns are hypothetical simulations, not a historical-policy baseline.",
        "Exact historical-policy reproduction is not claimed: the persisted account configuration may not be the policy active during this interval.",
        "metric | CURRENT POLICY | CANDIDATE POLICY",
        "--- | ---: | ---:",
    ]
    for key in ("evaluations", "qualifying_valid_execution_opportunities", "execution_invalid_opportunities_excluded_from_simulation", "hypothetical_admissions", "unique_contracts_admitted", "capital_deployed_usd", "average_executable_edge", "best_executable_edge", "average_holding_days", "modeled_ev_usd", "duplicate_or_correlated_theme_blocks"):
        lines.append(f"{key} | {baseline[key]} | {candidate[key]}")
    lines.append(f"historical actual admissions (persisted) | {baseline['historical_actual_admissions']} | {candidate['historical_actual_admissions']}")
    lines.append(f"historical actual rejection reasons (persisted) | {baseline['historical_actual_rejection_reasons']} | {candidate['historical_actual_rejection_reasons']}")
    lines.append(f"edge >= 2% / 3% / 4% | {baseline['executable_edge_counts']} | {candidate['executable_edge_counts']}")
    lines.append(f"category exposure | {baseline['category_exposure']} | {candidate['category_exposure']}")
    lines.append(f"rejection reasons | {baseline['rejection_reasons']} | {candidate['rejection_reasons']}")
    lines.append(f"execution failures | {baseline['execution_failures']} | {candidate['execution_failures']}")
    return "\n".join(lines)
