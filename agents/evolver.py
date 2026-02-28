# agents/evolver.py
from __future__ import annotations

import random
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Keep these aligned with your existing agent implementations.
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
    role: str               # operator|skeptic
    mode: str
    seed: int
    generation: int
    parent_agent_id: Optional[str]
    mutation: str


def _mode_pool(role: str) -> List[str]:
    if role == "operator":
        return OPERATOR_MODES
    if role == "skeptic":
        return SKEPTIC_MODES
    raise ValueError(f"Unknown role: {role}")


def ensure_seed_population(conn: sqlite3.Connection, per_role: int = 12, seed: int = 1337) -> Dict[str, Any]:
    """
    Create initial population if agent_population is empty.
    Returns a small dict suitable for logging/telemetry.
    """
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM agent_population;")
    existing = int(cur.fetchone()[0] or 0)
    if existing > 0:
        return {"status": "already_seeded", "existing": existing}

    rng = random.Random(int(seed))
    ts = utc_now_iso()

    def insert(role: str, mode: str, member_seed: int) -> None:
        cur.execute(
            """
            INSERT INTO agent_population (
              agent_id, role, mode, seed, generation,
              parent_agent_id, mutation, created_ts_utc, is_active
            ) VALUES (?, ?, ?, ?, 0, NULL, 'seed_init', ?, 1);
            """,
            (str(uuid.uuid4()), role, mode, int(member_seed), ts),
        )

    for _ in range(per_role):
        insert("operator", rng.choice(OPERATOR_MODES), rng.randrange(1_000_000))
    for _ in range(per_role):
        insert("skeptic", rng.choice(SKEPTIC_MODES), rng.randrange(1_000_000))

    conn.commit()
    return {"status": "seeded", "per_role": per_role, "total": per_role * 2}


def sample_active(conn: sqlite3.Connection, role: str, k: int, seed: int) -> List[PopMember]:
    """
    Sample k active members for a role. Deterministic given seed.
    """
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


def _rank_agents_by_fitness(
    conn: sqlite3.Connection,
    role: str,
    window_agent_runs: int,
    min_obs: int,
) -> List[Tuple[str, int, float]]:
    """
    Returns list of (agent_id, n, avg_fitness), ordered desc.
    Only uses agent_runs rows that actually map to agent_population via agent_id.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT ar.agent_id,
               COUNT(*) AS n,
               AVG(ar.fitness) AS avg_fit
        FROM (
          SELECT id, agent_id, fitness
          FROM agent_runs
          WHERE agent_id IS NOT NULL AND agent_id != ''
          ORDER BY id DESC
          LIMIT ?
        ) ar
        JOIN agent_population ap
          ON ap.agent_id = ar.agent_id
        WHERE ap.role = ?
        GROUP BY ar.agent_id
        HAVING n >= ?
        ORDER BY avg_fit DESC;
        """,
        (window_agent_runs, role, min_obs),
    )
    rows = cur.fetchall()
    return [(str(a), int(n), float(f)) for (a, n, f) in rows]


def evolve(
    conn: sqlite3.Connection,
    role: str,
    window_agent_runs: int = 1500,
    min_obs: int = 10,
    keep_top: int = 8,
    spawn: int = 8,
    rng_seed: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Evolution step:
      - rank last window_agent_runs population-linked agent_runs by avg fitness
      - deactivate non-top actives
      - spawn new children from top set (mode swap or seed jitter)

    Returns a dict suitable for writing into logs/events.jsonl.
    """
    ranked = _rank_agents_by_fitness(conn, role, window_agent_runs, min_obs)

    if not ranked:
        return {
            "role": role,
            "status": "no_ranked_agents",
            "window": window_agent_runs,
            "min_obs": min_obs,
        }

    top = ranked[:keep_top]
    top_ids = [a for (a, _, _) in top]

    cur = conn.cursor()

    # Deactivate all active members not in top_ids for this role.
    cur.execute(
        f"""
        UPDATE agent_population
        SET is_active=0
        WHERE role=? AND is_active=1 AND agent_id NOT IN ({",".join("?" * len(top_ids))});
        """,
        (role, *top_ids),
    )

    gen = _current_generation(conn) + 1
    ts = utc_now_iso()
    rng = random.Random(int(rng_seed) if rng_seed is not None else (uuid.uuid4().int & 0xFFFFFFFF))
    pool = _mode_pool(role)

    def get_parent(pid: str) -> Tuple[str, int]:
        cur.execute("SELECT mode, seed FROM agent_population WHERE agent_id=?;", (pid,))
        row = cur.fetchone()
        if not row:
            raise RuntimeError(f"Missing parent in agent_population: {pid}")
        return str(row[0]), int(row[1])

    spawned = 0
    for _ in range(int(spawn)):
        parent_id = rng.choice(top_ids)
        pmode, pseed = get_parent(parent_id)

        # Mutations:
        #  - 30% mode swap
        #  - 70% seed jitter
        if rng.random() < 0.30:
            choices = [m for m in pool if m != pmode] or [pmode]
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
        "window": window_agent_runs,
        "min_obs": min_obs,
        "kept": [{"agent_id": a, "n": n, "avg_fitness": f} for (a, n, f) in top],
        "spawned": spawned,
    }
