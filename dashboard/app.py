"""
SWARM — Streamlit Dashboard
BlackGlassLab prediction market trading system
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="SWARM — BlackGlassLab",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "memory" / "runs.sqlite"
DIAG_PATH = ROOT / "signals" / "infer_diagnostics.json"
LOG_PATH = ROOT / "logs" / "infer_loop.log"
CUTOFF = "2026-03-28T21:00"  # pre-fix trades ignored

# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

@st.cache_data(ttl=30)
def load_trades() -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame()
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        f"SELECT * FROM paper_trades WHERE ts_utc > '{CUTOFF}' ORDER BY ts_utc DESC",
        conn,
    )
    conn.close()
    if df.empty:
        return df
    # Parse notes JSON
    def parse_notes(n):
        if not n:
            return {}
        try:
            return json.loads(n) if isinstance(n, str) else n
        except Exception:
            return {}
    df["_notes"] = df["notes"].apply(parse_notes)
    df["crowd_p_yes"] = df["_notes"].apply(lambda n: n.get("p_yes_market", None))
    df["llm_confidence"] = df["_notes"].apply(lambda n: n.get("llm", {}).get("confidence", None))
    df["rationale"] = df["_notes"].apply(lambda n: n.get("llm", {}).get("rationale", ""))
    df["ts_utc"] = pd.to_datetime(df["ts_utc"], utc=True)
    return df


@st.cache_data(ttl=15)
def load_diagnostics() -> dict:
    if not DIAG_PATH.exists():
        return {}
    try:
        return json.loads(DIAG_PATH.read_text())
    except Exception:
        return {}


@st.cache_data(ttl=15)
def load_last_log_lines(n: int = 8) -> list:
    if not LOG_PATH.exists():
        return []
    try:
        lines = LOG_PATH.read_text().splitlines()
        return lines[-n:]
    except Exception:
        return []


def kelly_bet(claude_p_win: float, crowd_price_of_winning_side: float,
              bankroll: float, fraction: float = 0.5) -> float:
    """Half-Kelly bet size."""
    b = (1 - crowd_price_of_winning_side) / max(crowd_price_of_winning_side, 0.001)
    p = claude_p_win
    q = 1 - p
    raw_kelly = (b * p - q) / max(b, 0.001)
    return max(0.0, raw_kelly * fraction * bankroll)


def payout_if_win(bet: float, crowd_price_of_losing_side: float, side: str) -> float:
    """Payout if trade wins (each share pays $1)."""
    share_price = (1 - crowd_price_of_losing_side) if side == "NO" else crowd_price_of_losing_side
    if share_price <= 0:
        return 0.0
    return bet / share_price


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
st.sidebar.image("https://em-content.zobj.net/source/twitter/376/high-voltage_26a1.png", width=48)
st.sidebar.title("SWARM")
st.sidebar.caption("BlackGlassLab · Paper Trading")

bankroll = st.sidebar.number_input("Bankroll ($)", min_value=100, max_value=1_000_000,
                                    value=10_000, step=500)
kelly_fraction = st.sidebar.select_slider(
    "Kelly Fraction",
    options=[0.25, 0.5, 0.75, 1.0],
    value=0.5,
    format_func=lambda x: f"{x:.0%} Kelly",
)
st.sidebar.divider()

# Loop status
log_lines = load_last_log_lines(3)
last_run = next((l for l in reversed(log_lines) if "infer loop" in l), None)
if last_run:
    st.sidebar.success(f"🟢 Loop alive\n{last_run.replace('== ', '').replace(' : infer loop ==', '')}")
else:
    st.sidebar.error("🔴 Loop not detected")

st.sidebar.divider()
if st.sidebar.button("🔄 Refresh Data"):
    st.cache_data.clear()
    st.rerun()

# ---------------------------------------------------------------------------
# Main tabs
# ---------------------------------------------------------------------------
tab1, tab2, tab3, tab4 = st.tabs(["📊 Portfolio", "🔍 Scanner", "📈 Charts", "🏆 Performance"])

# ===========================================================================
# TAB 1 — PORTFOLIO
# ===========================================================================
with tab1:
    st.header("Open Positions")
    df = load_trades()

    if df.empty:
        st.info("No real trades yet. The loop is scanning markets.")
    else:
        # Summary metrics
        total_trades = len(df)
        total_deployed = total_trades * 100.0
        total_payout = 0.0
        rows = []

        for _, row in df.iterrows():
            crowd = row.get("crowd_p_yes") or row["p_yes"]
            side = row["side"]
            bet = 100.0

            share_price = (1 - crowd) if side == "NO" else crowd
            if share_price <= 0:
                continue
            payout = bet / share_price
            profit = payout - bet
            roi = (profit / bet) * 100
            total_payout += payout

            k_bet = kelly_bet(
                claude_p_win=(1 - row["p_yes"]) if side == "NO" else row["p_yes"],
                crowd_price_of_winning_side=share_price,
                bankroll=bankroll,
                fraction=kelly_fraction,
            )

            rows.append({
                "Market": row["market_id"],
                "Side": side,
                "Claude %": f"{row['p_yes']*100:.1f}%",
                "Crowd %": f"{crowd*100:.1f}%",
                "Edge": f"{row['edge']*100:.1f}%",
                "Flat Bet": f"${bet:.0f}",
                "Payout": f"${payout:.0f}",
                "Profit": f"+${profit:.0f}",
                "ROI": f"{roi:.0f}%",
                f"Kelly Bet ({kelly_fraction:.0%})": f"${k_bet:.0f}",
                "Date": row["ts_utc"].strftime("%m/%d %H:%M"),
            })

        total_profit = total_payout - total_deployed

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Open Trades", total_trades)
        col2.metric("Deployed (flat $100)", f"${total_deployed:.0f}")
        col3.metric("Payout if ALL win", f"${total_payout:.0f}")
        col4.metric("Profit if ALL win", f"+${total_profit:.0f}", f"{total_profit/total_deployed*100:.0f}% ROI")

        st.divider()
        st.subheader("Positions")
        positions_df = pd.DataFrame(rows)
        st.dataframe(positions_df, use_container_width=True, hide_index=True)

        st.divider()
        st.subheader("📐 Kelly Bet Optimizer")
        st.caption(f"Bankroll: ${bankroll:,} · {kelly_fraction:.0%} Kelly · Recommended bet per trade based on edge + confidence")

        kelly_rows = []
        for _, row in df.iterrows():
            crowd = row.get("crowd_p_yes") or row["p_yes"]
            side = row["side"]
            share_price = (1 - crowd) if side == "NO" else crowd
            if share_price <= 0:
                continue
            claude_p_win = (1 - row["p_yes"]) if side == "NO" else row["p_yes"]
            full_k = kelly_bet(claude_p_win, share_price, bankroll, 1.0)
            half_k = kelly_bet(claude_p_win, share_price, bankroll, 0.5)
            qtr_k = kelly_bet(claude_p_win, share_price, bankroll, 0.25)
            chosen = kelly_bet(claude_p_win, share_price, bankroll, kelly_fraction)
            payout = payout_if_win(chosen, crowd, side)
            kelly_rows.append({
                "Market": row["market_id"][:40],
                "Side": side,
                "Edge": f"{row['edge']*100:.1f}%",
                "Full Kelly": f"${full_k:.0f}",
                "Half Kelly": f"${half_k:.0f}",
                "Qtr Kelly": f"${qtr_k:.0f}",
                f"Your Kelly ({kelly_fraction:.0%})": f"${chosen:.0f}",
                "Payout if Win": f"${payout:.0f}",
            })

        st.dataframe(pd.DataFrame(kelly_rows), use_container_width=True, hide_index=True)

        st.divider()
        st.subheader("🧠 Claude Rationales")
        for _, row in df.iterrows():
            edge_pct = row["edge"] * 100
            with st.expander(f"{row['side']} · {row['market_id']} · Edge {edge_pct:.1f}%"):
                col1, col2, col3 = st.columns(3)
                crowd = row.get("crowd_p_yes") or row["p_yes"]
                col1.metric("Claude p_yes", f"{row['p_yes']*100:.1f}%")
                col2.metric("Crowd p_yes", f"{crowd*100:.1f}%")
                col3.metric("Confidence", f"{(row.get('llm_confidence') or 0)*100:.0f}%")
                st.info(row["rationale"] or "No rationale recorded")
                st.caption(f"Placed: {row['ts_utc']}")


# ===========================================================================
# TAB 2 — SCANNER
# ===========================================================================
with tab2:
    st.header("Live Market Scanner")
    diag = load_diagnostics()

    if not diag:
        st.info("No diagnostics yet. Waiting for loop to run.")
    else:
        settings = diag.get("settings", {})
        summary = diag.get("summary", {})
        ts = diag.get("ts_utc", "")

        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("Last Run", ts[11:19] + " UTC" if ts else "—")
        col2.metric("Evaluated", summary.get("evaluated", 0))
        col3.metric("Passed", summary.get("passed", 0))
        col4.metric("Max Disagree", f"{settings.get('max_disagree', 0):.0%}")
        col5.metric("Min Edge", f"{settings.get('min_edge_abs', 0):.0%}")

        st.divider()

        rows = diag.get("rows", [])
        if rows:
            scanner_data = []
            for r in rows:
                decision = r.get("decision", "")
                scanner_data.append({
                    "Market": r.get("slug", r.get("market_id", ""))[:50],
                    "Decision": decision,
                    "Edge": r.get("edge_abs", 0),
                    "Disagree": r.get("disagreement", 0),
                    "Crowd %": f"{r.get('p_yes_market', 0)*100:.1f}%",
                    "Claude %": f"{r.get('p_yes_model', 0)*100:.1f}%",
                    "Side": r.get("side", ""),
                    "Reason": r.get("reason", ""),
                })

            scan_df = pd.DataFrame(scanner_data)

            # Color code by decision
            def color_decision(val):
                if val == "PASS":
                    return "background-color: #1a4a1a; color: #00ff88"
                elif val == "REJECT":
                    return "background-color: #3a1a1a; color: #ff6666"
                return ""

            st.dataframe(
                scan_df.style.applymap(color_decision, subset=["Decision"]),
                use_container_width=True,
                hide_index=True,
            )

            # Edge bar chart
            st.divider()
            st.subheader("Edge by Market")
            fig = px.bar(
                scan_df.sort_values("Edge", ascending=True),
                x="Edge", y="Market", orientation="h",
                color="Decision",
                color_discrete_map={"PASS": "#00cc66", "REJECT": "#cc3333"},
                title="Claude Edge vs Market Price",
            )
            fig.add_vline(x=settings.get("min_edge_abs", 0.04), line_dash="dash",
                         line_color="yellow", annotation_text="Min Edge Threshold")
            fig.update_layout(template="plotly_dark", height=400)
            st.plotly_chart(fig, use_container_width=True)

        # Rejection breakdown
        st.divider()
        st.subheader("Rejection Reasons")
        rejected = summary.get("rejected", {})
        if rejected:
            rej_df = pd.DataFrame([
                {"Reason": k, "Count": v}
                for k, v in rejected.items() if v > 0
            ])
            if not rej_df.empty:
                fig2 = px.pie(rej_df, names="Reason", values="Count",
                              title="Why Markets Were Rejected",
                              color_discrete_sequence=px.colors.qualitative.Set3)
                fig2.update_layout(template="plotly_dark")
                st.plotly_chart(fig2, use_container_width=True)
            else:
                st.success("No rejections this cycle — all markets passed!")

        # Raw log
        st.divider()
        st.subheader("Loop Log (last 8 lines)")
        log = load_last_log_lines(8)
        st.code("\n".join(log), language="bash")


# ===========================================================================
# TAB 3 — CHARTS
# ===========================================================================
with tab3:
    st.header("Charts & Analytics")
    df = load_trades()

    if df.empty or len(df) < 2:
        st.info("Need at least 2 trades to show charts. Check back soon.")
    else:
        col1, col2 = st.columns(2)

        # Edge distribution
        with col1:
            st.subheader("Edge Distribution")
            fig = px.histogram(
                df, x="edge", nbins=20,
                title="Claude Edge Size Across Trades",
                labels={"edge": "Edge (%)"},
                color_discrete_sequence=["#00cc88"],
            )
            fig.update_xaxes(tickformat=".0%")
            fig.add_vline(x=0.04, line_dash="dash", line_color="yellow",
                         annotation_text="4% threshold")
            fig.update_layout(template="plotly_dark")
            st.plotly_chart(fig, use_container_width=True)

        # Claude vs Crowd
        with col2:
            st.subheader("Claude vs Crowd Price")
            df_chart = df.copy()
            df_chart["crowd"] = df_chart["crowd_p_yes"].fillna(df_chart["p_yes"])
            fig2 = go.Figure()
            fig2.add_trace(go.Bar(
                name="Claude p_yes",
                x=df_chart["market_id"].str[:25],
                y=df_chart["p_yes"],
                marker_color="#4488ff",
            ))
            fig2.add_trace(go.Bar(
                name="Crowd p_yes",
                x=df_chart["market_id"].str[:25],
                y=df_chart["crowd"],
                marker_color="#ff8844",
            ))
            fig2.update_layout(
                barmode="group",
                template="plotly_dark",
                title="Where Claude Disagrees With the Crowd",
                xaxis_tickangle=-30,
            )
            st.plotly_chart(fig2, use_container_width=True)

        st.divider()

        # Kelly bet sizes
        st.subheader(f"Kelly Bet Sizing at ${bankroll:,} Bankroll")
        kelly_chart_rows = []
        for _, row in df.iterrows():
            crowd = row.get("crowd_p_yes") or row["p_yes"]
            side = row["side"]
            share_price = (1 - crowd) if side == "NO" else crowd
            if share_price <= 0:
                continue
            claude_p_win = (1 - row["p_yes"]) if side == "NO" else row["p_yes"]
            for frac, label in [(0.25, "Qtr Kelly"), (0.5, "Half Kelly"), (1.0, "Full Kelly")]:
                k = kelly_bet(claude_p_win, share_price, bankroll, frac)
                kelly_chart_rows.append({
                    "Market": row["market_id"][:30],
                    "Kelly Fraction": label,
                    "Bet Size": k,
                })

        kelly_df = pd.DataFrame(kelly_chart_rows)
        fig3 = px.bar(
            kelly_df, x="Market", y="Bet Size", color="Kelly Fraction",
            barmode="group",
            title=f"Recommended Bet Size by Kelly Fraction (${bankroll:,} bankroll)",
            color_discrete_map={
                "Qtr Kelly": "#4466aa",
                "Half Kelly": "#44aaff",
                "Full Kelly": "#00ddff",
            },
        )
        fig3.update_layout(template="plotly_dark", xaxis_tickangle=-30)
        st.plotly_chart(fig3, use_container_width=True)

        st.divider()

        # Payout waterfall
        st.subheader("Payout Potential per Trade ($100 flat bet)")
        payout_rows = []
        for _, row in df.iterrows():
            crowd = row.get("crowd_p_yes") or row["p_yes"]
            side = row["side"]
            share_price = (1 - crowd) if side == "NO" else crowd
            if share_price <= 0:
                continue
            payout = 100.0 / share_price
            payout_rows.append({
                "Market": row["market_id"][:35],
                "Payout": payout,
                "Profit": payout - 100,
                "ROI %": (payout - 100),
            })

        pay_df = pd.DataFrame(payout_rows).sort_values("Payout", ascending=True)
        fig4 = px.bar(
            pay_df, x="Payout", y="Market", orientation="h",
            title="Payout if Each Trade Wins ($100 bet)",
            color="ROI %",
            color_continuous_scale="Greens",
        )
        fig4.add_vline(x=100, line_dash="dash", line_color="white",
                      annotation_text="Break even")
        fig4.update_layout(template="plotly_dark", height=350)
        st.plotly_chart(fig4, use_container_width=True)


# ===========================================================================
# TAB 4 — PERFORMANCE
# ===========================================================================
with tab4:
    st.header("Performance Tracker")
    df = load_trades()

    resolved = df[df["status"] == "RESOLVED"] if not df.empty else pd.DataFrame()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Trades", len(df) if not df.empty else 0)
    col2.metric("Resolved", len(resolved))
    col3.metric("Pending", len(df) - len(resolved) if not df.empty else 0)

    if not resolved.empty:
        wins = resolved[resolved["resolved_outcome"] == resolved["side"]]
        win_rate = len(wins) / len(resolved) * 100
        col4.metric("Win Rate", f"{win_rate:.0f}%")

        st.divider()
        st.subheader("Resolved Trades")
        st.dataframe(resolved[["market_id", "side", "p_yes", "edge",
                                "resolved_outcome", "ts_utc"]], use_container_width=True)

        # Calibration chart
        st.divider()
        st.subheader("Calibration Chart")
        st.caption("Perfect calibration = points along the diagonal")
        cal_rows = []
        for _, row in resolved.iterrows():
            outcome = 1.0 if row["resolved_outcome"] == "YES" else 0.0
            cal_rows.append({"Claude p_yes": row["p_yes"], "Actual Outcome": outcome})
        cal_df = pd.DataFrame(cal_rows)
        fig5 = px.scatter(
            cal_df, x="Claude p_yes", y="Actual Outcome",
            title="Claude Calibration: Predicted vs Actual",
            trendline="ols",
        )
        fig5.add_shape(type="line", x0=0, y0=0, x1=1, y1=1,
                      line=dict(color="yellow", dash="dash"))
        fig5.update_layout(template="plotly_dark")
        st.plotly_chart(fig5, use_container_width=True)

    else:
        col4.metric("Win Rate", "—")
        st.divider()
        st.info("No resolved trades yet. Performance stats will appear here once markets settle.")

        st.subheader("What to expect here once trades resolve:")
        st.markdown("""
        - **Win Rate** — % of Claude's trades that were correct
        - **Brier Score** — Calibration quality (lower = better, 0 = perfect)
        - **ROI by Category** — Are crypto trades better than sports?
        - **Edge vs Outcome** — Do higher-edge trades win more often?
        - **P&L Curve** — Cumulative profit over time
        - **Calibration Chart** — Is Claude over or under-confident?
        """)

        st.subheader("Your open trades resolve on:")
        if not df.empty:
            for _, row in df.iterrows():
                market = row["market_id"]
                if "june-30" in market or "2026-06-30" in market:
                    st.write(f"• `{market}` → **June 30, 2026**")
                elif "ncaa" in market:
                    st.write(f"• `{market}` → **Early April 2026** (tournament ends)")
                elif "masters" in market:
                    st.write(f"• `{market}` → **April 13, 2026** (Masters ends)")
                elif "2026" in market:
                    st.write(f"• `{market}` → **End of 2026**")
                else:
                    st.write(f"• `{market}`")
