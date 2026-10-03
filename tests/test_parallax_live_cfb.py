from dataclasses import replace
from datetime import UTC, datetime, timedelta

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import parallax.cfb as cfb_module
from parallax.alerts import AlertConfig, AlertDeliveryStore, AlertDispatcher, DeliveryResult
from parallax.cfb import (
    CFBGame, CFBEvidenceProvider, VALIDATION_ECE, active_cfb_season, is_supported_market,
    map_market_to_game, probability_for_game,
)
from parallax.demo import demo_inputs
from parallax.engine import qualify
from parallax.fees import attach_fees
from parallax.models import Action, Side, Venue
from parallax.normalization import normalize_kalshi
cfb_live_scan_spec = importlib.util.spec_from_file_location(
    "cfb_live_scan", Path(__file__).parents[1] / "scripts" / "cfb_live_scan.py"
)
cfb_live_scan = importlib.util.module_from_spec(cfb_live_scan_spec)
assert cfb_live_scan_spec.loader is not None
cfb_live_scan_spec.loader.exec_module(cfb_live_scan)

cfb_v1_validation_spec = importlib.util.spec_from_file_location(
    "cfb_v1_validation", Path(__file__).parents[1] / "scripts" / "cfb_v1_validation.py"
)
cfb_v1_validation = importlib.util.module_from_spec(cfb_v1_validation_spec)
assert cfb_v1_validation_spec.loader is not None
cfb_v1_validation_spec.loader.exec_module(cfb_v1_validation)


def game(*, home="Alabama", away="Georgia", date="2026-09-19", fcs=False, neutral=False, home_points=None, away_points=None):
    return CFBGame("target", 2026, "regular", f"{date}T18:00:00+00:00", home, away, "1", "2", "fbs", "fcs" if fcs else "fbs", neutral, home_points, away_points, home_points is not None)


def market(*, home="Alabama", away="Georgia", date="2026-09-19", title="CFB moneyline"):
    markets, _ = demo_inputs()
    base = markets[0]
    raw = {"homeTeam": home, "awayTeam": away, "gameStartTime": f"{date}T18:00:00Z", "marketType": "moneyline", "sport": "CFB", "ticker": "KXCFB-TEST", "rules_primary": f"The {home} wins the {away} vs {home} college football game."}
    return replace(base, venue=Venue.KALSHI, venue_market_id="KXCFB-TEST", slug="KXCFB-TEST", title=title, description="college football game winner", resolution_rules=raw["rules_primary"], original_metadata={"market": raw}, resolution_time="2026-09-19T22:00:00+00:00", status="OPEN", yes_bid=.45, yes_ask=.50, no_bid=.45, no_ask=.50, best_bid_size=100, best_ask_size=100, executable_depth={"YES": ((.50, 100),), "NO": ((.50, 100),)}, data_timestamp="2026-09-13T15:00:00+00:00", book_timestamp="2026-09-13T15:00:00+00:00")


def test_active_cfb_season_uses_fall_calendar_year():
    assert active_cfb_season(datetime(2026, 10, 1, tzinfo=UTC)) == 2026


def test_active_cfb_season_keeps_january_postseason_in_preceding_year():
    assert active_cfb_season(datetime(2027, 1, 15, tzinfo=UTC)) == 2026


def test_exact_mapping_and_neutral_site():
    m = market()
    mapped = map_market_to_game(m, [game(neutral=True)], now=datetime(2026, 9, 13, tzinfo=UTC))
    assert mapped.status == "MAPPED" and mapped.game.neutral_site


def test_wrong_opponent_and_date_rejected():
    assert map_market_to_game(market(away="Texas"), [game()], now=datetime(2026, 9, 13, tzinfo=UTC)).status == "TEAM_PAIR_MISMATCH"
    assert map_market_to_game(market(date="2026-09-20"), [game()], now=datetime(2026, 9, 13, tzinfo=UTC)).status == "DATE_MISMATCH"


def test_derivative_and_fcs_excluded():
    assert map_market_to_game(market(title="CFB spread"), [game()], now=datetime(2026, 9, 13, tzinfo=UTC)).status == "DERIVATIVE"
    fcs_market = market()
    fcs_raw = {**fcs_market.original_metadata["market"], "marketSides": [{"team": {"league": "fcs", "ordering": "away", "name": "Georgia"}}, {"team": {"league": "fbs", "ordering": "home", "name": "Alabama"}}]}
    assert map_market_to_game(replace(fcs_market, original_metadata={"market": fcs_raw}), [game(fcs=True)], now=datetime(2026, 9, 13, tzinfo=UTC)).status == "FCS_EXCLUDED"


def test_probability_is_independent_of_market_price_and_target_result():
    prior = game(home="Florida", away="Auburn", date="2025-09-01", home_points=21, away_points=7)
    target = game()
    changed_target = game(home_points=3, away_points=42)
    p1 = probability_for_game(target, [prior, target])
    p2 = probability_for_game(target, [prior, changed_target])
    assert p1 == p2


def test_cfb_safety_is_strictly_greater_and_evidence_is_required():
    assert not (VALIDATION_ECE > VALIDATION_ECE)
    assert VALIDATION_ECE + 1e-9 > VALIDATION_ECE
    m = market()
    assert is_supported_market(m)
    no_evidence = qualify(m, Side.YES, None, now=datetime(2026, 9, 13, tzinfo=UTC))
    assert no_evidence.suggested_action != Action.BUY


def test_cfb_evidence_and_fee_economics_bind(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 13, tzinfo=tz)

    monkeypatch.setattr(cfb_module, "datetime", FixedDatetime)
    m = market()
    evidence = CFBEvidenceProvider(lambda: [game()]).assess(m)
    assert evidence and evidence.source == "CollegeFootballData" and evidence.validation_status == "CALIBRATED"
    scored = qualify(m, Side.YES, evidence, now=datetime(2026, 9, 13, tzinfo=UTC))
    assert scored.retail_examples[1].maximum_loss == 25
    assert scored.evidence is not None


def test_current_kalshi_cfb_schema_is_supported():
    raw = {
        "ticker": "KXNCAAFGAME-26SEP17SYRPITT-SYR",
        "event_ticker": "KXNCAAFGAME-26SEP17SYRPITT",
        "title": "Syracuse wins",
        "yes_sub_title": "Syracuse",
        "occurrence_datetime": "2026-09-18T02:30:00Z",
        "rules_primary": "If Syracuse wins the Syracuse vs Pittsburgh college football game originally scheduled for Sep 17, 2026, then the market resolves to Yes.",
        "status": "active",
    }
    event = {
        "event_ticker": "KXNCAAFGAME-26SEP17SYRPITT",
        "series_ticker": "KXNCAAFGAME",
        "title": "Syracuse vs Pittsburgh",
        "product_metadata": {
            "competition": "NCAA Football",
            "competition_scope": "Game",
        },
    }
    m = normalize_kalshi(raw, {}, "2026-09-13T21:00:00+00:00", event=event)
    assert is_supported_market(m)


def test_kalshi_cfb_disclaimer_property_does_not_trigger_prop_rejection():
    raw = {
        "ticker": "KXNCAAFGAME-26SEP26NAUMTST-NAU",
        "event_ticker": "KXNCAAFGAME-26SEP26NAUMTST",
        "title": "Northern Arizona wins",
        "yes_sub_title": "Northern Arizona",
        "no_sub_title": "Northern Arizona",
        "occurrence_datetime": "2026-09-27T05:30:00Z",
        "market_type": "binary",
        "rules_primary": (
            "If Northern Arizona wins the Northern Arizona vs Montana St. college football game "
            "originally scheduled for Sep 26, 2026, then the market resolves to Yes. "
            "All trademarks, logos, and brand names are the property of their respective owners."
        ),
    }
    event = {
        "event_ticker": "KXNCAAFGAME-26SEP26NAUMTST",
        "series_ticker": "KXNCAAFGAME",
        "title": "Northern Arizona vs Montana St.",
        "product_metadata": {
            "competition": "NCAA Football",
            "competition_scope": "Game",
        },
    }

    market = normalize_kalshi(raw, {}, "test", event=event)

    assert is_supported_market(market)


def test_cfb_evaluated_play_reaches_prospective_capture(monkeypatch):
    captured = []
    from types import SimpleNamespace
    sentinel = SimpleNamespace(id="play", venue="PMUS", market_id="market", side=Side.YES)
    monkeypatch.setattr(cfb_live_scan, "qualify", lambda *args, **kwargs: sentinel)

    class Store:
        def capture_prospective(self, *args, **kwargs):
            captured.append((args, kwargs))
            return {"observation_id": "PX-1", "play_id": "play", "venue": "PMUS", "market_id": "market", "side": Side.YES}

        def prospective_record(self, observation_id):
            return {"observation_id": observation_id}

    result = cfb_live_scan._capture_evaluated(Store(), object(), Side.YES, object(), "now")
    assert result is sentinel and len(captured) == 1


def test_cfb_live_scan_fetches_exactly_one_active_season(monkeypatch):
    from types import SimpleNamespace

    fetched = []

    class Client:
        def markets_page(self, **kwargs):
            raise AssertionError("mock paginator must not call the provider client")

        def close(self):
            pass

    monkeypatch.setattr(cfb_live_scan, "active_cfb_season", lambda now: 2026)
    monkeypatch.setattr(cfb_live_scan, "fetch_games", lambda seasons: fetched.append(seasons) or [])
    monkeypatch.setattr(cfb_live_scan, "TrackRecord", lambda path: object())
    monkeypatch.setattr(cfb_live_scan, "PolymarketUSPublicClient", Client)
    monkeypatch.setattr(cfb_live_scan, "KalshiPublicClient", Client)
    monkeypatch.setattr(cfb_live_scan, "paginate", lambda *args, **kwargs: ([], SimpleNamespace(pages=0)))
    monkeypatch.setattr(cfb_live_scan, "_scope_kalshi", lambda client: ([], {"state": "COMPLETE"}))

    result = cfb_live_scan._scan(alert_dispatcher=object())

    assert fetched == [(2026,)]
    assert result["cfbd_calls"] == 1


class _AlertTransport:
    def __init__(self):
        self.calls = []

    def post_json(self, url, payload):
        self.calls.append((url, payload))
        return DeliveryResult("SENT", http_status=200)


def _cfb_alert_play(action=Action.BUY):
    return SimpleNamespace(
        suggested_action=action,
        venue=Venue.KALSHI,
        market_id="KXCFB-TEST",
        side=Side.YES,
        side_description="Alabama",
        market_title="Georgia at Alabama",
        market_url="https://kalshi.com/markets/KXCFB-TEST",
        executable_price=0.40,
        model_probability=0.55,
        edge_points=15.0,
        executable_size=100.0,
        confidence_band="HIGH",
        resolution_time="2026-10-10T18:00:00+00:00",
    )


def _alert_dispatcher(tmp_path, transport):
    return AlertDispatcher(
        AlertDeliveryStore(tmp_path / "alerts.sqlite"),
        AlertConfig(
            mode="ntfy",
            ntfy_topic="cfb-test",
            ntfy_server="https://ntfy.example.test",
        ),
        transport,
    )


def _alert_mapping():
    return SimpleNamespace(
        game=SimpleNamespace(
            away_team="Georgia",
            home_team="Alabama",
            kickoff="2026-10-10T18:00:00+00:00",
        )
    )


def test_qualifying_cfb_buy_alerts_once_and_duplicate_dedupes(tmp_path):
    transport = _AlertTransport()
    dispatcher = _alert_dispatcher(tmp_path, transport)
    mapping = _alert_mapping()
    detected_at = datetime(2026, 10, 3, 18, tzinfo=UTC)
    kwargs = {
        "publication_eligible": True,
        "economic_key": "CFB:game-1:alabama",
        "selected_team": "Alabama",
    }

    first = cfb_live_scan._dispatch_eligible_buy_alert(
        dispatcher, _cfb_alert_play(), market(), mapping, detected_at, **kwargs
    )
    duplicate = cfb_live_scan._dispatch_eligible_buy_alert(
        dispatcher, _cfb_alert_play(), market(), mapping, detected_at, **kwargs
    )

    assert first["status"] == "SENT" and first["deduplicated"] is False
    assert duplicate["status"] == "SENT" and duplicate["deduplicated"] is True
    assert len(transport.calls) == 1
    assert "Sport: CFB" in transport.calls[0][1]["message"]


def test_cfb_watch_pass_and_uncertified_buy_are_silent(tmp_path):
    transport = _AlertTransport()
    dispatcher = _alert_dispatcher(tmp_path, transport)
    mapping = _alert_mapping()
    detected_at = datetime(2026, 10, 3, 18, tzinfo=UTC)
    for action, eligible in (
        (Action.WATCH, False),
        (Action.PASS, False),
        (Action.BUY, False),
    ):
        assert cfb_live_scan._dispatch_eligible_buy_alert(
            dispatcher,
            _cfb_alert_play(action),
            market(),
            mapping,
            detected_at,
            publication_eligible=eligible,
            economic_key="CFB:game-1:alabama",
            selected_team="Alabama",
        ) is None

    assert transport.calls == []


def test_cfb_alert_exception_is_contained(monkeypatch):
    monkeypatch.setattr(
        cfb_live_scan,
        "dispatch_scored_buy",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("ntfy down")),
    )

    result = cfb_live_scan._dispatch_eligible_buy_alert(
        object(),
        _cfb_alert_play(),
        market(),
        _alert_mapping(),
        datetime(2026, 10, 3, 18, tzinfo=UTC),
        publication_eligible=True,
        economic_key="CFB:game-1:alabama",
        selected_team="Alabama",
    )

    assert result["status"] == "FAILED"
    assert result["error_code"] == "ALERT_DISPATCH_EXCEPTION"


def test_cfb_v1_validation_keeps_historical_season_range(monkeypatch, capsys):
    fetched = []
    monkeypatch.setattr(cfb_v1_validation, "fetch_games", lambda seasons: fetched.append(seasons) or [])
    monkeypatch.setattr(cfb_v1_validation, "validate", lambda games: {"games": len(games)})
    monkeypatch.setattr("sys.argv", ["cfb_v1_validation.py"])

    cfb_v1_validation.main()

    assert fetched == [tuple(range(2010, 2026))]
    assert '"games": 0' in capsys.readouterr().out
