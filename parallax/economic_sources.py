"""Small cached clients for authoritative economic observations."""

from __future__ import annotations

import json
import os
import csv
import io
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .economic_evidence import EconomicSubtype

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
ALFRED_GRAPH_URL = "https://alfred.stlouisfed.org/graph/alfredgraph.csv"
BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
DEFAULT_CACHE_SECONDS = 15 * 60


def _freshness_seconds(observation_date: str) -> float:
    try:
        observed = datetime.fromisoformat(observation_date).replace(tzinfo=UTC)
        return max(0.0, (datetime.now(UTC) - observed).total_seconds())
    except ValueError:
        return float("inf")


@dataclass(frozen=True)
class EconomicObservation:
    source: str
    series_id: str
    observation_date: str
    value: float
    retrieved_at: str
    freshness_seconds: float
    vintage_start: str | None = None
    vintage_end: str | None = None


class _TTLCache:
    def __init__(self, ttl_seconds: float):
        self.ttl_seconds = ttl_seconds
        self._lock = RLock()
        self._values: dict[tuple, tuple[float, EconomicObservation]] = {}

    def get(self, key: tuple) -> EconomicObservation | None:
        with self._lock:
            item = self._values.get(key)
            if item and time.monotonic() - item[0] < self.ttl_seconds:
                return item[1]
            return None

    def put(self, key: tuple, value: EconomicObservation) -> EconomicObservation:
        with self._lock:
            self._values[key] = (time.monotonic(), value)
        return value


class FREDClient:
    """Credentialed FRED observations with explicit vintage query parameters."""

    def __init__(self, *, api_key: str | None = None, ttl_seconds: float = DEFAULT_CACHE_SECONDS, transport=None):
        self.api_key = api_key or os.environ.get("FRED_API_KEY")
        self.transport = transport or self._get
        self.cache = _TTLCache(ttl_seconds)
        self._historical_cache: dict[tuple, list[tuple[str, float]]] = {}

    @staticmethod
    def _get(url: str) -> dict:
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "PARALLAX-economic/1"})
        with urlopen(request, timeout=8) as response:
            return json.load(response)

    def latest(self, series_id: str, *, vintage_start: str | None = None, vintage_end: str | None = None) -> EconomicObservation:
        if not self.api_key:
            raise RuntimeError("missing_fred_api_key")
        key = (series_id, vintage_start, vintage_end)
        cached = self.cache.get(key)
        if cached:
            return cached
        params = {"api_key": self.api_key, "file_type": "json", "series_id": series_id, "sort_order": "desc", "limit": "1"}
        if vintage_start:
            params["realtime_start"] = vintage_start
        if vintage_end:
            params["realtime_end"] = vintage_end
        payload = self.transport(f"{FRED_URL}?{urlencode(params)}")
        observations = payload.get("observations") if isinstance(payload, dict) else None
        if not observations:
            raise RuntimeError(f"fred_no_observation:{series_id}")
        row = observations[0]
        try:
            value = float(row["value"])
            if not value == value:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise RuntimeError(f"fred_invalid_observation:{series_id}") from None
        now = datetime.now(UTC).isoformat()
        observation_date = str(row["date"])
        return self.cache.put(key, EconomicObservation("FRED", series_id, observation_date, value, now, _freshness_seconds(observation_date), vintage_start, vintage_end))

    def historical(self, series_id: str, *, vintage_date: str, start: str, end: str) -> list[tuple[str, float]]:
        """Return ALFRED observations as known on ``vintage_date``.

        This is intentionally separate from ``latest``: the experiment must
        never substitute a currently revised value for a point-in-time value.
        The graph endpoint is public and does not require the FRED API key.
        """
        key = ("historical", series_id, vintage_date, start, end)
        cached = self._historical_cache.get(key)
        if cached is not None:
            return cached
        params = {"id": series_id, "vintage_date": vintage_date, "cosd": start, "coed": end}
        request = Request(f"{ALFRED_GRAPH_URL}?{urlencode(params)}")
        with urlopen(request, timeout=60) as response:
            text = response.read().decode("utf-8")
        rows: list[tuple[str, float]] = []
        for row in csv.DictReader(io.StringIO(text)):
            raw = row.get("observation_date")
            value = next((value for name, value in row.items() if name != "observation_date"), "")
            try:
                if raw and value not in (None, "", "."):
                    rows.append((raw, float(value)))
            except ValueError:
                continue
        if not rows:
            raise RuntimeError(f"alfred_no_observations:{series_id}:{vintage_date}")
        self._historical_cache[key] = rows
        return rows


class BLSClient:
    """Credential-free bounded BLS public API client with TTL caching."""

    def __init__(self, *, ttl_seconds: float = DEFAULT_CACHE_SECONDS, transport=None):
        self.transport = transport or self._post
        self.cache = _TTLCache(ttl_seconds)

    @staticmethod
    def _post(url: str, body: dict) -> dict:
        request = Request(url, data=json.dumps(body).encode(), headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "PARALLAX-economic/1"}, method="POST")
        with urlopen(request, timeout=8) as response:
            return json.load(response)

    def latest(self, series_id: str) -> EconomicObservation:
        key = (series_id,)
        cached = self.cache.get(key)
        if cached:
            return cached
        year = str(datetime.now(UTC).year)
        payload = self.transport(BLS_URL, {"seriesid": [series_id], "startyear": str(int(year) - 1), "endyear": year})
        rows = ((payload.get("Results") or {}).get("series") or [{}])[0].get("data") or []
        row = next((item for item in rows if item.get("value") not in (None, "")), None)
        if row is None:
            raise RuntimeError(f"bls_no_observation:{series_id}")
        try:
            value = float(row["value"])
        except (KeyError, TypeError, ValueError):
            raise RuntimeError(f"bls_invalid_observation:{series_id}") from None
        now = datetime.now(UTC).isoformat()
        observation_date = f"{row.get('year')}-{row.get('period', '').removeprefix('M') or '00'}-01"
        return self.cache.put(key, EconomicObservation("BLS", series_id, observation_date, value, now, _freshness_seconds(observation_date)))


class EconomicSources:
    """Maps ECON subtypes to source observations; it does not forecast."""

    FRED_SERIES = {EconomicSubtype.FED_RATES: "DFF", EconomicSubtype.CPI_INFLATION: "CPIAUCSL", EconomicSubtype.GDP: "GDP", EconomicSubtype.RECESSION: "USREC"}
    BLS_SERIES = {EconomicSubtype.EMPLOYMENT: "LNS14000000"}

    def __init__(self, fred: FREDClient | None = None, bls: BLSClient | None = None):
        self.fred = fred or FREDClient()
        self.bls = bls or BLSClient()

    def latest(self, subtype: EconomicSubtype) -> EconomicObservation:
        if subtype in self.FRED_SERIES:
            return self.fred.latest(self.FRED_SERIES[subtype])
        series = {EconomicSubtype.EMPLOYMENT: "LNS14000000"}.get(subtype)
        if series:
            return self.bls.latest(series)
        raise RuntimeError(f"no_authoritative_series_mapping:{subtype.value}")


__all__ = ["BLSClient", "EconomicObservation", "EconomicSources", "FREDClient"]
