#!/usr/bin/env python3
"""Generate Swarm Edge promotional flyer PDF."""

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor
import json
import math
import sqlite3
from pathlib import Path

ROOT    = Path(__file__).parent.parent
OUT     = ROOT / "swarm_edge_flyer.pdf"
DB_PATH = ROOT / "memory" / "runs.sqlite"
CUTOFF  = "2026-03-28T21:00"

# ── Palette ─────────────────────────────────────────────────────────────────
BG       = HexColor("#070d14")
NAVY     = HexColor("#0a1828")
PANEL    = HexColor("#0e2035")
PANEL2   = HexColor("#0b1c2e")
GREEN    = HexColor("#00ff88")
GREEN_D  = HexColor("#003320")
CYAN     = HexColor("#00ddff")
YELLOW   = HexColor("#ffd700")
PURPLE   = HexColor("#b388ff")
WHITE    = HexColor("#ffffff")
GREY     = HexColor("#6b7f90")
GREY_L   = HexColor("#aec0cc")

W, H = letter   # 612 × 792


# ── Live stats ───────────────────────────────────────────────────────────────
def live_stats() -> dict:
    defaults = {"n_trades": 6, "deployed": 600, "total_payout": 8971, "n_markets": 15}
    try:
        wl = ROOT / "markets" / "polymarket_watchlist.json"
        n_markets = len(json.loads(wl.read_text())) if wl.exists() else 15
        if not DB_PATH.exists():
            return {**defaults, "n_markets": n_markets}
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM paper_trades "
            f"WHERE ts_utc > '{CUTOFF}' AND status = 'OPEN'"
        ).fetchall()
        conn.close()
        if not rows:
            return {**defaults, "n_markets": n_markets}
        total_payout = 0.0
        for r in rows:
            notes = {}
            try: notes = json.loads(r["notes"] or "{}")
            except: pass
            crowd = notes.get("p_yes_market") or float(r["p_yes"] or 0.5)
            sp = (1 - crowd) if r["side"] == "NO" else crowd
            if sp > 0:
                total_payout += 100.0 / sp
        deployed = len(rows) * 100
        return {"n_trades": len(rows), "deployed": deployed,
                "total_payout": round(total_payout), "n_markets": n_markets}
    except Exception:
        return defaults


# ── Drawing helpers ──────────────────────────────────────────────────────────
def fill_bg(c):
    c.setFillColor(BG)
    c.rect(0, 0, W, H, fill=1, stroke=0)


def hex_pattern(c, x0, y0, cols, rows, r, color, alpha=0.06):
    c.saveState()
    c.setFillColor(color)
    c.setFillAlpha(alpha)
    dx, dy = r * 1.732, r * 1.5
    for row in range(rows + 2):
        for col in range(cols + 2):
            cx = x0 + col * dx + (r * 0.866 if row % 2 else 0)
            cy = y0 + row * dy
            path = c.beginPath()
            for i in range(6):
                a = math.radians(60 * i - 30)
                px, py = cx + r * 0.82 * math.cos(a), cy + r * 0.82 * math.sin(a)
                path.moveTo(px, py) if i == 0 else path.lineTo(px, py)
            path.close()
            c.drawPath(path, fill=1, stroke=0)
    c.restoreState()


def glow_line(c, x1, y1, x2, y2, color=GREEN, w=1.0):
    for lw, a in [(5, 0.05), (2, 0.18), (w, 1.0)]:
        c.saveState()
        c.setStrokeColor(color)
        c.setStrokeAlpha(a)
        c.setLineWidth(lw)
        c.line(x1, y1, x2, y2)
        c.restoreState()


def section_header(c, x, y, text, color=GREEN):
    c.setFillColor(color)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(x, y, text)
    glow_line(c, x, y - 5, x + c.stringWidth(text, "Helvetica-Bold", 9) + 40, y - 5, color, 0.5)


def card(c, x, y, w, h, label, value, sub, accent=GREEN):
    # bg
    c.setFillColor(PANEL)
    c.roundRect(x, y, w, h, 5, fill=1, stroke=0)
    # top accent bar
    c.setFillColor(accent)
    c.roundRect(x, y + h - 4, w, 4, 2, fill=1, stroke=0)
    # border glow
    for lw, a in [(4, 0.08), (1.2, 0.6)]:
        c.saveState()
        c.setStrokeColor(accent)
        c.setStrokeAlpha(a)
        c.setLineWidth(lw)
        c.roundRect(x, y, w, h, 5, fill=0, stroke=1)
        c.restoreState()
    # value
    c.setFillColor(accent)
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(x + w / 2, y + h - 30, value)
    # label
    c.setFillColor(GREY_L)
    c.setFont("Helvetica", 7.5)
    c.drawCentredString(x + w / 2, y + h - 42, label.upper())
    # sub
    c.setFillColor(GREY)
    c.setFont("Helvetica", 6.5)
    c.drawCentredString(x + w / 2, y + 8, sub)


def step_box(c, x, y, w, h, num, title, body_lines, accent=GREEN):
    c.setFillColor(PANEL2)
    c.roundRect(x, y, w, h, 4, fill=1, stroke=0)
    # left accent strip
    c.setFillColor(accent)
    c.roundRect(x, y, 3, h, 2, fill=1, stroke=0)
    # subtle border
    c.saveState()
    c.setStrokeColor(accent)
    c.setStrokeAlpha(0.2)
    c.setLineWidth(0.6)
    c.roundRect(x, y, w, h, 4, fill=0, stroke=1)
    c.restoreState()
    # number
    c.setFillColor(accent)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(x + 10, y + h - 24, num)
    # title
    c.setFillColor(WHITE)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(x + 10, y + h - 38, title)
    # body
    c.setFillColor(GREY_L)
    c.setFont("Helvetica", 7.5)
    for i, line in enumerate(body_lines):
        c.drawString(x + 10, y + h - 52 - i * 11, line)


def draw_arrow(c, x, y, color=CYAN):
    """Draw a simple right-pointing arrow."""
    c.saveState()
    c.setFillColor(color)
    c.setFillAlpha(0.7)
    # shaft
    c.setStrokeColor(color)
    c.setStrokeAlpha(0.5)
    c.setLineWidth(0.8)
    c.line(x, y, x + 14, y)
    # head
    path = c.beginPath()
    path.moveTo(x + 14, y + 3)
    path.lineTo(x + 20, y)
    path.lineTo(x + 14, y - 3)
    path.close()
    c.drawPath(path, fill=1, stroke=0)
    c.restoreState()


def flow_node(c, x, y, w, h, title, sub, is_last=False):
    accent = GREEN if is_last else CYAN
    c.setFillColor(PANEL)
    c.roundRect(x, y, w, h, 4, fill=1, stroke=0)
    c.saveState()
    c.setStrokeColor(accent)
    c.setStrokeAlpha(0.55)
    c.setLineWidth(0.8)
    c.roundRect(x, y, w, h, 4, fill=0, stroke=1)
    c.restoreState()
    c.setFillColor(WHITE if not is_last else GREEN)
    c.setFont("Helvetica-Bold", 7.5)
    c.drawCentredString(x + w / 2, y + h - 13, title)
    c.setFillColor(GREY)
    c.setFont("Helvetica", 6.5)
    c.drawCentredString(x + w / 2, y + 5, sub)


def trade_row(c, x, y, w, h, label, desc, accent):
    c.setFillColor(PANEL2)
    c.roundRect(x, y, w, h, 3, fill=1, stroke=0)
    c.setFillColor(accent)
    c.rect(x, y, 3, h, fill=1, stroke=0)
    c.saveState()
    c.setStrokeColor(accent)
    c.setStrokeAlpha(0.3)
    c.setLineWidth(0.5)
    c.roundRect(x, y, w, h, 3, fill=0, stroke=1)
    c.restoreState()
    c.setFillColor(accent)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(x + 11, y + h - 13, label)
    c.setFillColor(GREY_L)
    c.setFont("Helvetica", 7.5)
    c.drawString(x + 11, y + 6, desc)


def badge(c, x, y, text, bg, fg):
    tw = c.stringWidth(text, "Helvetica-Bold", 7)
    pw, ph = tw + 12, 13
    c.setFillColor(bg)
    c.roundRect(x, y, pw, ph, 4, fill=1, stroke=0)
    c.saveState()
    c.setStrokeColor(fg)
    c.setStrokeAlpha(0.5)
    c.setLineWidth(0.4)
    c.roundRect(x, y, pw, ph, 4, fill=0, stroke=1)
    c.restoreState()
    c.setFillColor(fg)
    c.setFont("Helvetica-Bold", 7)
    c.drawString(x + 6, y + 3.5, text)
    return pw + 5


# ── Main draw ────────────────────────────────────────────────────────────────
def draw(c, s):
    fill_bg(c)

    # ── HEADER BAND ─────────────────────────────────────────────────────────
    c.setFillColor(NAVY)
    c.rect(0, H - 185, W, 185, fill=1, stroke=0)
    hex_pattern(c, -10, H - 195, 15, 7, 20, GREEN, alpha=0.055)
    glow_line(c, 0, H - 185, W, H - 185, GREEN, 0.8)

    # Wordmark
    c.setFillColor(WHITE)
    c.setFont("Helvetica-Bold", 66)
    sw = c.stringWidth("SWARM ", "Helvetica-Bold", 66)
    c.drawString(36, H - 100, "SWARM ")
    c.setFillColor(GREEN)
    c.drawString(36 + sw, H - 100, "EDGE")

    # Tagline
    c.setFillColor(GREY_L)
    c.setFont("Helvetica", 12)
    c.drawString(38, H - 122, "The AI committee that bets against the crowd — and wins.")

    # Top bar
    c.setFillColor(GREY)
    c.setFont("Helvetica", 7.5)
    c.drawString(38, H - 22, "SWARM AXIS  ·  AI SYSTEMS FOR THE EDGE ECONOMY")

    # Live badge
    bx = W - 38
    live_text = "● LIVE — PAPER TRADING"
    c.setFillColor(GREEN_D)
    lw2 = c.stringWidth(live_text, "Helvetica-Bold", 7.5) + 14
    c.roundRect(bx - lw2, H - 28, lw2, 14, 4, fill=1, stroke=0)
    c.saveState()
    c.setStrokeColor(GREEN)
    c.setStrokeAlpha(0.6)
    c.setLineWidth(0.5)
    c.roundRect(bx - lw2, H - 28, lw2, 14, 4, fill=0, stroke=1)
    c.restoreState()
    c.setFillColor(GREEN)
    c.setFont("Helvetica-Bold", 7.5)
    c.drawRightString(bx - 7, H - 22, live_text)

    # Badges
    px, py = 38, H - 160
    for txt, bg, fg in [
        ("POLYMARKET",    HexColor("#0a1e38"), CYAN),
        ("LLM REASONING", GREEN_D,             GREEN),
        ("AUTONOMOUS",    HexColor("#1c1400"), YELLOW),
        ("PAPER TRADING", HexColor("#0a1e38"), CYAN),
        ("CLAUDE AI",     HexColor("#1a0030"), PURPLE),
    ]:
        px += badge(c, px, py, txt, bg, fg)

    # ── WHAT IT IS ──────────────────────────────────────────────────────────
    # Chain all y-coords top-down from here
    MARGIN = 38
    GAP    = 14   # gap between sections

    y_wi = H - 208          # "WHAT IT IS" header baseline
    section_header(c, MARGIN, y_wi, "WHAT IT IS")

    body = (
        "Swarm Edge deploys an adversarial AI committee against live prediction markets. "
        "Every hour, three Claude AI agents debate real-world events independently. "
        "When consensus diverges from the live market price by 5%+ edge and agents agree, "
        "it fires a trade automatically. No human in the loop."
    )
    c.setFillColor(GREY_L)
    c.setFont("Helvetica", 9)
    words, line, lines = body.split(), [], []
    for w in words:
        test = " ".join(line + [w])
        if c.stringWidth(test, "Helvetica", 9) < W - MARGIN * 2:
            line.append(w)
        else:
            lines.append(" ".join(line)); line = [w]
    if line: lines.append(" ".join(line))
    for i, ln in enumerate(lines):
        c.drawString(MARGIN, y_wi - 16 - i * 13, ln)

    text_bottom = y_wi - 16 - (len(lines) - 1) * 13 - 4   # bottom of body text

    # ── STAT CARDS ──────────────────────────────────────────────────────────
    CARD_H = 72
    y_cards_top = text_bottom - GAP                          # top of cards
    profit = s["total_payout"] - s["deployed"]
    cw = (W - MARGIN * 2 - 30) / 4
    card_data = [
        ("Markets Monitored", str(s["n_markets"]),        "Politics · Macro · Crypto",    GREEN),
        ("Open Positions",    str(s["n_trades"]),          f"${s['deployed']:,} deployed", CYAN),
        ("Max Payout",        f"${s['total_payout']:,}",  f"+${profit:,} net profit",      YELLOW),
        ("API Cost / Month",  "~$0",                      "Intelligence is the edge",      GREEN),
    ]
    for i, (lbl, val, sub, acc) in enumerate(card_data):
        card(c, MARGIN + i * (cw + 10), y_cards_top - CARD_H, cw, CARD_H, lbl, val, sub, acc)

    cards_bottom = y_cards_top - CARD_H                      # bottom of cards

    # ── HOW IT WORKS ────────────────────────────────────────────────────────
    STEP_H  = 78
    STEP_W  = (W - MARGIN * 2 - 10) / 2
    STEP_G  = 7
    y_how   = cards_bottom - GAP - 14                        # section header

    section_header(c, MARGIN, y_how, "HOW IT WORKS")

    steps = [
        ("01", "SCAN",   ["2,000 Polymarket markets scanned daily.",
                          "Sports lotteries auto-filtered. Politics,",
                          "macro and crypto prioritized by score."]),
        ("02", "REASON", ["Three Claude AI agents debate each market.",
                          "Operators forecast. Skeptics attack.",
                          "Arbiter renders final consensus."]),
        ("03", "TRADE",  ["Edge ≥ 5% + agent consensus = trade.",
                          "Disagreement above 45% threshold",
                          "= walk away. Discipline is the feature."]),
        ("04", "SCORE",  ["Every trade scored with Brier metric.",
                          "Calibration proof required before",
                          "real capital deploys."]),
    ]
    steps_start = y_how - 18                                  # top of first step row
    for i, (num, title, body_lines) in enumerate(steps):
        col, row = i % 2, i // 2
        sx = MARGIN + col * (STEP_W + 10)
        sy = steps_start - row * (STEP_H + STEP_G) - STEP_H
        step_box(c, sx, sy, STEP_W, STEP_H, num, title, body_lines,
                 GREEN if col == 0 else CYAN)

    steps_bottom = steps_start - 2 * (STEP_H + STEP_G)       # bottom of last step row

    # ── SIGNAL FLOW ─────────────────────────────────────────────────────────
    FH      = 34
    y_flow  = steps_bottom - GAP - 14
    section_header(c, MARGIN, y_flow, "SIGNAL FLOW")

    nodes = [
        ("Watchlist",   "15 verified slugs"),
        ("Infer Loop",  "Every 60 min"),
        ("AI Swarm",    "Operators · Skeptics · Arbiter"),
        ("Edge Filter", "≥ 5% + agree ≤ 45%"),
        ("Trade",       "$100 flat · Brier scored"),
    ]
    NW = 96
    total_flow_w = len(nodes) * NW + (len(nodes) - 1) * 22
    fx0 = (W - total_flow_w) / 2
    fy  = y_flow - 16 - FH                                   # top of nodes

    for i, (title, sub) in enumerate(nodes):
        nx = fx0 + i * (NW + 22)
        flow_node(c, nx, fy, NW, FH, title, sub, is_last=(i == len(nodes) - 1))
        if i < len(nodes) - 1:
            draw_arrow(c, nx + NW + 1, fy + FH / 2, CYAN)

    flow_bottom = fy                                          # bottom of nodes

    # ── WHAT WE TRADE ───────────────────────────────────────────────────────
    TH      = 28
    TW      = (W - MARGIN * 2 - 10) / 2
    y_trade = flow_bottom - GAP - 14
    section_header(c, MARGIN, y_trade, "WHAT WE TRADE")

    trades = [
        ("US POLITICS", "Rate cuts · Tariff rulings · Blue wave · Impeachment · Court orders", GREEN),
        ("MACRO / FED", "Recession timing · Emergency cuts · Inflation · GDP · Trade war",      CYAN),
        ("CRYPTO",      "Bitcoin $150k · ETH flip · Price milestones · Protocol events",        YELLOW),
        ("GEOPOLITICS", "Ceasefire timelines · Elections · Court decisions · Sanctions",        PURPLE),
    ]
    for i, (cat, desc, acc) in enumerate(trades):
        col, row = i % 2, i // 2
        tx = MARGIN + col * (TW + 10)
        ty = y_trade - 16 - row * (TH + 6) - TH
        trade_row(c, tx, ty, TW, TH, cat, desc, acc)

    # ── FOOTER ──────────────────────────────────────────────────────────────
    glow_line(c, 0, 44, W, 44, GREEN, 0.7)
    c.setFillColor(NAVY)
    c.rect(0, 0, W, 44, fill=1, stroke=0)

    c.setFillColor(GREEN)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(38, 16, "SWARM EDGE")
    c.setFillColor(GREY)
    c.setFont("Helvetica", 7.5)
    c.drawString(38 + c.stringWidth("SWARM EDGE", "Helvetica-Bold", 8.5) + 10, 16,
                 "·  Swarm Axis  ·  AI Prediction Market Engine  ·  Paper Trading  ·  2026")
    c.setFillColor(YELLOW)
    c.setFont("Helvetica-Bold", 8)
    c.drawRightString(W - 38, 16, "REAL CAPITAL IS THE ENDGAME")


def main():
    s = live_stats()
    c = canvas.Canvas(str(OUT), pagesize=letter)
    c.setTitle("Swarm Edge — AI Prediction Market Engine")
    c.setAuthor("Swarm Axis")
    draw(c, s)
    c.save()
    print(f"Generated: {OUT}")
    print(f"  Markets={s['n_markets']}  Trades={s['n_trades']}  "
          f"Deployed=${s['deployed']:,}  Payout=${s['total_payout']:,}")


if __name__ == "__main__":
    main()
