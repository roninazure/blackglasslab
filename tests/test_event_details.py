from __future__ import annotations

from types import SimpleNamespace

import pytest

from parallax.event_details import fetch_kalshi_market_detail, fetch_pmus_market_detail
from parallax.event_discovery import discover_event_candidate
from parallax.models import Venue


def _kalshi_candidate():
    decision = discover_event_candidate(
        {
            "ticker": "KXTEST-YES",
            "title": "Will Congress pass H.R. 1 on Sep 15, 2026?",
            "rules_primary": "Resolves YES if Congress passes H.R. 1 on September 15, 2026 according to the official U.S. House roll call.",
            "status": "open",
            "yes_sub_title": "YES",
            "no_sub_title": "NO",
        },
        venue=Venue.KALSHI,
    )
    assert decision.candidate
    return decision.candidate


def _pmus_candidate():
    decision = discover_event_candidate(
        {
            "id": "123",
            "slug": "test-market",
            "question": "Will Congress pass H.R. 1 on Sep 15, 2026?",
            "description": "Resolves YES if Congress passes H.R. 1 on September 15, 2026 according to the official U.S. House roll call.",
            "active": True,
            "closed": False,
            "marketSides": [
                {"long": True, "description": "YES"},
                {"long": False, "description": "NO"},
            ],
        },
        venue=Venue.POLYMARKET,
    )
    assert decision.candidate
    return decision.candidate


def test_kalshi_detail_uses_one_get_and_preserves_raw_response():
    class Client:
        def __init__(self):
            self.calls = []

        def get(self, path):
            self.calls.append(path)
            return {"market": {"ticker": "KXTEST-YES", "title": "detail"}, "extra": 1}

    client = Client()
    detail = fetch_kalshi_market_detail(client, _kalshi_candidate())
    assert client.calls == ["/markets/KXTEST-YES"]
    assert detail.raw_response["extra"] == 1
    assert detail.market["ticker"] == "KXTEST-YES"


def test_pmus_detail_uses_public_sdk_slug_and_records_request():
    class Markets:
        def __init__(self):
            self.calls = []

        def retrieve_by_slug(self, slug):
            self.calls.append(slug)
            return {"market": {"id": 123, "slug": slug, "description": "rules"}}

    class Meter:
        count = 0

        def record(self):
            self.count += 1

    client = SimpleNamespace(client=SimpleNamespace(markets=Markets()), meter=Meter())
    detail = fetch_pmus_market_detail(client, _pmus_candidate())
    assert client.client.markets.calls == ["test-market"]
    assert client.meter.count == 1
    assert detail.market["slug"] == "test-market"


@pytest.mark.parametrize("payload", [{}, {"market": []}, {"market": {"ticker": "WRONG"}}])
def test_malformed_or_disagreeing_detail_raises_without_market_absence(payload):
    class Client:
        def get(self, path):
            return payload

    with pytest.raises((TypeError, ValueError)):
        fetch_kalshi_market_detail(Client(), _kalshi_candidate())
