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


def _classify(market: dict[str, Any], event: dict[str, Any]) -> tuple[str, str, float | None, str | None]:
    text = " ".join(str(market.get(k, "")) + " " + str(event.get(k, "")) for k in ("question", "slug", "title", "tags", "sport", "league")).lower()
    end = market.get("endDate") or event.get("endDate") or event.get("gameStartTime")
    horizon = None
    if end:
        try: horizon = max(0.0, (datetime.fromisoformat(str(end).replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds())
        except ValueError: pass
    if event.get("negRisk"):
        return ("negrisk_structural", "neg_risk", horizon, None) if negrisk_event_valid(event) else ("unsupported", "unsupported", horizon, "invalid_neg_risk_event_basket")
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


def capture_revenue_control(store: CensusStore, production_db: Path | None = None) -> dict[str, Any]:
    path = (production_db or canonical_production_db()).resolve()
    if not path.exists(): raise RuntimeError(f"Revenue control unavailable: production DB missing: {path}")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True); conn.execute("PRAGMA query_only=ON")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"revenue_poc_positions", "revenue_poc_evaluations", "revenue_poc_equity_points"}
        missing = required - tables
        if missing: raise RuntimeError(f"Revenue control schema unavailable: {sorted(missing)}")
        position_count = conn.execute("SELECT COUNT(*) FROM revenue_poc_positions").fetchone()[0]
        horizon_mix = dict(conn.execute("SELECT COALESCE(CAST(ROUND(expected_holding_days) AS TEXT),'UNKNOWN'),COUNT(*) FROM revenue_poc_positions GROUP BY 1").fetchall())
        latest = conn.execute("SELECT realized_pnl_usd,unrealized_pnl_usd,deployed_capital_usd FROM revenue_poc_equity_points ORDER BY timestamp_utc DESC,id DESC LIMIT 1").fetchone()
        if latest is None: raise RuntimeError("Revenue control unavailable: no equity point")
        opportunity_count = conn.execute("SELECT COUNT(*) FROM revenue_poc_evaluations").fetchone()[0]
        admission_count = conn.execute("SELECT COUNT(*) FROM revenue_poc_evaluations WHERE lower(production_decision) IN ('admitted','admit','open','accepted')").fetchone()[0]
        row = {"captured_at_utc": utc_now(), "production_db_path": str(path), "position_count": position_count, "horizon_mix": horizon_mix, "realized_pnl_usd": float(latest[0]), "unrealized_pnl_usd": float(latest[1]), "deployed_capital_usd": float(latest[2]), "opportunity_count": opportunity_count, "admission_count": admission_count, "status": "OK"}
        store.record_revenue_control(row); store.commit(); conn.close(); return row
    except Exception:
        try: conn.close()
        except UnboundLocalError: pass
        raise


def _episode_key(engine: str, market_id: str) -> str:
    return hashlib.sha256(f"{engine}:{market_id}".encode()).hexdigest()


async def run_stream(*, db: Path, pid: Path, limit: int, duration_hours: float | None, log_path: Path | None) -> None:
    started_utc, started_mono = utc_now(), time.monotonic()
    client = PublicGetClient(); params = urllib.parse.urlencode({"limit": min(max(limit, 1), 1000), "offset": 0, "active": "true", "closed": "false", "order": "liquidity", "ascending": "false"})
    payload = client.get(f"https://gamma-api.polymarket.com/events?{params}")
    store = CensusStore(db); capture_revenue_control(store)
    markets: dict[str, tuple[dict[str, Any], dict[str, Any], str, str, float | None]] = {}; events: dict[str, dict[str, Any]] = {}
    eligible = {name: set() for name in ENGINE_NAMES}; tracked = {name: set() for name in ENGINE_NAMES}; neg_seen: set[str] = set()
    for event in payload if isinstance(payload, list) else []:
        event_id = str(event.get("id") or event.get("slug") or "")
        events[event_id] = event
        for market in event.get("markets", []) if isinstance(event, dict) else []:
            engine, category, horizon, unsupported_reason = _classify(market, event); market_id = str(market.get("id") or market.get("conditionId") or market.get("slug"));
            if engine in eligible: eligible[engine].add(market_id)
            tokens = _list(market.get("clobTokenIds"))
            for token in tokens: markets[str(token)] = (market, event, engine, category, horizon)
            if event.get("negRisk") and event_id not in neg_seen:
                neg_seen.add(event_id); store.record_neg_risk(utc_now(), event_id=event_id, candidate_basket=1, validated_basket=int(engine == "negrisk_structural"), rejection_reason=unsupported_reason, executable_simultaneous_depth_usd=None, gross_structural_edge_usd=None, net_economics_status="UNKNOWN", metadata={"market_count": len(event.get("markets", []))})
    store.record_coverage(started_utc, sampling_mode="gamma_events_active_closed_false_order_liquidity", requested_events=limit, observed_events=len(payload) if isinstance(payload, list) else 0, observed_markets=len({v[0].get("id") or v[0].get("slug") for v in markets.values()}), observed_tokens=len(markets), exclusion_reason="bounded liquidity-ordered bootstrap; not entire Polymarket universe", metadata={"stream_url": "wss://ws-subscriptions-clob.polymarket.com/ws/market"})
    books, tracker, previous_mid = BookState(), DurabilityTracker(), {}; active_labels: dict[str, tuple[str, str, str | None]] = {}; stats = StreamStats(started_utc); stop = asyncio.Event(); loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try: loop.add_signal_handler(signum, stop.set)
        except (NotImplementedError, RuntimeError): pass
    async def deadline() -> None:
        if duration_hours is not None: await asyncio.sleep(max(0.0, duration_hours * 3600)); stop.set()
    deadline_task = asyncio.create_task(deadline())
    async def on_message(message: dict[str, Any], mono: int) -> None:
        changed = books.apply(message)
        for asset in changed:
            if asset not in markets: continue
            market, event, engine, category, horizon = markets[asset]; market_id = str(market.get("id") or market.get("conditionId") or market.get("slug")); tracked[engine].add(market_id)
            quote = books.top(asset); executable = all(quote[k] is not None and quote[k] > 0 for k in ("bid", "ask", "bid_size", "ask_size")); mid = (quote["bid"] + quote["ask"]) / 2 if executable else None; movement = mid - previous_mid[asset] if mid is not None and asset in previous_mid and previous_mid[asset] is not None else None; previous_mid[asset] = mid
            meta = _fee_metadata(market); depth = min(float(quote["bid"] * quote["bid_size"]), float(quote["ask"] * quote["ask_size"])) if executable else None; adverse = movement
            if engine == "negrisk_structural":
                eid = str(event.get("id") or event.get("slug") or ""); basket = []
                for child in event.get("markets", []):
                    child_tokens = _list(child.get("clobTokenIds")); child_quote = books.top(str(child_tokens[0])) if child_tokens else {}; basket.append(child_quote)
                if basket and all(q.get("ask") is not None and q.get("ask_size", 0) > 0 for q in basket):
                    asks = [float(q["ask"]) for q in basket]; sizes = [float(q["ask_size"]) for q in basket]; gross = 1.0 - sum(asks); simultaneous = min(a * s for a, s in zip(asks, sizes)); store.record_neg_risk(utc_now(), event_id=eid, candidate_basket=1, validated_basket=1, executable_simultaneous_depth_usd=simultaneous, gross_structural_edge_usd=gross, net_economics_status="UNKNOWN", metadata={"fee_source": meta["fee_source"], "slippage_source": "UNKNOWN"})
                    executable = gross > 0; depth = simultaneous
            if engine == "unsupported": continue
            key = _episode_key(engine, market_id); active_labels[key] = (engine, market_id, str(event.get("id") or event.get("slug") or "") or None)
            state = tracker.observe(key, mono, utc_now(), executable=executable, bid=quote["bid"], ask=quote["ask"], depth_usd=depth, movement=movement, adverse_selection=adverse)
            state.gross_edge_usd = (quote["ask"] - quote["bid"]) * min(float(quote["bid_size"]), float(quote["ask_size"])) if executable else state.gross_edge_usd; state.fee_source, state.fee_rate = meta["fee_source"], meta["fee_rate"]; state.maker_rebate_rate, state.maker_rebate_economics = meta["maker_rebate_rate"], meta["maker_rebate_economics"]; state.slippage_source = "UNKNOWN"; state.rejection_reason = "fill_probability_not_measured" if engine == "maker_spread_rebate" else "directional_fill_model_not_implemented"
        for state in tracker.expire(mono):
            engine, market_id, event_id = active_labels.pop(state.key, ("maker_spread_rebate", state.key, None)); store.record_episode(state, engine=engine, market_id=market_id, event_id=event_id, ended_at_utc=utc_now(), metadata={})
    try:
        await consume_market_stream(list(markets), on_message, stop=stop, stats=stats)
    finally:
        for key, state in list(tracker.active.items()):
            closed = tracker.disappear(key, time.monotonic_ns(), reason=None, continuous=True)
            if closed:
                engine, market_id, event_id = active_labels.get(key, ("maker_spread_rebate", key, None)); store.record_episode(closed, engine=engine, market_id=market_id, event_id=event_id, ended_at_utc=utc_now(), metadata={})
        stopped_utc = utc_now(); duration = time.monotonic() - started_mono; stats_row = {"started_at_utc": started_utc, "stopped_at_utc": stopped_utc, "connection_count": stats.connection_count, "reconnect_count": stats.reconnect_count, "disconnect_count": stats.disconnect_count, "protocol_error_count": stats.protocol_error_count, "error_count": stats.error_count, "last_message_at_utc": stats.last_message_at_utc, "messages": stats.messages, "messages_per_second": stats.messages / duration if duration else 0, "stale_stream_events": stats.stale_stream_events, "max_recovery_seconds": stats.max_recovery_seconds, "duration_seconds": duration, "automatic_shutdown": int(duration_hours is not None and duration >= duration_hours * 3600), "details": stats.details}; store.record_stream_health(stats_row)
        store.record_engine_coverage([{"alpha_engine": e, "eligible_markets": len(eligible[e]), "tracked_markets": len(tracked[e]), "opportunity_episodes": store.conn.execute("SELECT COUNT(*) FROM opportunity_episodes WHERE alpha_engine=?", (e,)).fetchone()[0], "supported": 1, "unsupported_reason": None} for e in ENGINE_NAMES] + [{"alpha_engine": "unsupported", "eligible_markets": len([1 for v in markets.values() if v[2] == "unsupported"]), "tracked_markets": 0, "opportunity_episodes": 0, "supported": 0, "unsupported_reason": "invalid_neg_risk_event_basket or no supported live model"}]); store.record_resources({"captured_at_utc": stopped_utc, "cpu_user_seconds": resource.getrusage(resource.RUSAGE_SELF).ru_utime, "cpu_system_seconds": resource.getrusage(resource.RUSAGE_SELF).ru_stime, "max_rss_bytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if os.uname().sysname != "Darwin" else resource.getrusage(resource.RUSAGE_SELF).ru_maxrss), "db_bytes": db.stat().st_size if db.exists() else 0, "wal_bytes": db.with_name(db.name + "-wal").stat().st_size if db.with_name(db.name + "-wal").exists() else 0, "persisted_episodes": store.conn.execute("SELECT COUNT(*) FROM opportunity_episodes").fetchone()[0], "episodes_per_minute": store.conn.execute("SELECT COUNT(*) FROM opportunity_episodes").fetchone()[0] / max(duration / 60, 1), "details": {"messages_per_second": stats.messages / duration if duration else 0}}); store.event(stopped_utc, "stopped", {"transport": "public_market_websocket", "automatic_shutdown": stats_row["automatic_shutdown"]}); store.close(); deadline_task.cancel()


def run_stream_forever(**kwargs: Any) -> None: asyncio.run(run_stream(**kwargs))


def run_forever(*, db: Path = DEFAULT_DB, pid: Path = DEFAULT_PID, interval: float = 5.0, limit: int = 100, duration_hours: float | None = None, log_path: Path | None = None) -> None:
    pid.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(pid, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.write(fd, str(os.getpid()).encode()); os.close(fd)
    except FileExistsError as exc: raise RuntimeError(f"census already has PID file: {pid}") from exc
    try: run_stream_forever(db=db, pid=pid, limit=limit, duration_hours=duration_hours, log_path=log_path)
    except Exception as exc:
        if log_path: log_path.write_text(json.dumps({"timestamp_utc": utc_now(), "event_type": "collector_error", "error": f"{type(exc).__name__}: {exc}"}) + "\n", encoding="utf-8")
        raise
    finally:
        try: pid.unlink()
        except FileNotFoundError: pass
