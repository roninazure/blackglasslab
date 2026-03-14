# reporting/eval_live.py
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone


DB_PATH = os.path.join("memory", "runs.sqlite")
OUT_MD = os.path.join("reporting", "live_report.md")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    os.makedirs("reporting", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # quick health metrics
    cur.execute("SELECT COUNT(*) FROM runs;")
    n_runs = int(cur.fetchone()[0] or 0)

    cur.execute("SELECT COUNT(*) FROM agent_runs;")
    n_agent_runs = int(cur.fetchone()[0] or 0)

    cur.execute("SELECT COUNT(*) FROM arbiter_runs;")
    n_arb = int(cur.fetchone()[0] or 0)

    cur.execute("SELECT COUNT(*) FROM paper_trades;")
    n_paper = int(cur.fetchone()[0] or 0)

    cur.execute("SELECT COUNT(*) FROM paper_trades WHERE status='OPEN';")
    n_open = int(cur.fetchone()[0] or 0)

    cur.execute("""
      SELECT round(avg(disagreement),4), round(avg(consensus_p_yes),4)
      FROM arbiter_runs;
    """)
    row = cur.fetchone() or (None, None)
    avg_dis, avg_p_yes = row[0], row[1]

    # leaderboard snapshot (last 600 linked rows)
    cur.execute("""
    SELECT agent_name, round(avg(fitness),4) avg_fit, count(*) n
    FROM (
      SELECT agent_name, fitness
      FROM agent_runs
      WHERE agent_id IS NOT NULL AND agent_id!=''
      ORDER BY id DESC LIMIT 600
    )
    GROUP BY agent_name
    ORDER BY avg_fit DESC;
    """)
    lb = cur.fetchall()

    conn.close()

    lines = []
    lines.append(f"# BlackGlassLab Live Report\n")
    lines.append(f"- Generated (UTC): {utc_now_iso()}\n")
    lines.append(f"- Runs: {n_runs}\n")
    lines.append(f"- Agent runs: {n_agent_runs}\n")
    lines.append(f"- Arbiter runs: {n_arb}\n")
    lines.append(f"- Paper trades: {n_paper} (open={n_open})\n")
    lines.append(f"- Avg disagreement: {avg_dis}\n")
    lines.append(f"- Avg consensus_p_yes: {avg_p_yes}\n")
    lines.append("\n## Fitness leaderboard (last 600 linked agent_runs)\n")
    if not lb:
        lines.append("_No linked rows yet._\n")
    else:
        lines.append("| agent_name | avg_fitness | n |\n")
        lines.append("|---|---:|---:|\n")
        for a, f, n in lb:
            lines.append(f"| {a} | {f} | {n} |\n")

    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.writelines(lines)

    print(f"Wrote {OUT_MD}")


if __name__ == "__main__":
    main()
