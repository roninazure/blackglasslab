#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
WATCHLIST_PATH = ROOT / "markets" / "polymarket_watchlist.json"
DB_PATH = ROOT / "memory" / "runs.sqlite"
CANDIDATES_PATH = ROOT / "signals" / "trade_candidates_arbiter.json"
REPORT_PATH = ROOT / "signals" / "arbiter_watchlist_validation.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_watchlist(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    slugs: list[str] = []
    for item in data:
        if isinstance(item, str) and item.strip():
            slugs.append(item.strip())
            continue
        if isinstance(item, dict):
            slug = item.get("market_id") or item.get("slug") or item.get("id")
            if isinstance(slug, str) and slug.strip():
                slugs.append(slug.strip())
    return slugs


def run_cmd(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def read_candidate() -> dict[str, Any] | None:
    if not CANDIDATES_PATH.exists():
        return None
    try:
        data = json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            return first
    return None


def extract_reason(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    warn_lines = [line for line in lines if line.startswith("[WARN]")]
    if warn_lines:
        return warn_lines[-1]
    if lines:
        return lines[-1]
    return "no output"


def truncate(text: str, limit: int = 76) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def latest_question_for_market(db_path: Path, market_id: str) -> str | None:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            """
            SELECT question
            FROM runs
            WHERE market_id=?
            ORDER BY id DESC
            LIMIT 1;
            """,
            (market_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return None
    return str(row[0])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watchlist", default=str(WATCHLIST_PATH))
    ap.add_argument("--source", default="polymarket")
    ap.add_argument("--paper", action="store_true", default=True)
    ap.add_argument("--no-paper", dest="paper", action="store_false")
    ap.add_argument("--reset-db", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    watchlist = load_watchlist(Path(args.watchlist))
    if args.limit > 0:
        watchlist = watchlist[: int(args.limit)]

    if not watchlist:
        raise SystemExit(f"No watchlist entries found in {args.watchlist}")

    if args.reset_db and DB_PATH.exists():
        DB_PATH.unlink()

    rows: list[dict[str, Any]] = []
    generated = 0

    print("candidate  side  edge     disagree  market_id")
    print("---------  ----  -------  --------  ---------")

    for market_id in watchlist:
        env = os.environ.copy()
        env["BGL_MARKET_ID"] = market_id
        env.pop("BGL_MARKET_QUESTION", None)

        orch = run_cmd([sys.executable, "orchestrator.py"], env=env)
        live_args = [sys.executable, "live_runner.py", "--mode", "arbiter", "--source", args.source]
        if args.paper:
            live_args.append("--paper")
        live = run_cmd(live_args, env=env)

        candidate = read_candidate()
        combined_output = "\n".join(
            x for x in [orch.stdout, orch.stderr, live.stdout, live.stderr] if x
        )

        row: dict[str, Any] = {
            "market_id": market_id,
            "question": latest_question_for_market(DB_PATH, market_id),
            "candidate_generated": False,
            "side": None,
            "consensus_p_yes": None,
            "p_yes_market": None,
            "edge": None,
            "disagreement": None,
            "status": "no_candidate",
            "reason": extract_reason(combined_output),
            "orchestrator_rc": orch.returncode,
            "live_runner_rc": live.returncode,
        }

        if candidate is not None and candidate.get("market_id") == market_id:
            notes = candidate.get("notes") if isinstance(candidate.get("notes"), dict) else {}
            row.update(
                {
                    "question": candidate.get("question"),
                    "candidate_generated": True,
                    "side": candidate.get("side"),
                    "consensus_p_yes": candidate.get("consensus_p_yes"),
                    "p_yes_market": notes.get("p_yes_market"),
                    "edge": candidate.get("edge"),
                    "disagreement": candidate.get("disagreement"),
                    "status": str(candidate.get("status") or "OPEN"),
                    "reason": "candidate",
                }
            )
            generated += 1

        rows.append(row)

        edge_text = "-" if row["edge"] is None else f"{float(row['edge']):.3f}"
        disagreement_text = "-" if row["disagreement"] is None else f"{float(row['disagreement']):.3f}"
        side_text = str(row["side"] or "-")
        cand_text = "yes" if row["candidate_generated"] else "no"
        print(f"{cand_text:<9}  {side_text:<4}  {edge_text:<7}  {disagreement_text:<8}  {truncate(market_id)}")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(
            {
                "ts_utc": utc_now_iso(),
                "source": args.source,
                "paper": bool(args.paper),
                "watchlist": str(args.watchlist),
                "total_markets": len(watchlist),
                "generated_candidates": generated,
                "rows": rows,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    print(
        f"\nBATCH_VALIDATE OK generated={generated}/{len(watchlist)} report={REPORT_PATH.relative_to(ROOT)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
