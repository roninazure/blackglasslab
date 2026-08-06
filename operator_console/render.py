from __future__ import annotations

from datetime import UTC, datetime

from rich.console import Console
from rich.table import Table
from rich.text import Text

from operator_console.models import ConsoleSnapshot
from reporting.discovery_breakdown import render_discovery_breakdown


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
    else:
        revenue_status(snapshot, console)
