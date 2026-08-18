from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import pstdev
from typing import Any

from .reference import COINBASE_EXCHANGE_BASE_URL, PRODUCTS


FIVE_MINUTES = 300
ANNUALIZATION_PERIODS_5M = 365.0 * 24.0 * 12.0


@dataclass(frozen=True)
class RealizedVolatility:
    asset: str
    product_id: str
    candle_count: int
    return_count: int
    window_hours: float
    granularity_seconds: int
    realized_vol_annualized: float
    first_candle_utc: str
    last_candle_utc: str
    source: str = "coinbase_exchange_candles"


def _utc(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_candle(row: Any) -> tuple[int, float]:
    if not isinstance(row, list) or len(row) < 5:
        raise ValueError("Coinbase candle row is malformed")

    try:
        timestamp = int(row[0])
        close = float(row[4])
    except (TypeError, ValueError) as exc:
        raise ValueError("Coinbase candle values are malformed") from exc

    if timestamp <= 0:
        raise ValueError("candle timestamp must be positive")

    if close <= 0:
        raise ValueError("candle close must be positive")

    return timestamp, close


def fetch_coinbase_candles(
    asset: str,
    *,
    window_hours: float = 24.0,
    granularity_seconds: int = FIVE_MINUTES,
    now: datetime | None = None,
    timeout: float = 10.0,
) -> list[tuple[int, float]]:
    normalized_asset = str(asset).strip().upper()

    try:
        product_id = PRODUCTS[normalized_asset]
    except KeyError as exc:
        raise ValueError(
            f"unsupported crypto volatility asset: {asset!r}"
        ) from exc

    if window_hours <= 0:
        raise ValueError("window_hours must be positive")

    if granularity_seconds != FIVE_MINUTES:
        raise ValueError(
            "initial crypto volatility model requires 300-second candles"
        )

    max_points = math.ceil(
        window_hours * 3600.0 / granularity_seconds
    )

    if max_points > 300:
        raise ValueError(
            "requested candle window exceeds Coinbase 300-candle limit"
        )

    now = now or datetime.now(timezone.utc)

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    end = now.astimezone(timezone.utc)
    start = end - timedelta(hours=window_hours)

    params = urllib.parse.urlencode(
        {
            "granularity": granularity_seconds,
            "start": _utc(start),
            "end": _utc(end),
        }
    )

    url = (
        f"{COINBASE_EXCHANGE_BASE_URL}/products/"
        f"{product_id}/candles?{params}"
    )

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "swarm-edge-crypto-volatility/0.1",
            "Accept": "application/json",
        },
    )

    with urllib.request.build_opener(
        urllib.request.ProxyHandler({})
    ).open(req, timeout=timeout) as response:
        payload = json.load(response)

    if not isinstance(payload, list):
        raise RuntimeError("Coinbase candles response is not a list")

    candles = [_parse_candle(row) for row in payload]

    candles.sort(key=lambda row: row[0])

    deduplicated: list[tuple[int, float]] = []

    for row in candles:
        if deduplicated and row[0] == deduplicated[-1][0]:
            deduplicated[-1] = row
        else:
            deduplicated.append(row)

    if len(deduplicated) < 3:
        raise RuntimeError("insufficient Coinbase candle history")

    return deduplicated


def realized_volatility(
    asset: str,
    *,
    window_hours: float = 24.0,
    now: datetime | None = None,
    timeout: float = 10.0,
) -> RealizedVolatility:
    normalized_asset = str(asset).strip().upper()

    candles = fetch_coinbase_candles(
        normalized_asset,
        window_hours=window_hours,
        granularity_seconds=FIVE_MINUTES,
        now=now,
        timeout=timeout,
    )

    log_returns = []

    for (_, previous), (_, current) in zip(
        candles,
        candles[1:],
    ):
        if previous <= 0 or current <= 0:
            raise ValueError("candle close must be positive")

        log_returns.append(math.log(current / previous))

    if len(log_returns) < 2:
        raise RuntimeError("insufficient returns for volatility estimate")

    sigma_period = pstdev(log_returns)
    sigma_annual = sigma_period * math.sqrt(
        ANNUALIZATION_PERIODS_5M
    )

    if not math.isfinite(sigma_annual) or sigma_annual <= 0:
        raise RuntimeError("invalid realized volatility estimate")

    product_id = PRODUCTS[normalized_asset]

    return RealizedVolatility(
        asset=normalized_asset,
        product_id=product_id,
        candle_count=len(candles),
        return_count=len(log_returns),
        window_hours=window_hours,
        granularity_seconds=FIVE_MINUTES,
        realized_vol_annualized=sigma_annual,
        first_candle_utc=_utc(
            datetime.fromtimestamp(
                candles[0][0],
                tz=timezone.utc,
            )
        ),
        last_candle_utc=_utc(
            datetime.fromtimestamp(
                candles[-1][0],
                tz=timezone.utc,
            )
        ),
    )
