#!/usr/bin/env python3
"""
make_thumbnail.py — Generate a social-share thumbnail PNG for Swarm Edge.
Output: swarm_edge_thumb.png  (1200×630 — standard Open Graph size)
"""
from __future__ import annotations
import json, math, sqlite3
from pathlib import Path
from datetime import datetime, timezone

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    raise SystemExit("pip install Pillow")

ROOT     = Path(__file__).parent.parent
DB_PATH  = ROOT / "memory" / "runs.sqlite"
DATA_DIR = ROOT / "data"
OUT      = ROOT / "swarm_edge_thumb.png"

# ── colours ──────────────────────────────────────────────────────────────────
BG       = (5,   8,  16)      # near-black
PANEL    = (12,  18,  32)     # card bg
BORDER   = (0,  200, 150)     # teal accent
ACCENT   = (0,  200, 150)     # same
GOLD     = (255, 210,  60)
WHITE    = (240, 245, 255)
GREY     = (140, 155, 175)
DIM      = (60,  75,  95)
RED      = (255,  70,  70)
GREEN    = (0,  220, 120)

W, H = 1200, 630

# ── fonts ─────────────────────────────────────────────────────────────────────
def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        f"/usr/share/fonts/truetype/dejavu/DejaVuSansMono{'-Bold' if bold else ''}.ttf",
        f"/usr/share/fonts/truetype/liberation/LiberationMono-{'Bold' if bold else 'Regular'}.ttf",
        f"/usr/share/fonts/truetype/ubuntu/UbuntuMono-{'B' if bold else 'R'}.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


# ── live stats ────────────────────────────────────────────────────────────────
def live_stats() -> dict:
    trades, total_stake, total_ev = [], 0.0, 0.0

    # Try JSON first (cloud-friendly)
    jfile = DATA_DIR / "paper_trades.json"
    if jfile.exists():
        rows = json.loads(jfile.read_text())
        for t in rows:
            notes = {}
            try: notes = json.loads(t.get("notes") or "{}")
            except: pass
            crowd = notes.get("crowd_p_yes") or t.get("p_yes", 0.5)
            try:
                crowd = float(crowd)
                if math.isnan(crowd): crowd = float(t.get("p_yes", 0.5))
            except: crowd = 0.5
            stake = float(t.get("size_usd", 0))
            payout = stake / crowd if crowd > 0 else 0
            ev = payout - stake
            total_stake += stake
            total_ev += ev
            trades.append({"q": t.get("question", ""), "crowd": crowd,
                           "stake": stake, "ev": ev})

    elif DB_PATH.exists():
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM paper_trades WHERE status='OPEN' ORDER BY ts_utc DESC"
        ).fetchall()
        conn.close()
        for r in rows:
            notes = {}
            try: notes = json.loads(r["notes"] or "{}")
            except: pass
            crowd = notes.get("crowd_p_yes") or r["p_yes"]
            try:
                crowd = float(crowd)
                if math.isnan(crowd): crowd = float(r["p_yes"])
            except: crowd = 0.5
            stake = float(r["size_usd"])
            payout = stake / crowd if crowd > 0 else 0
            ev = payout - stake
            total_stake += stake
            total_ev += ev
            trades.append({"q": r["question"], "crowd": crowd,
                           "stake": stake, "ev": ev})

    # watchlist count
    wfile = ROOT / "config" / "watchlist.json"
    watch_count = 0
    if wfile.exists():
        try: watch_count = len(json.loads(wfile.read_text()))
        except: pass

    return {
        "n_trades": len(trades),
        "total_stake": total_stake,
        "total_ev": total_ev,
        "max_payout": total_stake + total_ev,
        "watch_count": watch_count or 25,
        "trades": trades[:6],
    }


# ── helpers ────────────────────────────────────────────────────────────────────
def draw_rounded_rect(draw: ImageDraw.Draw, xy, radius: int, fill, outline=None, width=1):
    x0, y0, x1, y1 = xy
    draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill,
                           outline=outline, width=width)

def centered_text(draw, cx, y, text, font, fill):
    bb = draw.textbbox((0, 0), text, font=font)
    w = bb[2] - bb[0]
    draw.text((cx - w // 2, y), text, font=font, fill=fill)

def right_text(draw, rx, y, text, font, fill):
    bb = draw.textbbox((0, 0), text, font=font)
    w = bb[2] - bb[0]
    draw.text((rx - w, y), text, font=font, fill=fill)


# ── main ───────────────────────────────────────────────────────────────────────
def main():
    stats = live_stats()
    img = Image.new("RGB", (W, H), BG)
    d   = ImageDraw.Draw(img)

    # load fonts
    f_huge   = load_font(88, bold=True)
    f_big    = load_font(44, bold=True)
    f_med    = load_font(28, bold=True)
    f_small  = load_font(22, bold=False)
    f_tiny   = load_font(17, bold=False)
    f_mono   = load_font(19, bold=False)
    f_mono_b = load_font(21, bold=True)

    # ── top accent bar ──
    d.rectangle([0, 0, W, 6], fill=ACCENT)

    # ── header ──────────────────────────────────────────────────────────────
    # SWARM EDGE (huge)
    centered_text(d, W // 2, 22, "SWARM EDGE", f_huge, WHITE)

    # Tagline
    tag = "AI-Powered Prediction Market Intelligence"
    centered_text(d, W // 2, 120, tag, f_small, GREY)

    # SWARM AXIS label  — top right
    d.text((W - 20, 14), "SWARM AXIS", font=f_tiny, fill=DIM, anchor="ra")

    # ── divider ──────────────────────────────────────────────────────────────
    d.line([(60, 164), (W - 60, 164)], fill=DIM, width=1)

    # ── stat cards (4 boxes) ─────────────────────────────────────────────────
    CARD_Y = 178
    CARD_H = 110
    GAP    = 16
    CW     = (W - 60*2 - GAP*3) // 4

    cards = [
        ("ACTIVE\nTRADES",   str(stats["n_trades"]),           WHITE,  "markets tracked"),
        ("DEPLOYED\nCAPITAL", f"${stats['total_stake']:,.0f}", GOLD,   "paper USD at risk"),
        ("MAX\nPAYOUT",       f"${stats['max_payout']:,.0f}",  GREEN,  "if all YES hits"),
        ("MARKETS\nWATCHED",  str(stats["watch_count"]),        ACCENT, "live watchlist"),
    ]

    for i, (label, val, col, sub) in enumerate(cards):
        x0 = 60 + i * (CW + GAP)
        x1 = x0 + CW
        draw_rounded_rect(d, [x0, CARD_Y, x1, CARD_Y + CARD_H], 10,
                          fill=PANEL, outline=DIM, width=1)
        # label (two lines)
        lines = label.split("\n")
        d.text((x0 + 12, CARD_Y + 10), lines[0], font=f_tiny, fill=GREY)
        if len(lines) > 1:
            d.text((x0 + 12, CARD_Y + 28), lines[1], font=f_tiny, fill=GREY)
        # value
        bb = d.textbbox((0, 0), val, font=f_big)
        vw = bb[2] - bb[0]
        vx = x0 + CW // 2 - vw // 2
        d.text((vx, CARD_Y + 50), val, font=f_big, fill=col)
        # subtext
        centered_text(d, x0 + CW // 2, CARD_Y + CARD_H - 22, sub, f_tiny, DIM)

    # ── divider ──────────────────────────────────────────────────────────────
    d.line([(60, CARD_Y + CARD_H + 14), (W - 60, CARD_Y + CARD_H + 14)], fill=DIM, width=1)

    # ── active positions list ─────────────────────────────────────────────────
    LIST_Y = CARD_Y + CARD_H + 26
    d.text((60, LIST_Y), "ACTIVE POSITIONS", font=f_med, fill=ACCENT)

    # column headers
    HY = LIST_Y + 36
    d.text((60,   HY), "MARKET",  font=f_tiny, fill=GREY)
    d.text((820,  HY), "CROWD",   font=f_tiny, fill=GREY)
    d.text((930,  HY), "STAKE",   font=f_tiny, fill=GREY)
    d.text((1050, HY), "UPSIDE",  font=f_tiny, fill=GREY)
    d.line([(60, HY + 20), (W - 60, HY + 20)], fill=DIM, width=1)

    ROW_H = 36
    for i, t in enumerate(stats["trades"][:5]):
        ry = HY + 26 + i * ROW_H
        # alternate row bg
        if i % 2 == 0:
            d.rectangle([60, ry - 4, W - 60, ry + ROW_H - 8], fill=(10, 15, 26))

        # truncate question
        q = t["q"]
        if len(q) > 52: q = q[:49] + "..."
        d.text((68, ry), q, font=f_mono, fill=WHITE)

        crowd_pct = f"{t['crowd']*100:.0f}%"
        crowd_col = GREEN if t["crowd"] < 0.35 else (GOLD if t["crowd"] < 0.55 else RED)
        d.text((820, ry), crowd_pct, font=f_mono_b, fill=crowd_col)

        d.text((930, ry), f"${t['stake']:.0f}", font=f_mono, fill=GREY)

        upside = t["ev"]
        up_col = GREEN if upside > 0 else RED
        d.text((1050, ry), f"+${upside:,.0f}", font=f_mono_b, fill=up_col)

    # ── bottom bar ────────────────────────────────────────────────────────────
    BAR_Y = H - 44
    d.rectangle([0, BAR_Y, W, H], fill=PANEL)
    d.line([(0, BAR_Y), (W, BAR_Y)], fill=ACCENT, width=2)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    d.text((20, BAR_Y + 12), f"Paper mode  |  {now}", font=f_tiny, fill=GREY)
    centered_text(d, W // 2, BAR_Y + 12,
                  "Operators  x  Skeptics  x  Arbiter", f_tiny, DIM)
    right_text(d, W - 20, BAR_Y + 12,
               "swarm-axis.ai", f_tiny, ACCENT)

    # ── bottom accent bar ──
    d.rectangle([0, H - 6, W, H], fill=ACCENT)

    img.save(OUT, "PNG", optimize=True)
    print(f"Saved → {OUT}  ({W}x{H}px)")


if __name__ == "__main__":
    main()
