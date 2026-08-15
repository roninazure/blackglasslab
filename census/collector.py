from __future__ import annotations

import asyncio
import hashlib
import json
import os
import resource
import signal
import sqlite3
import time
import urllib.parse
import urllib.request
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .durability import DurabilityTracker
from .storage import CensusStore
from .stream import BookState, StreamStats, consume_market_stream

ALLOWED_HOSTS = {"gamma-api.polymarket.com", "clob.polymarket.com"}
DEFAULT_DB = Path(os.environ.get("SWARM_CENSUS_DB", "census/census.sqlite"))
DEFAULT_PID = Path(os.environ.get("SWARM_CENSUS_PID", "census/census.pid"))
ENGINE_NAMES = ("revenue_directional_control", "sports_event_driven", "short_duration_crypto", "negrisk_structural", "maker_spread_rebate")
MAX_STREAM_ASSETS = int(os.environ.get("SWARM_CENSUS_MAX_STREAM_ASSETS", "1000"))
PRODUCTION_CONTROL_TIMEOUT = float(os.environ.get("SWARM_CENSUS_PRODUCTION_CONTROL_TIMEOUT", "15"))
GAMMA_BOOTSTRAP_TIMEOUT = float(os.environ.get("SWARM_CENSUS_GAMMA_BOOTSTRAP_TIMEOUT", "30"))
CLASSIFICATION_TIMEOUT = float(os.environ.get("SWARM_CENSUS_CLASSIFICATION_TIMEOUT", "20"))
WEBSOCKET_CONNECT_TIMEOUT = float(os.environ.get("SWARM_CENSUS_WEBSOCKET_CONNECT_TIMEOUT", "20"))
SUBSCRIPTION_SEND_TIMEOUT = float(os.environ.get("SWARM_CENSUS_SUBSCRIPTION_SEND_TIMEOUT", "10"))
FIRST_MESSAGE_TIMEOUT = float(os.environ.get("SWARM_CENSUS_FIRST_MESSAGE_TIMEOUT", "30"))
INITIAL_PERSISTENCE_TIMEOUT = float(os.environ.get("SWARM_CENSUS_INITIAL_PERSISTENCE_TIMEOUT", "10"))
RUNTIME_PERSIST_INTERVAL = float(os.environ.get("SWARM_CENSUS_RUNTIME_PERSIST_INTERVAL", "2"))
COLLECTOR_HEARTBEAT_INTERVAL = max(
    5.0, float(os.environ.get("SWARM_CENSUS_HEARTBEAT_INTERVAL", "30"))
)


class StartupPhaseError(RuntimeError):
    """A required startup phase failed or exceeded its deadline."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_production_db() -> Path:
    value = os.environ.get("BGL_DB_PATH")
    env_file = os.environ.get("BGL_RUNTIME_ENV_FILE", "/Users/scottsteele/Library/Application Support/SwarmEdge/config/runtime.env")
    if not value and Path(env_file).exists():
        for line in Path(env_file).read_text(encoding="utf-8").splitlines():
            if line.startswith("BGL_DB_PATH="):
                value = line.split("=", 1)[1].strip().strip("'\"")
                break
    return Path(value or "memory/runs.sqlite").expanduser().resolve()


def authoritative_production_cycle(production_db: Path | None = None) -> dict[str, Any]:
    """Read the same cycle source used by operator status/watch, read-only."""
    runtime_env_file = os.environ.get("BGL_RUNTIME_ENV_FILE", "/Users/scottsteele/Library/Application Support/SwarmEdge/config/runtime.env")
    resolved_production_db = (production_db or canonical_production_db()).resolve()
    from operator_console.data import OperatorDataSource

    environ = dict(os.environ)
    environ["BGL_RUNTIME_ENV_FILE"] = runtime_env_file
    environ["BGL_DB_PATH"] = str(resolved_production_db)
    source = OperatorDataSource(environ=environ)
    report = {}
    try:
        report_path = source.paths.signals_dir / "infer_pipeline_report.json"
        if report_path.exists(): report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        report = {}
    with closing(source._connect()) as conn:  # same read-only connection/status boundary as watch
        timestamp = source._latest_cycle(conn, report)
        row = conn.execute("SELECT run_id,timestamp_utc FROM revenue_poc_evaluations ORDER BY timestamp_utc DESC,id DESC LIMIT 1").fetchone()
    now = datetime.now(timezone.utc)
    freshness = max(0.0, (now - timestamp).total_seconds()) if timestamp else None
    system = source._system_status(database_status="OK / READ-ONLY", last_cycle=timestamp)
    tolerance = source.cycle_interval * 1.5
    warning = None if freshness is not None and freshness <= tolerance else f"production cycle stale: freshness={freshness} tolerance={tolerance}"
    return {"captured_at_utc": utc_now(), "cycle_id": str(row[0]) if row else None, "cycle_timestamp_utc": timestamp.isoformat().replace("+00:00", "Z") if timestamp else None, "freshness_seconds": freshness, "runner_pid": system.launchd_pid, "runner_state": system.runtime_status, "cycle_state": system.cycle_state, "warning": warning, "source": "operator_console.data.OperatorDataSource._latest_cycle/_system_status", "tolerance_seconds": tolerance}


class PublicGetClient:
    def get(self, url: str) -> Any:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise ValueError(f"census URL is outside public allowlist: {url}")
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "swarm-edge-alpha-census/2.0", "Accept": "application/json"})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=20) as response:
            return json.loads(response.read())


def _list(value: Any) -> list[Any]:
    if isinstance(value, list): return value
    if isinstance(value, str):
        try: parsed = json.loads(value)
        except json.JSONDecodeError: return []
        return parsed if isinstance(parsed, list) else []
    return []


def _levels(book: dict[str, Any], side: str) -> list[tuple[float, float]]:
    result = []
    for item in book.get(side, []):
        try: result.append((float(item["price"]), float(item["size"])))
        except (KeyError, TypeError, ValueError): pass
    return sorted(result, key=lambda x: x[0], reverse=side == "bids")


def negrisk_event_valid(event: dict[str, Any]) -> bool:
    markets = event.get("markets") if isinstance(event.get("markets"), list) else []
    if not bool(event.get("negRisk")) or len(markets) < 3: return False
    ids = [str(m.get("conditionId") or m.get("id") or "") for m in markets if isinstance(m, dict)]
    return len(ids) == len(markets) and len(set(ids)) == len(ids) and all(len(_list(m.get("clobTokenIds"))) >= 2 for m in markets if isinstance(m, dict))


def _classify(market: dict[str, Any], event: dict[str, Any], *, valid_neg_risk: bool | None = None) -> tuple[str, str, float | None, str | None]:
    text = " ".join(str(market.get(k, "")) + " " + str(event.get(k, "")) for k in ("question", "slug", "title", "tags", "sport", "league")).lower()
    end = market.get("endDate") or event.get("endDate") or event.get("gameStartTime")
    horizon = None
    if end:
        try: horizon = max(0.0, (datetime.fromisoformat(str(end).replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds())
        except ValueError: pass
    if event.get("negRisk"):
        valid = negrisk_event_valid(event) if valid_neg_risk is None else valid_neg_risk
        return ("negrisk_structural", "neg_risk", horizon, None) if valid else ("unsupported", "unsupported", horizon, "invalid_neg_risk_event_basket")
    if any(x in text for x in ("nba", "nfl", "mlb", "nhl", "soccer", "tennis", "golf", "sports")): return "sports_event_driven", "sports", horizon, None
    if any(x in text for x in ("bitcoin", "btc", "ethereum", "eth", "solana", "crypto")) and horizon is not None and horizon <= 7 * 86400: return "short_duration_crypto", "crypto", horizon, None
    return "maker_spread_rebate", "other", horizon, None


def _fee_metadata(market: dict[str, Any]) -> dict[str, Any]:
    schedule = market.get("fee_schedule") or market.get("feeSchedule")
    if isinstance(schedule, dict) and schedule.get("rate") is not None:
        return {"fee_rate": float(schedule["rate"]), "fee_source": "polymarket_fee_schedule", "maker_rebate_rate": float(schedule["rebate_rate"]) if schedule.get("rebate_rate") is not None else None, "maker_rebate_economics": "schedule_rate_only"}
    if market.get("takerBaseFee") is not None: return {"fee_rate": float(market["takerBaseFee"]), "fee_source": "polymarket_taker_base_fee", "maker_rebate_rate": None, "maker_rebate_economics": "UNKNOWN"}
    if market.get("feesEnabled") is False: return {"fee_rate": 0.0, "fee_source": "polymarket_fees_disabled", "maker_rebate_rate": None, "maker_rebate_economics": "UNKNOWN"}
    return {"fee_rate": None, "fee_source": "UNKNOWN", "maker_rebate_rate": None, "maker_rebate_economics": "UNKNOWN"}


def _read_revenue_control(production_db: Path | None = None) -> dict[str, Any]:
    path = (production_db or canonical_production_db()).resolve()
    if not path.exists(): raise RuntimeError(f"Revenue control unavailable: production DB missing: {path}")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5); conn.execute("PRAGMA query_only=ON")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"revenue_poc_positions", "revenue_poc_evaluations", "revenue_poc_equity_points"}
        missing = required - tables
        if missing: raise RuntimeError(f"Revenue control schema unavailable: {sorted(missing)}")
        position_count = conn.execute("SELECT COUNT(*) FROM revenue_poc_positions").fetchone()[0]
        horizon_mix = dict(conn.execute("SELECT COALESCE(CAST(ROUND(expected_holding_days) AS TEXT),'UNKNOWN'),COUNT(*) FROM revenue_poc_positions GROUP BY 1").fetchall())
        latest = conn.execute("SELECT realized_pnl_usd,unrealized_pnl_usd,deployed_capital_usd FROM revenue_poc_equity_points ORDER BY timestamp_utc DESC,id DESC LIMIT 1").fetchone()
        if latest is None: raise RuntimeError("Revenue control unavailable: no equity point")
        opportunity_count = conn.execute("SELECT COUNT(*) FROM revenue_poc_evaluations").fetchone()[0]
        admission_count = conn.execute("SELECT COUNT(*) FROM revenue_poc_evaluations WHERE lower(production_decision) IN ('admitted','admit','open','accepted','candidate_pending_approval')").fetchone()[0]
        row = {"captured_at_utc": utc_now(), "production_db_path": str(path), "position_count": position_count, "horizon_mix": horizon_mix, "realized_pnl_usd": float(latest[0]), "unrealized_pnl_usd": float(latest[1]), "deployed_capital_usd": float(latest[2]), "opportunity_count": opportunity_count, "admission_count": admission_count, "status": "OK"}
        conn.close(); return row
    except Exception:
        try: conn.close()
        except UnboundLocalError: pass
        raise


def capture_revenue_control(store: CensusStore, production_db: Path | None = None) -> dict[str, Any]:
    row = _read_revenue_control(production_db)
    store.record_revenue_control(row)
    store.commit()
    return row


def _episode_key(engine: str, market_id: str) -> str:
    return hashlib.sha256(f"{engine}:{market_id}".encode()).hexdigest()


@dataclass
class BootstrapClassification:
    markets: dict[str, tuple[dict[str, Any], dict[str, Any], str, str, float | None]]
    events: dict[str, dict[str, Any]]
    eligible: dict[str, set[str]]
    tracked: dict[str, set[str]]
    neg_risk_rows: list[dict[str, Any]]
    stream_assets: list[str]
    observed_markets: int


def _classify_bootstrap(payload: Any, *, control_positions: int, timeout_seconds: float) -> BootstrapClassification:
    if not isinstance(payload, list):
        raise StartupPhaseError("Gamma bootstrap response is not an event list")
    deadline = time.monotonic() + timeout_seconds
    markets: dict[str, tuple[dict[str, Any], dict[str, Any], str, str, float | None]] = {}
    events: dict[str, dict[str, Any]] = {}
    eligible = {name: set() for name in ENGINE_NAMES}
    tracked = {name: set() for name in ENGINE_NAMES}
    eligible["revenue_directional_control"] = {f"control:{i}" for i in range(control_positions)}
    tracked["revenue_directional_control"] = set(eligible["revenue_directional_control"])
    neg_risk_rows: list[dict[str, Any]] = []
    observed_market_ids: set[str] = set()

    for event_index, event in enumerate(payload):
        if time.monotonic() >= deadline:
            raise StartupPhaseError(f"market_token_classification timed out after {timeout_seconds:.1f}s at event {event_index}")
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("id") or event.get("slug") or "")
        events[event_id] = event
        event_markets = event.get("markets", []) if isinstance(event.get("markets"), list) else []
        valid_neg_risk = negrisk_event_valid(event) if event.get("negRisk") else False
        if event.get("negRisk"):
            neg_risk_rows.append({"event_id": event_id, "candidate_basket": 1, "validated_basket": int(valid_neg_risk), "rejection_reason": None if valid_neg_risk else "invalid_neg_risk_event_basket", "executable_simultaneous_depth_usd": None, "gross_structural_edge_usd": None, "net_economics_status": "UNKNOWN", "metadata": {"market_count": len(event_markets)}})
        for market_index, market in enumerate(event_markets):
            if market_index % 128 == 0 and time.monotonic() >= deadline:
                raise StartupPhaseError(f"market_token_classification timed out after {timeout_seconds:.1f}s at event {event_index} market {market_index}")
            if not isinstance(market, dict):
                continue
            engine, category, horizon, _unsupported_reason = _classify(market, event, valid_neg_risk=valid_neg_risk)
            market_id = str(market.get("id") or market.get("conditionId") or market.get("slug") or "")
            observed_market_ids.add(market_id)
            if engine in eligible:
                eligible[engine].add(market_id)
            for token in _list(market.get("clobTokenIds")):
                markets[str(token)] = (market, event, engine, category, horizon)

    stream_assets: list[str] = []
    stream_asset_set: set[str] = set()
    basket_budget = MAX_STREAM_ASSETS // 2
    neg_events = sorted((event for event in events.values() if negrisk_event_valid(event)), key=lambda event: len(event.get("markets", [])))
    for event in neg_events:
        basket_tokens = [str(token) for child in event.get("markets", []) for token in _list(child.get("clobTokenIds"))]
        additions = [token for token in basket_tokens if token in markets and token not in stream_asset_set]
        if len(stream_assets) + len(additions) <= basket_budget:
            stream_assets.extend(additions)
            stream_asset_set.update(additions)
    grouped: dict[str, list[str]] = {engine: [] for engine in ENGINE_NAMES}
    for token, (_market, _event, engine, _category, _horizon) in markets.items():
        if token not in stream_asset_set and engine in grouped:
            grouped[engine].append(token)
    offsets = {engine: 0 for engine in ENGINE_NAMES}
    while len(stream_assets) < MAX_STREAM_ASSETS and any(offsets[engine] < len(grouped[engine]) for engine in ENGINE_NAMES):
        for engine in ENGINE_NAMES:
            index = offsets[engine]
            if index < len(grouped[engine]) and len(stream_assets) < MAX_STREAM_ASSETS:
                token = grouped[engine][index]
                offsets[engine] += 1
                stream_assets.append(token)
                stream_asset_set.add(token)
    if not stream_assets:
        raise StartupPhaseError("market_token_classification produced no public stream assets")
    if time.monotonic() >= deadline:
        raise StartupPhaseError(f"market_token_classification timed out after {timeout_seconds:.1f}s while constructing the subscription")
    return BootstrapClassification(markets, events, eligible, tracked, neg_risk_rows, stream_assets, len(observed_market_ids))


def _append_log(log_path: Path | None, event_type: str, detail: dict[str, Any]) -> None:
    if log_path is None:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"timestamp_utc": utc_now(), "event_type": event_type, **detail}, sort_keys=True) + "\n")


class StartupRecorder:
    def __init__(self, store: CensusStore, log_path: Path | None) -> None:
        self.store = store
        self.log_path = log_path
        self.current_phase = "initializing"
        self.started: dict[str, float] = {}

    def emit(self, phase: str, state: str, detail: dict[str, Any] | None = None) -> None:
        now = utc_now()
        self.current_phase = phase
        values = dict(detail or {})
        if state == "before":
            self.started[phase] = time.monotonic()
        elif phase in self.started:
            values["elapsed_seconds"] = time.monotonic() - self.started[phase]
        event_type = f"startup_phase_{state}"
        self.store.update_status("FAILED" if state == "failed" else "STARTING", phase, now, error=values.get("error"))
        self.store.event(now, event_type, {"phase": phase, **values})
        self.store.commit()
        _append_log(self.log_path, event_type, {"phase": phase, **values})


async def run_stream(*, db: Path, pid: Path, limit: int, duration_hours: float | None, log_path: Path | None) -> None:
    del pid  # PID lifecycle is owned by run_forever.
    started_utc, started_mono = utc_now(), time.monotonic()
    store = CensusStore(db)
    store.initialize_status(started_utc)
    store.event(started_utc, "collector_starting", {"status": "STARTING"})
    store.commit()
    _append_log(log_path, "collector_starting", {"status": "STARTING"})
    recorder = StartupRecorder(store, log_path)
    stats = StreamStats(started_utc)
    deadline_task: asyncio.Task[None] | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    automatic_deadline_reached = False
    startup_complete = False

    def fail_phase(phase: str, exc: BaseException) -> StartupPhaseError:
        error = f"{type(exc).__name__}: {exc}"
        recorder.emit(phase, "failed", {"error": error})
        return StartupPhaseError(f"{phase} failed: {error}")

    try:
        recorder.emit("production_control_capture", "before", {"timeout_seconds": PRODUCTION_CONTROL_TIMEOUT})
        try:
            async with asyncio.timeout(PRODUCTION_CONTROL_TIMEOUT):
                control = await asyncio.to_thread(_read_revenue_control)
                production_db = Path(control["production_db_path"])
                initial_cycle = await asyncio.to_thread(authoritative_production_cycle, production_db)
                store.record_revenue_control(control)
                store.record_production_cycle(initial_cycle)
        except Exception as exc:
            raise fail_phase("production_control_capture", exc) from exc
        recorder.emit("production_control_capture", "after", {"position_count": control["position_count"], "cycle_id": initial_cycle.get("cycle_id")})

        client = PublicGetClient()
        params = urllib.parse.urlencode({"limit": min(max(limit, 1), 1000), "offset": 0, "active": "true", "closed": "false", "order": "liquidity", "ascending": "false"})
        gamma_url = f"https://gamma-api.polymarket.com/events?{params}"
        recorder.emit("gamma_bootstrap", "before", {"timeout_seconds": GAMMA_BOOTSTRAP_TIMEOUT, "requested_events": limit})
        try:
            payload = await asyncio.wait_for(asyncio.to_thread(client.get, gamma_url), timeout=GAMMA_BOOTSTRAP_TIMEOUT)
            if not isinstance(payload, list):
                raise TypeError("Gamma response was not a list")
        except Exception as exc:
            raise fail_phase("gamma_bootstrap", exc) from exc
        recorder.emit("gamma_bootstrap", "after", {"observed_events": len(payload)})

        recorder.emit("market_token_classification", "before", {"timeout_seconds": CLASSIFICATION_TIMEOUT})
        try:
            bootstrap = _classify_bootstrap(payload, control_positions=int(control["position_count"]), timeout_seconds=CLASSIFICATION_TIMEOUT)
        except Exception as exc:
            raise fail_phase("market_token_classification", exc) from exc
        recorder.emit("market_token_classification", "after", {"observed_markets": bootstrap.observed_markets, "observed_tokens": len(bootstrap.markets), "streamed_tokens": len(bootstrap.stream_assets)})

        markets, eligible, tracked = bootstrap.markets, bootstrap.eligible, bootstrap.tracked
        books, tracker, previous_mid = BookState(), DurabilityTracker(), {}
        active_labels: dict[str, tuple[str, str, str | None]] = {}
        neg_live_seen: set[str] = set()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        last_cycle_check = time.monotonic()
        last_persist = 0.0
        latest_cycle = initial_cycle

        for signum in (signal.SIGTERM, signal.SIGINT):
            try: loop.add_signal_handler(signum, stop.set)
            except (NotImplementedError, RuntimeError): pass

        async def deadline() -> None:
            nonlocal automatic_deadline_reached
            if duration_hours is None:
                return
            remaining = max(0.0, duration_hours * 3600 - (time.monotonic() - started_mono))
            await asyncio.sleep(remaining)
            automatic_deadline_reached = True
            stop.set()

        async def heartbeat() -> None:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(
                        stop.wait(), timeout=COLLECTOR_HEARTBEAT_INTERVAL
                    )
                except TimeoutError:
                    if startup_complete:
                        store.update_status("RUNNING", "running", utc_now())
                        store.commit()

        deadline_task = asyncio.create_task(deadline())
        heartbeat_task = asyncio.create_task(heartbeat())

        def stream_health_row(*, stopped_at_utc: str | None = None) -> dict[str, Any]:
            duration = time.monotonic() - started_mono
            return {"started_at_utc": started_utc, "stopped_at_utc": stopped_at_utc, "connection_count": stats.connection_count, "reconnect_count": stats.reconnect_count, "disconnect_count": stats.disconnect_count, "protocol_error_count": stats.protocol_error_count, "error_count": stats.error_count, "last_message_at_utc": stats.last_message_at_utc, "messages": stats.messages, "messages_per_second": stats.messages / duration if duration else 0, "stale_stream_events": stats.stale_stream_events, "max_recovery_seconds": stats.max_recovery_seconds, "duration_seconds": duration, "automatic_shutdown": int(automatic_deadline_reached), "details": stats.details}

        def engine_rows() -> list[dict[str, Any]]:
            rows = []
            for engine in ENGINE_NAMES:
                supported = int(engine == "revenue_directional_control" or bool(eligible[engine]))
                reason = None if supported else "no eligible markets in bounded bootstrap sample"
                if engine == "revenue_directional_control":
                    reason = "read-only production control; no public stream route"
                rows.append({"alpha_engine": engine, "eligible_markets": len(eligible[engine]), "tracked_markets": len(tracked[engine]), "opportunity_episodes": store.conn.execute("SELECT COUNT(*) FROM opportunity_episodes WHERE alpha_engine=?", (engine,)).fetchone()[0], "supported": supported, "unsupported_reason": reason})
            unsupported_markets = {str(value[0].get("id") or value[0].get("conditionId") or value[0].get("slug") or "") for value in markets.values() if value[2] == "unsupported"}
            rows.append({"alpha_engine": "unsupported", "eligible_markets": len(unsupported_markets), "tracked_markets": 0, "opportunity_episodes": 0, "supported": 0, "unsupported_reason": "invalid_neg_risk_event_basket or no supported live model"})
            return rows

        def persist_runtime(*, stopped_at_utc: str | None = None) -> None:
            duration = time.monotonic() - started_mono
            episodes = store.conn.execute("SELECT COUNT(*) FROM opportunity_episodes").fetchone()[0]
            health = stream_health_row(stopped_at_utc=stopped_at_utc)
            store.record_stream_health(health)
            store.record_engine_coverage(engine_rows())
            store.record_resources({"captured_at_utc": stopped_at_utc or utc_now(), "cpu_user_seconds": resource.getrusage(resource.RUSAGE_SELF).ru_utime, "cpu_system_seconds": resource.getrusage(resource.RUSAGE_SELF).ru_stime, "max_rss_bytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss), "db_bytes": db.stat().st_size if db.exists() else 0, "wal_bytes": db.with_name(db.name + "-wal").stat().st_size if db.with_name(db.name + "-wal").exists() else 0, "persisted_episodes": episodes, "episodes_per_minute": episodes / max(duration / 60, 1), "production_freshness_seconds": latest_cycle.get("freshness_seconds"), "details": {"messages_per_second": health["messages_per_second"], "production_cycle_source": latest_cycle.get("source")}})
            store.commit()

        async def on_startup_event(phase: str, state: str, detail: dict[str, Any]) -> None:
            recorder.emit(phase, state, detail)

        def persist_closed(states: list[Any] | tuple[Any, ...]) -> None:
            for state in states:
                engine, market_id, event_id = active_labels.pop(
                    state.key,
                    ("maker_spread_rebate", state.tracking_key, None),
                )
                store.record_episode(
                    state,
                    engine=engine,
                    market_id=market_id,
                    event_id=event_id,
                    ended_at_utc=utc_now(),
                    metadata={
                        "checkpoint_reasons": {
                            str(checkpoint): reason
                            for checkpoint, reason in state.checkpoint_reasons.items()
                            if reason is not None
                        }
                    },
                )

        async def on_stream_gap(reason: str, mono: int) -> None:
            closed = tracker.close_all(reason=reason)
            if closed:
                persist_closed(closed)
                store.event(
                    utc_now(),
                    "durability_observation_gap",
                    {"reason": reason, "closed_intervals": len(closed), "at_ns": mono},
                )
                store.commit()

        async def on_message(message: dict[str, Any], mono: int) -> None:
            nonlocal last_cycle_check, last_persist, latest_cycle, startup_complete
            if time.monotonic() - last_cycle_check >= 60.0:
                latest_cycle = await asyncio.to_thread(authoritative_production_cycle, production_db)
                store.record_production_cycle(latest_cycle)
                last_cycle_check = time.monotonic()
                if latest_cycle["warning"]: store.event(latest_cycle["captured_at_utc"], "production_cycle_warning", latest_cycle)
            persist_closed(tracker.expire(mono))
            changed = books.apply(message)
            for asset in changed:
                if asset not in markets: continue
                market, event, engine, _category, _horizon = markets[asset]; market_id = str(market.get("id") or market.get("conditionId") or market.get("slug")); tracked[engine].add(market_id)
                quote = books.top(asset); book_valid = all(quote[k] is not None and quote[k] > 0 for k in ("bid", "ask", "bid_size", "ask_size")); mid = (quote["bid"] + quote["ask"]) / 2 if book_valid else None; movement = mid - previous_mid[asset] if mid is not None and asset in previous_mid and previous_mid[asset] is not None else None; previous_mid[asset] = mid
                meta = _fee_metadata(market); depth = min(float(quote["bid"] * quote["bid_size"]), float(quote["ask"] * quote["ask_size"])) if book_valid else None; adverse = movement
                qualifying = book_valid
                economic_executable: bool | None = None
                execution_validation_status = "NOT_ECONOMICALLY_VALIDATED"
                durability_basis = "observable_two_sided_book"
                gross_edge = (quote["ask"] - quote["bid"]) * min(float(quote["bid_size"]), float(quote["ask_size"])) if book_valid else None
                episode_bid, episode_ask = quote["bid"], quote["ask"]
                tracking_market_id = market_id
                if engine == "negrisk_structural":
                    eid = str(event.get("id") or event.get("slug") or ""); basket = []
                    basket_assets = {
                        str(tokens[0])
                        for child in event.get("markets", [])
                        if (tokens := _list(child.get("clobTokenIds")))
                    }
                    if asset not in basket_assets:
                        continue
                    for child in event.get("markets", []):
                        child_tokens = _list(child.get("clobTokenIds")); child_quote = books.top(str(child_tokens[0])) if child_tokens else {}; basket.append(child_quote)
                    basket_valid = bool(basket) and all(q.get("ask") is not None and q.get("ask_size", 0) > 0 for q in basket)
                    gross_edge = None
                    depth = None
                    if basket_valid:
                        asks = [float(q["ask"]) for q in basket]; sizes = [float(q["ask_size"]) for q in basket]; gross = 1.0 - sum(asks); simultaneous = min(a * s for a, s in zip(asks, sizes))
                        if eid not in neg_live_seen:
                            neg_live_seen.add(eid); store.record_neg_risk(utc_now(), event_id=eid, candidate_basket=1, validated_basket=1, executable_simultaneous_depth_usd=simultaneous, gross_structural_edge_usd=gross, net_economics_status="UNKNOWN", metadata={"fee_source": meta["fee_source"], "slippage_source": "UNKNOWN"})
                        gross_edge = gross; depth = simultaneous
                    economic_executable = bool(basket_valid and gross_edge is not None and gross_edge > 0)
                    qualifying = economic_executable
                    book_valid = basket_valid
                    execution_validation_status = "VALIDATED_EXECUTABLE" if economic_executable else "VALIDATED_NOT_EXECUTABLE"
                    durability_basis = "negrisk_structural_executable_basket"
                    episode_bid, episode_ask = None, None
                    tracking_market_id = eid
                if engine == "unsupported": continue
                tracking_key = _episode_key(engine, tracking_market_id)
                transition = tracker.observe(
                    tracking_key,
                    mono,
                    utc_now(),
                    qualifying=qualifying,
                    book_valid=book_valid,
                    economic_executable=economic_executable,
                    execution_validation_status=execution_validation_status,
                    durability_basis=durability_basis,
                    bid=episode_bid,
                    ask=episode_ask,
                    depth_usd=depth,
                    gross_edge_usd=gross_edge,
                    movement=movement,
                    adverse_selection=adverse,
                )
                persist_closed(transition.closed)
                state = transition.active
                if state is not None:
                    active_labels[state.key] = (engine, tracking_market_id, str(event.get("id") or event.get("slug") or "") or None)
                    state.fee_source, state.fee_rate = meta["fee_source"], meta["fee_rate"]
                    state.maker_rebate_rate, state.maker_rebate_economics = meta["maker_rebate_rate"], meta["maker_rebate_economics"]
                    state.slippage_source = "UNKNOWN"
                    state.rejection_reason = "fill_probability_not_measured" if engine == "maker_spread_rebate" else "directional_fill_model_not_implemented"

            if not startup_complete:
                recorder.emit("initial_coverage_engine_persistence", "before", {"timeout_seconds": INITIAL_PERSISTENCE_TIMEOUT})
                persistence_started = time.monotonic()
                store.record_coverage(utc_now(), sampling_mode="gamma_events_active_closed_false_order_liquidity", requested_events=limit, observed_events=len(payload), observed_markets=bootstrap.observed_markets, observed_tokens=len(markets), exclusion_reason="bounded liquidity-ordered bootstrap; stream subscription sampled to protect public WebSocket", metadata={"stream_url": "wss://ws-subscriptions-clob.polymarket.com/ws/market", "streamed_tokens": len(bootstrap.stream_assets), "stream_asset_limit": MAX_STREAM_ASSETS})
                for row in bootstrap.neg_risk_rows:
                    store.record_neg_risk(utc_now(), **row)
                persist_runtime()
                elapsed = time.monotonic() - persistence_started
                if elapsed > INITIAL_PERSISTENCE_TIMEOUT:
                    raise StartupPhaseError(f"initial_coverage_engine_persistence timed out after {elapsed:.1f}s")
                recorder.emit("initial_coverage_engine_persistence", "after", {"coverage_rows": 1, "engine_coverage_rows": len(ENGINE_NAMES) + 1, "messages": stats.messages})
                now = utc_now()
                store.update_status("RUNNING", "running", now)
                store.event(now, "collector_running", {"status": "RUNNING", "messages": stats.messages})
                store.commit()
                _append_log(log_path, "collector_running", {"status": "RUNNING", "messages": stats.messages})
                startup_complete = True
                last_persist = time.monotonic()
            elif time.monotonic() - last_persist >= RUNTIME_PERSIST_INTERVAL:
                persist_runtime()
                last_persist = time.monotonic()

        await consume_market_stream(bootstrap.stream_assets, on_message, stop=stop, stats=stats, startup_event=on_startup_event, on_stream_gap=on_stream_gap, connect_timeout=WEBSOCKET_CONNECT_TIMEOUT, subscription_timeout=SUBSCRIPTION_SEND_TIMEOUT, first_message_timeout=FIRST_MESSAGE_TIMEOUT)
        if not startup_complete:
            raise StartupPhaseError("market stream ended before RUNNING criteria were satisfied")

        persist_closed(tracker.close_all(reason="collector_stopped"))
        latest_cycle = await asyncio.to_thread(authoritative_production_cycle, production_db)
        store.record_production_cycle(latest_cycle)
        if latest_cycle["warning"]: store.event(latest_cycle["captured_at_utc"], "production_cycle_warning", latest_cycle)
        stopped_utc = utc_now()
        persist_runtime(stopped_at_utc=stopped_utc)
        store.update_status("STOPPED", "stopped", stopped_utc)
        store.event(stopped_utc, "stopped", {"transport": "public_market_websocket", "automatic_shutdown": int(automatic_deadline_reached)})
        store.commit()
        _append_log(log_path, "stopped", {"transport": "public_market_websocket", "automatic_shutdown": int(automatic_deadline_reached)})
    except Exception as exc:
        stats.record("error", {"error": f"{type(exc).__name__}: {exc}"})
        failed_at = utc_now()
        try:
            store.record_stream_health({"started_at_utc": started_utc, "stopped_at_utc": failed_at, "connection_count": stats.connection_count, "reconnect_count": stats.reconnect_count, "disconnect_count": stats.disconnect_count, "protocol_error_count": stats.protocol_error_count, "error_count": stats.error_count, "last_message_at_utc": stats.last_message_at_utc, "messages": stats.messages, "messages_per_second": stats.messages / max(time.monotonic() - started_mono, 0.001), "stale_stream_events": stats.stale_stream_events, "max_recovery_seconds": stats.max_recovery_seconds, "duration_seconds": time.monotonic() - started_mono, "automatic_shutdown": 0, "details": stats.details})
            store.update_status("FAILED", recorder.current_phase, failed_at, error=f"{type(exc).__name__}: {exc}")
            store.event(failed_at, "collector_failed", {"phase": recorder.current_phase, "error": f"{type(exc).__name__}: {exc}"})
            store.commit()
            _append_log(log_path, "collector_failed", {"phase": recorder.current_phase, "error": f"{type(exc).__name__}: {exc}"})
        except Exception as persistence_exc:  # noqa: BLE001 - preserve the original collector failure
            _append_log(log_path, "collector_failure_persistence_error", {"error": f"{type(persistence_exc).__name__}: {persistence_exc}", "original_error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        for task in (deadline_task, heartbeat_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        store.close()


def run_stream_forever(**kwargs: Any) -> None: asyncio.run(run_stream(**kwargs))


def read_collector_status(db: Path) -> dict[str, Any] | None:
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True, timeout=1)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT status,phase,started_at_utc,updated_at_utc,running_at_utc,stopped_at_utc,error FROM collector_status WHERE id=1").fetchone()
        conn.close()
    except (sqlite3.Error, OSError):
        return None
    return dict(row) if row is not None else None


def run_forever(*, db: Path = DEFAULT_DB, pid: Path = DEFAULT_PID, interval: float = 5.0, limit: int = 100, duration_hours: float | None = None, log_path: Path | None = None) -> None:
    del interval
    pid.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(pid, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.write(fd, str(os.getpid()).encode()); os.close(fd)
    except FileExistsError as exc: raise RuntimeError(f"census already has PID file: {pid}") from exc
    try: run_stream_forever(db=db, pid=pid, limit=limit, duration_hours=duration_hours, log_path=log_path)
    except Exception as exc:
        _append_log(log_path, "collector_error", {"error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        try: pid.unlink()
        except FileNotFoundError: pass
