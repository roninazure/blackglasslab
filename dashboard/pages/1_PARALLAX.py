"""Retail page in the existing Streamlit product, also runnable independently."""

import os
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from parallax.__main__ import make_service, refresh, start_refresh_loop
from parallax.entitlements import PUBLIC_PLANS, Plan

st.set_page_config(page_title="PARALLAX · Today's Plays", page_icon="◈", layout="wide")
st.markdown(
    """<style>
.stApp {background:#0c1420;color:#edf2f7}
.block-container {max-width:1180px;padding-top:2rem}
h1 {letter-spacing:.16em;font-weight:800!important}
div[data-testid="stVerticalBlockBorderWrapper"] {border-color:#2a394e;border-radius:14px}
div[data-testid="stMetricValue"] {font-size:1.65rem}
</style>""",
    unsafe_allow_html=True,
)

DEMO = os.environ.get("PARALLAX_MODE", "live") == "demo"
DB = os.environ.get(
    "PARALLAX_PRODUCT_DB", "data/parallax-commercial/publications.sqlite"
)


@st.cache_resource
def product(db, demo):
    service = make_service(db, demo=demo, limit=6)
    if not demo:
        start_refresh_loop(service, demo=False, limit=6)
    return service


st.title("PARALLAX")
st.caption("Prediction-market intelligence. A clearer decision.")
st.header("Today's Plays")
if DEMO:
    st.warning(
        "SYNTHETIC DEMO · Every market, price and value below is invented to demonstrate the product. No recommendations or historical results."
    )
else:
    st.caption(
        "LIVE READ-ONLY · Polymarket US + Kalshi · Bounded market sample · Prices can change before entry."
    )

with st.sidebar:
    st.subheader("Find your next decision")
    for tier in PUBLIC_PLANS:
        st.write(f"**{tier['name']}** · {tier['price']}")
    st.caption(
        "Paid access and billing integration are pending. No payments are collected."
    )
    plan = (
        Plan(
            st.selectbox("Demo access preview", ["pro", "explorer"], disabled=not DEMO)
        )
        if DEMO
        else Plan.EXPLORER
    )
    st.caption(
        "Prediction markets involve risk. Models can be wrong. Past results do not guarantee future performance."
    )

service = product(DB, DEMO)
if st.button("Refresh market prices"):
    with st.spinner("Checking both venues…"):
        refresh(service, demo=DEMO, limit=6)


def cents(value):
    return "Not established" if value is None else f"{value * 100:.1f}¢"


def money(value):
    return "Unknown" if value is None else f"${value:,.2f}"


def render_card(play):
    with st.container(border=True):
        st.caption(
            f"PARALLAX PLAY · {'POLYMARKET US' if play['venue'] == 'POLYMARKET' else 'KALSHI'} · {play['status']}"
        )
        action = play["suggested_action"]
        st.subheader(f"BUY {play['side']}" if action == "BUY" else action)
        st.write(play["market_title"])
        st.caption(f"Considering {play['side']}: {play['side_description']}")
        if play["data_freshness"] != "FRESH":
            st.info(
                "This price is stale or unverified. The figures below describe a past quote. Refresh before considering entry."
            )
        a, b, c = st.columns(3)
        a.metric(
            "Current entry price"
            if play["data_freshness"] == "FRESH"
            else "Last observed price",
            cents(play["current_price"]),
        )
        b.metric("PARALLAX Value", cents(play["parallax_fair_value"]))
        c.metric(
            "PARALLAX Edge",
            "Not established"
            if play["edge_points"] is None
            else f"{play['edge_points']:.1f} points",
        )
        st.write(f"Confidence: **{play['confidence_band']}**")
        st.caption("Evidence qualification, not your chance of winning.")
        stake = st.radio(
            "If you risk (contract budget)",
            [10, 25, 50, 100],
            format_func=lambda x: f"${x}",
            horizontal=True,
            key=play["id"],
        )
        example = next(e for e in play["retail_examples"] if e["stake"] == stake)
        if example["available"]:
            a, b, c = st.columns(3)
            a.metric("Payout if correct", money(example["estimated_payout_if_correct"]))
            b.metric(
                "Profit before fees", money(example["estimated_profit_if_correct"])
            )
            c.metric("Maximum loss before fees", money(example["maximum_loss"]))
            st.caption(
                f"{example['contracts_or_shares']:g} contracts · {money(example['amount_spent'])} spent · {money(example['unspent'])} unspent"
            )
            if example["fees_estimate"] is None:
                st.caption(
                    "Fees are not verified. They add to your cost and maximum loss. Net profit is unavailable."
                )
            else:
                st.caption(
                    f"Estimated fees: {money(example['fees_estimate'])} · Slippage buffer: {money(example['slippage_estimate'])} · Total possible loss: {money(example['maximum_loss_including_fees'])} · Profit after estimated costs: {money(example['net_profit_if_correct'])}"
                )
            st.caption(
                "At the displayed ask only. A larger order or a changing book may cost more. These are examples, not a personal stake recommendation."
            )
        else:
            st.info(example["reason"])
        st.write(f"Resolution: {play['resolution_time'] or 'Date not verified'}")
        st.write(
            "**WHY PARALLAX LIKES IT**" if action == "BUY" else "**WHY THIS VERDICT**"
        )
        st.write(play["reason_summary"])
        st.write("**WHAT COULD GO WRONG**")
        st.write(" ".join(play["risk_factors"]))
        st.write(f"**PARALLAX VERDICT · {action}**")
        with st.expander("Play details · What would change this verdict?"):
            st.caption(f"Venue reference: {play['market_reference']}")
            for condition in play["invalidation_conditions"]:
                st.write(f"• {condition}")
            st.caption(
                f"Price freshness: {play['data_freshness']} · Valid until {play['expires_at']}"
            )
            if plan == Plan.EXPLORER:
                st.info(
                    "PRO includes full evidence, economic reasoning and advanced filters. Billing is not connected yet."
                )
            else:
                full = service.play(play["id"], plan)
                market = next(
                    m
                    for m in service.market_views(plan)
                    if m["venue"] == play["venue"]
                    and m["venue_market_id"] == play["market_id"]
                )
                st.write("**Venue settlement rules**")
                st.text(
                    market["resolution_rules"] or "Rules not provided by the source."
                )
                for reason in full["reason_factors"]:
                    st.write(reason)
                st.json(
                    {
                        "qualification_score": full["confidence_score"],
                        "method": full["confidence_method"],
                        "evidence": full["evidence"],
                        "checks_to_resolve": full["decision_reasons"],
                        "expected_profit_on_25_example": full["expected_value"],
                        "expected_return": full["expected_return"],
                    }
                )


@st.fragment(run_every=30)
def feed():
    # Demo evidence may refresh; live quotes keep their true age until a read completes.
    if DEMO:
        refresh(service, demo=True, limit=6)
    rows = service.plays(plan)["items"]
    a, b, c, d = st.columns(4)
    a.metric(
        "ELITE",
        sum(
            p["confidence_band"] == "ELITE" and p["suggested_action"] == "BUY"
            for p in rows
        ),
    )
    b.metric(
        "HIGH",
        sum(
            p["confidence_band"] == "HIGH" and p["suggested_action"] == "BUY"
            for p in rows
        ),
    )
    c.metric("WATCH", sum(p["suggested_action"] == "WATCH" for p in rows))
    d.metric("PASS", sum(p["suggested_action"] == "PASS" for p in rows))
    st.caption("Counts reflect the accessible feed. Zero BUY plays is a valid result.")
    venue = st.radio("Venue", ["ALL", "POLYMARKET", "KALSHI"], horizontal=True)
    filters = {} if venue == "ALL" else {"venue": venue}
    if plan != Plan.EXPLORER:
        with st.expander("Advanced filters"):
            a, b, c = st.columns(3)
            action = a.selectbox("Verdict", ["ALL", "BUY", "WATCH", "PASS"])
            confidence = b.selectbox(
                "Confidence", ["ALL", "ELITE", "HIGH", "MEDIUM", "PASS"]
            )
            kind = c.selectbox(
                "Play type",
                [
                    "ALL",
                    "PARALLAX_EDGE",
                    "PARALLAX_REPRICE",
                    "PARALLAX_VALUE",
                    "PARALLAX_AVOID",
                ],
            )
            edge = st.number_input("Minimum edge (points)", min_value=0.0, value=0.0)
            horizon = st.selectbox(
                "Resolves within", ["Any time", "24 hours", "7 days", "30 days"]
            )
            filters.update(
                {
                    k: v
                    for k, v in {
                        "action": action,
                        "confidence": confidence,
                        "play_type": kind,
                    }.items()
                    if v != "ALL"
                }
            )
            if edge:
                filters["minimum_edge"] = edge
            if horizon != "Any time":
                filters["resolution_horizon"] = {
                    "24 hours": 24,
                    "7 days": 168,
                    "30 days": 720,
                }[horizon]
    response = service.plays(plan, **filters)
    if not response["items"]:
        st.info(
            "No plays meet these filters. PARALLAX will not invent recommendations to fill the feed."
        )
    for play in response["items"]:
        render_card(play)
    health = service.health()
    if health["status"] != "ok":
        st.caption(
            "Coverage is limited or some prices need refreshing. Check source status below."
        )
    with st.expander("Source status"):
        st.json({"venues": service.venues(), "health": health})
    st.subheader("Transparent track record")
    record = service.store.summary()
    if not record["published_plays"]:
        st.info(
            "No published actionable plays yet. Track record starts with explicitly published live PARALLAX BUY plays. Synthetic demos and research trials never count."
        )
    else:
        a, b, c, d = st.columns(4)
        a.metric("Published plays", record["published_plays"])
        b.metric("Wins", record["wins"])
        c.metric("Losses", record["losses"])
        d.metric("Voids", record["voids"])
        st.json(
            {k: v for k, v in record.items() if k not in {"publications", "outcomes"}}
        )


feed()
