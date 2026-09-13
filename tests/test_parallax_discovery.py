from parallax.discovery import Coverage, Routing, classify_market, deduplicate, discover_kalshi_scoped, normalize_market, paginate, paginate_collection


def row(**overrides):
    value = {"id": "m1", "title": "Will the CPI rise?", "description": "official CPI release", "category": "macro", "active": True}
    value.update(overrides)
    return value


def test_pagination_exposes_bounded_truncation_and_dedupes():
    def page(*, limit, offset):
        return {"markets": [row(id="same"), row(id=f"m{offset}")]}
    rows, report = paginate(page, page_size=2, max_pages=2)
    assert report.state is Coverage.BOUNDED
    assert report.unique_rows == 3
    assert len(deduplicate(normalize_market(x, venue="PMUS") for x in rows)) == 3


def test_nested_pmus_metadata_and_metadata_first_classification():
    market = normalize_market(row(id="mlb-1", title="Away vs Home baseball game", description="rules", category="", sports={"league": "MLB", "marketType": "moneyline"}, marketSides=[{"team": {"league": "MLB"}}]), venue="PMUS")
    classified = classify_market(market)
    assert classified.classification == "SPORTS"
    assert classified.subcategory == "MLB"
    assert classified.routing is Routing.SUPPORTED
    assert classified.classification_reason.startswith("metadata")


def test_kalshi_series_event_scope_and_unsupported_visibility():
    rows, report = discover_kalshi_scoped([{"ticker": "S1"}], lambda s: [{"ticker": "E1"}], lambda e: [row(id="k1", ticker="k1"), row(id="k1", ticker="k1")])
    assert report["markets_unique"] == 1
    market = classify_market(normalize_market(rows[0], venue="KALSHI"))
    assert market.routing is Routing.CANDIDATE
    assert market.venue_market_id == "k1"


def test_malformed_market_fails_closed():
    market = normalize_market({}, venue="KALSHI")
    assert market.routing is Routing.UNSUPPORTED
    assert market.venue_market_id == ""


def test_kalshi_ticker_families_are_metadata_classified():
    cases = {"KXMLBGAME-1": "MLB", "KXNFLGAME-1": "NFL", "KXCFBGAME-1": "CFB", "KXLIGAEXPSPREAD-1": "SOCCER", "KXTENNIS-1": "TENNIS", "KXUFCVICROUND-1": "OTHER_SPORTS"}
    for ticker, expected in cases.items():
        market = classify_market(normalize_market(row(id=ticker, ticker=ticker, title="generic spread total", description="rules", category="sports"), venue="KALSHI"))
        assert market.classification == "SPORTS"
        assert market.subcategory == expected


def test_finance_ticker_and_title_are_not_unsupported():
    market = classify_market(normalize_market(row(id="KXUST10AD-1", ticker="KXUST10AD-1", title="Treasury yield above 5%", description="rules", category="finance"), venue="KALSHI"))
    assert market.classification == "FINANCE"


def test_scope_failure_preserves_other_series_and_reports_partial():
    rows, report = discover_kalshi_scoped([{"ticker": "GOOD"}, {"ticker": "BAD"}], lambda series: (_ for _ in ()).throw(OSError()) if series == "BAD" else [{"ticker": "E1"}], lambda event: [row(id=event)])
    assert len(rows) == 1
    assert report["coverage"] == Coverage.PARTIAL.value
    assert report["failed_scopes"][0]["scope"] == "BAD"


def test_scope_pagination_hard_ceiling_is_bounded():
    def page(*, limit, cursor):
        return {"items": [{"id": f"{cursor}-{n}"} for n in range(limit)], "next_cursor": str(int(cursor or 0) + 1)}
    rows, report = paginate_collection(page, key="items", page_size=3, max_pages=20, max_rows=5)
    assert len(rows) == 5
    assert report.state is Coverage.BOUNDED


def test_sports_spread_total_does_not_invent_league():
    market = classify_market(normalize_market(row(id="KXUNKNOWNSPREAD-1", ticker="KXUNKNOWNSPREAD-1", title="generic spread total", description="rules", category="sports"), venue="KALSHI"))
    assert market.classification == "OTHER_UNSUPPORTED"
    assert market.routing is Routing.UNSUPPORTED


def test_metadata_beats_conflicting_title_and_nfl_stays_candidate():
    market = normalize_market(row(id="x", title="Will the Fed raise rates?", description="rules", category="sports", sport="NFL"), venue="KALSHI")
    classified = classify_market(market)
    assert classified.subcategory == "NFL"
    assert classified.routing is Routing.CANDIDATE
