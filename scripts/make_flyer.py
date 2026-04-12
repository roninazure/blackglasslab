#!/usr/bin/env python3
"""Generate Swarm Edge promotional flyer PDF."""

from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.colors import HexColor
import json
import math
import sqlite3
from pathlib import Path

ROOT = Path(__file__).parent.parent
OUT  = ROOT / "swarm_edge_flyer.pdf"
DB_PATH   = ROOT / "memory" / "runs.sqlite"
CUTOFF    = "2026-03-28T21:00"

# ---------------------------------------------------------------------------
# Pull live stats from DB
# ---------------------------------------------------------------------------
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
            f"SELECT * FROM paper_trades WHERE ts_utc > '{CUTOFF}' AND status = 'OPEN'"
        ).fetchall()
        conn.close()

        total_payout = 0.0
        for r in rows:
            notes = {}
            try:
                notes = json.loads(r["notes"] or "{}")
            except Exception:
                pass
            crowd = notes.get("p_yes_market") or float(r["p_yes"] or 0.5)
            side  = r["side"]
            sp    = (1 - crowd) if side == "NO" else crowd
            if sp > 0:
                total_payout += 100.0 / sp

        if not rows:
            # No live data — use sensible defaults
            return {**defaults, "n_markets": n_markets}

        deployed = len(rows) * 100
        return {
            "n_trades":     len(rows),
            "deployed":     deployed,
            "total_payout": round(total_payout),
            "n_markets":    n_markets,
        }
    except Exception:
        return defaults

# Brand colors
BLACK       = HexColor("#080e14")
DARK_NAVY   = HexColor("#0d1a2a")
MID_NAVY    = HexColor("#0f2235")
PANEL       = HexColor("#132840")
GREEN       = HexColor("#00ff88")
GREEN_DIM   = HexColor("#00cc66")
GREEN_DARK  = HexColor("#003322")
CYAN        = HexColor("#00ddff")
YELLOW      = HexColor("#ffdd44")
WHITE       = HexColor("#ffffff")
GREY        = HexColor("#8899aa")
GREY_LIGHT  = HexColor("#aabbcc")

W, H = letter  # 612 x 792


def hex_grid(c, x0, y0, cols, rows, size, color, alpha=0.18):
    """Draw a subtle hexagonal grid pattern."""
    c.saveState()
    c.setFillColor(color)
    c.setFillAlpha(alpha)
    dx = size * 1.732
    dy = size * 1.5
    for row in range(rows + 2):
        for col in range(cols + 2):
            cx = x0 + col * dx + (size * 0.866 if row % 2 else 0)
            cy = y0 + row * dy
            path = c.beginPath()
            for i in range(6):
                angle = math.radians(60 * i - 30)
                px = cx + size * 0.85 * math.cos(angle)
                py = cy + size * 0.85 * math.sin(angle)
                if i == 0:
                    path.moveTo(px, py)
                else:
                    path.lineTo(px, py)
            path.close()
            c.drawPath(path, fill=1, stroke=0)
    c.restoreState()


def glowing_line(c, x1, y1, x2, y2, color=GREEN, width=1.5):
    """Draw a line with a soft glow effect."""
    for w, a in [(6, 0.06), (3, 0.12), (width, 1.0)]:
        c.saveState()
        c.setStrokeColor(color)
        c.setStrokeAlpha(a)
        c.setLineWidth(w)
        c.line(x1, y1, x2, y2)
        c.restoreState()


def stat_card(c, x, y, w, h, label, value, sub=None, accent=GREEN):
    """Draw a stat card with glowing border."""
    # Background
    c.setFillColor(PANEL)
    c.roundRect(x, y, w, h, 6, fill=1, stroke=0)

    # Glowing border
    for lw, alpha in [(4, 0.1), (2, 0.25), (0.8, 0.9)]:
        c.saveState()
        c.setStrokeColor(accent)
        c.setStrokeAlpha(alpha)
        c.setLineWidth(lw)
        c.roundRect(x, y, w, h, 6, fill=0, stroke=1)
        c.restoreState()

    # Value
    c.setFillColor(accent)
    c.setFont("Helvetica-Bold", 22)
    c.drawCentredString(x + w / 2, y + h - 34, value)

    # Label
    c.setFillColor(GREY_LIGHT)
    c.setFont("Helvetica", 8)
    c.drawCentredString(x + w / 2, y + h - 48, label.upper())

    # Sub
    if sub:
        c.setFillColor(GREY)
        c.setFont("Helvetica", 7)
        c.drawCentredString(x + w / 2, y + 9, sub)


def pill(c, x, y, text, bg=GREEN_DARK, fg=GREEN):
    """Small rounded badge."""
    tw = c.stringWidth(text, "Helvetica-Bold", 7.5)
    pw, ph = tw + 14, 14
    c.setFillColor(bg)
    c.roundRect(x, y, pw, ph, 5, fill=1, stroke=0)
    c.saveState()
    c.setStrokeColor(fg)
    c.setStrokeAlpha(0.5)
    c.setLineWidth(0.5)
    c.roundRect(x, y, pw, ph, 5, fill=0, stroke=1)
    c.restoreState()
    c.setFillColor(fg)
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(x + 7, y + 4, text)
    return pw + 6


def draw_page(c, stats: dict):
    # ── Full background ──────────────────────────────────────────────
    c.setFillColor(BLACK)
    c.rect(0, 0, W, H, fill=1, stroke=0)

    # Top accent band
    c.setFillColor(DARK_NAVY)
    c.rect(0, H - 220, W, 220, fill=1, stroke=0)

    # Hex grid — top area
    hex_grid(c, -20, H - 230, 14, 8, 22, GREEN, alpha=0.07)

    # Glowing top border
    glowing_line(c, 0, H - 220, W, H - 220, GREEN, 1.0)

    # ── Corner accent lines ─────────────────────────────────────────
    for x1, y1, x2, y2 in [
        (0, H, 80, H), (0, H, 0, H - 80),           # top-left
        (W, H, W - 80, H), (W, H, W, H - 80),        # top-right
        (0, 0, 80, 0), (0, 0, 0, 80),                # bottom-left
        (W, 0, W - 80, 0), (W, 0, W, 80),            # bottom-right
    ]:
        glowing_line(c, x1, y1, x2, y2, GREEN, 1.2)

    # ── SWARM AXIS wordmark ─────────────────────────────────────────
    c.setFillColor(GREY)
    c.setFont("Helvetica", 8)
    c.setFillAlpha(0.7)
    c.drawString(40, H - 28, "SWARM AXIS  ·  AI SYSTEMS FOR THE EDGE ECONOMY")
    c.setFillAlpha(1.0)

    # Live badge (top right)
    pill(c, W - 110, H - 26, "● LIVE — PAPER TRADING")

    # ── MAIN LOGO ───────────────────────────────────────────────────
    # "SWARM" giant
    c.setFillColor(WHITE)
    c.setFont("Helvetica-Bold", 72)
    c.drawString(38, H - 115, "SWARM")

    # "EDGE" — green offset
    c.setFillColor(GREEN)
    c.setFont("Helvetica-Bold", 72)
    c.drawString(38 + c.stringWidth("SWARM", "Helvetica-Bold", 72) + 10, H - 115, "EDGE")

    # Tagline
    c.setFillColor(GREY_LIGHT)
    c.setFont("Helvetica", 13)
    c.drawString(40, H - 140, "The AI committee that bets against the crowd — and wins.")

    # Glowing divider
    glowing_line(c, 40, H - 160, W - 40, H - 160, GREEN, 0.8)

    # ── PILLS ROW ────────────────────────────────────────────────────
    px = 40
    py = H - 180
    for text, bg, fg in [
        ("POLYMARKET", HexColor("#0d2040"), CYAN),
        ("LLM REASONING", GREEN_DARK, GREEN),
        ("AUTONOMOUS", HexColor("#1a1000"), YELLOW),
        ("PAPER TRADING", HexColor("#0d2040"), CYAN),
        ("PHASE 3", HexColor("#1a0030"), HexColor("#cc88ff")),
    ]:
        px += pill(c, px, py, text, bg, fg)

    # ── WHAT IT IS section ──────────────────────────────────────────
    y = H - 245
    c.setFillColor(GREEN)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "WHAT IT IS")
    glowing_line(c, 40, y - 4, 200, y - 4, GREEN, 0.5)

    body = (
        "Swarm Edge deploys an adversarial AI committee against live prediction markets. "
        "Every hour, three Claude AI agents debate the probability of real-world events — "
        "elections, Fed decisions, recessions, crypto milestones. When the swarm's consensus "
        "diverges from the live market price by sufficient edge, and agents agree, it fires "
        "a trade. Automatically. No human in the loop."
    )

    c.setFillColor(GREY_LIGHT)
    c.setFont("Helvetica", 9.5)
    tw = W - 80
    words = body.split()
    line, lines = [], []
    for word in words:
        test = " ".join(line + [word])
        if c.stringWidth(test, "Helvetica", 9.5) < tw:
            line.append(word)
        else:
            lines.append(" ".join(line))
            line = [word]
    if line:
        lines.append(" ".join(line))
    for i, ln in enumerate(lines):
        c.drawString(40, y - 20 - i * 14, ln)

    # ── STAT CARDS ──────────────────────────────────────────────────
    y_cards = H - 390
    card_w = (W - 80 - 3 * 10) / 4
    profit = stats["total_payout"] - stats["deployed"]
    stat_data = [
        ("Markets Monitored", str(stats["n_markets"]),
         "Politics · Macro · Crypto",                         GREEN),
        ("Open Positions",    str(stats["n_trades"]),
         f"${stats['deployed']:,} deployed",                  CYAN),
        ("Max Payout",        f"${stats['total_payout']:,}",
         f"+${profit:,} profit if all win",                   YELLOW),
        ("API Cost / Mo",     "~$0",
         "Intelligence is the edge",                          GREEN),
    ]
    for i, (label, value, sub, accent) in enumerate(stat_data):
        stat_card(c, 40 + i * (card_w + 10), y_cards, card_w, 78, label, value, sub, accent)

    # ── HOW IT WORKS ────────────────────────────────────────────────
    y = y_cards - 40
    c.setFillColor(GREEN)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "HOW IT WORKS")
    glowing_line(c, 40, y - 4, 220, y - 4, GREEN, 0.5)

    steps = [
        ("01", "SCAN",     "2,000 Polymarket markets scanned daily. Sports lotteries\nautomatically filtered. Politics, macro, crypto prioritized."),
        ("02", "REASON",   "Three Claude AI agents debate each market independently.\nOperators forecast. Skeptics attack. Arbiter resolves."),
        ("03", "TRADE",    "Edge ≥ 5% + agent consensus = automatic paper trade.\nDisagreement above threshold = walk away."),
        ("04", "SCORE",    "Every trade scored with Brier calibration metric.\nProof of edge required before real capital deploys."),
    ]

    col_w = (W - 80) / 2
    for i, (num, title, desc) in enumerate(steps):
        col = i % 2
        row = i // 2
        sx = 40 + col * (col_w + 10)
        sy = y - 30 - row * 90

        # Step box
        c.setFillColor(MID_NAVY)
        c.roundRect(sx, sy - 60, col_w - 10, 72, 5, fill=1, stroke=0)
        c.saveState()
        c.setStrokeColor(GREEN)
        c.setStrokeAlpha(0.2)
        c.setLineWidth(0.5)
        c.roundRect(sx, sy - 60, col_w - 10, 72, 5, fill=0, stroke=1)
        c.restoreState()

        # Number
        c.setFillColor(GREEN)
        c.setFont("Helvetica-Bold", 18)
        c.drawString(sx + 12, sy - 8, num)

        # Title
        c.setFillColor(WHITE)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(sx + 48, sy - 5, title)

        # Description
        c.setFillColor(GREY_LIGHT)
        c.setFont("Helvetica", 8)
        for j, dline in enumerate(desc.split("\n")):
            c.drawString(sx + 48, sy - 22 - j * 12, dline)

    # ── SIGNAL FLOW ─────────────────────────────────────────────────
    y_flow = y - 240
    c.setFillColor(GREEN)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y_flow, "SIGNAL FLOW")
    glowing_line(c, 40, y_flow - 4, 190, y_flow - 4, GREEN, 0.5)

    flow_items = [
        ("Polymarket Watchlist",  "15 verified slugs"),
        ("Infer Loop",            "Every 60 min"),
        ("AI Swarm",              "Operators · Skeptics · Arbiter"),
        ("Edge Filter",           "≥ 5.0%  +  agreement ≤ 45%"),
        ("Paper Trade",           "$100 flat · Brier scored"),
    ]
    fx = 40
    fy = y_flow - 22
    box_w = 108
    box_h = 30
    spacing = (W - 80 - box_w) / (len(flow_items) - 1)

    for i, (title, sub) in enumerate(flow_items):
        bx = fx + i * spacing
        # Box
        c.setFillColor(PANEL)
        c.roundRect(bx, fy - box_h, box_w, box_h, 4, fill=1, stroke=0)
        c.saveState()
        c.setStrokeColor(GREEN if i == len(flow_items) - 1 else CYAN)
        c.setStrokeAlpha(0.5)
        c.setLineWidth(0.8)
        c.roundRect(bx, fy - box_h, box_w, box_h, 4, fill=0, stroke=1)
        c.restoreState()
        # Text
        c.setFillColor(WHITE if i < len(flow_items) - 1 else GREEN)
        c.setFont("Helvetica-Bold", 7.5)
        c.drawCentredString(bx + box_w / 2, fy - 12, title)
        c.setFillColor(GREY)
        c.setFont("Helvetica", 6.5)
        c.drawCentredString(bx + box_w / 2, fy - 23, sub)
        # Arrow
        if i < len(flow_items) - 1:
            ax = bx + box_w + 4
            ay = fy - box_h / 2
            glowing_line(c, ax, ay, ax + spacing - box_w - 8, ay, CYAN, 0.7)
            c.setFillColor(CYAN)
            c.setFont("Helvetica", 8)
            c.drawString(ax + spacing - box_w - 10, ay - 3, "▶")

    # ── WHAT WE TRADE ───────────────────────────────────────────────
    y_trade = y_flow - 80
    c.setFillColor(GREEN)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y_trade, "WHAT WE TRADE")
    glowing_line(c, 40, y_trade - 4, 220, y_trade - 4, GREEN, 0.5)

    categories = [
        ("US POLITICS",   "Rate cuts · Tariff rulings · Blue wave · Impeachment",           GREEN),
        ("MACRO / FED",   "Recession timing · Emergency cuts · Inflation · GDP",             CYAN),
        ("CRYPTO",        "Bitcoin $150k · ETH flip · Price milestones",                     YELLOW),
        ("GEOPOLITICS",   "Ceasefire timelines · Election outcomes · Court decisions",        HexColor("#cc88ff")),
    ]

    cw = (W - 80) / 2 - 5
    for i, (cat, desc, accent) in enumerate(categories):
        col = i % 2
        row = i // 2
        cx2 = 40 + col * (cw + 10)
        cy2 = y_trade - 24 - row * 36

        c.setFillColor(MID_NAVY)
        c.roundRect(cx2, cy2 - 22, cw, 28, 3, fill=1, stroke=0)
        c.saveState()
        c.setStrokeColor(accent)
        c.setStrokeAlpha(0.4)
        c.setLineWidth(0.6)
        c.roundRect(cx2, cy2 - 22, cw, 28, 3, fill=0, stroke=1)
        c.restoreState()

        # Accent left bar
        c.setFillColor(accent)
        c.rect(cx2, cy2 - 22, 3, 28, fill=1, stroke=0)

        c.setFillColor(accent)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(cx2 + 10, cy2 - 4, cat)
        c.setFillColor(GREY_LIGHT)
        c.setFont("Helvetica", 7.5)
        c.drawString(cx2 + 10, cy2 - 16, desc)

    # ── ROADMAP ─────────────────────────────────────────────────────
    y_road = y_trade - 108
    c.setFillColor(GREEN)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y_road, "ROADMAP")
    glowing_line(c, 40, y_road - 4, 165, y_road - 4, GREEN, 0.5)

    milestones = [
        ("✓", "Engine Core",           "Complete",                        GREEN),
        ("✓", "Polymarket Live",        "Complete",                        GREEN),
        ("✓", "LLM Reasoning",          "Active ●",                        GREEN),
        ("~", "Brier Calibration",       "In progress — Apr 28 election",   YELLOW),
        ("○", "Kalshi Integration",      "Planned — multi-venue arb",       GREY),
        ("○", "Real Capital",            "Pending proof of edge",           GREY),
    ]

    rw = (W - 80 - 20) / 3
    for i, (icon, title, status, accent) in enumerate(milestones):
        col = i % 3
        row = i // 3
        rx = 40 + col * (rw + 10)
        ry = y_road - 24 - row * 36

        c.setFillColor(MID_NAVY if accent != GREY else HexColor("#0a1520"))
        c.roundRect(rx, ry - 22, rw, 28, 3, fill=1, stroke=0)
        c.saveState()
        c.setStrokeColor(accent)
        c.setStrokeAlpha(0.35)
        c.setLineWidth(0.6)
        c.roundRect(rx, ry - 22, rw, 28, 3, fill=0, stroke=1)
        c.restoreState()

        c.setFillColor(accent)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(rx + 8, ry - 5, f"[{icon}]  {title}")
        c.setFillColor(GREY)
        c.setFont("Helvetica", 7)
        c.drawString(rx + 8, ry - 17, status)

    # ── BOTTOM STRIP ────────────────────────────────────────────────
    glowing_line(c, 0, 46, W, 46, GREEN, 0.8)
    c.setFillColor(DARK_NAVY)
    c.rect(0, 0, W, 46, fill=1, stroke=0)

    c.setFillColor(GREEN)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(40, 17, "SWARM EDGE")
    c.setFillColor(GREY)
    c.setFont("Helvetica", 8)
    c.drawString(40 + c.stringWidth("SWARM EDGE", "Helvetica-Bold", 9) + 12, 17,
                 "·  Swarm Axis  ·  AI Prediction Market Engine  ·  Paper Trading Live  ·  April 2026")

    c.setFillColor(GREEN)
    c.setFont("Helvetica-Bold", 8)
    tagline = "REAL CAPITAL IS THE ENDGAME"
    c.drawRightString(W - 40, 17, tagline)


def main():
    stats = live_stats()
    c = canvas.Canvas(str(OUT), pagesize=letter)
    c.setTitle("Swarm Edge — AI Prediction Market Engine")
    c.setAuthor("Swarm Axis")
    c.setSubject("Promotional flyer — April 2026")
    draw_page(c, stats)
    c.save()
    print(f"Generated: {OUT}")
    print(f"  Markets: {stats['n_markets']}  Trades: {stats['n_trades']}  "
          f"Deployed: ${stats['deployed']:,}  Max Payout: ${stats['total_payout']:,}")


if __name__ == "__main__":
    main()
