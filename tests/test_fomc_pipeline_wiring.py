from __future__ import annotations

from parallax import sources
from parallax.economic_evidence import EconomicSubtype, EconomicsEvidenceProvider, economic_subtype
from parallax.models import Venue


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


def normalized_economic_market(question: str, description: str):
    from parallax.direct_contracts import normalized_for_direct_contract
    from parallax.event_discovery import discover_event_candidate

    candidate = discover_event_candidate(
        {
            **fomc_row("economic-market"),
            "id": "economic-market",
            "question": question,
            "title": question,
            "description": description,
        },
        venue=Venue.POLYMARKET,
    ).candidate
    assert candidate is not None
    return normalized_for_direct_contract(candidate)


def test_reusable_economic_lane_classifies_all_required_subtypes():
    cases = {
        EconomicSubtype.FED_RATES: ("Fed Decision in September", "Federal Reserve policy decision rules."),
        EconomicSubtype.CPI_INFLATION: ("Will CPI inflation exceed 3 percent?", "Official economic release rules."),
        EconomicSubtype.EMPLOYMENT: ("Will unemployment fall in September?", "Official economic release rules."),
        EconomicSubtype.GDP: ("Will GDP growth exceed expectations?", "Official economic release rules."),
        EconomicSubtype.RECESSION: ("Will a recession be declared?", "Official economic release rules."),
    }
    for subtype, (question, description) in cases.items():
        assert economic_subtype(
            normalized_economic_market(question, description)
        ) is subtype


def test_economic_provider_is_registered_fail_closed_and_never_uses_price():
    market = normalized_economic_market(
        "Will CPI inflation exceed 3 percent?", "Official CPI release rules."
    )
    provider = EconomicsEvidenceProvider()
    assert provider.supports(market)
    assert provider.assess(market) is None
    assert provider.last_reason.startswith("missing_independent_economic_source:CPI_INFLATION")


def test_authoritative_source_observation_is_recorded_without_becoming_a_forecast():
    from parallax.economic_sources import EconomicObservation

    observation = EconomicObservation(
        "FRED", "DFF", "2026-09-15", 3.75, "2026-09-15T12:00:00+00:00", 0.0
    )

    class Sources:
        def latest(self, subtype):
            return observation

    market = normalized_economic_market(
        "Fed Decision in September", "Federal Reserve policy decision rules."
    )
    provider = EconomicsEvidenceProvider(Sources())
    assert provider.assess(market) is None
    assert provider.last_observation == observation
    assert provider.last_reason == "economic_probability_methodology_unvalidated:FED_RATES"


def test_source_clients_use_mocked_authoritative_payloads_and_cache_them():
    from parallax.economic_sources import BLSClient, FREDClient

    fred_calls = []

    def fred_transport(url):
        fred_calls.append(url)
        return {"observations": [{"date": "2026-09-15", "value": "3.75"}]}

    fred = FREDClient(api_key="test-only", transport=fred_transport)
    first = fred.latest("DFF")
    second = fred.latest("DFF")
    assert first.series_id == "DFF"
    assert first.value == 3.75
    assert second == first
    assert len(fred_calls) == 1

    bls_calls = []

    def bls_transport(url, body):
        bls_calls.append((url, body))
        return {"Results": {"series": [{"data": [{"year": "2026", "period": "M08", "value": "4.3"}]}]}}

    bls = BLSClient(transport=bls_transport)
    assert bls.latest("LNS14000000").value == 4.3
    assert bls.latest("LNS14000000").value == 4.3
    assert len(bls_calls) == 1


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
