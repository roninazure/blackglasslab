"""
BlackGlassLab — Claude forecast client (Phase 3)

Provides:
  claude_enabled() -> bool
  forecast_yes_probability(question, context) -> (p_yes, confidence, rationale)

Model is controlled by BGL_LLM_MODEL env var (default: claude-haiku-4-5-20251001).
API key: ANTHROPIC_API_KEY in environment or .env file.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

from context.temporal import build_temporal_context, format_temporal_context_block
from loop_engine.prompts import build_forecast_prompts, classify_market
from loop_engine.skeptic import SkepticReview, normalize_skeptic_review
from swarm_edge_runtime import load_runtime_environment

# ---------------------------------------------------------------------------
# .env loader — simple KV parse, no external deps needed
# ---------------------------------------------------------------------------

def _load_dotenv(path: Optional[str] = None) -> None:
    if path is None or path == ".env":
        load_runtime_environment()
        return
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()

# ---------------------------------------------------------------------------
# SDK import (optional — graceful degradation if not installed)
# ---------------------------------------------------------------------------

try:
    import anthropic as _anthropic
    _SDK_AVAILABLE = True
except ImportError:
    _anthropic = None  # type: ignore
    _SDK_AVAILABLE = False

_client: Optional[object] = None


def _get_client():
    global _client
    if _client is None:
        if not _SDK_AVAILABLE:
            raise RuntimeError(
                "anthropic SDK not installed. Run: pip install anthropic"
            )
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Add it to .env or export it."
            )
        _client = _anthropic.Anthropic(api_key=api_key)
    return _client


def claude_enabled() -> bool:
    if not _SDK_AVAILABLE:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


# ---------------------------------------------------------------------------
# Core forecast function
# ---------------------------------------------------------------------------

def _model_name() -> str:
    model = os.environ.get("BGL_LLM_MODEL", "claude-haiku-4-5-20251001").strip()
    if not model.startswith("claude-"):
        return "claude-haiku-4-5-20251001"
    return model


def _parse_json_response(raw: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", raw.strip())
    cleaned = cleaned.rstrip("` \n")
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("Claude response must be a JSON object")
    return parsed


def forecast_yes_probability(
    question: str,
    context: dict,
) -> Tuple[float, float, str]:
    """
    Call Claude to estimate P(YES) for a prediction market question.

    Args:
        question: The market question text.
        context:  Dict with keys: p_yes_market, market_snapshot, venue, slug, policy.

    Returns:
        (p_yes, confidence, rationale)
        p_yes       — probability of YES resolving (0.01–0.99)
        confidence  — model self-reported confidence (0.50–0.95)
        rationale   — 1-2 sentence explanation
    """
    client = _get_client()
    model = _model_name()

    p_yes_market = float(context.get("p_yes_market", 0.5))
    snap = context.get("market_snapshot", {})
    venue = str(context.get("venue", "polymarket"))
    now_utc = datetime.now(timezone.utc)
    temporal_context = context.get("temporal_context") or build_temporal_context(
        snap,
        question=question,
        slug=str(context.get("slug") or ""),
        now=now_utc,
    )
    category = str(context.get("category") or classify_market(question))
    system_prompt, user_prompt = build_forecast_prompts(
        question=question,
        venue=venue,
        p_yes_market=p_yes_market,
        market_snapshot=snap,
        temporal_context=temporal_context,
        category=category,
    )
    try:
        from context.crypto import get_crypto_context

        crypto_context = get_crypto_context(question)
    except Exception:
        crypto_context = ""
    if crypto_context:
        user_prompt = f"{user_prompt}\nVerified live context:\n{crypto_context}"

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=350,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as api_err:
        err_str = str(api_err)
        if "credit balance" in err_str or "402" in err_str or "payment" in err_str.lower():
            raise RuntimeError(f"Anthropic billing error — add credits at console.anthropic.com: {api_err}") from api_err
        raise

    data = _parse_json_response(resp.content[0].text)

    p_yes = float(data["p_yes"])
    confidence = float(data.get("confidence", 0.70))
    rationale = str(data.get("rationale", ""))

    # Hard clamps
    p_yes = max(0.01, min(0.99, p_yes))
    confidence = max(0.50, min(0.95, confidence))

    return (p_yes, confidence, rationale)


def review_forecast(
    *,
    question: str,
    category: str,
    p_yes_market: float,
    p_yes_model: float,
    confidence: float,
    rationale: str,
    temporal_context: dict[str, Any],
) -> SkepticReview:
    """Run a compact second pass over forecasts that are close to trade-worthy."""
    client = _get_client()
    system_prompt = (
        "You are a skeptical prediction-market risk reviewer. Test temporal validity, "
        "stale facts, malformed or novelty-driven wording, and whether apparent edge is "
        "supported rather than explanation-driven. Return JSON only."
    )
    user_prompt = "\n".join(
        [
            f"Question: {question}",
            f"Category: {category}",
            f"Market P(YES): {p_yes_market:.4f}",
            f"Forecast P(YES): {p_yes_model:.4f}",
            f"Forecast confidence: {confidence:.3f}",
            f"Forecast rationale: {rationale or 'none'}",
            format_temporal_context_block(temporal_context),
            "Choose ALLOW, DOWNGRADE, or REJECT. DOWNGRADE means shrink the forecast halfway toward the market.",
            'Return: {"action":"ALLOW|DOWNGRADE|REJECT","reason":"short code","rationale":"short","temporal_valid":true,"stale_facts":false,"malformed_or_novelty":false,"edge_real":true}',
        ]
    )
    resp = client.messages.create(
        model=_model_name(),
        max_tokens=240,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return normalize_skeptic_review(_parse_json_response(resp.content[0].text))
