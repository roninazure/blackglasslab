from dataclasses import replace

from parallax import sources
from parallax.demo import demo_inputs
from parallax.inbox import InboxStore
from parallax.models import Evidence, Venue
from parallax.normalization import rules_digest
from parallax.service import PlayService
from parallax.track_record import TrackRecord


def pmus_row(slug="aec-mlb-laa-wsh-2026-09-13"):
    raw = {
        "question": "Who will win Los Angeles Angels vs Washington Nationals?",
        "description": "Winner of the MLB baseball game.",
        "category": "sports",
        "marketType": "moneyline",
        "sportsMarketTypeV2": "SPORTS_MARKET_TYPE_MONEYLINE",
        "gameStartTime": "2026-09-13T17:35:00Z",
        "endDate": "2026-09-27T17:35:00Z",
        "marketSides": [
            {"long": True, "description": "Los Angeles Angels", "team": {"name": "Los Angeles Angels", "league": "mlb", "ordering": "away"}},
            {"long": False, "description": "Washington Nationals", "team": {"name": "Washington Nationals", "league": "mlb", "ordering": "home"}},
        ],
    }
    return {"raw": raw, "id": slug, "slug": slug, "question": raw["question"], "event_id": "E", "active": True, "closed": False, "accepting_orders": True}


def book(slug):
    return {
        f"{slug}::YES": {"best_bid": 0.45, "best_ask": 0.47, "bid_size_shares": 100, "ask_size_shares": 100},
        f"{slug}::NO": {"best_bid": 0.53, "best_ask": 0.55, "bid_size_shares": 100, "ask_size_shares": 100},
    }


def test_current_nested_pmus_metadata_is_mlb_moneyline():
    assert sources._is_mlb_moneyline(pmus_row())
    assert not sources._is_mlb_moneyline({"raw": {"marketType": "moneyline", "question": "NFL winner"}})


def test_bulk_mapping_precedes_book_and_isolates_book_failure(monkeypatch):
    first, second = pmus_row(), pmus_row("aec-mlb-laa-wsh-2026-09-14")
    calls = []

    class FakePMUS:
        def markets_page(self, *, limit, offset):
            return [first, second] if offset == 0 else []

        def book(self, slug):
            calls.append(slug)
            if slug.endswith("09-14"):
                raise RuntimeError("rate limited")
            return book(slug)

        def close(self):
            pass

    class FakeKalshi:
        def mlb_markets_page(self, limit=100):
            return {"markets": []}

    class FakeProvider:
        def assess(self, market):
            assert market.yes_ask is None
            return Evidence(Venue.POLYMARKET, market.venue_market_id, 0.5, "test", "mlb-v2", "2026-09-13T12:00:00+00:00", "2026-09-13T13:00:00+00:00", rules_digest(market), "test", "test")

    monkeypatch.setattr(sources, "PolymarketUSPublicClient", FakePMUS)
    monkeypatch.setattr(sources, "KalshiPublicClient", FakeKalshi)
    monkeypatch.setattr(sources, "MLBEvidenceProvider", FakeProvider)
    markets, report = sources.collect_markets(limit=2)
    assert [m.slug for m in markets] == [first["slug"]]
    assert calls == [first["slug"], second["slug"]]
    assert report["metrics"]["POLYMARKET.evidence_produced"] == 1
    assert any(error["stage"] == "market" for error in report["errors"])


def test_duplicate_pmus_market_gets_one_book_request(monkeypatch):
    row = pmus_row()
    calls = []

    class FakePMUS:
        def markets_page(self, *, limit, offset):
            return [row, row] if offset == 0 else []

        def book(self, slug):
            calls.append(slug)
            return book(slug)

        def close(self):
            pass

    class FakeKalshi:
        def mlb_markets_page(self, limit=100):
            return {"markets": []}

    class FakeProvider:
        def assess(self, market):
            return Evidence(Venue.POLYMARKET, market.venue_market_id, 0.5, "test", "mlb-v2", "2026-09-13T12:00:00+00:00", "2026-09-13T13:00:00+00:00", rules_digest(market), "test", "test")

    monkeypatch.setattr(sources, "PolymarketUSPublicClient", FakePMUS)
    monkeypatch.setattr(sources, "KalshiPublicClient", FakeKalshi)
    monkeypatch.setattr(sources, "MLBEvidenceProvider", FakeProvider)
    sources.collect_markets(limit=2)
    assert calls == [row["slug"]]


class SpyEvidenceEngine:
    def __init__(self):
        self.calls = 0

    def assess(self, market):
        self.calls += 1
        return None


def service_with_spy(tmp_path):
    spy = SpyEvidenceEngine()
    service = PlayService(
        TrackRecord(tmp_path / "record.sqlite"),
        inbox_store=InboxStore(tmp_path / "inbox.sqlite"),
        evidence_engine=spy,
    )
    service.refresh_inbox = lambda: None
    return service, spy


def test_collected_evidence_reaches_scoring_without_second_lookup(tmp_path):
    now = __import__("parallax.models", fromlist=["utcnow"]).utcnow()
    markets, evidence = demo_inputs(now)
    market = replace(markets[0], demo=False)
    proof = replace(evidence[(markets[0].venue, markets[0].venue_market_id)], demo=False)
    service, spy = service_with_spy(tmp_path)
    service.replace_inputs([market], collection={"_evidence": {(market.venue, market.venue_market_id): proof}})
    assert service.evidence[(market.venue, market.venue_market_id)] == proof
    assert spy.calls == 0
    play = service._plays()[0]
    assert play.evidence == proof
    assert play.edge_points is not None and play.expected_value is not None


def test_missing_collected_evidence_keeps_fail_closed_lookup(tmp_path):
    markets, _ = demo_inputs()
    market = replace(markets[0], demo=False)
    service, spy = service_with_spy(tmp_path)
    service.replace_inputs([market], collection={})
    assert spy.calls == 1
    assert service.evidence == {}
