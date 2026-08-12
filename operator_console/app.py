from __future__ import annotations

from typing import ClassVar

from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Static,
    TabbedContent,
    TabPane,
)

from operator_console.data import OperatorDataSource, filter_positions
from operator_console.models import ConsoleSnapshot, PositionSnapshot
from operator_console.render import (
    age,
    money,
    percent,
    probability,
    state_symbol,
    utc_time,
)


def _kv_table(title: str, rows: list[tuple[str, str]]) -> Table:
    table = Table(title=title, box=None, show_header=False, expand=True, padding=(0, 1))
    table.add_column(style="cyan", ratio=2)
    table.add_column(justify="right", ratio=3)
    for row in rows:
        table.add_row(*row)
    return table


class SearchScreen(ModalScreen[str | None]):
    DEFAULT_CSS = """
    SearchScreen { align: center middle; background: rgba(0,0,0,0.6); }
    #search-box { width: 70; max-width: 90%; height: 7; border: solid #f5a623; background: #0b1118; padding: 1 2; }
    #search-hint { color: #aeb8c2; height: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="search-box"):
            yield Static("SEARCH POSITIONS / MARKETS", classes="panel-title")
            yield Input(
                placeholder="question, slug, category, side, status", id="search-input"
            )
            yield Static("Enter apply  •  Escape cancel", id="search-hint")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def key_escape(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    HelpScreen { align: center middle; background: rgba(0,0,0,0.7); }
    #help-box { width: 78; max-width: 94%; height: 24; border: solid #f5a623; background: #0b1118; padding: 1 2; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss", "Close"),
        Binding("h", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        text = Text.from_markup(
            "[bold #f5a623]SWARM EDGE OPERATOR CONSOLE[/]\n\n"
            "q  quit                 r  refresh display\n"
            "p  portfolio            t  positions\n"
            "m  market / pipeline    a  API economics\n"
            "l  event / log feed     e  evaluations / rejections\n"
            "s  system               h  help\n"
            "/  search               ↑/↓ navigate\n"
            "Enter  position detail  Esc close dialog\n\n"
            "[bold]SAFETY[/]\n"
            "This console opens SQLite in read-only/query-only mode.\n"
            "No key can trade, approve, restart, migrate, or write state."
        )
        with Vertical(id="help-box"):
            yield Static(text)

    def action_dismiss(self) -> None:
        self.dismiss(None)


class PositionDetailScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    PositionDetailScreen { align: center middle; background: rgba(0,0,0,0.72); }
    #detail-box { width: 108; max-width: 96%; height: 31; max-height: 94%; border: solid #20c5c7; background: #080d12; padding: 1 2; overflow-y: auto; }
    #detail-title { color: #f5a623; text-style: bold; height: auto; margin-bottom: 1; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss", "Close"),
        Binding("enter", "dismiss", "Close"),
    ]

    def __init__(self, position: PositionSnapshot) -> None:
        super().__init__()
        self.position = position

    def compose(self) -> ComposeResult:
        p = self.position
        title = Text(
            f"POSITION {p.id:03d}  {p.status}  {state_symbol(p.quote_state)} {p.quote_state}\n{p.question}"
        )
        rows = [
            ("Contract", p.market_id),
            ("Category / side", f"{p.category} / {p.side}"),
            ("Entry timestamp", utc_time(p.entry_timestamp)),
            (
                "Entry bid / ask",
                f"{probability(p.entry_bid)} / {probability(p.entry_ask)}",
            ),
            ("Entry executable", probability(p.entry_price)),
            (
                "Current bid / ask",
                f"{probability(p.current_bid)} / {probability(p.current_ask)}",
            ),
            ("Current executable mark", probability(p.current_mark)),
            ("Stake / shares", f"{money(p.stake)} / {p.shares:.4f}"),
            ("Gross P&L", money(p.gross_pnl, signed=True)),
            ("Fees", money(p.fees)),
            ("Slippage", money(p.slippage)),
            ("Net P&L", money(p.net_pnl, signed=True)),
            (
                "Model / market probability",
                f"{probability(p.model_probability)} / {probability(p.market_probability)}",
            ),
            (
                "Raw / executable edge",
                f"{percent(p.raw_edge)} / {percent(p.executable_edge)}",
            ),
            ("Modeled EV", money(p.modeled_ev, signed=True)),
            ("Entry spread", percent(p.spread)),
            (
                "Resolution horizon",
                f"{p.expected_holding_days:.1f} days"
                if p.expected_holding_days is not None
                else "UNKNOWN",
            ),
            ("Expected resolution", utc_time(p.expected_resolution)),
            ("Source forecast", p.source_forecast),
            ("Admission reason", p.admission_reason),
            ("Current status", p.status),
            ("Last quote", utc_time(p.quote_timestamp)),
            ("Quote freshness", f"{p.quote_state} / {age(p.quote_age_seconds)}"),
        ]
        with Vertical(id="detail-box"):
            yield Static(title, id="detail-title")
            yield Static(_kv_table("EXECUTABLE POSITION ECONOMICS", rows))

    def action_dismiss(self) -> None:
        self.dismiss(None)


class OperatorConsoleApp(App[None]):
    TITLE = "SWARM EDGE"
    SUB_TITLE = "REVENUE POC · READ ONLY"
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen { background: #06090d; color: #e7edf3; }
    Header { background: #101820; color: #f5a623; height: 1; }
    Footer { background: #101820; color: #aeb8c2; height: 1; }
    #system-ticker { height: 2; background: #0c141c; border-bottom: solid #243447; padding: 0 1; }
    TabbedContent { height: 1fr; }
    ContentSwitcher { background: #06090d; }
    Tabs { height: 2; background: #0b1118; }
    Tab { color: #8fa1b3; padding: 0 1; }
    Tab.-active { color: #f5a623; text-style: bold; }
    TabPane { padding: 0; }
    .summary-row { height: 9; }
    .summary-panel { width: 1fr; border: solid #243447; padding: 0 1; background: #090f15; }
    .full-panel { height: 1fr; border: solid #243447; background: #090f15; }
    .section-title { height: 1; padding: 0 1; color: #f5a623; background: #101820; text-style: bold; }
    #overview-positions { height: 12; }
    #overview-events { height: 1fr; }
    DataTable { background: #080d12; color: #dfe7ee; }
    DataTable > .datatable--header { background: #182635; color: #ffffff; text-style: bold; }
    DataTable > .datatable--cursor { background: #244b5a; color: #ffffff; }
    DataTable > .datatable--odd-row { background: #0b1219; }
    #portfolio-view, #api-view, #pipeline-view, #system-view { padding: 1 2; }
    #positions-table, #evaluations-table, #logs-table { height: 1fr; }
    .compact .summary-row { layout: vertical; height: 18; }
    .compact .summary-panel { width: 1fr; height: 6; }
    .compact #overview-positions { height: 8; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh_screen", "Refresh"),
        Binding("p", "tab('portfolio')", "Portfolio"),
        Binding("t", "tab('positions')", "Positions"),
        Binding("m", "tab('pipeline')", "Market"),
        Binding("a", "tab('api')", "API"),
        Binding("l", "tab('logs')", "Logs"),
        Binding("e", "tab('evaluations')", "Evaluations"),
        Binding("s", "tab('system')", "System"),
        Binding("h", "help", "Help"),
        Binding("/", "search", "Search"),
        Binding("enter", "position_detail", "Detail", show=False),
    ]

    def __init__(
        self,
        source: OperatorDataSource,
        *,
        initial_snapshot: ConsoleSnapshot | None = None,
    ) -> None:
        super().__init__()
        self.source = source
        self.snapshot = initial_snapshot or ConsoleSnapshot()
        self.search_query = ""
        self.displayed_positions: tuple[PositionSnapshot, ...] = ()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(id="system-ticker")
        with TabbedContent(initial="overview", id="tabs"):
            with TabPane("OVERVIEW", id="overview"):
                with Horizontal(classes="summary-row", id="overview-summary"):
                    yield Static(classes="summary-panel", id="portfolio-summary")
                    yield Static(classes="summary-panel", id="pipeline-summary")
                    yield Static(classes="summary-panel", id="api-summary")
                yield Static(
                    "LIVE POSITIONS · sorted by |P&L|", classes="section-title"
                )
                yield DataTable(id="overview-positions", cursor_type="row")
                yield Static("EVENT FEED", classes="section-title")
                yield DataTable(id="overview-events", cursor_type="row")
            with TabPane("PORTFOLIO", id="portfolio"):
                yield Static(id="portfolio-view", classes="full-panel")
            with TabPane("POSITIONS", id="positions"):
                yield DataTable(id="positions-table", cursor_type="row")
            with TabPane("MARKET", id="pipeline"):
                yield Static(id="pipeline-view", classes="full-panel")
            with TabPane("API", id="api"):
                yield Static(id="api-view", classes="full-panel")
            with TabPane("EVALUATIONS", id="evaluations"):
                yield DataTable(id="evaluations-table", cursor_type="row")
            with TabPane("SYSTEM", id="system"):
                yield Static(id="system-view", classes="full-panel")
            with TabPane("LOG", id="logs"):
                yield DataTable(id="logs-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self._configure_tables()
        self._load(include_logs=True)
        self._start_slow_refresh()
        self.set_interval(2.0, self._refresh_database)
        self.set_interval(5.0, self._refresh_logs)
        self.set_interval(45.0, self._start_slow_refresh)
        self._update_compact(self.size.width)

    def _configure_tables(self) -> None:
        position_columns = (
            "#",
            "MARKET",
            "CAT",
            "SIDE",
            "STAKE",
            "ENTRY",
            "MARK",
            "P&L",
            "EDGE",
            "EV",
            "SPREAD",
            "FEES",
            "SLIP",
            "AGE",
            "RESOLUTION",
            "STATE",
            "QUOTE",
        )
        for selector in ("#overview-positions", "#positions-table"):
            self.query_one(selector, DataTable).add_columns(*position_columns)
        for selector in ("#overview-events", "#logs-table"):
            self.query_one(selector, DataTable).add_columns("TIME", "TYPE", "EVENT")
        self.query_one("#evaluations-table", DataTable).add_columns(
            "TIME", "MARKET", "DECISION", "REASON", "EDGE", "EV"
        )

    def _load(self, *, include_logs: bool) -> None:
        try:
            self.snapshot = self.source.read(
                include_logs=include_logs, include_reports=False
            )
        except TypeError:
            # Keep lightweight test and embedding data sources compatible.
            self.snapshot = self.source.read(include_logs=include_logs)
        try:
            self._render()
        except NoMatches:
            # A timer may complete while Textual is tearing down the screen.
            # The snapshot remains valid and no retry or state mutation occurs.
            return

    def _refresh_database(self) -> None:
        self._load(include_logs=False)

    def _refresh_logs(self) -> None:
        self._load(include_logs=True)

    def _start_slow_refresh(self) -> None:
        refresh = getattr(self.source, "refresh_slow", None)
        if refresh is not None:
            self.run_worker(refresh, thread=True)

    def action_refresh_screen(self) -> None:
        self._load(include_logs=True)
        self._start_slow_refresh()
        self.notify("Display refreshed from read-only sources", timeout=1.5)

    def action_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id
        if tab_id == "positions":
            self.query_one("#positions-table", DataTable).focus()
        elif tab_id == "evaluations":
            self.query_one("#evaluations-table", DataTable).focus()
        elif tab_id == "logs":
            self.query_one("#logs-table", DataTable).focus()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_search(self) -> None:
        self.push_screen(SearchScreen(), self._apply_search)

    def _apply_search(self, query: str | None) -> None:
        if query is None:
            return
        self.search_query = query.strip()
        self._render_positions()
        self.query_one(TabbedContent).active = "positions"
        self.query_one("#positions-table", DataTable).focus()

    def _selected_position(self) -> PositionSnapshot | None:
        tab = self.query_one(TabbedContent).active
        selector = "#positions-table" if tab == "positions" else "#overview-positions"
        table = self.query_one(selector, DataTable)
        index = table.cursor_row
        if 0 <= index < len(self.displayed_positions):
            return self.displayed_positions[index]
        return None

    def action_position_detail(self) -> None:
        position = self._selected_position()
        if position:
            self.push_screen(PositionDetailScreen(position))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id in {"positions-table", "overview-positions"}:
            self.action_position_detail()

    def on_resize(self, event: events.Resize) -> None:
        self._update_compact(event.size.width)

    def _update_compact(self, width: int) -> None:
        self.set_class(width < 100, "compact")

    def _render(self) -> None:
        self._render_ticker()
        self._render_summaries()
        self._render_positions()
        self._render_events()
        self._render_evaluations()
        self._render_full_views()

    def _render_ticker(self) -> None:
        s = self.snapshot.system
        ticker = Text()
        ticker.append(
            f" {state_symbol(s.runtime_status)} {s.runtime_status}",
            style="bold green" if s.runtime_status == "RUNNING" else "bold yellow",
        )
        ticker.append(f"  PID {s.launchd_pid or '—'}  ", style="white")
        ticker.append(f"REL {s.release_sha[:10]}  ", style="cyan")
        ticker.append(
            f"DB {s.database_status}  ",
            style="green" if s.database_status.startswith("OK") else "red",
        )
        ticker.append(
            f"CYCLE {s.cycle_state} {age(s.cycle_age_seconds)}  ",
            style="yellow" if s.cycle_state == "STALE" else "green",
        )
        ticker.append(utc_time(s.current_time), style="white")
        if s.message:
            ticker.append(f"  ! {s.message}", style="bold red")
        self.query_one("#system-ticker", Static).update(ticker)

    def _render_summaries(self) -> None:
        p, pipe, api = (
            self.snapshot.portfolio,
            self.snapshot.pipeline,
            self.snapshot.api,
        )
        self.query_one("#portfolio-summary", Static).update(
            _kv_table(
                "PORTFOLIO",
                [
                    ("Equity", money(p.equity)),
                    (
                        "Cash / Deployed",
                        f"{money(p.cash)} / {money(p.deployed_capital)}",
                    ),
                    (
                        "Realized / Unrealized",
                        f"{money(p.realized_pnl, signed=True)} / {money(p.unrealized_pnl, signed=True)}",
                    ),
                    (
                        "Open EV / DD",
                        f"{money(p.modeled_open_ev, signed=True)} / {money(p.maximum_drawdown)}",
                    ),
                    (
                        "Positions / Util",
                        f"{p.open_positions}+{p.resolved_positions} / {percent(p.capital_utilization)}",
                    ),
                ],
            )
        )
        self.query_one("#pipeline-summary", Static).update(
            _kv_table(
                "PIPELINE",
                [
                    (
                        "Watch / Fetch / Rank",
                        f"{pipe.watchlist_size} / {pipe.fetched} / {pipe.ranked}",
                    ),
                    (
                        "Evaluate / LLM / Skeptic",
                        f"{pipe.evaluated} / {pipe.llm_calls} / {pipe.skeptic_calls}",
                    ),
                    ("Strict candidates", str(pipe.strict_candidates)),
                    (
                        "Revenue candidate / admit",
                        f"{pipe.revenue_candidates} / {pipe.revenue_admissions}",
                    ),
                    (
                        "Cache / Budget skip",
                        f"{pipe.cache_hits:,} / {pipe.budget_skips}",
                    ),
                    (
                        "Dynamic / Outside fixed",
                        f"{pipe.dynamic_shortlist_size} / {pipe.outside_watchlist}",
                    ),
                ],
            )
        )
        self.query_one("#api-summary", Static).update(
            _kv_table(
                "API",
                [
                    (
                        "Budget / Spend",
                        f"{money(api.daily_budget)} / {money(api.estimated_spend_today)}",
                    ),
                    (
                        "Remaining / Calls",
                        f"{money(api.remaining_budget)} / {api.calls_today}",
                    ),
                    ("Reserved / Cost skips", f"{money(api.reserved_budget)} / {api.calls_skipped_by_cost}"),
                    (
                        "Tokens in / out",
                        f"{api.input_tokens:,} / {api.output_tokens:,}",
                    ),
                    (
                        "Cache tokens / Avoided",
                        f"{api.cache_tokens:,} / {api.calls_avoided:,}",
                    ),
                    ("Provider cache savings", money(api.provider_cache_savings_usd)),
                    ("Models", ", ".join(f"{k}={v}" for k, v in sorted(api.calls_by_model.items())) or "none"),
                    ("Unknown history", f"{api.unknown_historical_calls:,} UNKNOWN"),
                ],
            )
        )

    def _position_cells(self, item: PositionSnapshot) -> tuple[object, ...]:
        opened_age = (
            (self.snapshot.system.current_time - item.entry_timestamp).total_seconds()
            if self.snapshot.system.current_time and item.entry_timestamp
            else None
        )
        return (
            str(item.id),
            item.question,
            item.category,
            item.side,
            f"${item.stake:.2f}",
            probability(item.entry_price),
            probability(item.current_mark),
            money(item.net_pnl, signed=True),
            percent(item.executable_edge),
            money(item.modeled_ev, signed=True),
            percent(item.spread),
            money(item.fees),
            money(item.slippage),
            age(opened_age),
            utc_time(item.expected_resolution).replace(" UTC", ""),
            f"{state_symbol(item.quote_state)} {item.status}/{item.quote_state}",
            utc_time(item.quote_timestamp).replace(" UTC", ""),
        )

    def _render_positions(self) -> None:
        self.displayed_positions = filter_positions(
            self.snapshot.positions, self.search_query
        )
        for selector in ("#overview-positions", "#positions-table"):
            table = self.query_one(selector, DataTable)
            table.clear()
            for item in self.displayed_positions:
                table.add_row(*self._position_cells(item), key=str(item.id))
        title = "POSITIONS"
        if self.search_query:
            title += f" · FILTER: {self.search_query} · {len(self.displayed_positions)} MATCHES"
        self.query_one("#positions", TabPane).label = title

    def _render_events(self) -> None:
        for selector in ("#overview-events", "#logs-table"):
            table = self.query_one(selector, DataTable)
            table.clear()
            events = (
                self.snapshot.events[:12]
                if selector == "#overview-events"
                else self.snapshot.events
            )
            for event in events:
                table.add_row(
                    utc_time(event.timestamp).replace(" UTC", ""),
                    event.kind,
                    event.message,
                )

    def _render_evaluations(self) -> None:
        table = self.query_one("#evaluations-table", DataTable)
        table.clear()
        for item in self.snapshot.evaluations:
            table.add_row(
                utc_time(item.timestamp).replace(" UTC", ""),
                item.question,
                item.decision,
                item.reason,
                percent(item.executable_edge),
                money(item.expected_value, signed=True),
            )

    def _render_full_views(self) -> None:
        s, p, pipe, api = (
            self.snapshot.system,
            self.snapshot.portfolio,
            self.snapshot.pipeline,
            self.snapshot.api,
        )
        self.query_one("#portfolio-view", Static).update(
            _kv_table(
                "PORTFOLIO / PERFORMANCE",
                [
                    ("Starting balance", money(p.starting_balance)),
                    ("Available cash", money(p.cash)),
                    ("Deployed capital", money(p.deployed_capital)),
                    ("Current equity", money(p.equity)),
                    ("Realized P&L", money(p.realized_pnl, signed=True)),
                    ("Unrealized P&L", money(p.unrealized_pnl, signed=True)),
                    ("Modeled open EV", money(p.modeled_open_ev, signed=True)),
                    ("Maximum drawdown", money(p.maximum_drawdown)),
                    ("Open / resolved", f"{p.open_positions} / {p.resolved_positions}"),
                    ("Capital utilization", percent(p.capital_utilization)),
                ],
            )
        )
        rejection_lines = (
            "\n".join(
                f"{reason:<42} {count:>6}" for reason, count in pipe.rejections.items()
            )
            or "No rejection data"
        )
        self.query_one("#pipeline-view", Static).update(
            Text.from_markup(
                f"[bold #f5a623]MARKET / PIPELINE[/]\n\n"
                f"Discovered {pipe.discovered_markets}    Eligible {pipe.eligible_markets}    Watchlist {pipe.watchlist_size}\n"
                f"Fetched {pipe.fetched}    Ranked {pipe.ranked}    Evaluated {pipe.evaluated}\n"
                f"LLM {pipe.llm_calls}    Skeptic {pipe.skeptic_calls}    Budget skips {pipe.budget_skips}\n"
                f"Strict candidates {pipe.strict_candidates}    Revenue candidates {pipe.revenue_candidates}    Admissions {pipe.revenue_admissions}\n"
                f"Cache hits {pipe.cache_hits:,}\n\n[bold cyan]REJECTIONS[/]\n{rejection_lines}"
            )
        )
        self.query_one("#api-view", Static).update(
            _kv_table(
                "API ECONOMICS",
                [
                    ("Daily budget", money(api.daily_budget)),
                    ("Estimated spend today", money(api.estimated_spend_today)),
                    ("Remaining budget", money(api.remaining_budget)),
                    ("Calls today", str(api.calls_today)),
                    ("Input tokens", f"{api.input_tokens:,}"),
                    ("Output tokens", f"{api.output_tokens:,}"),
                    ("Cache tokens", f"{api.cache_tokens:,}"),
                    ("Calls avoided", f"{api.calls_avoided:,}"),
                    ("Cost / evaluation", money(api.cost_per_evaluation)),
                    ("Cost / candidate", money(api.cost_per_candidate)),
                    ("Cost / admitted trade", money(api.cost_per_admitted_trade)),
                    (
                        "Unknown historical usage",
                        f"{api.unknown_historical_calls:,} calls · UNKNOWN, not zero",
                    ),
                    ("Measurement", api.measurement),
                ],
            )
        )
        self.query_one("#system-view", Static).update(
            _kv_table(
                "SYSTEM HEALTH",
                [
                    (
                        "Runtime status",
                        f"{state_symbol(s.runtime_status)} {s.runtime_status}",
                    ),
                    ("launchd PID", str(s.launchd_pid or "UNKNOWN")),
                    ("Release SHA", s.release_sha),
                    ("Last completed cycle", utc_time(s.last_completed_cycle)),
                    (
                        "Cycle freshness",
                        f"{state_symbol(s.cycle_state)} {s.cycle_state} / {age(s.cycle_age_seconds)}",
                    ),
                    ("Next expected cycle", utc_time(s.next_expected_cycle)),
                    ("Database", s.database_status),
                    ("Error count", str(s.error_count)),
                    ("Current time", utc_time(s.current_time)),
                    (
                        "Database path",
                        str(self.snapshot.raw.get("database", "UNKNOWN")),
                    ),
                    ("Log path", str(self.snapshot.raw.get("logs", "UNKNOWN"))),
                    ("Data warning", s.message or "None"),
                ],
            )
        )


def run_console(source: OperatorDataSource) -> None:
    OperatorConsoleApp(source).run()
