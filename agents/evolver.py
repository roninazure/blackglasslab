from __future__ import annotations

import random
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


OPERATOR_MODES = [
    "heuristic_yes_bias",
    "mutant_0",
    "mutant_2",
    "mutant_3",
    "mutant_5",
    "mutant_7",
]

SKEPTIC_MODES = [
    "always_opposite",
    "always_no",
    "mirror_confidence",
]


@dataclass(frozen=True)
class PopMember:
    agent_id: str
    role: str
    mode: str
    seed: int
    generation: int
    parent_agent_id: Optional[str]
    mutation: str


def ensure_seed_population(conn: sqlite3.Connection, per_role: int = 12) -> None:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM agent_population;")
    n = int(cur.fetchone()[0] or 0)
    if n > 0:
        return

    ts = utc_now_iso()
    rng = random.Random(1337)

    def insert(role: str, mode: str, seed: int) -> None:
        cur.execute(
            """
            INSERT INTO agent_population (
              agent_id, role, mode, seed, generation,
              parent_agent_id, mutation, created_ts_utc, is_active
            ) VALUES (?, ?, ?, ?, 0, NULL, 'seed_init', ?, 1);
            """,
            (str(uuid.uuid4()), role, mode, int(seed), ts),
        )

    for _ in range(per_role):
        insert("operator", rng.choice(OPERATOR_MODES), rng.randrange(1_000_000))
    for _ in range(per_role):
        insert("skeptic", rng.choice(SKEPTIC_MODES), rng.randrange(1_000_000))

    conn.commit()


def sample_active(conn: sqlite3.Connection, role: str, k: int, seed: int) -> List[PopMember]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT agent_id, role, mode, seed, generation, parent_agent_id, mutation
        FROM agent_population
        WHERE role=? AND is_active=1
        ORDER BY agent_id;
        """,
        (role,),
    )
    rows = cur.fetchall()
    if not rows:
        raise RuntimeError(f"No active population members for role={role}")

    rng = random.Random(int(seed))
    rng.shuffle(rows)
    rows = rows[: min(k, len(rows))]
    return [PopMember(*r) for r in rows]


def _current_generation(conn: sqlite3.Connection) -> int:
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(MAX(generation), 0) FROM agent_population;")
    return int(cur.fetchone()[0] or 0)


def evolve(
    conn: sqlite3.Connection,
    role: str,
    window_agent_runs: int = 1500,
    min_obs: int = 10,
    keep_top: int = 8,
    spawn: int = 8,
) -> Dict[str, Any]:
    cur = conn.cursor()

    cur.execute(
        """
        SELECT ar.agent_id,
               COUNT(*) AS n,
               AVG(ar.fitness) AS avg_fit
        FROM (
          SELECT ar2.id, ar2.agent_id, ar2.fitness
          FROM agent_runs ar2
          JOIN agent_population ap2 ON ap2.agent_id = ar2.agent_id
          WHERE ap2.role = ?
          ORDER BY ar2.id DESC
          LIMIT ?
        ) ar
        GROUP BY ar.agent_id
        HAVING n >= ?
        ORDER BY avg_fit DESC;
        """,
        (role, window_agent_runs, min_obs),
    )
    ranked = cur.fetchall()

    if not ranked:
        return {"role": role, "status": "no_ranked_agents", "min_obs": min_obs, "window": window_agent_runs}

    top = ranked[:keep_top]
    top_ids = [r[0] for r in top]

    # Cull non-top actives
    cur.execute(
        """
        UPDATE agent_population
        SET is_active=0
        WHERE role=? AND is_active=1 AND agent_id NOT IN (%s);
        """ % ",".join("?" * len(top_ids)),
        (role, *top_ids),
    )

    gen = _current_generation(conn) + 1
    ts = utc_now_iso()
    rng = random.Random(uuid.uuid4().int & 0xFFFFFFFF)
    mode_pool = OPERATOR_MODES if role == "operator" else SKEPTIC_MODES

    def get_parent(pid: str) -> tuple[str, int]:
        cur.execute("SELECT mode, seed FROM agent_population WHERE agent_id=?;", (pid,))
        m, s = cur.fetchone()
        return str(m), int(s)

    spawned = 0
    for _ in range(spawn):
        parent_id = rng.choice(top_ids)
        pmode, pseed = get_parent(parent_id)

        if rng.random() < 0.30:
            choices = [m for m in mode_pool if m != pmode] or [pmode]
            new_mode = rng.choice(choices)
            new_seed = rng.randrange(1_000_000)
            mutation = f"mode_swap:{pmode}->{new_mode}"
        else:
            new_mode = pmode
            new_seed = (pseed + rng.randrange(1, 50_000)) % 1_000_000
            mutation = "seed_jitter"

        cur.execute(
            """
            INSERT INTO agent_population (
              agent_id, role, mode, seed, generation,
              parent_agent_id, mutation, created_ts_utc, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1);
            """,
            (str(uuid.uuid4()), role, new_mode, int(new_seed), int(gen), parent_id, mutation, ts),
        )
        spawned += 1

    conn.commit()

    return {
        "role": role,
        "status": "evolved",
        "generation": gen,
        "kept": [{"agent_id": a, "n": int(n), "avg_fitness": float(f)} for a, n, f in top],
        "spawned": spawned,
    }
