"""
SWARM — BlackGlassLab Trading Dashboard
Bloomberg-style dark theme with live auto-refresh
"""
import json
import math
import random
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ---------------------------------------------------------------------------
# Page config + Bloomberg CSS
# ---------------------------------------------------------------------------
st.set_page_config(page_title="SWARM · BlackGlassLab", page_icon="⚡", layout="wide")

st.markdown("""
<style>
  /* Bloomberg terminal theme */
  .stApp { background-color: #0a0a0a; color: #e0e0e0; font-family: 'Courier New', monospace; }
  .stApp header { background-color: #0a0a0a; }
  .block-container { padding-top: 1rem; }
  div[data-testid="metric-container"] {
    background: #111; border: 1px solid #00ff88;
    border-radius: 4px; padding: 12px; margin: 4px;
  }
  div[data-testid="metric-container"] label { color: #888 !important; font-size: 11px; }
  div[data-testid="metric-container"] div[data-testid="metric-value"] { color: #00ff88 !important; font-size: 22px; font-weight: bold; }
  .stTabs [data-baseweb="tab"] { background: #111; color: #888; border: 1px solid #222; }
  .stTabs [aria-selected="true"] { background: #001a0d !important; color: #00ff88 !important; border-color: #00ff88 !important; }
  .stDataFrame { border: 1px solid #00ff88; }
  .stButton>button { background: #001a0d; color: #00ff88; border: 1px solid #00ff88; }
  .stButton>button:hover { background: #00ff88; color: #000; }
  .stSidebar { background: #060606; border-right: 1px solid #222; }
  h1, h2, h3 { color: #00ff88 !important; }
  .trade-card { background: #111; border-left: 3px solid #00ff88; padding: 10px; margin: 8px 0; border-radius: 2px; }
  .alert-banner { background: #001a0d; border: 1px solid #00ff88; padding: 8px 12px; color: #00ff88; font-weight: bold; margin-bottom: 10px; }
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "memory" / "runs.sqlite"
DIAG_PATH = ROOT / "signals" / "infer_diagnostics.json"
LOG_PATH = ROOT / "logs" / "infer_loop.log"
CUTOFF = "2026-03-28T21:00"

MARKET_EXPIRY = {
    "will-the-court-force-trump-to-refund-tariffs-2026-06-30": "2026-06-30",
    "will-duke-win-the-2026-ncaa-tournament": "2026-04-08",
    "will-tennessee-win-the-2026-ncaa-tournament": "2026-04-08",
    "will-iowa-win-the-2026-ncaa-tournament": "2026-04-08",
    "will-rory-mcilroy-win-the-2026-masters-tournament": "2026-04-13",
    "will-scottie-scheffler-win-the-2026-masters-tournament": "2026-04-13",
    "will-bryson-dechambeau-win-the-2026-masters-tournament": "2026-04-13",
    "will-jon-rahm-win-the-2026-masters-tournament": "2026-04-13",
    "will-jason-day-win-the-2026-masters-tournament": "2026-04-13",
    "will-brooks-koepka-win-the-2026-masters-tournament": "2026-04-13",
    "will-no-fed-rate-cuts-happen-in-2026": "2026-12-31",
    "eth-flipped-in-2026": "2026-12-31",
    "will-bitcoin-hit-150k-by-june-30-2026": "2026-06-30",
    "us-recession-by-end-of-2026": "2026-12-31",
}

# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------
@st.cache_data(ttl=20)
def load_trades() -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame()
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        f"SELECT * FROM paper_trades WHERE ts_utc > '{CUTOFF}' ORDER BY ts_utc DESC", conn)
    conn.close()
    if df.empty:
        return df
    def parse_notes(n):
        try: return json.loads(n) if isinstance(n, str) else (n or {})
        except: return {}
    df["_notes"] = df["notes"].apply(parse_notes)
    df["crowd_p_yes"] = df["_notes"].apply(lambda n: n.get("p_yes_market"))
    df["llm_confidence"] = df["_notes"].apply(lambda n: n.get("llm", {}).get("confidence", 0.7))
    df["rationale"] = df["_notes"].apply(lambda n: n.get("llm", {}).get("rationale", ""))
    df["ts_utc"] = pd.to_datetime(df["ts_utc"], utc=True)
    return df

@st.cache_data(ttl=15)
def load_diagnostics() -> dict:
    if not DIAG_PATH.exists(): return {}
    try: return json.loads(DIAG_PATH.read_text())
    except: return {}

@st.cache_data(ttl=15)
def load_log(n=10) -> list:
    if not LOG_PATH.exists(): return []
    try: return LOG_PATH.read_text().splitlines()[-n:]
    except: return []

# ---------------------------------------------------------------------------
# Finance helpers
# ---------------------------------------------------------------------------
def kelly_bet(p_win, share_price, bankroll, fraction=0.5):
    b = (1 - share_price) / max(share_price, 0.001)
    raw = (b * p_win - (1 - p_win)) / max(b, 0.001)
    return max(0.0, raw * fraction * bankroll)

def expected_value(p_win, bet, crowd_price, side):
    sp = (1 - crowd_price) if side == "NO" else crowd_price
    if sp <= 0: return 0
    payout = bet / sp
    return p_win * (payout - bet) - (1 - p_win) * bet

def days_until(date_str):
    try:
        exp = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        delta = exp - datetime.now(timezone.utc)
        return max(0, delta.days)
    except: return None

def monte_carlo(trades_data, bankroll, n_sim=2000):
    if not trades_data: return []
    results = []
    for _ in range(n_sim):
        total = 0.0
        for p_win, bet, crowd, side in trades_data:
            sp = (1 - crowd) if side == "NO" else crowd
            if sp <= 0: continue
            payout = bet / sp
            won = random.random() < p_win
            total += (payout - bet) if won else -bet
        results.append(total)
    return sorted(results)

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
st.sidebar.markdown('<h2 style="color:#00ff88;letter-spacing:3px;">⚡ SWARM</h2>', unsafe_allow_html=True)
st.sidebar.markdown('<p style="color:#444;font-size:11px;">BlackGlassLab · AI Prediction Markets</p>', unsafe_allow_html=True)
st.sidebar.divider()

bankroll = st.sidebar.number_input("Bankroll ($)", 100, 1_000_000, 10_000, 500)
kelly_frac = st.sidebar.select_slider("Kelly Fraction", [0.25, 0.5, 0.75, 1.0], 0.5,
                                       format_func=lambda x: f"{x:.0%} Kelly")
auto_refresh = st.sidebar.toggle("Auto-Refresh (30s)", value=True)
st.sidebar.divider()

# Loop status
log = load_log(5)
last_run = next((l for l in reversed(log) if "infer loop" in l), None)
if last_run:
    ts = last_run.replace("== ", "").replace(" : infer loop ==", "")
    st.sidebar.markdown(f'<div style="color:#00ff88;font-size:12px;">🟢 LOOP ALIVE<br><span style="color:#888">{ts}</span></div>', unsafe_allow_html=True)
else:
    st.sidebar.markdown('<div style="color:#ff4444;">🔴 LOOP NOT DETECTED</div>', unsafe_allow_html=True)

st.sidebar.divider()
if st.sidebar.button("⟳ Refresh Now"):
    st.cache_data.clear()
    st.rerun()

now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
st.sidebar.caption(f"Last loaded: {now_str}")

# Auto-refresh
if auto_refresh:
    import time
    st.sidebar.caption("⏱ Auto-refreshing every 30s")

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.markdown('<div class="alert-banner">⚡ SWARM TRADING TERMINAL &nbsp;·&nbsp; PAPER MODE &nbsp;·&nbsp; CLAUDE HAIKU REASONING ENGINE</div>', unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "📊 Portfolio", "🔍 Scanner", "📈 Charts", "🎲 Monte Carlo", "🏆 Performance"
])

df = load_trades()

# ===========================================================================
# TAB 1 — PORTFOLIO
# ===========================================================================
with tab1:
    if df.empty:
        st.info("No real trades yet.")
    else:
        # Top metrics
        total_bet = len(df) * 100.0
        rows, ev_total = [], 0.0
        for _, r in df.iterrows():
            crowd = r.get("crowd_p_yes") or r["p_yes"]
            side = r["side"]
            conf = r.get("llm_confidence") or 0.7
            p_win = (1 - r["p_yes"]) if side == "NO" else r["p_yes"]
            sp = (1 - crowd) if side == "NO" else crowd
            if sp <= 0: continue
            payout = 100.0 / sp
            profit = payout - 100
            roi = profit
            ev = expected_value(p_win, 100, crowd, side)
            ev_total += ev
            k = kelly_bet(p_win, sp, bankroll, kelly_frac)
            exp_str = "—"
            mkt = r["market_id"]
            if mkt in MARKET_EXPIRY:
                d = days_until(MARKET_EXPIRY[mkt])
                exp_str = f"{d}d" if d is not None else "—"
            rows.append({
                "Market": mkt[:38],
                "Side": side,
                "Claude%": f"{r['p_yes']*100:.0f}%",
                "Crowd%": f"{crowd*100:.0f}%",
                "Edge": f"{r['edge']*100:.1f}%",
                "Conf": f"{conf*100:.0f}%",
                "EV": f"${ev:+.0f}",
                "Payout": f"${payout:.0f}",
                "ROI": f"{roi:.0f}%",
                f"Kelly({kelly_frac:.0%})": f"${k:.0f}",
                "Expires": exp_str,
            })

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Open Trades", len(df))
        c2.metric("Deployed", f"${total_bet:.0f}")
        c3.metric("Total EV", f"${ev_total:+.0f}")
        total_payout = sum(100 / ((1 - (r.get("crowd_p_yes") or r["p_yes"])) if r["side"]=="NO" else (r.get("crowd_p_yes") or r["p_yes"])) for _, r in df.iterrows() if ((1 - (r.get("crowd_p_yes") or r["p_yes"])) if r["side"]=="NO" else (r.get("crowd_p_yes") or r["p_yes"])) > 0)
        c4.metric("Payout if ALL Win", f"${total_payout:.0f}")
        c5.metric("Profit if ALL Win", f"+${total_payout - total_bet:.0f}")

        st.divider()
        st.subheader("Positions")
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        # Countdown timers
        st.divider()
        st.subheader("⏳ Market Expiry Countdown")
        timer_cols = st.columns(min(len(df), 4))
        for i, (_, r) in enumerate(df.iterrows()):
            mkt = r["market_id"]
            if mkt in MARKET_EXPIRY:
                d = days_until(MARKET_EXPIRY[mkt])
                label = mkt.replace("will-", "").replace("-2026", "").replace("-", " ")[:22]
                timer_cols[i % 4].metric(label, f"{d} days" if d else "—",
                                          "⚠️ SOON" if d and d < 7 else "")

        # Confidence gauges
        st.divider()
        st.subheader("🎯 Claude Confidence Gauges")
        gauge_cols = st.columns(min(len(df), 4))
        for i, (_, r) in enumerate(df.iterrows()):
            conf = (r.get("llm_confidence") or 0.7) * 100
            fig_g = go.Figure(go.Indicator(
                mode="gauge+number",
                value=conf,
                number={"suffix": "%", "font": {"color": "#00ff88"}},
                gauge={
                    "axis": {"range": [50, 95], "tickcolor": "#444"},
                    "bar": {"color": "#00ff88"},
                    "bgcolor": "#111",
                    "steps": [
                        {"range": [50, 65], "color": "#1a0000"},
                        {"range": [65, 80], "color": "#0d1a00"},
                        {"range": [80, 95], "color": "#001a0d"},
                    ],
                    "threshold": {"line": {"color": "yellow", "width": 2}, "value": 72},
                },
                title={"text": r["market_id"][:20], "font": {"color": "#888", "size": 10}},
            ))
            fig_g.update_layout(height=180, margin=dict(t=30, b=0, l=10, r=10),
                                 paper_bgcolor="#0a0a0a", font_color="#e0e0e0")
            gauge_cols[i % 4].plotly_chart(fig_g, use_container_width=True)

        # Rationales
        st.divider()
        st.subheader("🧠 Claude Rationales")
        for _, r in df.iterrows():
            crowd = r.get("crowd_p_yes") or r["p_yes"]
            ev = expected_value(
                (1 - r["p_yes"]) if r["side"] == "NO" else r["p_yes"],
                100, crowd, r["side"])
            with st.expander(f"{r['side']} · {r['market_id'][:50]} · Edge {r['edge']*100:.1f}% · EV ${ev:+.0f}"):
                col1, col2, col3 = st.columns(3)
                col1.metric("Claude p_yes", f"{r['p_yes']*100:.1f}%")
                col2.metric("Crowd p_yes", f"{crowd*100:.1f}%")
                col3.metric("Confidence", f"{(r.get('llm_confidence') or 0.7)*100:.0f}%")
                st.info(r["rationale"] or "No rationale recorded")
                st.caption(str(r["ts_utc"]))


# ===========================================================================
# TAB 2 — SCANNER
# ===========================================================================
with tab2:
    diag = load_diagnostics()
    if not diag:
        st.info("Waiting for loop to run...")
    else:
        s = diag.get("settings", {})
        sm = diag.get("summary", {})
        ts = diag.get("ts_utc", "")

        c1,c2,c3,c4,c5 = st.columns(5)
        c1.metric("Last Run", ts[11:19]+" UTC" if ts else "—")
        c2.metric("Evaluated", sm.get("evaluated", 0))
        c3.metric("Passed", sm.get("passed", 0))
        c4.metric("Max Disagree", f"{s.get('max_disagree',0):.0%}")
        c5.metric("Min Edge", f"{s.get('min_edge_abs',0):.0%}")

        rows = diag.get("rows", [])
        if rows:
            st.divider()
            scan = [{
                "Market": r.get("slug","")[:48],
                "Decision": r.get("decision",""),
                "Edge": r.get("edge_abs", 0),
                "Disagree": r.get("disagreement", 0),
                "Crowd%": f"{r.get('p_yes_market',0)*100:.1f}%",
                "Claude%": f"{r.get('p_yes_model',0)*100:.1f}%",
                "Side": r.get("side",""),
                "Reason": r.get("reason",""),
            } for r in rows]
            scan_df = pd.DataFrame(scan)

            fig = px.bar(scan_df.sort_values("Edge"),
                x="Edge", y="Market", orientation="h",
                color="Decision",
                color_discrete_map={"PASS":"#00cc66","REJECT":"#cc3333"},
                title="Edge by Market — Last Cycle")
            fig.add_vline(x=s.get("min_edge_abs",0.04), line_dash="dash",
                         line_color="yellow", annotation_text="Threshold")
            fig.update_layout(template="plotly_dark", height=380,
                             paper_bgcolor="#0a0a0a", plot_bgcolor="#111")
            st.plotly_chart(fig, use_container_width=True)
            st.dataframe(scan_df, use_container_width=True, hide_index=True)

        rej = {k:v for k,v in sm.get("rejected",{}).items() if v > 0}
        if rej:
            st.divider()
            fig2 = px.pie(pd.DataFrame([{"Reason":k,"Count":v} for k,v in rej.items()]),
                names="Reason", values="Count", title="Rejection Reasons",
                color_discrete_sequence=px.colors.qualitative.Dark24)
            fig2.update_layout(template="plotly_dark", paper_bgcolor="#0a0a0a")
            st.plotly_chart(fig2, use_container_width=True)

        st.divider()
        st.subheader("Loop Log")
        st.code("\n".join(load_log(10)), language="bash")


# ===========================================================================
# TAB 3 — CHARTS
# ===========================================================================
with tab3:
    if df.empty or len(df) < 2:
        st.info("Need at least 2 trades for charts.")
    else:
        col1, col2 = st.columns(2)

        # EV Ranking
        with col1:
            st.subheader("💰 Expected Value Ranking")
            ev_rows = []
            for _, r in df.iterrows():
                crowd = r.get("crowd_p_yes") or r["p_yes"]
                p_win = (1-r["p_yes"]) if r["side"]=="NO" else r["p_yes"]
                ev = expected_value(p_win, 100, crowd, r["side"])
                ev_rows.append({"Market": r["market_id"][:30], "EV": ev, "Side": r["side"]})
            ev_df = pd.DataFrame(ev_rows).sort_values("EV")
            fig = px.bar(ev_df, x="EV", y="Market", orientation="h",
                color="EV", color_continuous_scale=["#cc0000","#ffaa00","#00cc66"],
                title="Expected Value per Trade ($100 bet)")
            fig.add_vline(x=0, line_color="white", line_dash="dash")
            fig.update_layout(template="plotly_dark", paper_bgcolor="#0a0a0a",
                             plot_bgcolor="#111", height=320)
            st.plotly_chart(fig, use_container_width=True)

        # Claude vs Crowd
        with col2:
            st.subheader("🔍 Claude vs Crowd")
            cc_rows = []
            for _, r in df.iterrows():
                crowd = r.get("crowd_p_yes") or r["p_yes"]
                cc_rows.append({"Market": r["market_id"][:25],
                               "Claude": r["p_yes"], "Crowd": crowd})
            cc_df = pd.DataFrame(cc_rows)
            fig2 = go.Figure()
            fig2.add_trace(go.Bar(name="Claude", x=cc_df["Market"], y=cc_df["Claude"],
                                  marker_color="#4488ff"))
            fig2.add_trace(go.Bar(name="Crowd", x=cc_df["Market"], y=cc_df["Crowd"],
                                  marker_color="#ff8844"))
            fig2.update_layout(barmode="group", template="plotly_dark",
                              paper_bgcolor="#0a0a0a", plot_bgcolor="#111",
                              xaxis_tickangle=-30, height=320,
                              title="Where Claude Diverges From Crowd")
            st.plotly_chart(fig2, use_container_width=True)

        st.divider()

        # Kelly sizing
        st.subheader(f"📐 Kelly Bet Sizes — ${bankroll:,} Bankroll")
        k_rows = []
        for _, r in df.iterrows():
            crowd = r.get("crowd_p_yes") or r["p_yes"]
            sp = (1-crowd) if r["side"]=="NO" else crowd
            if sp <= 0: continue
            p_win = (1-r["p_yes"]) if r["side"]=="NO" else r["p_yes"]
            for frac, lbl in [(0.25,"Qtr"),(0.5,"Half"),(1.0,"Full")]:
                k_rows.append({"Market": r["market_id"][:28],
                               "Kelly": lbl, "Bet": kelly_bet(p_win,sp,bankroll,frac)})
        fig3 = px.bar(pd.DataFrame(k_rows), x="Market", y="Bet", color="Kelly",
            barmode="group", title="Recommended Bet by Kelly Fraction",
            color_discrete_map={"Qtr":"#224488","Half":"#4488ff","Full":"#00ddff"})
        fig3.update_layout(template="plotly_dark", paper_bgcolor="#0a0a0a",
                          plot_bgcolor="#111", xaxis_tickangle=-30)
        st.plotly_chart(fig3, use_container_width=True)

        st.divider()

        # Payout waterfall
        st.subheader("💵 Payout if Win ($100 flat bet)")
        pay_rows = []
        for _, r in df.iterrows():
            crowd = r.get("crowd_p_yes") or r["p_yes"]
            sp = (1-crowd) if r["side"]=="NO" else crowd
            if sp <= 0: continue
            payout = 100/sp
            pay_rows.append({"Market": r["market_id"][:32], "Payout": payout, "Profit": payout-100})
        pay_df = pd.DataFrame(pay_rows).sort_values("Payout", ascending=True)
        fig4 = px.bar(pay_df, x="Payout", y="Market", orientation="h",
            color="Profit", color_continuous_scale="Greens",
            title="Payout per Trade if Correct")
        fig4.add_vline(x=100, line_color="white", line_dash="dash",
                      annotation_text="Break even")
        fig4.update_layout(template="plotly_dark", paper_bgcolor="#0a0a0a",
                          plot_bgcolor="#111", height=320)
        st.plotly_chart(fig4, use_container_width=True)


# ===========================================================================
# TAB 4 — MONTE CARLO
# ===========================================================================
with tab4:
    st.subheader("🎲 Monte Carlo Portfolio Simulation")
    st.caption("2,000 simulated outcomes based on Claude's probability estimates")

    if df.empty:
        st.info("No trades to simulate yet.")
    else:
        n_sims = st.slider("Simulations", 500, 5000, 2000, 500)
        bet_size = st.number_input("Bet size per trade ($)", 50, 10000, 100, 50)

        trades_data = []
        for _, r in df.iterrows():
            crowd = r.get("crowd_p_yes") or r["p_yes"]
            p_win = (1-r["p_yes"]) if r["side"]=="NO" else r["p_yes"]
            trades_data.append((p_win, float(bet_size), crowd, r["side"]))

        results = monte_carlo(trades_data, bankroll, n_sims)
        if results:
            p10 = results[int(len(results)*0.10)]
            p50 = results[int(len(results)*0.50)]
            p90 = results[int(len(results)*0.90)]
            p_pos = sum(1 for r in results if r > 0) / len(results) * 100

            c1,c2,c3,c4 = st.columns(4)
            c1.metric("Worst 10%", f"${p10:+.0f}")
            c2.metric("Median Outcome", f"${p50:+.0f}")
            c3.metric("Best 10%", f"${p90:+.0f}")
            c4.metric("P(Profit)", f"{p_pos:.0f}%")

            st.divider()
            fig = go.Figure()
            fig.add_trace(go.Histogram(x=results, nbinsx=60,
                marker_color="#00cc66", opacity=0.8, name="Outcomes"))
            fig.add_vline(x=0, line_color="white", line_dash="dash",
                         annotation_text="Break even", annotation_font_color="white")
            fig.add_vline(x=p50, line_color="#00ff88", line_dash="dot",
                         annotation_text=f"Median ${p50:+.0f}", annotation_font_color="#00ff88")
            fig.add_vline(x=p10, line_color="#ff4444", line_dash="dot",
                         annotation_text=f"10th pct ${p10:+.0f}", annotation_font_color="#ff4444")
            fig.update_layout(
                title=f"Distribution of Portfolio Outcomes ({n_sims:,} simulations, ${bet_size}/trade)",
                template="plotly_dark", paper_bgcolor="#0a0a0a", plot_bgcolor="#111",
                xaxis_title="P&L ($)", yaxis_title="Frequency", height=420)
            st.plotly_chart(fig, use_container_width=True)

            st.divider()
            st.subheader("Per-Trade Win Probabilities")
            prob_rows = []
            for (p_win, bet, crowd, side), (_, r) in zip(trades_data, df.iterrows()):
                sp = (1-crowd) if side=="NO" else crowd
                payout = bet/sp if sp > 0 else 0
                prob_rows.append({
                    "Market": r["market_id"][:40],
                    "Side": side,
                    "Claude Win%": f"{p_win*100:.1f}%",
                    "Payout if Win": f"${payout:.0f}",
                    "EV": f"${expected_value(p_win,bet,crowd,side):+.0f}",
                })
            st.dataframe(pd.DataFrame(prob_rows), use_container_width=True, hide_index=True)


# ===========================================================================
# TAB 5 — PERFORMANCE
# ===========================================================================
with tab5:
    st.subheader("🏆 Performance Tracker")
    resolved = df[df["status"]=="RESOLVED"] if not df.empty else pd.DataFrame()

    c1,c2,c3,c4 = st.columns(4)
    c1.metric("Total Trades", len(df) if not df.empty else 0)
    c2.metric("Resolved", len(resolved))
    c3.metric("Pending", len(df)-len(resolved) if not df.empty else 0)

    if not resolved.empty:
        wins = resolved[resolved["resolved_outcome"]==resolved["side"]]
        c4.metric("Win Rate", f"{len(wins)/len(resolved)*100:.0f}%")
    else:
        c4.metric("Win Rate", "Pending")
        st.divider()
        st.info("Performance stats appear here once markets resolve.")
        st.subheader("📅 Open Trade Resolution Schedule")
        sched = []
        if not df.empty:
            for _, r in df.iterrows():
                mkt = r["market_id"]
                exp = MARKET_EXPIRY.get(mkt, "Unknown")
                d = days_until(exp) if exp != "Unknown" else None
                crowd = r.get("crowd_p_yes") or r["p_yes"]
                p_win = (1-r["p_yes"]) if r["side"]=="NO" else r["p_yes"]
                ev = expected_value(p_win, 100, crowd, r["side"])
                sched.append({
                    "Market": mkt[:42],
                    "Side": r["side"],
                    "Expires": exp,
                    "Days Left": d if d is not None else "—",
                    "EV": f"${ev:+.0f}",
                    "Edge": f"{r['edge']*100:.1f}%",
                })
            sched_df = pd.DataFrame(sched)
            if "Days Left" in sched_df.columns:
                sched_df = sched_df.sort_values("Days Left",
                    key=lambda x: pd.to_numeric(x, errors="coerce"))
            st.dataframe(sched_df, use_container_width=True, hide_index=True)

        st.divider()
        st.subheader("What you'll see here once trades resolve:")
        st.markdown("""
        - **Win Rate** by category (crypto vs politics vs sports vs macro)
        - **Brier Score** — calibration quality (lower = better)
        - **P&L Equity Curve** — cumulative profit over time
        - **Edge vs Outcome** — do higher-edge trades win more?
        - **Calibration Chart** — is Claude over/underconfident?
        - **Sharpe Ratio** — risk-adjusted return
        """)

# ---------------------------------------------------------------------------
# Auto-refresh
# ---------------------------------------------------------------------------
if auto_refresh:
    import time
    time.sleep(30)
    st.cache_data.clear()
    st.rerun()
