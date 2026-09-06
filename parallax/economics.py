from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal

from .models import Mechanics, RetailExample


def decimal(value: float) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Values must be finite")
    return result


def valid_price(price: float, mechanics: Mechanics) -> bool:
    p = decimal(price)
    return 0 < p < 1 and any(
        decimal(lo) <= p <= decimal(hi)
        and decimal(step) > 0
        and (p - decimal(lo)) % decimal(step) == 0
        for lo, hi, step in mechanics.price_ranges
    )


def retail_example(
    stake: float,
    price: float | None,
    depth: float,
    mechanics: Mechanics,
    *,
    slippage_per_contract: float = 0,
) -> RetailExample:
    """Stake is the contract budget; fees are additional, explicitly itemized.

    Round quantity DOWN to venue increments. Never extrapolate beyond top ask.
    Slippage is an optional conservative buffer, charged separately in net math.
    """
    budget, size, slip = decimal(stake), decimal(depth), decimal(slippage_per_contract)
    if budget <= 0 or size < 0 or slip < 0:
        raise ValueError("Stake must be positive; depth and slippage nonnegative")

    def unavailable(reason: str) -> RetailExample:
        return RetailExample(stake, False, reason, unspent=stake)

    if price is None or not 0 < decimal(price) < 1:
        return unavailable("No valid entry price")
    if (
        mechanics.payout != 1
        or not mechanics.quantity_step
        or not mechanics.minimum_quantity
    ):
        return unavailable("Venue payout or quantity rules need verification")
    if not valid_price(price, mechanics):
        return unavailable("Price does not match verified venue tick rules")
    step, minimum = (
        decimal(mechanics.quantity_step),
        decimal(mechanics.minimum_quantity),
    )
    if step <= 0 or minimum <= 0:
        return unavailable("Invalid venue quantity rules")
    p = decimal(price)
    quantity = (budget / p / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if quantity < minimum:
        return unavailable("Budget is below the venue minimum")
    if quantity > size:
        return unavailable("Not enough contracts at this price for this scenario")
    spent = quantity * p
    payout = quantity
    slippage = quantity * slip
    fees = None
    if mechanics.fee_rate is not None:
        rate = decimal(mechanics.fee_rate)
        if rate < 0 or mechanics.fee_rounding not in ("CEILING", "HALF_EVEN"):
            raise ValueError("Invalid fee schedule")
        fees = (rate * quantity * p * (1 - p)).quantize(
            Decimal("0.01"),
            rounding=ROUND_CEILING
            if mechanics.fee_rounding == "CEILING"
            else ROUND_HALF_EVEN,
        )
    total = None if fees is None else spent + fees + slippage
    return RetailExample(
        stake,
        True,
        None,
        float(quantity),
        float(spent),
        float(budget - spent),
        float(payout),
        float(payout - spent),
        float(spent),
        None if fees is None else float(fees),
        float(slippage),
        None if total is None else float(total),
        None if total is None else float(payout - total),
        None if total is None else float(total),
    )
