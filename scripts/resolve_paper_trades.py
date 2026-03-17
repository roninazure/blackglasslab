#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple


DB_PATH = os.path.join("memory", "runs.sqlite")
GAMMA_BASE = "https://gamma-api.polymarket.com"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _json_load_maybe(value: Any) -> Any:
    """
    Polymarket Gamma frequently returns list-like fields as JSON-encoded strings.
    If it looks like JSON, parse it; otherwise return as-is.
    """
    if isinstance(value, str):
        s = value.strip()
        if (s.startswith("[") and s.endswith("]")) or (s.startswith("{") and s.endswith("}")):
            try:
                return json.loads(s)
            except Exception:
                return value
    return value


def fetch_market_by_slug(slug: str, timeout_s: int = 20) -> Dict[str, Any]:
    """
    Robust fetch that avoids the stricter /markets/slug/<slug> endpoint.
    Uses /markets?slug=<slug>, which tends to be allowed (matches your working curl pattern).
    """
    qs = urllib.parse.urlencode({"slug": slug, "limit": 1})
    url = f"{GAMMA_BASE}/markets?{qs}"

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://polymarket.com/",
        "Origin": "https://polymarket.com",
    }

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = resp.read().decode("utf-8", errors="replace")

    obj = json.loads(data)

    # /markets returns a LIST
    if isinstance(obj, list) and obj:
        m = obj[0]
    else:
        raise ValueError("No market returned for slug")

    # Normalize fields that may be JSON strings.
    m["outcomes"] = _json_load_maybe(m.get("outcomes"))
    m["outcomePrices"] = _json_load_maybe(m.get("outcomePrices"))
    return m

def infer_market_yes_prob(market: Dict[str, Any]) -> Optional[float]:
    """
    Returns market-implied P(YES) if outcomes/prices are present.
    """
    outcomes = market.get("outcomes")
    prices = market.get("outcomePrices")
    if not isinstance(outcomes, list) or not isinstance(prices, list):
        return None
    if len(outcomes) != len(prices) or len(outcomes) < 2:
        return None

    # Find "Yes" index
    try:
        idx_yes = next(i for i, o in enumerate(outcomes) if str(o).strip().lower() == "yes")
    except StopIteration:
        return None

    try:
        return float(prices[idx_yes])
    except Exception:
        return None


def resolved_outcome_from_snapshot(market: Dict[str, Any]) -> Tuple[bool, Optional[str], str]:
    """
    Best-effort resolution detection.
    Gamma does not consistently expose a single 'resolved_outcome' field in docs.
    We infer resolution when:
      - market is closed/archived/inactive OR umaResolutionStatus indicates resolution
      - AND outcomePrices are essentially binary (near 1/0)
    """
    closed = bool(market.get("closed"))
    active = market.get("active")
    archived = bool(market.get("archived"))
    uma_status = str(market.get("umaResolutionStatus") or "").strip().lower()

    outcomes = market.get("outcomes")
    prices = market.get("outcomePrices")

    # Base condition: seems "done-ish"
    doneish = closed or archived or (active is False) or ("resolved" in uma_status) or ("final" in uma_status)

    if not isinstance(outcomes, list) or not isinstance(prices, list) or len(outcomes) < 2 or len(prices) < 2:
        return (False, None, "no_outcomes_or_prices")

    # Map YES/NO
    try:
        idx_yes = next(i for i, o in enumerate(outcomes) if str(o).strip().lower() == "yes")
        idx_no = next(i for i, o in enumerate(outcomes) if str(o).strip().lower() == "no")
        p_yes = float(prices[idx_yes])
        p_no = float(prices[idx_no])
    except Exception:
        return (False, None, "cannot_map_yes_no")

    # If not clearly binary, don't call it resolved.
    # Thresholds are conservative to avoid false closes.
    if (p_yes >= 0.999 and p_no <= 0.001):
        return (True, "YES", "binary_prices_yes")
    if (p_no >= 0.999 and p_yes <= 0.001):
        return (True, "NO", "binary_prices_no")

    # Some markets may be marked closed but not have 1/0 in prices; avoid guessing.
    if doneish:
        return (False, None, "doneish_but_not_binary_prices")

    return (False, None, "not_resolved")


def brier(p_yes_model: float, resolved_outcome: str) -> float:
    y = 1.0 if resolved_outcome.upper() == "YES" else 0.0
    return (float(p_yes_model) - y) ** 2


def compute_profit_usd(
    side: str,
    size_usd: float,
    outcome: str,
    p_yes_market_entry: Optional[float],
) -> float:
    """
    Compute paper trade profit/loss in USD.

    YES bet wins when outcome=YES:  profit = size * (1/p_yes_entry - 1)
    NO  bet wins when outcome=NO:   profit = size * (p_yes_entry / (1 - p_yes_entry))
    Either bet loses:               profit = -size

    Falls back to binary +size / -size if no entry price is stored.
    """
    side_up = side.upper()
    outcome_up = outcome.upper()
    correct = (side_up == "YES" and outcome_up == "YES") or (
        side_up == "NO" and outcome_up == "NO"
    )
    if not correct:
        return -float(size_usd)

    if p_yes_market_entry is not None:
        p = max(0.001, min(0.999, float(p_yes_market_entry)))
        if side_up == "YES":
            return round(float(size_usd) * (1.0 / p - 1.0), 4)
        else:  # NO
            return round(float(size_usd) * (p / (1.0 - p)), 4)

    # No entry price — simple binary
    return float(size_usd)


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 1.4: resolve/close OPEN paper_trades and compute Brier.")
    ap.add_argument("--db", default=DB_PATH, help="Path to SQLite DB (default: memory/runs.sqlite)")
    ap.add_argument("--limit", type=int, default=25, help="Max OPEN trades to process per run")
    ap.add_argument("--sleep", type=float, default=0.25, help="Sleep between API calls (seconds)")
    ap.add_argument("--dry-run", action="store_true", help="Do not write updates; print what would change")
    ap.add_argument("--timeout", type=int, default=20, help="HTTP timeout seconds")
    args = ap.parse_args()

    conn = _connect_db(args.db)
    cur = conn.cursor()

    # Only resolve Polymarket real slugs, status OPEN
    cur.execute(
        """
        SELECT id, market_id, consensus_p_yes, p_yes, side, size_usd, notes
        FROM paper_trades
        WHERE status='OPEN'
          AND venue='polymarket'
          AND market_id NOT LIKE 'FAKE-%'
        ORDER BY id ASC
        LIMIT ?;
        """,
        (int(args.limit),),
    )
    rows = cur.fetchall()

    if not rows:
        print("RESOLVER: no OPEN polymarket paper_trades to process.")
        conn.close()
        return 0

    changed = 0
    checked = 0

    for (trade_id, slug, consensus_p_yes, p_yes, side, size_usd, notes) in rows:
        checked += 1

        try:
            snap = fetch_market_by_slug(str(slug), timeout_s=int(args.timeout))
        except urllib.error.HTTPError as e:
            print(f"RESOLVER: id={trade_id} slug={slug} http_error={e.code} (skipping)")
            time.sleep(args.sleep)
            continue
        except Exception as e:
            print(f"RESOLVER: id={trade_id} slug={slug} error={e} (skipping)")
            time.sleep(args.sleep)
            continue

        is_resolved, outcome, why = resolved_outcome_from_snapshot(snap)

        if not is_resolved or not outcome:
            # Keep OPEN, but you may still want to observe drift in market price (optional later).
            print(f"RESOLVER: id={trade_id} slug={slug} OPEN (reason={why})")
            time.sleep(args.sleep)
            continue

        # Determine model probability used for scoring
        p_model = p_yes if p_yes is not None else consensus_p_yes
        if p_model is None:
            # Should not happen in your schema, but guard anyway.
            print(f"RESOLVER: id={trade_id} slug={slug} cannot_score (no p_model) (keeping OPEN)")
            time.sleep(args.sleep)
            continue

        b = brier(float(p_model), outcome)

        # Compute profit_usd using entry market price from notes
        notes_dict: Dict[str, Any] = {}
        if notes:
            try:
                parsed = json.loads(notes) if isinstance(notes, str) else {}
                if isinstance(parsed, dict):
                    notes_dict = parsed
            except Exception:
                pass
        p_yes_entry = notes_dict.get("p_yes_market")
        profit_usd = compute_profit_usd(
            side=str(side or "YES"),
            size_usd=float(size_usd or 100.0),
            outcome=outcome,
            p_yes_market_entry=float(p_yes_entry) if p_yes_entry is not None else None,
        )

        # Append minimal resolution metadata into notes (keep existing notes as prefix)
        meta = {
            "resolved_at_utc": utc_now_iso(),
            "resolved_outcome": outcome,
            "profit_usd": profit_usd,
            "resolver": "phase_2.4",
            "resolver_reason": why,
            "market_closed": bool(snap.get("closed")),
            "market_active": snap.get("active"),
            "umaResolutionStatus": snap.get("umaResolutionStatus"),
        }

        # notes field might already contain JSON; we won't attempt to merge deeply.
        new_notes = None
        if notes is None or str(notes).strip() == "":
            new_notes = json.dumps({"resolution": meta}, separators=(",", ":"))
        else:
            # Preserve existing notes verbatim; append a JSON line.
            new_notes = str(notes).rstrip() + "\n" + json.dumps({"resolution": meta}, separators=(",", ":"))

        print(f"RESOLVER: id={trade_id} slug={slug} CLOSED outcome={outcome} brier={b:.6f} profit_usd={profit_usd:+.2f}")

        if not args.dry_run:
            conn.execute(
                """
                UPDATE paper_trades
                SET status='CLOSED',
                    resolved_outcome=?,
                    brier=?,
                    notes=?
                WHERE id=?;
                """,
                (outcome, float(b), new_notes, int(trade_id)),
            )
            conn.commit()

        changed += 1
        time.sleep(args.sleep)

    conn.close()
    print(f"RESOLVER SUMMARY: checked={checked} closed={changed} dry_run={bool(args.dry_run)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
