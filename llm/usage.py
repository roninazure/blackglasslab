from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from swarm_edge_runtime import RUNTIME_PATHS


PRICE_PER_MILLION = {
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_create": 1.25, "cache_read": 0.10},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0, "cache_create": 3.75, "cache_read": 0.30},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_create": 3.75, "cache_read": 0.30},
}


def _pricing(model: str) -> tuple[dict[str, float] | None, str]:
    for prefix, prices in PRICE_PER_MILLION.items():
        if model.startswith(prefix):
            return prices, "anthropic_public_pricing_2026-08-04"
    return None, "unknown_model_pricing"


def capture_usage(response: Any, *, operation: str) -> dict[str, Any]:
    usage = response.usage
    model = str(getattr(response, "model", "") or "")
    payload: dict[str, Any] = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "model": model,
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_creation_input_tokens": int(
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        ),
        "cache_read_input_tokens": int(
            getattr(usage, "cache_read_input_tokens", 0) or 0
        ),
    }
    prices, pricing_source = _pricing(model)
    payload["pricing_source"] = pricing_source
    if prices is None:
        payload["estimated_cost_usd"] = None
        payload["estimated_cache_savings_usd"] = None
    else:
        payload["estimated_cost_usd"] = round(
            (
                payload["input_tokens"] * prices["input"]
                + payload["output_tokens"] * prices["output"]
                + payload["cache_creation_input_tokens"] * prices["cache_create"]
                + payload["cache_read_input_tokens"] * prices["cache_read"]
            )
            / 1_000_000.0,
            8,
        )
        payload["estimated_cache_savings_usd"] = round(
            payload["cache_read_input_tokens"]
            * (prices["input"] - prices["cache_read"])
            / 1_000_000.0,
            8,
        )
    directory = RUNTIME_PATHS.signals_dir / "anthropic_usage"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{payload['timestamp_utc'].replace(':', '')}-{uuid.uuid4().hex}.json"
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return payload
