from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from operator_console.models import (
    ApiSnapshot,
    ConsoleSnapshot,
    EvaluationSnapshot,
    EventSnapshot,
    PipelineSnapshot,
    PortfolioSnapshot,
    PositionSnapshot,
    SystemSnapshot,
)
from reporting.discovery_breakdown import build_discovery_breakdown
from revenue_poc.reporting import portfolio_dashboard
from swarm_edge_runtime import RuntimePaths, get_runtime_paths

_CYCLE_RE = re.compile(r"^==\s+([^ ]+)\s+:\s+infer loop")
_PID_RE = re.compile(r"\bpid = (\d+)")
_STATE_RE = re.compile(r"\bstate = ([a-zA-Z]+)")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def parse_datetime(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _read_tail(path: Path, max_bytes: int = 256_000) -> list[str]:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        data = handle.read()
    if size > max_bytes:
        data = data.split(b"\n", 1)[-1]
    return data.decode("utf-8", errors="replace").splitlines()


def _safe_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


class OperatorDataSource:
    """Read-only aggregation boundary for the operator console."""

    def __init__(
        self,
        paths: RuntimePaths | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        now: Callable[[], datetime] | None = None,
        launchctl: Callable[[], subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.environ = dict(os.environ if environ is None else environ)
        self.paths = paths or get_runtime_paths(dict(self.environ))
        self.now = now or (lambda: datetime.now(UTC))
        self._launchctl = launchctl or self._launchctl_status
        self.cycle_interval = max(
            1, int(self.environ.get("SLEEP_SECS", "3600") or 3600)
        )
        self._last_valid: ConsoleSnapshot | None = None
        self._cached_log_events: tuple[EventSnapshot, ...] = ()

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{self.paths.db_path.resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=0.25)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=250")
        return conn

    def _launchctl_status(self) -> subprocess.CompletedProcess[str]:
        label = self.environ.get("SWARM_EDGE_LAUNCHD_LABEL", "com.swarmedge.runner")
        domain = self.environ.get("SWARM_EDGE_LAUNCHD_DOMAIN", f"gui/{os.getuid()}")
        return subprocess.run(
            ["launchctl", "print", f"{domain}/{label}"],
            capture_output=True,
            text=True,
            timeout=0.75,
            check=False,
        )

    def _system_status(
        self,
        *,
        database_status: str,
        last_cycle: datetime | None,
        message: str | None = None,
    ) -> SystemSnapshot:
        now = self.now().astimezone(UTC)
        pid: int | None = None
        state = "UNAVAILABLE"
        try:
            result = self._launchctl()
            if result.returncode == 0:
                state_match = _STATE_RE.search(result.stdout)
                state = (state_match.group(1) if state_match else "loaded").upper()
                pid_match = _PID_RE.search(result.stdout)
                pid = int(pid_match.group(1)) if pid_match else None
            else:
                state = "NOT LOADED"
        except (OSError, subprocess.SubprocessError, ValueError):
            state = "UNAVAILABLE"

        age = max(0.0, (now - last_cycle).total_seconds()) if last_cycle else None
        stale_after = self.cycle_interval * 1.5
        cycle_state = (
            "UNKNOWN" if age is None else ("STALE" if age > stale_after else "FRESH")
        )
        next_cycle = (
            last_cycle + timedelta(seconds=self.cycle_interval) if last_cycle else None
        )
        error_log = self.paths.log_dir / "infer_loop.err.log"
        error_count = 0
        try:
            error_count = sum(1 for line in _read_tail(error_log) if line.strip())
        except OSError:
            pass
        return SystemSnapshot(
            runtime_status=state,
            launchd_pid=pid,
            release_sha=self._release_sha(),
            last_completed_cycle=last_cycle,
            cycle_age_seconds=age,
            next_expected_cycle=next_cycle,
            cycle_state=cycle_state,
            database_status=database_status,
            error_count=error_count,
            current_time=now,
            message=message,
        )

    def _release_sha(self) -> str:
        roots: list[Path] = []
        configured = self.environ.get("SWARM_EDGE_ROOT")
        if configured:
            roots.append(Path(configured).expanduser())
        roots.append(self.paths.root)
        for root in roots:
            manifest = _safe_json(root / "deployment-manifest.json")
            sha = str(manifest.get("git_sha", ""))
            if _SHA_RE.fullmatch(sha):
                return sha
            try:
                resolved_name = root.resolve().name
            except OSError:
                resolved_name = root.name
            if _SHA_RE.fullmatch(resolved_name):
                return resolved_name
        return "unknown"

    def _latest_cycle(
        self, conn: sqlite3.Connection, pipeline: dict[str, object]
    ) -> datetime | None:
        candidates: list[datetime] = []
        if "revenue_poc_marks" in self._tables(conn):
            value = conn.execute(
                "SELECT MAX(quote_timestamp_utc) FROM revenue_poc_marks"
            ).fetchone()[0]
            parsed = parse_datetime(value)
            if parsed:
                candidates.append(parsed)
        parsed = parse_datetime(pipeline.get("ts_utc"))
        if parsed:
            candidates.append(parsed)
        try:
            for line in _read_tail(self.paths.log_dir / "infer_loop.log"):
                match = _CYCLE_RE.match(line)
                parsed = parse_datetime(match.group(1)) if match else None
                if parsed:
                    candidates.append(parsed)
        except OSError:
            pass
        return max(candidates) if candidates else None

    @staticmethod
    def _tables(conn: sqlite3.Connection) -> set[str]:
        return {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    def _positions(
        self, conn: sqlite3.Connection, now: datetime
    ) -> tuple[PositionSnapshot, ...]:
        rows = conn.execute(
            """
            SELECT p.*,e.run_id,e.source_shadow_forecast_id,e.executable_bid entry_bid,
                   e.executable_ask entry_ask,e.market_probability,e.raw_edge,
                   e.executable_edge,e.spread,
                   m.quote_timestamp_utc,m.executable_bid current_bid,
                   m.executable_ask current_ask,m.mark_price,
                   m.gross_unrealized_pnl_usd,m.estimated_exit_fee_usd,
                   m.estimated_exit_slippage_usd,m.unrealized_pnl_usd,
                   (SELECT d.reason FROM revenue_poc_decisions d
                    WHERE d.evaluation_id=p.evaluation_id AND d.decision='ADMIT'
                    ORDER BY d.id LIMIT 1) admission_reason
            FROM revenue_poc_positions p
            JOIN revenue_poc_evaluations e ON e.id=p.evaluation_id
            LEFT JOIN revenue_poc_marks m ON m.id=(
                SELECT id FROM revenue_poc_marks
                WHERE position_id=p.id ORDER BY quote_timestamp_utc DESC,id DESC LIMIT 1
            )
            ORDER BY p.id
            """
        ).fetchall()
        result: list[PositionSnapshot] = []
        for row in rows:
            opened = parse_datetime(row["opened_at_utc"])
            quote = parse_datetime(row["quote_timestamp_utc"])
            holding = (
                float(row["expected_holding_days"])
                if row["expected_holding_days"] is not None
                else None
            )
            resolution = (
                opened + timedelta(days=holding)
                if opened and holding is not None
                else None
            )
            quote_age = max(0.0, (now - quote).total_seconds()) if quote else None
            quote_state = (
                "NO MARK"
                if quote_age is None
                else ("STALE" if quote_age > self.cycle_interval * 1.5 else "FRESH")
            )
            status = str(row["status"])
            if status == "OPEN":
                gross = (
                    float(row["gross_unrealized_pnl_usd"])
                    if row["gross_unrealized_pnl_usd"] is not None
                    else None
                )
                net = (
                    float(row["unrealized_pnl_usd"])
                    if row["unrealized_pnl_usd"] is not None
                    else None
                )
                fees = float(row["fee_usd"] or 0) + float(
                    row["estimated_exit_fee_usd"] or 0
                )
                slippage = float(row["slippage_usd"] or 0) + float(
                    row["estimated_exit_slippage_usd"] or 0
                )
            else:
                gross = (
                    float(row["gross_realized_pnl_usd"])
                    if row["gross_realized_pnl_usd"] is not None
                    else None
                )
                net = (
                    float(row["realized_pnl_usd"])
                    if row["realized_pnl_usd"] is not None
                    else None
                )
                fees = float(row["realized_fee_usd"] or 0)
                slippage = float(row["realized_slippage_usd"] or 0)
                quote_state = "RESOLVED" if status == "RESOLVED" else status
            result.append(
                PositionSnapshot(
                    id=int(row["id"]),
                    question=str(row["question"]),
                    market_id=str(row["market_id"]),
                    category=str(row["category"]),
                    side=str(row["side"]),
                    stake=float(row["size_usd"]),
                    entry_timestamp=opened,
                    entry_bid=float(row["entry_bid"])
                    if row["entry_bid"] is not None
                    else None,
                    entry_ask=float(row["entry_ask"])
                    if row["entry_ask"] is not None
                    else None,
                    entry_price=float(row["entry_price"]),
                    current_bid=float(row["current_bid"])
                    if row["current_bid"] is not None
                    else None,
                    current_ask=float(row["current_ask"])
                    if row["current_ask"] is not None
                    else None,
                    current_mark=float(row["mark_price"])
                    if row["mark_price"] is not None
                    else None,
                    gross_pnl=gross,
                    fees=fees,
                    slippage=slippage,
                    net_pnl=net,
                    shares=float(row["size_usd"]) / float(row["entry_price"]),
                    model_probability=float(row["model_probability"]),
                    market_probability=float(row["market_probability"]),
                    raw_edge=float(row["raw_edge"]),
                    executable_edge=float(row["executable_edge"]),
                    modeled_ev=float(row["expected_value_usd"]),
                    spread=float(row["spread"]),
                    expected_holding_days=holding,
                    expected_resolution=resolution,
                    source_forecast=(
                        f"shadow:{row['source_shadow_forecast_id']} / {row['run_id']}"
                    ),
                    admission_reason=str(row["admission_reason"] or "admitted"),
                    status=status,
                    quote_timestamp=quote,
                    quote_age_seconds=quote_age,
                    quote_state=quote_state,
                )
            )
        return tuple(
            sorted(result, key=lambda item: abs(item.net_pnl or 0.0), reverse=True)
        )

    def _pipeline(
        self, conn: sqlite3.Connection, report: dict[str, object]
    ) -> PipelineSnapshot:
        summary = (
            report.get("summary") if isinstance(report.get("summary"), dict) else {}
        )
        markets = (
            report.get("markets") if isinstance(report.get("markets"), list) else []
        )
        reasons = Counter(
            str(row.get("reason", "unknown"))
            for row in markets
            if isinstance(row, dict) and row.get("decision") in {"REJECT", "SKIP"}
        )
        revenue_candidates = conn.execute(
            "SELECT COUNT(DISTINCT evaluation_id) FROM revenue_poc_decisions WHERE decision='ADMIT'"
        ).fetchone()[0]
        revenue_admissions = conn.execute(
            "SELECT COUNT(*) FROM revenue_poc_positions"
        ).fetchone()[0]
        cache_hits = conn.execute(
            "SELECT COALESCE(SUM(cache_hits),0) FROM revenue_poc_api_daily"
        ).fetchone()[0]
        eligible = sum(
            1
            for row in markets
            if isinstance(row, dict)
            and row.get("final_stage") not in {"policy_filter", "quality_filter"}
        )
        discovered = int(summary.get("finalized_markets", 0) or len(markets))
        return PipelineSnapshot(
            discovered_markets=discovered,
            eligible_markets=eligible,
            watchlist_size=int(summary.get("watchlist_total", len(markets)) or 0),
            fetched=max(
                0,
                int(summary.get("fetch_attempted", 0) or 0)
                - int(summary.get("fetch_failed", 0) or 0),
            ),
            ranked=int(summary.get("opportunity_scored", 0) or 0),
            evaluated=int(
                summary.get("diagnostics_written", summary.get("evaluated", 0)) or 0
            ),
            llm_calls=int(summary.get("llm_attempted", 0) or 0),
            skeptic_calls=int(summary.get("skeptic_attempted", 0) or 0),
            strict_candidates=int(summary.get("candidates_generated", 0) or 0),
            revenue_candidates=int(revenue_candidates),
            revenue_admissions=int(revenue_admissions),
            cache_hits=int(cache_hits),
            budget_skips=int(summary.get("budget_skipped", 0) or 0),
            dynamic_shortlist_size=int(
                (report.get("optimization") or {}).get("discovery", {}).get("shortlisted", 0)
                if isinstance(report.get("optimization"), dict)
                else 0
            ),
            outside_watchlist=int(
                (report.get("optimization") or {}).get("discovery", {}).get("outside_fixed_watchlist", 0)
                if isinstance(report.get("optimization"), dict)
                else 0
            ),
            modeled_ev_skipped_budget=float(
                summary.get("modeled_ev_skipped_budget", 0) or 0
            ),
            quarantined_markets=int(
                (report.get("optimization") or {}).get("quarantine", {}).get("quarantined", 0)
                if isinstance(report.get("optimization"), dict)
                else 0
            ),
            rejections=dict(reasons.most_common()),
            discovery_breakdown=build_discovery_breakdown(report, conn=conn),
        )

    def _api(
        self, conn: sqlite3.Connection, dashboard: dict[str, object]
    ) -> ApiSnapshot:
        api = dashboard["api"]
        account = conn.execute(
            "SELECT daily_api_budget_usd FROM revenue_poc_accounts WHERE id=1"
        ).fetchone()
        today = self.now().astimezone(UTC).date().isoformat()
        daily = conn.execute(
            "SELECT * FROM revenue_poc_api_daily WHERE date_utc=?", (today,)
        ).fetchone()
        cache_tokens = int(api["cache_creation_input_tokens"]) + int(
            api["cache_read_input_tokens"]
        )
        return ApiSnapshot(
            daily_budget=float(account[0]) if account else 0.0,
            estimated_spend_today=(
                float(daily["estimated_cost_usd"])
                if daily and daily["estimated_cost_usd"] is not None
                else None
            ),
            remaining_budget=api["remaining_daily_budget_usd"],
            calls_today=int(daily["api_calls"]) if daily else 0,
            input_tokens=int(daily["input_tokens"]) if daily else 0,
            output_tokens=int(daily["output_tokens"]) if daily else 0,
            cache_tokens=(
                int(daily["cache_creation_input_tokens"])
                + int(daily["cache_read_input_tokens"])
            )
            if daily
            else cache_tokens,
            calls_avoided=int(api["calls_avoided"]),
            cost_per_evaluation=api["cost_per_evaluated_market"],
            cost_per_candidate=api["cost_per_candidate"],
            cost_per_admitted_trade=api["cost_per_admitted_trade"],
            unknown_historical_calls=int(api["unknown_cost_calls"]),
            measurement=str(api["cost_measurement"]),
            reserved_budget=float(
                (api.get("budget") or {}).get("reserved_usd", 0.0) or 0.0
            ),
            calls_skipped_by_cost=int(
                (api.get("budget") or {}).get("calls_skipped_by_cost", 0) or 0
            ),
            calls_skipped_by_emergency=int(
                (api.get("budget") or {}).get("calls_skipped_by_emergency", 0) or 0
            ),
            calls_by_model=dict(api.get("calls_by_model") or {}),
            provider_cache_savings_usd=float(
                api.get("provider_cache_savings_usd", 0.0) or 0.0
            ),
        )

    def _evaluations(self, conn: sqlite3.Connection) -> tuple[EvaluationSnapshot, ...]:
        rows = conn.execute(
            """
            SELECT d.timestamp_utc,e.market_id,e.question,d.decision,d.reason,
                   e.executable_edge,e.expected_value_usd
            FROM revenue_poc_decisions d
            LEFT JOIN revenue_poc_evaluations e ON e.id=d.evaluation_id
            ORDER BY d.id DESC LIMIT 100
            """
        ).fetchall()
        return tuple(
            EvaluationSnapshot(
                parse_datetime(row[0]),
                str(row[1] or "-"),
                str(row[2] or row[1] or "-"),
                str(row[3]),
                str(row[4]),
                float(row[5]) if row[5] is not None else None,
                float(row[6]) if row[6] is not None else None,
            )
            for row in rows
        )

    def _db_events(self, conn: sqlite3.Connection) -> list[EventSnapshot]:
        events: list[EventSnapshot] = []
        for row in conn.execute(
            "SELECT opened_at_utc,question,side,size_usd FROM revenue_poc_positions ORDER BY id DESC LIMIT 20"
        ):
            events.append(
                EventSnapshot(
                    parse_datetime(row[0]),
                    "OPEN",
                    f"{row[2]} ${float(row[3]):.2f} · {row[1]}",
                )
            )
        for row in conn.execute(
            "SELECT recorded_at_utc,position_id,unrealized_pnl_usd FROM revenue_poc_marks ORDER BY id DESC LIMIT 30"
        ):
            pnl = float(row[2])
            events.append(
                EventSnapshot(
                    parse_datetime(row[0]),
                    "MARK",
                    f"Position {row[1]} MTM {pnl:+.4f}",
                    "GAIN" if pnl >= 0 else "LOSS",
                )
            )
        for row in conn.execute(
            "SELECT resolved_at_utc,question,resolved_outcome,realized_pnl_usd FROM revenue_poc_positions WHERE status='RESOLVED' ORDER BY id DESC LIMIT 20"
        ):
            events.append(
                EventSnapshot(
                    parse_datetime(row[0]),
                    "RESOLVE",
                    f"{row[2]} {float(row[3]):+.4f} · {row[1]}",
                    "INFO",
                )
            )
        for row in conn.execute(
            """
            SELECT d.timestamp_utc,d.decision,d.reason,e.question
            FROM revenue_poc_decisions d
            LEFT JOIN revenue_poc_evaluations e ON e.id=d.evaluation_id
            ORDER BY d.id DESC LIMIT 30
            """
        ):
            decision = str(row[1])
            events.append(
                EventSnapshot(
                    parse_datetime(row[0]),
                    "DECISION",
                    f"{decision} · {row[2]} · {row[3] or 'unknown market'}",
                    "WARNING" if decision == "REJECT" else "INFO",
                )
            )
        return events

    def _log_events(self) -> tuple[EventSnapshot, ...]:
        events: list[EventSnapshot] = []
        current_cycle: datetime | None = None
        try:
            lines = _read_tail(self.paths.log_dir / "infer_loop.log")
        except OSError:
            lines = []
        for line in lines:
            match = _CYCLE_RE.match(line)
            if match:
                current_cycle = parse_datetime(match.group(1))
                events.append(
                    EventSnapshot(current_cycle, "CYCLE", "Inference cycle started")
                )
            elif "LIVE_RUNNER OK" in line:
                events.append(EventSnapshot(current_cycle, "CYCLE", line.strip()))
            elif "RESOLVER SUMMARY" in line:
                events.append(EventSnapshot(current_cycle, "RESOLVER", line.strip()))
            elif "[export]" in line:
                events.append(EventSnapshot(current_cycle, "EXPORT", line.strip()))
            elif "[WARN]" in line or "[FATAL]" in line:
                events.append(
                    EventSnapshot(current_cycle, "WARNING", line.strip(), "WARNING")
                )
        try:
            error_lines = [
                line.strip()
                for line in _read_tail(self.paths.log_dir / "infer_loop.err.log")
                if line.strip()
            ]
        except OSError:
            error_lines = []
        for line in error_lines[-20:]:
            events.append(EventSnapshot(None, "ERROR", line, "ERROR"))
        return tuple(events[-80:])

    def read(self, *, include_logs: bool = True) -> ConsoleSnapshot:
        now = self.now().astimezone(UTC)
        report = _safe_json(self.paths.signals_dir / "infer_pipeline_report.json")
        try:
            with closing(self._connect()) as conn:
                tables = self._tables(conn)
                required = {
                    "revenue_poc_accounts",
                    "revenue_poc_positions",
                    "revenue_poc_evaluations",
                }
                if not required.issubset(tables):
                    raise RuntimeError("Revenue POC schema unavailable")
                dashboard = portfolio_dashboard(conn)
                last_cycle = self._latest_cycle(conn, report)
                p = dashboard["portfolio"]
                perf = dashboard["performance"]
                execution = dashboard["execution"]
                portfolio = PortfolioSnapshot(
                    starting_balance=float(p["starting_balance_usd"]),
                    cash=float(p["cash_usd"]),
                    deployed_capital=float(p["deployed_capital_usd"]),
                    equity=float(p["equity_usd"]),
                    realized_pnl=float(perf["realized_pnl_usd"]),
                    unrealized_pnl=float(perf["unrealized_pnl_usd"]),
                    modeled_open_ev=float(perf["expected_value_open_usd"]),
                    maximum_drawdown=float(perf["maximum_drawdown_usd"]),
                    open_positions=int(p["open_positions"]),
                    resolved_positions=int(p["resolved_positions"]),
                    capital_utilization=float(execution["capital_utilization"] or 0),
                )
                if include_logs:
                    self._cached_log_events = self._log_events()
                events = self._db_events(conn) + list(self._cached_log_events)
                events.sort(
                    key=lambda item: item.timestamp or datetime.min.replace(tzinfo=UTC),
                    reverse=True,
                )
                snapshot = ConsoleSnapshot(
                    system=self._system_status(
                        database_status="OK / READ-ONLY", last_cycle=last_cycle
                    ),
                    portfolio=portfolio,
                    positions=self._positions(conn, now),
                    pipeline=self._pipeline(conn, report),
                    api=self._api(conn, dashboard),
                    events=tuple(events[:100]),
                    evaluations=self._evaluations(conn),
                    raw={
                        "database": str(self.paths.db_path),
                        "logs": str(self.paths.log_dir),
                    },
                )
                self._last_valid = snapshot
                return snapshot
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            message = f"{type(exc).__name__}: {exc}"
            if self._last_valid is not None:
                return replace(
                    self._last_valid,
                    system=self._system_status(
                        database_status="UNAVAILABLE / LAST VALUE RETAINED",
                        last_cycle=self._last_valid.system.last_completed_cycle,
                        message=message,
                    ),
                )
            return ConsoleSnapshot(
                system=self._system_status(
                    database_status="UNAVAILABLE", last_cycle=None, message=message
                ),
                events=(EventSnapshot(self.now(), "ERROR", message, "ERROR"),),
                raw={
                    "database": str(self.paths.db_path),
                    "logs": str(self.paths.log_dir),
                },
            )


def filter_positions(
    positions: Iterable[PositionSnapshot], query: str
) -> tuple[PositionSnapshot, ...]:
    needle = query.strip().casefold()
    if not needle:
        return tuple(positions)
    return tuple(
        item
        for item in positions
        if needle
        in f"{item.question} {item.market_id} {item.category} {item.side} {item.status}".casefold()
    )
