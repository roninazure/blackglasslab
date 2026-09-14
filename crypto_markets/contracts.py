from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


@dataclass(frozen=True)
class CryptoContract:
    event_id: str
    market_id: str
    condition_id: str
    slug: str
    asset: str
    contract_type: str
    strike_usd: float
    expiry_utc: str
    yes_token: str
    no_token: str
    indicative_yes_price: float | None
    indicative_no_price: float | None


_ASSET_MAP = {
    "bitcoin": "BTC",
    "btc": "BTC",
    "ethereum": "ETH",
    "eth": "ETH",
}

_PATTERNS = (
    (
        "ABOVE",
        re.compile(
            r"^will the price of "
            r"(bitcoin|btc|ethereum|eth) "
            r"be above \$([0-9][0-9,]*(?:\.[0-9]+)?) "
            r"on .+\?$",
            re.IGNORECASE,
        ),
    ),
    (
        "REACH",
        re.compile(
            r"^will "
            r"(bitcoin|btc|ethereum|eth) "
            r"reach \$([0-9][0-9,]*(?:\.[0-9]+)?) "
            r"on .+\?$",
            re.IGNORECASE,
        ),
    ),
    (
        "DIP",
        re.compile(
            r"^will "
            r"(bitcoin|btc|ethereum|eth) "
            r"dip to \$([0-9][0-9,]*(?:\.[0-9]+)?) "
            r"on .+\?$",
            re.IGNORECASE,
        ),
    ),
)


def _parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value

    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []

        return parsed if isinstance(parsed, list) else []

    return []


def _parse_utc(value: Any) -> str | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return (
        parsed.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _price(values: list[Any], index: int) -> float | None:
    try:
        value = float(values[index])
    except (IndexError, TypeError, ValueError):
        return None

    if 0.0 <= value <= 1.0:
        return value

    return None


def normalize_crypto_contract(
    market: Mapping[str, Any],
    *,
    event: Mapping[str, Any] | None = None,
) -> CryptoContract | None:
    event = event or {}

    question = str(market.get("question") or "").strip()

    if not question:
        return None

    matched_type = None
    asset = None
    strike = None

    for contract_type, pattern in _PATTERNS:
        match = pattern.fullmatch(question)

        if match is None:
            continue

        matched_type = contract_type
        asset = _ASSET_MAP[match.group(1).lower()]

        try:
            strike = float(match.group(2).replace(",", ""))
        except ValueError:
            return None

        break

    if matched_type is None or asset is None or strike is None:
        return None

    if strike <= 0:
        return None

    expiry = _parse_utc(
        market.get("endDate")
        or market.get("endDateIso")
        or event.get("endDate")
    )

    if expiry is None:
        return None

    outcomes = _parse_json_list(market.get("outcomes"))
    tokens = _parse_json_list(market.get("clobTokenIds"))
    prices = _parse_json_list(market.get("outcomePrices"))

    if len(outcomes) != 2 or len(tokens) != 2:
        return None

    normalized_outcomes = [
        str(value).strip().lower()
        for value in outcomes
    ]

    if sorted(normalized_outcomes) != ["no", "yes"]:
        return None

    yes_index = normalized_outcomes.index("yes")
    no_index = normalized_outcomes.index("no")

    yes_token = str(tokens[yes_index]).strip()
    no_token = str(tokens[no_index]).strip()

    if not yes_token or not no_token or yes_token == no_token:
        return None

    market_id = str(market.get("id") or "").strip()
    condition_id = str(market.get("conditionId") or "").strip()

    if not market_id or not condition_id:
        return None

    return CryptoContract(
        event_id=str(event.get("id") or "").strip(),
        market_id=market_id,
        condition_id=condition_id,
        slug=str(market.get("slug") or "").strip(),
        asset=asset,
        contract_type=matched_type,
        strike_usd=strike,
        expiry_utc=expiry,
        yes_token=yes_token,
        no_token=no_token,
        indicative_yes_price=_price(prices, yes_index),
        indicative_no_price=_price(prices, no_index),
    )
