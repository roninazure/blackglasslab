from __future__ import annotations

import sqlite3
from typing import List, Tuple

DB_PATH = "memory/runs.sqlite"


def top_agents(conn: sqlite3.Connection, window: int) -> List[Tuple]:
    cur = conn.cursor()
    # fitness is stored per agent run; window is last N agent_runs rows (approx. last N/agents runs)
    cur.execute(
        """
        SELECT agent_name,
               COUNT(*) as n,
               ROUND(AVG(fitness), 4) as avg_fitness,
               ROUND(AVG(reward), 4) as avg_reward,
               ROUND(AVG(brier), 4)  as avg_brier
        FROM (
            SELECT agent_name, fitness, reward, brier
            FROM agent_runs
            ORDER BY id DESC
            LIMIT ?
        )
        GROUP BY agent_name
        ORDER BY avg_fitness DESC, n DESC;
        """,
        (window,),
    )
    return cur.fetchall()


def main() -> None:
    conn = sqlite3.connect(DB_PATH)

    print("\n=== BlackGlassLab v0.6 — Fitness Leaderboard ===\n")

    for window in (200, 1000):
        rows = top_agents(conn, window)
        print(f"--- Top agents (last {window} agent_runs rows) ---")
        if not rows:
            print("No data.")
            continue

        for agent_name, n, avg_fit, avg_rew, avg_brier in rows[:12]:
            print(f"{agent_name:22} n={n:4} avgFitness={avg_fit:7} avgReward={avg_rew:7} avgBrier={avg_brier:7}")
        print()

    conn.close()


if __name__ == "__main__":
    main()
