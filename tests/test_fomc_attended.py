from __future__ import annotations

import sys

import pytest

from maker_spread_economics.polymarket_us import normalize_market_page
from parallax.track_record import TrackRecord
from scripts import fomc_attended as fomc


def response(market_id, *, closed=True, status=None, ep3_status="EXPIRED"):
    return {
        "id": market_id,
        "slug": fomc.TARGETS[market_id],
        "question": "Fed Decision in September",
        "title": "25 bps Increase" if market_id == "313137" else "No Change",
        "description": "Resolves from the Federal Reserve decision at the September 2026 FOMC meeting.",
        "category": "macro",
        "active": True,
        "closed": closed,
        "status": status,
        "ep3Status": ep3_status,
        "marketSides": [
            {"long": True, "description": "Yes", "tradable": True},
            {"long": False, "description": "No", "tradable": True},
        ],
        "bestBidQuote": {"value": "0.9900", "currency": "USD"} if market_id == "313137" else None,
        "bestAskQuote": None,
        "endDate": "2026-09-16T14:10:58Z",
    }


class Client:
    def __init__(self, **state):
        self.state = state
        self.requested = []
        self.book_calls = []
        self.closed = False

    def market_by_id(self, market_id):
        self.requested.append(market_id)
        return normalize_market_page({"markets": [response(market_id, **self.state)]})[0]

    def book(self, slug):
        self.book_calls.append(slug)
        raise RuntimeError("book unavailable")

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_external_economic_requests(monkeypatch):
    monkeypatch.setattr(fomc.EconomicsEvidenceProvider, "assess", lambda self, market: None)


@pytest.mark.parametrize("status", [None, "MARKET_STATUS_RESOLVING"])
@pytest.mark.parametrize("omit_missing_quotes", [False, True])
def test_closed_nullable_quotes_are_captured_without_fabricated_asks(tmp_path, status, omit_missing_quotes):
    store = TrackRecord(tmp_path / "prospective.sqlite")
    # Retain earlier prospective evidence unchanged when final observations append.
    earlier = fomc.observe_once(store, Client(closed=False, ep3_status="OPEN"))
    preserved = [store.prospective_record(r["observation_id"]) for r in earlier["rows"]]
    client = Client(status=status)
    if omit_missing_quotes:
        original = client.market_by_id

        def market_by_id(market_id):
            row = original(market_id)
            row["raw"] = {k: v for k, v in row["raw"].items() if not (k in {"bestBidQuote", "bestAskQuote"} and v is None)}
            return row

        client.market_by_id = market_by_id
    report = fomc.observe_once(store, client)

    assert client.requested == ["313137", "313138"]
    assert client.book_calls == []
    assert len(report["rows"]) == 4
    assert all(r["status"] == "CLOSED" and not r["open"] and r["price"] is None for r in report["rows"])
    assert [r["bid"] for r in report["rows"]] == [0.99, None, None, None]
    assert len(store.prospective_records()) == 8
    assert [store.prospective_record(r["observation_id"]) for r in earlier["rows"]] == preserved
    for row in report["rows"]:
        record = store.prospective_record(row["observation_id"])
        snapshot = record["market_snapshot"]
        assert snapshot["yes_ask"] is None and snapshot["no_ask"] is None
        assert record["executable_price"] is None
        assert snapshot["original_metadata"]["market"]["status"] == status
        assert snapshot["executable_depth"] == {"YES": [], "NO": []}


def test_expired_venue_state_with_missing_status_is_observed(tmp_path):
    client = Client(closed=False)
    report = fomc.observe_once(TrackRecord(tmp_path / "prospective.sqlite"), client)
    assert all(r["status"] == "EXPIRED" and not r["open"] and r["price"] is None for r in report["rows"])
    assert report["rows"][0]["bid"] == 0.99
    assert client.book_calls == []


def test_missing_quotes_on_open_contract_do_not_imply_closure(tmp_path):
    client = Client(closed=False, ep3_status="OPEN")
    report = fomc.observe_once(TrackRecord(tmp_path / "prospective.sqlite"), client)
    assert all(r["open"] for r in report["rows"])
    assert report["rows"][0]["price"] is None
    assert report["rows"][0]["bid"] == 0.99
    assert report["rows"][1]["price"] == pytest.approx(0.01)
    assert all(r["price"] is None for r in report["rows"][2:])


def test_watcher_exits_normally_after_both_contracts_close(tmp_path, monkeypatch):
    client = Client()
    monkeypatch.setattr(fomc, "PolymarketUSPublicClient", lambda: client)
    monkeypatch.setattr(fomc.signal, "signal", lambda *args: None)
    monkeypatch.setattr(fomc.time, "sleep", lambda *_: pytest.fail("closed targets must terminate"))
    monkeypatch.setattr(sys, "argv", ["fomc_attended", "--db", str(tmp_path / "prospective.sqlite")])
    assert fomc.main() == 0
    assert client.requested == ["313137", "313138"]
    assert client.closed
