from __future__ import annotations

from datetime import UTC, datetime

from rich.console import Console
from rich.table import Table
from rich.text import Text

from operator_console.models import ConsoleSnapshot
from reporting.discovery_breakdown import render_discovery_breakdown
from revenue_poc.velocity import horizon_for_days


def money(value: float | None, *, signed: bool = False) -> str:
    if value is None:
        return "UNKNOWN"
    return f"${value:+,.4f}" if signed else f"${value:,.4f}"


def probability(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


def percent(value: float | None) -> str:
    return "UNKNOWN" if value is None else f"{value * 100:.2f}%"


def utc_time(value: datetime | None) -> str:
    return (
        "UNKNOWN"
        if value is None
        else value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    )


def age(value: float | None) -> str:
    if value is None:
        return "UNKNOWN"
    seconds = max(0, int(value))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86_400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86_400}d {(seconds % 86_400) // 3600}h"


def state_symbol(value: str) -> str:
    normalized = value.upper()
    if normalized in {"FRESH", "RUNNING", "OK / READ-ONLY", "RESOLVED"}:
        return "✓"
    if normalized in {"STALE", "WARNING", "NO MARK"}:
        return "!"
    if normalized in {"UNAVAILABLE", "ERROR", "NOT LOADED"}:
        return "×"
    return "•"


def pnl_text(value: float | None) -> Text:
    if value is None:
        return Text("—", style="dim")
    symbol = "+" if value >= 0 else "−"
    style = "bold green" if value >= 0 else "bold red"
    return Text(f"{symbol}${abs(value):.4f}", style=style)


def portfolio_table(snapshot: ConsoleSnapshot) -> Table:
    p = snapshot.portfolio
    table = Table(
        title="REVENUE POC PORTFOLIO", box=None, show_header=False, padding=(0, 2)
    )
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")
    for label, value in (
        ("Starting balance", money(p.starting_balance)),
        ("Available cash", money(p.cash)),
        ("Deployed capital", money(p.deployed_capital)),
        ("Current equity", money(p.equity)),
        ("Realized P&L", money(p.realized_pnl, signed=True)),
        ("Unrealized P&L", money(p.unrealized_pnl, signed=True)),
        ("Modeled open EV", money(p.modeled_open_ev, signed=True)),
        ("Maximum drawdown", money(p.maximum_drawdown)),
        ("Positions", f"{p.open_positions} open / {p.resolved_positions} resolved"),
        ("Capital utilization", percent(p.capital_utilization)),
    ):
        table.add_row(label, value)
    return table


def positions_table(snapshot: ConsoleSnapshot, *, compact: bool = False) -> Table:
    table = Table(
        title="REVENUE POC POSITIONS",
        header_style="bold white on #243447",
        row_styles=("", "on #111820"),
    )
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column(
        "Market", max_width=30 if compact else 52, overflow="ellipsis", no_wrap=True
    )
    if not compact:
        table.add_column("Cat", max_width=12, no_wrap=True)
    table.add_column("Side", justify="center", no_wrap=True)
    table.add_column("Stake", justify="right", no_wrap=True)
    if not compact:
        table.add_column("Entry", justify="right", no_wrap=True)
    table.add_column("Mark", justify="right", no_wrap=True)
    table.add_column("Net P&L", justify="right", no_wrap=True)
    if not compact:
        table.add_column("Edge", justify="right", no_wrap=True)
        table.add_column("EV", justify="right", no_wrap=True)
        table.add_column("Fees", justify="right", no_wrap=True)
        table.add_column("Slip", justify="right", no_wrap=True)
        table.add_column("Quote", justify="right", no_wrap=True)
    table.add_column("State", no_wrap=True)
    open_positions = [item for item in snapshot.positions if item.status == "OPEN"]
    for item in open_positions:
        cells: list[object] = [str(item.id), item.question]
        if not compact:
            cells.append(item.category)
        cells.extend((item.side, f"${item.stake:.2f}"))
        if not compact:
            cells.append(probability(item.entry_price))
        cells.extend((probability(item.current_mark), pnl_text(item.net_pnl)))
        if not compact:
            cells.extend(
                (
                    percent(item.executable_edge),
                    money(item.modeled_ev, signed=True),
                    money(item.fees),
                    money(item.slippage),
                    age(item.quote_age_seconds),
                )
            )
        cells.append(f"{state_symbol(item.quote_state)} {item.status}/{item.quote_state}")
        table.add_row(*cells)
    if not open_positions:
        column_count = 7 if compact else 14
        table.add_row(
            "—",
            "No Revenue POC positions",
            *("—" for _ in range(column_count - 3)),
            "EMPTY",
        )
    return table


def system_table(snapshot: ConsoleSnapshot) -> Table:
    s = snapshot.system
    table = Table(title="SYSTEM", box=None, show_header=False, padding=(0, 2))
    table.add_column("Metric", style="cyan")
    table.add_column("Value")
    rows = (
        ("Runtime", f"{state_symbol(s.runtime_status)} {s.runtime_status}"),
        ("launchd PID", str(s.launchd_pid or "UNKNOWN")),
        ("Release", s.release_sha),
        ("Last cycle", utc_time(s.last_completed_cycle)),
        (
            "Cycle freshness",
            f"{state_symbol(s.cycle_state)} {s.cycle_state} ({age(s.cycle_age_seconds)})",
        ),
        ("Next expected", utc_time(s.next_expected_cycle)),
        ("Database", f"{state_symbol(s.database_status)} {s.database_status}"),
        ("Errors", str(s.error_count)),
        ("Current time", utc_time(s.current_time)),
    )
    for row in rows:
        table.add_row(*row)
    if s.message:
        table.add_row("Data warning", s.message)
    return table


def api_table(snapshot: ConsoleSnapshot) -> Table:
    a = snapshot.api
    table = Table(title="API ECONOMICS", box=None, show_header=False, padding=(0, 2))
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")
    rows = (
        ("Daily budget", money(a.daily_budget)),
        ("Estimated spend today", money(a.estimated_spend_today)),
        ("Remaining budget", money(a.remaining_budget)),
        ("Reserved budget", money(a.reserved_budget)),
        ("Calls today", str(a.calls_today)),
        ("Input / output tokens", f"{a.input_tokens:,} / {a.output_tokens:,}"),
        ("Cache tokens", f"{a.cache_tokens:,}"),
        ("Calls avoided", f"{a.calls_avoided:,}"),
        ("Skipped cost / ceiling", f"{a.calls_skipped_by_cost} / {a.calls_skipped_by_emergency}"),
        ("Provider cache savings", money(a.provider_cache_savings_usd)),
        ("Calls by model", ", ".join(f"{k}={v}" for k, v in sorted(a.calls_by_model.items())) or "none"),
        ("Cost / evaluation", money(a.cost_per_evaluation)),
        ("Cost / candidate", money(a.cost_per_candidate)),
        ("Cost / admitted trade", money(a.cost_per_admitted_trade)),
        ("Historical unknown calls", f"{a.unknown_historical_calls:,} UNKNOWN"),
        ("Measurement", a.measurement),
    )
    for row in rows:
        table.add_row(*row)
    return table


def revenue_status(snapshot: ConsoleSnapshot, console: Console) -> None:
    console.print(system_table(snapshot))
    console.print(portfolio_table(snapshot))
    console.print(api_table(snapshot))
    alpha = snapshot.alpha
    console.print(
        f"[cyan]ALPHA SCOREBOARD[/] status={alpha.status} ranking={alpha.ranking_status} "
        f"evaluations={alpha.attributable_evaluations}/{alpha.total_evaluations} "
        f"resolved={alpha.resolved_positions} completions={alpha.completed_attributions} "
        f"coverage={percent(alpha.resolved_position_coverage)}"
    )
    pipeline = snapshot.pipeline
    console.print(
        f"[bold cyan]PIPELINE[/] watchlist={pipeline.watchlist_size} fetched={pipeline.fetched} "
        f"ranked={pipeline.ranked} evaluated={pipeline.evaluated} llm={pipeline.llm_calls} "
        f"skeptic={pipeline.skeptic_calls} strict={pipeline.strict_candidates} "
        f"revenue={pipeline.revenue_candidates}/{pipeline.revenue_admissions} "
        f"cache={pipeline.cache_hits:,} budget_skips={pipeline.budget_skips} "
        f"dynamic={pipeline.dynamic_shortlist_size} outside={pipeline.outside_watchlist}"
    )
    if pipeline.rejections:
        console.print(
            "[cyan]Rejections[/] "
            + " · ".join(f"{key}={value}" for key, value in pipeline.rejections.items())
        )
    breakdown = pipeline.discovery_breakdown
    if breakdown:
        source = breakdown["discovery_source"]
        funnel = {item["stage"]: item for item in breakdown["survivor_funnel"]}
        console.print(
            "[cyan]Discovery funnel[/] "
            f"expanded={source['total_market_records_expanded']} scored={funnel['scored']['count']} "
            f"valid={funnel['valid_after_policy']['count']} shortlist={funnel['shortlisted']['count']} "
            f"outside={funnel['shortlisted_outside_fixed_watchlist']['count']} "
            f"revenue_evaluated={funnel['actually_evaluated']['count']} admitted={funnel['admitted']['count']}"
        )
        console.print(
            "[cyan]Discovery exclusions[/] "
            + " · ".join(
                f"{name}={item['count']}"
                for name, item in breakdown["quality_exclusions"].items()
                if item["count"]
            )
            or "[cyan]Discovery exclusions[/] none"
        )
        console.print(render_discovery_breakdown(breakdown))


def alpha_leaderboard(snapshot: ConsoleSnapshot, console: Console) -> None:
    report = snapshot.raw.get("alpha_leaderboard", {})
    summary = report.get("summary", {})
    console.print(
        f"[bold cyan]ALPHA LEADERBOARD[/] ranking={summary.get('ranking_status', 'unavailable')} "
        f"decision_coverage={percent(summary.get('decision_coverage'))} "
        f"resolved_coverage={percent(summary.get('resolved_position_coverage'))}"
    )
    table = Table("Rank", "Strategy", "Category", "Horizon", "Source", "Status", "Sample", "Resolved", "Net P&L", "P&L/cap-day")
    for item in report.get("leaderboard", []):
        table.add_row(
            str(item.get("rank") or "—"),
            f"{item.get('strategy_id')}/{item.get('strategy_version')}",
            str(item.get("category")), str(item.get("horizon_bucket")), str(item.get("source_type")),
            str(item.get("attribution_status")), str(item.get("sample_size_status")),
            str(item.get("resolved_positions", 0)), money(item.get("realized_net_pnl_usd"), signed=True),
            money(item.get("realized_pnl_per_capital_day"), signed=True),
        )
    console.print(table)
    if not report.get("leaderboard"):
        console.print("No attributable opportunities available.")


def _report_value(value: object, *, unavailable: str = "UNAVAILABLE") -> str:
    return unavailable if value is None else str(value)


def _investment_decision(snapshot: ConsoleSnapshot) -> str:
    if not snapshot.system.database_status.startswith("OK"):
        return "STRATEGY REVIEW"
    if snapshot.alpha.resolved_positions < 5:
        return "INSUFFICIENT SAMPLE"
    if snapshot.alpha.ranking_status == "realized":
        return "SCALE REVIEW" if snapshot.alpha.realized_pnl_usd > 0 else "STRATEGY REVIEW"
    return "OBSERVE"


def _capital_days(snapshot: ConsoleSnapshot) -> float | None:
    rows = snapshot.raw.get("alpha_leaderboard", {}).get("leaderboard", [])
    values = [float(row["capital_days"]) for row in rows if row.get("capital_days") is not None]
    return sum(values) if values else None


def investment_report(snapshot: ConsoleSnapshot, console: Console) -> None:
    """Render a deterministic institutional operating report from the snapshot."""
    p = snapshot.portfolio
    pipe = snapshot.pipeline
    alpha = snapshot.alpha
    velocity = snapshot.raw.get("velocity_shadow", {})
    velocity_universe = velocity.get("universe", {})
    categories = snapshot.raw.get("discovery_categories", {})
    breakdown = pipe.discovery_breakdown
    source = breakdown.get("discovery_source", {}) if breakdown else {}
    funnel = {item["stage"]: item for item in breakdown.get("survivor_funnel", [])} if breakdown else {}
    capital_days = _capital_days(snapshot)
    realized_per_day = None
    if capital_days:
        realized_per_day = p.realized_pnl / capital_days
    horizon_counts = {key: 0 for key in ("FAST", "WEEKLY", "MONTHLY", "LONG")}
    horizon_counts["UNKNOWN"] = 0
    for position in snapshot.positions:
        horizon = horizon_for_days(position.expected_holding_days) or "UNKNOWN"
        horizon_counts[horizon] = horizon_counts.get(horizon, 0) + 1

    console.rule("SWARM EDGE INVESTMENT COMMITTEE REPORT v1")
    console.print("[bold cyan]EXECUTIVE SUMMARY[/]")
    console.print(f"Active release: {snapshot.system.release_sha}")
    console.print(f"Runner health: {snapshot.system.runtime_status} / {snapshot.system.cycle_state}")
    console.print(f"Portfolio: {p.open_positions} open, {p.resolved_positions} resolved; equity {money(p.equity)}")
    console.print("Current objective: measure repeatable realized alpha and capital-time efficiency.")
    confidence = "HIGH" if snapshot.system.database_status.startswith("OK") and alpha.resolved_positions >= 5 else "LIMITED"
    console.print(f"System confidence/state: {confidence} / {_investment_decision(snapshot)}")

    console.print("\n[bold cyan]PORTFOLIO REVIEW[/]")
    portfolio_rows = [
        ("Open positions", str(p.open_positions)),
        ("Deployed capital", money(p.deployed_capital)),
        ("Equity", money(p.equity)),
        ("Unrealized P&L", money(p.unrealized_pnl, signed=True)),
        ("Realized P&L", money(p.realized_pnl, signed=True)),
        ("Maximum drawdown", money(p.maximum_drawdown)),
        ("Horizon distribution", " ".join(f"{key}={horizon_counts.get(key, 0)}" for key in ("FAST", "WEEKLY", "MONTHLY", "LONG"))),
    ]
    for label, value in portfolio_rows:
        console.print(f"{label}: {value}")

    console.print("\n[bold cyan]DISCOVERY REVIEW[/]")
    console.print(f"Markets scanned: {_report_value(source.get('total_market_records_expanded'))}")
    console.print(f"Categories discovered: {len(categories)} ({', '.join(sorted(categories)) or 'UNAVAILABLE'})")
    valid_opportunities = velocity_universe.get("valid_discovered_opportunities") or pipe.eligible_markets or None
    console.print(f"Valid opportunities: {_report_value(valid_opportunities)}")
    shortlist_count = pipe.dynamic_shortlist_size or funnel.get("shortlisted", {}).get("count") or None
    console.print(f"Shortlist size: {_report_value(shortlist_count)}")
    console.print(f"Outside-watchlist opportunities: {pipe.outside_watchlist}")
    coverage = (
        float(velocity_universe["evaluated_valid_opportunities"]) / float(velocity_universe["valid_discovered_opportunities"])
        if velocity_universe.get("valid_discovered_opportunities") else None
    )
    console.print(f"Evaluation coverage: {percent(coverage)}")

    console.print("\n[bold cyan]OPPORTUNITY PIPELINE[/]")
    current_candidates = velocity_universe.get("positive_net_ev_opportunities") or pipe.revenue_candidates or None
    console.print(f"Current candidates: {_report_value(current_candidates)}")
    console.print(f"Admitted positions: {pipe.revenue_admissions}")
    console.print("Rejected reasons: " + (" · ".join(f"{key}={value}" for key, value in pipe.rejections.items()) or "NONE"))
    top_velocity = velocity.get("top_velocity", [])
    if top_velocity:
        table = Table("Rank", "Market", "Horizon", "Modeled EV/capital-day", "Status")
        for item in top_velocity[:5]:
            table.add_row(str(item.get("velocity_rank", "—")), str(item.get("market_id")), str(item.get("horizon")), money(item.get("ev_per_capital_day"), signed=True), "MODELED ONLY")
        console.print("Top velocity opportunities (modeled, not realized):")
        console.print(table)
    else:
        console.print("Top velocity opportunities: UNAVAILABLE")

    console.print("\n[bold cyan]ALPHA ATTRIBUTION[/]")
    console.print(f"Attributable evaluations: {alpha.attributable_evaluations}/{alpha.total_evaluations}")
    console.print(f"Resolved positions: {alpha.resolved_positions}")
    console.print(f"Ranked strategies: {alpha.ranked_rows}")
    console.print(f"Sample status: {alpha.ranking_status}")
    console.print(f"Attribution coverage: {percent(alpha.resolved_position_coverage)}")
    console.print(f"Realized alpha availability: {'AVAILABLE' if alpha.ranked_rows else 'UNAVAILABLE — insufficient resolved sample'}")

    console.print("\n[bold cyan]ECONOMIC SCORECARD[/]")
    console.print(f"Realized P&L: {money(p.realized_pnl, signed=True)}")
    console.print(f"Unrealized P&L: {money(p.unrealized_pnl, signed=True)}")
    console.print(f"Capital utilization: {percent(p.capital_utilization)}")
    console.print(f"Capital-days: {_report_value(round(capital_days, 4) if capital_days is not None else None)}")
    console.print(f"Return/capital-day: {money(realized_per_day, signed=True)}")
    console.print(f"API cost: {money(snapshot.api.estimated_spend_today)} (today; historical measurement {snapshot.api.measurement})")
    console.print("Opportunity frequency: UNAVAILABLE — no persisted frequency series")

    console.print("\n[bold cyan]RISK REVIEW[/]")
    category_exposure: dict[str, float] = {}
    for position in snapshot.positions:
        category_exposure[position.category] = category_exposure.get(position.category, 0.0) + position.stake
    concentration = ", ".join(f"{key}={money(value)}" for key, value in sorted(category_exposure.items())) or "UNAVAILABLE"
    console.print(f"Category exposure: {concentration}")
    console.print(f"Unresolved exposure: {money(p.deployed_capital)}")
    console.print(f"Drawdown: {money(p.maximum_drawdown)}")
    console.print("Limits: UNAVAILABLE in current operator snapshot")

    console.print("\n[bold cyan]INVESTMENT COMMITTEE DECISION[/]")
    console.print(f"{_investment_decision(snapshot)} — deterministic status; no LLM opinion used.")


def render_command(command: str, snapshot: ConsoleSnapshot, console: Console) -> None:
    if command == "portfolio":
        console.print(portfolio_table(snapshot))
    elif command == "positions":
        console.print(positions_table(snapshot, compact=console.width < 160))
    elif command == "discovery-breakdown":
        if snapshot.pipeline.discovery_breakdown:
            console.print(render_discovery_breakdown(snapshot.pipeline.discovery_breakdown))
        else:
            console.print("DISCOVERY BREAKDOWN unavailable: no persisted discovery data")
    elif command == "alpha-leaderboard":
        alpha_leaderboard(snapshot, console)
    elif command == "investment-report":
        investment_report(snapshot, console)
    else:
        revenue_status(snapshot, console)
