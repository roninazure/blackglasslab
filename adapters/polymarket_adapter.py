from __future__ import annotations

import json
import subprocess
from typing import Any, Dict


class PolymarketAdapter:
    """
    Phase 1.9 adapter:
    - Fetch exact market by slug using direct slug endpoint
    - Avoid fuzzy search mismatch for runtime infer
    """

    VENUE = "polymarket"
    BASE_URL = "https://gamma-api.polymarket.com/markets"

    def venue(self) -> str:
        return self.VENUE

    def get_market(self, slug: str) -> Dict[str, Any]:
        slug = (slug or "").strip()
        if not slug:
            raise ValueError("get_market: slug is empty")

        url = f"{self.BASE_URL}/slug/{slug}"
        cmd = ["curl", "-sS", url]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"curl failed rc={res.returncode}: {res.stderr.strip()[:300]}")

        try:
            data = json.loads(res.stdout)
        except Exception as e:
            raise RuntimeError(f"gamma-api returned non-json for slug={slug}: {str(e)}")

        if not isinstance(data, dict):
            raise LookupError(f"slug endpoint did not return an object for slug={slug}")

        got_slug = str(data.get("slug") or "").strip()
        if got_slug and got_slug != slug:
            raise LookupError(f"slug mismatch: requested={slug} returned={got_slug}")

        return data
