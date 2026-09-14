from dataclasses import replace

from parallax import sources
from parallax.demo import demo_inputs
from parallax.inbox import InboxStore
from parallax.models import Action, Evidence, Venue
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


def _live_capture_workload(now, count=7):
    demo_markets, demo_evidence = demo_inputs(now)
    base = demo_markets[0]
    base_proof = demo_evidence[(base.venue, base.venue_market_id)]
    markets = []
    evidence = {}
    for index in range(count):
        market = replace(
            base,
            demo=False,
            venue_market_id=f"mlb-live-{index}",
            slug=f"mlb-live-{index}",
            title=f"MLB live market {index}",
            event=f"mlb-event-{index}",
        )
        proof = replace(
            base_proof,
            demo=False,
            market_id=market.venue_market_id,
            rules_digest=rules_digest(market),
            validation_reference="" if index == 0 else base_proof.validation_reference,
        )
        markets.append(market)
        evidence[(market.venue, market.venue_market_id)] = proof
    return markets, evidence


def test_live_mlb_capture_is_once_per_side_per_service_invocation(tmp_path, monkeypatch):
    now = __import__("parallax.models", fromlist=["utcnow"]).utcnow()
    first_at = now.replace(microsecond=0)
    monkeypatch.setattr("parallax.service.utcnow", lambda: first_at)
    markets, evidence = _live_capture_workload(first_at)
    publications = TrackRecord(tmp_path / "publications.sqlite")
    prospective = TrackRecord(tmp_path / "prospective.sqlite")

    class NoAlerts:
        def dispatch(self, _items):
            raise AssertionError("capture-only mode must not dispatch alerts")

        def status(self):
            return {
                "mode": "disabled",
                "pending": 0,
                "sent": 0,
                "failed": 0,
                "unknown": 0,
                "last_delivery_at": None,
            }

    class NoSocial:
        def enqueue(self, _item):
            raise AssertionError("capture-only mode must not enqueue social content")

        def publish_pending(self):
            raise AssertionError("capture-only mode must not publish social content")

        def status(self):
            return {
                "mode": "disabled",
                "pending": 0,
                "dry_run": 0,
                "sent": 0,
                "failed": 0,
                "unknown": 0,
            }

    service = PlayService(
        publications,
        inbox_store=InboxStore(tmp_path / "inbox.sqlite"),
        alert_dispatcher=NoAlerts(),
        social_publisher=NoSocial(),
        prospective_store=prospective,
        capture_only=True,
    )
    service.replace_inputs(markets, evidence)
    health = service.health()
    assert service.plays()["total"] == 14
    assert len(service._plays()) == 14

    records = prospective.prospective_records()
    assert len(records) == 14
    assert {record["verdict"] for record in records} == {
        Action.BUY,
        Action.WATCH,
        Action.PASS,
    }
    assert publications.summary()["published_plays"] == 0
    assert health["live_orders"] == 0 and not health["execution_enabled"]

    later = first_at + __import__("datetime").timedelta(seconds=1)
    monkeypatch.setattr("parallax.service.utcnow", lambda: later)
    second = PlayService(
        publications,
        inbox_store=InboxStore(tmp_path / "inbox-2.sqlite"),
        alert_dispatcher=NoAlerts(),
        social_publisher=NoSocial(),
        prospective_store=prospective,
        capture_only=True,
    )
    second.replace_inputs(markets, evidence)
    second._plays()
    assert len(prospective.prospective_records()) == 28
