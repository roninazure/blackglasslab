from dataclasses import replace
from datetime import timedelta

from parallax.demo import demo_inputs
from parallax.engine import qualify
from parallax.evidence import EvidenceEngine
from parallax.mlb import MLBGameFact, MLBEvidenceProvider, evaluate_walk_forward
from parallax.mlb_validation import evaluate_cache, write_cache
from parallax.models import Action, Venue, utcnow
from parallax.normalization import normalize_kalshi, normalize_pmus


class FakeMLBSource:
    def __init__(self, game=None, error=False):
        self.game = game
        self.error = error

    def game_for_market(self, market):
        if self.error:
            raise OSError("source unavailable")
        return self.game


def mlb_market():
    now = utcnow()
    markets, _ = demo_inputs(now)
    market = markets[0]
    raw = dict(market.original_metadata["market"])
    raw["mlb"] = {
        "league": "MLB",
        "market_type": "moneyline",
        "home_team": "New York Yankees",
        "away_team": "Boston Red Sox",
        "start_time": (now + timedelta(hours=3)).isoformat(),
    }
    return replace(
        market,
        demo=False,
        resolution_rules="YES pays if the named MLB game winner is the winner; otherwise NO.",
        outcomes={"YES": "New York Yankees", "NO": "Boston Red Sox"},
        original_metadata={"market": raw},
    )


def game():
    return MLBGameFact(
        "123", "2026-09-12", "2026-09-12T20:00:00+00:00",
        "New York Yankees", "Boston Red Sox", .62, .50, .35, -.10,
        3.5, 4.2, 3.8, 4.4,
    )


def test_mlb_provider_is_independent_and_bound():
    market = mlb_market()
    provider = MLBEvidenceProvider(FakeMLBSource(game()), clock=utcnow)
    evidence = provider.assess(market)
    assert evidence is not None
    assert 0 < evidence.fair_probability < 1
    assert evidence.rules_digest
    assert evidence.source == "official-mlb-statsapi"
    assert evidence.source_independence == "AUTHORITATIVE_PRIMARY"
    assert evidence.independent_sources


def test_provider_failure_is_fail_closed():
    market = mlb_market()
    provider = MLBEvidenceProvider(FakeMLBSource(error=True))
    assert EvidenceEngine((provider,)).assess(market) is None


def test_authoritative_facts_without_calibration_do_not_reach_high_confidence():
    market = mlb_market()
    provider = MLBEvidenceProvider(FakeMLBSource(game()), clock=utcnow)
    evidence = replace(provider.assess(market), validation_status="")
    play = qualify(market, "YES", evidence)
    assert play.confidence_band.value not in {"HIGH", "ELITE"}
    assert play.suggested_action != Action.BUY


def test_validated_mlb_v2_can_reach_existing_buy_path():
    market = mlb_market()
    provider = MLBEvidenceProvider(FakeMLBSource(game()), clock=utcnow)
    evidence = provider.assess(market)
    play = qualify(market, "YES", evidence)
    assert play.parallax_fair_value is not None
    assert play.model_probability is not None
    assert play.edge_points is not None
    assert play.confidence_score > 0
    assert play.suggested_action == Action.BUY


def test_away_team_yes_is_oriented_to_away_probability():
    market = replace(mlb_market(), outcomes={"YES": "Boston Red Sox", "NO": "New York Yankees"})
    evidence = MLBEvidenceProvider(FakeMLBSource(game()), clock=utcnow).assess(market)
    assert evidence is not None and evidence.fair_probability < 0.5


def test_kalshi_mlb_game_contract_semantics_are_explicit():
    raw = {"ticker": "KXMLBGAME-TEST-ATL", "status": "active", "market_type": "binary", "title": "Atlanta wins", "yes_sub_title": "Atlanta", "no_sub_title": "Atlanta", "rules_primary": "If Atlanta wins the Philadelphia vs Atlanta professional baseball game originally scheduled for Sep 12, 2026 at 7:15 PM EDT, then the market resolves to Yes.", "rules_secondary": "", "event_ticker": "KXMLBGAME-TEST", "price_level_structure": "linear_cent", "price_ranges": [{"start": "0", "end": "1", "step": "0.01"}]}
    market = normalize_kalshi(raw, {"orderbook_fp": {"yes_dollars": [["0.50", "10"]], "no_dollars": [["0.40", "10"]]}}, "2026-09-12T20:00:00+00:00")
    assert market.original_metadata["market"]["mlb"]["market_type"] == "moneyline"
    assert market.original_metadata["market"]["mlb"]["away_team"] == "Philadelphia"


def test_v2_cache_features_are_strictly_prior_games():
    from parallax.mlb_validation import _v2_predictions

    rows = [
        {"game_id": "1", "start_time": "2025-04-01T20:00:00Z", "home_id": "h", "away_id": "a", "home_runs": 9, "away_runs": 0, "home_won": True},
        {"game_id": "2", "start_time": "2025-04-02T20:00:00Z", "home_id": "h", "away_id": "a", "home_runs": 0, "away_runs": 9, "home_won": False},
    ]
    first, _ = _v2_predictions(rows)
    changed = [dict(rows[0], home_won=False, home_runs=0, away_runs=9), rows[1]]
    changed_first, _ = _v2_predictions(changed)
    assert first[0] == changed_first[0]


def test_v2_probability_is_bounded_and_deterministic():
    from parallax.mlb_validation import _v2_predictions

    row = {"game_id": "1", "start_time": "2025-04-01T20:00:00Z", "home_id": "h", "away_id": "a", "home_runs": 1, "away_runs": 0, "home_won": True}
    first = _v2_predictions([row])[0]
    assert first == _v2_predictions([row])[0]
    assert 0.05 <= first[0] <= 0.95


def test_walk_forward_report_is_deterministic_and_bounded():
    rows = [
        replace(game(), game_id=str(i), start_time=f"2026-09-{i + 1:02d}T20:00:00+00:00", home_won=i % 2 == 0)
        for i in range(10)
    ]
    first = evaluate_walk_forward(rows)
    second = evaluate_walk_forward(list(reversed(rows)))
    assert first == second
    assert first["predictions"] == 10
    assert 0 < first["brier_score"] < 1
    assert first["log_loss"] > 0
    assert first["calibration_buckets"]


def test_pmus_freshness_uses_fetch_time_not_old_transaction_time():
    now = utcnow()
    raw = {
        "description": "winner rules",
        "category": "MLB",
        "endDate": (now + timedelta(days=1)).isoformat(),
        "marketSides": [{"long": True, "description": "A"}, {"long": False, "description": "B"}],
    }
    row = {"raw": raw, "id": "m", "slug": "a-b", "question": "A vs B", "event_id": "e", "active": True, "closed": False, "accepting_orders": True}
    book = {
        "a-b::YES": {"best_bid": .40, "best_ask": .45, "bid_size_shares": 100, "ask_size_shares": 100},
        "a-b::NO": {"best_bid": .55, "best_ask": .60, "bid_size_shares": 100, "ask_size_shares": 100},
        "transact_time": "2020-01-01T00:00:00+00:00",
    }
    market = normalize_pmus(row, book, now.isoformat())
    assert market.book_timestamp == now.isoformat()
    assert market.original_metadata["venue_transact_time"] == book["transact_time"]


def test_historical_report_serializes_without_market_prices(tmp_path):
    rows = []
    for index in range(12):
        item = game()
        rows.append({
            "game_id": str(index), "season": 2025,
            "start_time": f"2025-04-{index + 1:02d}T20:00:00+00:00",
            "home_team": item.home_team, "away_team": item.away_team,
            "home_win_rate": item.home_win_rate, "away_win_rate": item.away_win_rate,
            "home_run_diff_per_game": item.home_run_diff_per_game,
            "away_run_diff_per_game": item.away_run_diff_per_game,
            "home_won": index % 2 == 0, "baseline_home_rate": 0.54,
        })
    cache = {"source": "official", "seasons": [2025], "rows": rows}
    report = evaluate_cache(cache)
    path = tmp_path / "mlb.json"
    write_cache(cache, path)
    assert path.stat().st_size < 10000
    assert report["predictions"] == 12
    assert report["model_calibration"]
    assert "market_probability" not in path.read_text()
