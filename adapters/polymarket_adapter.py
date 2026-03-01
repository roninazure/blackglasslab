from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, Optional


class PolymarketAdapter:
    """
    Phase 1.x adapter: minimal, reliable read-only market fetch for a given slug.

    We intentionally use curl here because gamma-api calls are known to work via curl in this env,
    while urllib may receive 403 depending on headers/rate/agent.
    """

    VENUE = "polymarket"
    BASE_URL = "https://gamma-api.polymarket.com/markets"

    def venue(self) -> str:
        return self.VENUE

    def get_market(self, slug: str) -> Dict[str, Any]:
        slug = (slug or "").strip()
        if not slug:
            raise ValueError("get_market: slug is empty")

        # Query markets endpoint using search=slug and pick exact match.
        # Use limit=80 to avoid missing the exact slug due to ranking.
        cmd = [
            "curl",
            "-sG",
            "--data-urlencode",
            f"search={slug}",
            "--data-urlencode",
            "limit=80",
            "--data-urlencode",
            "active=true",
            "--data-urlencode",
            "closed=false",
            self.BASE_URL,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"curl failed rc={res.returncode}: {res.stderr.strip()[:300]}")

        try:
            data = json.loads(res.stdout)
        except Exception as e:
            raise RuntimeError(f"gamma-api returned non-json for slug={slug}: {str(e)}")

        markets = data if isinstance(data, list) else data.get("markets", [])
        if not isinstance(markets, list):
            markets = []

        exact: Optional[Dict[str, Any]] = None
        for m in markets:
            if isinstance(m, dict) and m.get("slug") == slug:
                exact = m
                break

        if exact is None:
            # Provide a helpful error payload for debugging
            sample = []
            for m in markets[:10]:
                if isinstance(m, dict):
                    sample.append(m.get("slug"))
            raise LookupError(f"slug not found via search: {slug}. sample_slugs={sample}")

        return exact
