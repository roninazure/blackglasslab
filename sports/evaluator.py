from __future__ import annotations

from dataclasses import dataclass

from revenue_poc.economics import ExecutionEconomics, evaluate_execution

from .clob import TopOfBook


@dataclass(frozen=True)
class SportsEvaluation:
    team_a: str
    team_b: str
    fair_probability_a: float
    fair_probability_b: float
    economics: ExecutionEconomics
    selected_team: str
    executable_depth_usd: float


def evaluate_two_way_moneyline(
    *,
    team_a: str,
    team_b: str,
    fair_probability_a: float,
    book_a: TopOfBook,
    book_b: TopOfBook,
    stake_usd: float,
    fee_rate: float | None = None,
    fee_bps: float = 0.0,
    slippage_bps: float = 0.0,
) -> SportsEvaluation | None:

    if book_a.ask is None or book_b.ask is None:
        return None

    fair_a = float(fair_probability_a)
    fair_b = 1.0 - fair_a

    # Synthetic canonical YES book:
    #   YES = team A
    #   NO  = team B
    #
    # This makes evaluate_execution's NO entry:
    #   1 - best_bid
    # equal the actual executable team-B ask.
    best_ask = float(book_a.ask)
    best_bid = 1.0 - float(book_b.ask)

    if best_bid < 0.0 or best_ask > 1.0 or best_ask < best_bid:
        return None

    market_probability = (best_bid + best_ask) / 2.0
    side = "YES" if fair_a >= market_probability else "NO"

    if side == "YES":
        ask_size = book_a.ask_size or 0.0
        depth_usd = best_ask * ask_size
        selected_team = team_a
    else:
        ask_size = book_b.ask_size or 0.0
        depth_usd = float(book_b.ask) * ask_size
        selected_team = team_b

    economics = evaluate_execution(
        model_probability=fair_a,
        market_probability=market_probability,
        stake_usd=stake_usd,
        best_bid=best_bid,
        best_ask=best_ask,
        spread=best_ask - best_bid,
        depth_usd=depth_usd,
        fee_rate=fee_rate,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )

    return SportsEvaluation(
        team_a=team_a,
        team_b=team_b,
        fair_probability_a=fair_a,
        fair_probability_b=fair_b,
        economics=economics,
        selected_team=selected_team,
        executable_depth_usd=depth_usd,
    )
