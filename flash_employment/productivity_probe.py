from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.message import Message
from pathlib import Path
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
PROBE_EVENT_DATE = date(2026, 9, 3)
PROBE_SOURCES = {
    "prod2_current": "https://www.bls.gov/news.release/prod2.nr0.htm",
    "prod2_toc": "https://www.bls.gov/news.release/prod2.toc.htm",
}
MAX_PAYLOAD = 4_000_000


class ProbeFailure(RuntimeError):
    """The bounded publication probe could not observe the target release."""


def _normalized(payload: bytes) -> str:
    text = payload.decode("utf-8", errors="replace")
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.replace("&nbsp;", " ").split())


def validates_target_productivity_release(payload: bytes) -> bool:
    text = _normalized(payload).lower()
    target_heading = re.search(
        r"productivity and costs,?\s+second quarter 2026,\s*revised", text
    )
    return bool(target_heading) and all(
        required in text
        for required in ("september 3, 2026", "8:30 a.m. (et)")
    )


def validates_previous_productivity_release(payload: bytes) -> bool:
    text = _normalized(payload).lower()
    return (
        "productivity and costs" in text
        and "second quarter 2026" in text
        and "preliminary" in text
        and "august 6, 2026" in text
        and not validates_target_productivity_release(payload)
    )


def _header(headers: Message | None, name: str) -> str | None:
    value = headers.get(name) if headers is not None else None
    return value.strip() if value else None


@dataclass(frozen=True)
class ProbeAttempt:
    source_name: str
    source_url: str
    phase: str
    request_number: int
    request_started_wall_ns: int
    request_started_monotonic_ns: int
    body_complete_wall_ns: int
    body_complete_monotonic_ns: int
    parse_complete_wall_ns: int
    parse_complete_monotonic_ns: int
    http_status: int | None
    http_date: str | None
    etag: str | None
    last_modified: str | None
    cache_control: str | None
    payload: bytes
    payload_sha256: str
    validation_result: str
    rejection_reason: str | None


class ProductivityHttpClient:
    def __init__(
        self,
        source_name: str,
        *,
        contact: str,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.source_name = source_name
        self.url = PROBE_SOURCES[source_name]
        self.timeout_seconds = timeout_seconds
        self.user_agent = (
            "Mozilla/5.0 (compatible; ParallaxBLSPublicationProbe/1.0; "
            f"contact={contact.strip()})"
        )
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.etag: str | None = None
        self.last_modified: str | None = None

    def fetch(
        self,
        *,
        phase: str,
        request_number: int,
        baseline_sha256: str | None,
    ) -> ProbeAttempt:
        start_wall, start_mono = time.time_ns(), time.monotonic_ns()
        headers = {
            "Accept": "text/html",
            "User-Agent": self.user_agent,
            "Cache-Control": "no-cache, max-age=0",
            "Pragma": "no-cache",
        }
        if phase != "BASELINE" and self.etag:
            headers["If-None-Match"] = self.etag
        if phase != "BASELINE" and self.last_modified:
            headers["If-Modified-Since"] = self.last_modified
        status: int | None = None
        response_headers: Message | None = None
        payload = b""
        validation: str | None = None
        rejection: str | None = None
        try:
            request = urllib.request.Request(self.url, method="GET", headers=headers)
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                status = int(response.status)
                response_headers = response.headers
                payload = response.read(MAX_PAYLOAD + 1)
                if len(payload) > MAX_PAYLOAD:
                    validation = "INVALID"
                    rejection = "response exceeds bounded payload size"
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            response_headers = exc.headers
            validation = "UNCHANGED" if status == 304 else "HTTP_ERROR"
            rejection = f"HTTP {status}"
        except TimeoutError as exc:
            validation, rejection = "TIMEOUT", f"{type(exc).__name__}: {exc}"
        except urllib.error.URLError as exc:
            timeout = isinstance(exc.reason, TimeoutError)
            validation = "TIMEOUT" if timeout else "HTTP_ERROR"
            rejection = f"URLError: {exc.reason}"
        body_wall, body_mono = time.time_ns(), time.monotonic_ns()
        payload_hash = hashlib.sha256(payload).hexdigest()
        if status == 200:
            self.etag = _header(response_headers, "ETag") or self.etag
            self.last_modified = (
                _header(response_headers, "Last-Modified") or self.last_modified
            )
        if validation is None:
            if status != 200:
                validation, rejection = "HTTP_ERROR", f"unexpected HTTP {status}"
            elif not payload:
                validation, rejection = "INVALID", "empty HTTP 200 payload"
            elif phase == "BASELINE":
                if self.source_name == "prod2_current":
                    valid_previous = validates_previous_productivity_release(payload)
                    validation = "BASELINE_PREVIOUS" if valid_previous else "INVALID"
                    rejection = None if valid_previous else "not the expected Q2 preliminary release"
                else:
                    validation = "BASELINE_RECORDED"
            elif baseline_sha256 == payload_hash:
                validation, rejection = "UNCHANGED", "payload hash matches baseline"
            elif validates_target_productivity_release(payload):
                validation = "VALID_TARGET"
            else:
                validation, rejection = (
                    "CHANGED_INVALID",
                    "changed payload does not validate as Q2 2026 Revised",
                )
        parse_wall, parse_mono = time.time_ns(), time.monotonic_ns()
        return ProbeAttempt(
            self.source_name,
            self.url,
            phase,
            request_number,
            start_wall,
            start_mono,
            body_wall,
            body_mono,
            parse_wall,
            parse_mono,
            status,
            _header(response_headers, "Date"),
            _header(response_headers, "ETag"),
            _header(response_headers, "Last-Modified"),
            _header(response_headers, "Cache-Control"),
            payload,
            payload_hash,
            validation,
            rejection,
        )


PROBE_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS probe_runs (
  id INTEGER PRIMARY KEY,
  event_date TEXT NOT NULL,
  scheduled_release_wall_ns INTEGER NOT NULL,
  started_wall_ns INTEGER NOT NULL,
  started_monotonic_ns INTEGER NOT NULL,
  contact_configured INTEGER NOT NULL,
  status TEXT NOT NULL,
  error TEXT
);
CREATE TABLE IF NOT EXISTS probe_attempts (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES probe_runs(id),
  source_name TEXT NOT NULL,
  source_url TEXT NOT NULL,
  phase TEXT NOT NULL,
  request_number INTEGER NOT NULL,
  request_started_wall_ns INTEGER NOT NULL,
  request_started_monotonic_ns INTEGER NOT NULL,
  body_complete_wall_ns INTEGER NOT NULL,
  body_complete_monotonic_ns INTEGER NOT NULL,
  parse_complete_wall_ns INTEGER NOT NULL,
  parse_complete_monotonic_ns INTEGER NOT NULL,
  http_status INTEGER,
  http_date TEXT,
  etag TEXT,
  last_modified TEXT,
  cache_control TEXT,
  payload BLOB NOT NULL,
  payload_length INTEGER NOT NULL,
  payload_sha256 TEXT NOT NULL,
  validation_result TEXT NOT NULL,
  rejection_reason TEXT,
  UNIQUE(run_id,source_name,request_number)
);
CREATE TABLE IF NOT EXISTS probe_summary (
  run_id INTEGER PRIMARY KEY REFERENCES probe_runs(id),
  first_valid_source TEXT,
  first_valid_observed_at TEXT,
  scheduled_to_first_valid_ms REAL,
  source_arrival_deltas_json TEXT NOT NULL
);
"""


class ProbeStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(PROBE_SCHEMA)

    def start(self, event_date: date, scheduled_wall_ns: int) -> int:
        cursor = self.conn.execute(
            """INSERT INTO probe_runs
            (event_date,scheduled_release_wall_ns,started_wall_ns,started_monotonic_ns,
             contact_configured,status) VALUES (?,?,?,?,1,'RUNNING')""",
            (event_date.isoformat(), scheduled_wall_ns, time.time_ns(), time.monotonic_ns()),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def attempt(self, run_id: int, attempt: ProbeAttempt) -> None:
        self.conn.execute(
            """INSERT INTO probe_attempts
            (run_id,source_name,source_url,phase,request_number,request_started_wall_ns,
             request_started_monotonic_ns,body_complete_wall_ns,body_complete_monotonic_ns,
             parse_complete_wall_ns,parse_complete_monotonic_ns,http_status,http_date,etag,
             last_modified,cache_control,payload,payload_length,payload_sha256,
             validation_result,rejection_reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                attempt.source_name,
                attempt.source_url,
                attempt.phase,
                attempt.request_number,
                attempt.request_started_wall_ns,
                attempt.request_started_monotonic_ns,
                attempt.body_complete_wall_ns,
                attempt.body_complete_monotonic_ns,
                attempt.parse_complete_wall_ns,
                attempt.parse_complete_monotonic_ns,
                attempt.http_status,
                attempt.http_date,
                attempt.etag,
                attempt.last_modified,
                attempt.cache_control,
                attempt.payload,
                len(attempt.payload),
                attempt.payload_sha256,
                attempt.validation_result,
                attempt.rejection_reason,
            ),
        )
        self.conn.commit()

    def finish(
        self,
        run_id: int,
        *,
        status: str,
        error: str | None,
        first: ProbeAttempt | None,
        scheduled_wall_ns: int,
        valid_by_source: dict[str, ProbeAttempt],
    ) -> None:
        self.conn.execute(
            "UPDATE probe_runs SET status=?,error=? WHERE id=?", (status, error, run_id)
        )
        if first is not None:
            deltas = {
                source: (attempt.parse_complete_monotonic_ns - first.parse_complete_monotonic_ns)
                / 1e6
                for source, attempt in valid_by_source.items()
            }
            observed = datetime.fromtimestamp(
                first.parse_complete_wall_ns / 1e9, UTC
            ).isoformat().replace("+00:00", "Z")
            self.conn.execute(
                """INSERT OR REPLACE INTO probe_summary
                (run_id,first_valid_source,first_valid_observed_at,
                 scheduled_to_first_valid_ms,source_arrival_deltas_json)
                VALUES (?,?,?,?,?)""",
                (
                    run_id,
                    first.source_name,
                    observed,
                    (first.parse_complete_wall_ns - scheduled_wall_ns) / 1e6,
                    __import__("json").dumps(deltas, sort_keys=True),
                ),
            )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


@dataclass(frozen=True)
class ProbeConfig:
    event_date: date
    release_at: datetime
    duration_minutes: float
    db_path: Path
    contact: str


def first_valid_attempt(
    valid_by_source: dict[str, ProbeAttempt],
) -> ProbeAttempt:
    if not valid_by_source:
        raise ProbeFailure("no valid target release observed inside bounded window")
    return min(
        valid_by_source.values(), key=lambda item: item.parse_complete_monotonic_ns
    )


async def run_probe(config: ProbeConfig) -> tuple[int, dict[str, object]]:
    if config.event_date != PROBE_EVENT_DATE:
        raise ProbeFailure(f"probe is bounded to {PROBE_EVENT_DATE}")
    if not config.contact.strip():
        raise ProbeFailure("FLASH_CONTACT is missing or empty")
    scheduled_wall_ns = int(config.release_at.timestamp() * 1e9)
    store = ProbeStore(config.db_path)
    run_id = store.start(config.event_date, scheduled_wall_ns)
    clients = {
        source: ProductivityHttpClient(source, contact=config.contact)
        for source in PROBE_SOURCES
    }
    counts = {source: 0 for source in PROBE_SOURCES}
    baselines: dict[str, ProbeAttempt] = {}
    valid_by_source: dict[str, ProbeAttempt] = {}
    try:
        for source, client in clients.items():
            counts[source] += 1
            attempt = await asyncio.to_thread(
                client.fetch,
                phase="BASELINE",
                request_number=counts[source],
                baseline_sha256=None,
            )
            baselines[source] = attempt
            store.attempt(run_id, attempt)
            print(
                f"PROBE BASELINE source={source} http={attempt.http_status} "
                f"validation={attempt.validation_result} sha256={attempt.payload_sha256}",
                flush=True,
            )
        primary = baselines["prod2_current"]
        if primary.validation_result != "BASELINE_PREVIOUS":
            raise ProbeFailure(
                f"prod2 current baseline failed: {primary.validation_result} "
                f"{primary.rejection_reason or ''}"
            )
        observation_start = config.release_at - timedelta(seconds=5)
        delay = (observation_start - datetime.now(EASTERN)).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)
        end = config.release_at + timedelta(minutes=config.duration_minutes)

        async def worker(source: str) -> None:
            client = clients[source]
            baseline_age = (
                time.monotonic_ns()
                - baselines[source].request_started_monotonic_ns
            ) / 1e9
            if baseline_age < 1.0:
                await asyncio.sleep(1.0 - baseline_age)
            while datetime.now(EASTERN) < end and source not in valid_by_source:
                counts[source] += 1
                attempt = await asyncio.to_thread(
                    client.fetch,
                    phase="RELEASE_OBSERVATION",
                    request_number=counts[source],
                    baseline_sha256=baselines[source].payload_sha256,
                )
                store.attempt(run_id, attempt)
                if attempt.validation_result == "VALID_TARGET":
                    valid_by_source[source] = attempt
                    print(
                        f"PROBE VALID source={source} observed="
                        f"{datetime.fromtimestamp(attempt.parse_complete_wall_ns / 1e9, UTC).isoformat()} ",
                        flush=True,
                    )
                    return
                elapsed = (datetime.now(EASTERN) - config.release_at).total_seconds()
                interval = 1.0 if elapsed < 30 else 5.0
                since_start = (
                    time.monotonic_ns() - attempt.request_started_monotonic_ns
                ) / 1e9
                await asyncio.sleep(max(0.0, interval - since_start))

        await asyncio.gather(*(worker(source) for source in PROBE_SOURCES))
        first = first_valid_attempt(valid_by_source)
        result = {
            "first_valid_source": first.source_name,
            "first_valid_observed_at": datetime.fromtimestamp(
                first.parse_complete_wall_ns / 1e9, UTC
            ).isoformat().replace("+00:00", "Z"),
            "scheduled_to_first_valid_ms": (
                first.parse_complete_wall_ns - scheduled_wall_ns
            )
            / 1e6,
            "source_arrival_deltas_ms": {
                source: (attempt.parse_complete_monotonic_ns - first.parse_complete_monotonic_ns)
                / 1e6
                for source, attempt in valid_by_source.items()
            },
            "measurement_note": (
                "local observation after scheduled wall-clock time; does not prove "
                "when BLS internally published the file"
            ),
        }
        store.finish(
            run_id,
            status="COMPLETE",
            error=None,
            first=first,
            scheduled_wall_ns=scheduled_wall_ns,
            valid_by_source=valid_by_source,
        )
        return run_id, result
    except Exception as exc:
        first = (
            min(valid_by_source.values(), key=lambda item: item.parse_complete_monotonic_ns)
            if valid_by_source
            else None
        )
        store.finish(
            run_id,
            status="FAILED",
            error=f"{type(exc).__name__}: {exc}",
            first=first,
            scheduled_wall_ns=scheduled_wall_ns,
            valid_by_source=valid_by_source,
        )
        raise
    finally:
        store.close()
