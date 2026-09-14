from __future__ import annotations

import json
import math
import re
from calendar import month_name
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

BRACKETS = ("<-50k", "-50k–0", "0–50k", "50k–100k", "100k–150k", "150k+")
_MARKET_LABELS = {
    "<-50k": "<-50k",
    "-50k – 0": "-50k–0",
    "0 – 50k": "0–50k",
    "50k – 100k": "50k–100k",
    "100k – 150k": "100k–150k",
    "150k+": "150k+",
}
_RULE_SENTENCE = "If the reported value falls exactly between two brackets, then this market will resolve to the higher range bracket."


def resolve_payroll_change(change_jobs: int) -> str:
    """Map a reported integer payroll change to exactly one market bracket."""
    if isinstance(change_jobs, bool) or not isinstance(change_jobs, int):
        raise TypeError("payroll change must be an integer number of jobs")
    if change_jobs < -50_000:
        return BRACKETS[0]
    if change_jobs < 0:
        return BRACKETS[1]
    if change_jobs < 50_000:
        return BRACKETS[2]
    if change_jobs < 100_000:
        return BRACKETS[3]
    if change_jobs < 150_000:
        return BRACKETS[4]
    return BRACKETS[5]


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


@dataclass(frozen=True)
class Contract:
    bracket: str
    market_id: str
    condition_id: str
    yes_token: str
    no_token: str
    fee_rate: float
    fee_exponent: float
    fee_source: str


@dataclass(frozen=True)
class MarketBundle:
    event_id: str
    event_slug: str
    title: str
    rules: str
    resolution_source: str
    contracts: tuple[Contract, ...]

    @property
    def assets(self) -> tuple[str, ...]:
        return tuple(
            token
            for contract in self.contracts
            for token in (contract.yes_token, contract.no_token)
        )

    def known_one_dollar_claims(
        self, winning_bracket: str
    ) -> tuple[tuple[Contract, str, str], ...]:
        if winning_bracket not in BRACKETS:
            raise ValueError(f"unknown winning bracket: {winning_bracket}")
        return tuple(
            (contract, "YES", contract.yes_token)
            if contract.bracket == winning_bracket
            else (contract, "NO", contract.no_token)
            for contract in self.contracts
        )


def validate_market_event(
    event: dict[str, Any],
    *,
    expected_slug: str,
    reference_year: int,
    reference_month: int,
    release_at: datetime,
) -> MarketBundle:
    """Compile current Gamma metadata into a fail-closed six-contract mapping."""
    if str(event.get("slug") or "") != expected_slug:
        raise ValueError("Polymarket event slug mismatch")
    if (
        event.get("active") is not True
        or event.get("closed") is True
        or event.get("enableOrderBook") is not True
    ):
        raise ValueError("Polymarket event is not active with an enabled order book")
    title = str(event.get("title") or "")
    expected_title = f"How many jobs added in {month_name[reference_month]}?"
    if title != expected_title:
        raise ValueError(f"unexpected Polymarket title: {title!r}")
    rules = str(event.get("description") or "")
    expected_period = f"{month_name[reference_month]} {reference_year}"
    if (
        "change in the total nonfarm payroll employment" not in rules
        or expected_period not in rules
    ):
        raise ValueError(
            "Polymarket rules do not identify the expected payroll measure and period"
        )
    if _RULE_SENTENCE not in rules:
        raise ValueError("Polymarket higher-boundary rule is missing or changed")
    if release_at.astimezone(UTC).strftime("%B %-d, %Y") not in rules:
        raise ValueError("Polymarket rules do not identify the configured release date")
    if "8:30 AM ET" not in rules:
        raise ValueError("Polymarket rules do not identify the configured release time")
    source = str(event.get("resolutionSource") or "")
    if not re.fullmatch(r"https://www\.bls\.gov/.*", source):
        raise ValueError("Polymarket resolution source is not official BLS")

    markets = event.get("markets")
    if not isinstance(markets, list) or len(markets) != len(BRACKETS):
        raise ValueError(
            "Polymarket event does not contain exactly six bracket markets"
        )
    contracts: list[Contract] = []
    seen: set[str] = set()
    for market in markets:
        if (
            not isinstance(market, dict)
            or market.get("active") is not True
            or market.get("closed") is True
        ):
            raise ValueError("Polymarket child market is not active")
        raw_label = str(market.get("groupItemTitle") or "")
        bracket = _MARKET_LABELS.get(raw_label)
        if bracket is None or bracket in seen:
            raise ValueError(f"unrecognized or duplicate bracket label: {raw_label!r}")
        outcomes = [str(item).lower() for item in _json_list(market.get("outcomes"))]
        tokens = [str(item) for item in _json_list(market.get("clobTokenIds"))]
        if (
            outcomes != ["yes", "no"]
            or len(tokens) != 2
            or any(not token.isdigit() for token in tokens)
        ):
            raise ValueError(f"incomplete YES/NO token mapping for {bracket}")
        schedule = market.get("feeSchedule")
        if market.get("feesEnabled") is not True or not isinstance(schedule, dict):
            raise ValueError(
                f"authoritative taker fee schedule unavailable for {bracket}"
            )
        try:
            rate = float(schedule["rate"])
            exponent = float(schedule["exponent"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"malformed fee schedule for {bracket}") from exc
        if (
            not math.isfinite(rate)
            or not math.isfinite(exponent)
            or rate < 0
            or exponent < 0
        ):
            raise ValueError(f"invalid fee schedule for {bracket}")
        market_id = str(market.get("id") or "")
        condition_id = str(market.get("conditionId") or "")
        if not market_id or not condition_id:
            raise ValueError(f"missing market identity for {bracket}")
        contracts.append(
            Contract(
                bracket,
                market_id,
                condition_id,
                tokens[0],
                tokens[1],
                rate,
                exponent,
                "gamma.feeSchedule",
            )
        )
        seen.add(bracket)
    if seen != set(BRACKETS):
        raise ValueError(
            "Polymarket brackets do not form the required exhaustive mapping"
        )
    contracts.sort(key=lambda contract: BRACKETS.index(contract.bracket))
    return MarketBundle(
        str(event.get("id") or ""),
        expected_slug,
        title,
        rules,
        source,
        tuple(contracts),
    )


@dataclass(frozen=True)
class KnownPayoutEconomics:
    token: str
    outcome_side: str
    price: float
    available_shares: float
    capital_required: float
    gross_edge_per_share: float
    fee_per_share: float
    fees: float
    slippage_per_share: float
    conservative_slippage: float
    net_edge_per_share: float
    net_executable_dollars: float
    return_on_capital: float


def evaluate_known_payout(
    *,
    token: str,
    outcome_side: str,
    ask_levels: Iterable[tuple[float, float]],
    fee_rate: float,
    fee_exponent: float,
    slippage_bps: float = 10.0,
) -> tuple[KnownPayoutEconomics, ...]:
    """Evaluate each displayed ask level of a claim known to pay exactly $1."""
    if outcome_side not in {"YES", "NO"}:
        raise ValueError("outcome_side must be YES or NO")
    if fee_rate < 0 or fee_exponent < 0 or slippage_bps < 0:
        raise ValueError("cost inputs cannot be negative")
    rows: list[KnownPayoutEconomics] = []
    for raw_price, raw_size in sorted(ask_levels):
        price, size = float(raw_price), float(raw_size)
        if not (
            math.isfinite(price) and math.isfinite(size) and 0 < price < 1 and size > 0
        ):
            continue
        fee_per_share = fee_rate * (price * (1.0 - price)) ** fee_exponent
        slippage_per_share = price * slippage_bps / 10_000.0
        net_edge = 1.0 - price - fee_per_share - slippage_per_share
        if net_edge <= 0:
            continue
        purchase = price * size
        fees = fee_per_share * size
        slippage = slippage_per_share * size
        capital = purchase + fees + slippage
        rows.append(
            KnownPayoutEconomics(
                token=token,
                outcome_side=outcome_side,
                price=price,
                available_shares=size,
                capital_required=capital,
                gross_edge_per_share=1.0 - price,
                fee_per_share=fee_per_share,
                fees=fees,
                slippage_per_share=slippage_per_share,
                conservative_slippage=slippage,
                net_edge_per_share=net_edge,
                net_executable_dollars=net_edge * size,
                return_on_capital=(net_edge * size) / capital,
            )
        )
    return tuple(rows)
