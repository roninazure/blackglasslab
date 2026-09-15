from __future__ import annotations

from dataclasses import replace

from parallax import sources
from parallax.models import Action, Venue
from parallax.service import PlayService
from parallax.track_record import TrackRecord


def fomc_row(slug: str = "rdc-usfed-fomc-2026-09-16-hike25") -> dict:
    return {
        "id": slug.removeprefix("rdc-usfed-fomc-2026-09-16-") and ("313137" if slug.endswith("hike25") else "313138"),
        "slug": slug,
        "question": "Fed Decision in September",
        "title": "25 bps Increase" if slug.endswith("hike25") else "No Change",
        "description": (
            "Resolves from the Federal Reserve decision announced after the "
            "September 16, 2026 FOMC meeting."
        ),
        "category": "MACRO_ECONOMICS",
        "active": True,
        "closed": False,
        "accepting_orders": True,
        "marketSides": [
            {"long": True, "description": "YES"},
            {"long": False, "description": "NO"},
        ],
        "endDate": "2026-09-17T00:00:00Z",
    }


def fomc_book(slug: str, observed_at: str) -> dict:
    return {
        f"{slug}::YES": {"best_bid": 0.40, "best_ask": 0.42, "ask_size_shares": 100},
        f"{slug}::NO": {"best_bid": 0.56, "best_ask": 0.58, "ask_size_shares": 100},
        "transact_time": observed_at,
    }


def test_live_source_wires_fed_contract_to_exact_normalized_path(monkeypatch):
    row = fomc_row()
    unrelated = {
        **fomc_row("company-event"),
        "id": "company-event",
        "question": "Will the company appoint a new CEO?",
        "title": "CEO appointment",
        "description": "Resolves from the company's official announcement.",
        "category": "CORPORATE_BUSINESS",
    }
    observed_at = "2026-09-15T12:00:00+00:00"
    book_calls = []

    class PMUS:
        def markets_page(self, *, limit, offset):
            return [row, unrelated] if offset == 0 else []

        def book(self, slug):
            book_calls.append(slug)
            return fomc_book(slug, observed_at)

        def close(self):
            pass

    class Kalshi:
        def mlb_markets_page(self, limit=100):
            return {"markets": []}

    monkeypatch.setattr(sources, "PolymarketUSPublicClient", PMUS)
    monkeypatch.setattr(sources, "KalshiPublicClient", Kalshi)
    markets, report = sources.collect_markets(limit=2)

    assert len(markets) == 1
    market = markets[0]
    assert market.venue is Venue.POLYMARKET
    assert market.venue_market_id == "313137"
    assert market.slug == row["slug"]
    assert market.title == row["question"]
    assert market.event == "direct-contract:POLYMARKET:313137"
    assert book_calls == [row["slug"]]
    assert report["metrics"]["POLYMARKET.generic_scope_skipped"] == 1
    assert report["metrics"]["POLYMARKET.generic_markets_observed"] == 1


def test_fed_rates_scope_reuses_taxonomy_and_excludes_sports():
    assert sources._is_fed_rates_market(fomc_row())
    assert not sources._is_fed_rates_market(
        {
            **fomc_row("company-event"),
            "category": "CORPORATE_BUSINESS",
            "question": "Will the company appoint a new CEO?",
        }
    )
    assert not sources._is_fed_rates_market(
        {
            **fomc_row("sports-event"),
            "category": "SPORTS",
            "question": "Who will win the baseball game?",
        }
    )


def test_missing_fomc_evidence_is_not_buy_and_is_durably_captured(tmp_path):
    class NoEvidence:
        def assess(self, market):
            return None

    row = fomc_row()
    observed_at = "2026-09-15T12:00:00+00:00"
    from parallax.direct_contracts import normalized_for_direct_contract
    from parallax.event_discovery import discover_event_candidate

    candidate = discover_event_candidate(
        row, venue=Venue.POLYMARKET
    ).candidate
    assert candidate is not None
    market = normalized_for_direct_contract(replace(
        candidate,
        raw_provenance={**candidate.raw_provenance, "book": fomc_book(row["slug"], observed_at)},
    ))
    service = PlayService(
        TrackRecord(tmp_path / "record.sqlite"),
        prospective_store=TrackRecord(tmp_path / "prospective.sqlite"),
        evidence_engine=NoEvidence(),
        capture_only=True,
    )
    service.replace_inputs([market])
    plays = service.plays()["items"]

    assert plays
    assert all(play["suggested_action"] != Action.BUY.value for play in plays)
    records = service.prospective_store.prospective_records()
    assert len(records) == 2
    assert {record["observation_id"] for record in records}
    assert {record["market_id"] for record in records} == {"313137"}
