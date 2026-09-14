from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from parallax.event_details import VenueMarketDetail

_SPEC = importlib.util.spec_from_file_location(
    "parallax_event_enrichment_probe",
    Path(__file__).resolve().parents[1] / "scripts" / "parallax_event_enrichment_probe.py",
)
assert _SPEC is not None and _SPEC.loader is not None
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)


PMUS_ROW = {
    "id": "pmus-vague",
    "slug": "pmus-vague",
    "question": "Senate vote on CLARITY",
    "description": "Resolves according to published rules.",
    "active": True,
    "closed": False,
    "marketSides": [
        {"long": True, "description": "YES"},
        {"long": False, "description": "NO"},
    ],
}
KALSHI_ROW = {
    "ticker": "KX-VAGUE",
    "title": "Senate vote on CLARITY",
    "rules_primary": "Resolves according to published rules.",
    "status": "open",
    "yes_sub_title": "YES",
    "no_sub_title": "NO",
}
EXACT_RULES = (
    "Resolves YES if the U.S. Senate invokes cloture on the motion to proceed "
    "to H.R. 3633 on September 15, 2026 using the official U.S. Senate roll call."
)


def test_probe_isolates_partial_detail_failure_and_keeps_candidate(monkeypatch):
    class PMUS:
        def markets_page(self, *, limit, offset):
            return [{"id": "pmus-vague", "slug": "pmus-vague", "raw": PMUS_ROW}] if offset == 0 else []

        def close(self):
            pass

    class Kalshi:
        def markets_page(self, *, limit):
            return {"markets": [KALSHI_ROW]}

    def pmus_failure(client, candidate):
        raise OSError("fixture failure")

    def kalshi_success(client, candidate):
        market = {
            **KALSHI_ROW,
            "rules_primary": EXACT_RULES,
        }
        return VenueMarketDetail(
            market=market,
            raw_response={"market": market},
            source_reference="https://venue.example/KX-VAGUE",
        )

    monkeypatch.setattr(probe, "PolymarketUSPublicClient", PMUS)
    monkeypatch.setattr(probe, "KalshiPublicClient", Kalshi)
    monkeypatch.setattr(probe, "fetch_pmus_market_detail", pmus_failure)
    monkeypatch.setattr(probe, "fetch_kalshi_market_detail", kalshi_success)
    monkeypatch.setattr(
        probe,
        "utcnow",
        lambda: SimpleNamespace(
            isoformat=lambda: "2026-09-14T12:00:00+00:00",
            astimezone=lambda zone: SimpleNamespace(isoformat=lambda: "2026-09-14T12:00:00+00:00"),
        ),
    )

    report = probe.run_probe(per_venue=1, detail_cap=2)
    assert report["before"]["states"]["AMBIGUOUS"] == 2
    assert report["after"]["states"]["AMBIGUOUS"] == 1
    assert report["after"]["states"]["EXACT"] == 1
    assert report["ambiguity_reduction"] == 0.5
    assert report["venues"]["PMUS"]["detail_fetch_failures"] == 1
    assert report["venues"]["KALSHI"]["detail_fetch_successes"] == 1
    assert report["errors"][0]["error_type"] == "OSError"
