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
from pathlib import Path
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# .env loader — simple KV parse, no external deps needed
# ---------------------------------------------------------------------------

def _load_dotenv(path: str = ".env") -> None:
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

_SYSTEM_PROMPT = """\
You are a calibrated prediction market forecaster working for a quantitative trading system.

Your job: estimate the TRUE probability that a binary prediction market resolves YES.

Rules:
- Be calibrated. If something is 90% likely, say 0.90, not 0.99.
- The crowd price is informative but not always right. Diverge only with clear reasoning.
- For near-expiry markets (days away), weight recent reality heavily.
- For long-horizon markets, use base rates and fundamentals.
- Return ONLY valid JSON — no markdown, no preamble, nothing else.

Output format (exact):
{"p_yes": <float 0.01-0.99>, "confidence": <float 0.50-0.95>, "rationale": "<1-2 sentences max>"}
"""


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
    model = os.environ.get("BGL_LLM_MODEL", "claude-haiku-4-5-20251001").strip()

    p_yes_market = float(context.get("p_yes_market", 0.5))
    snap = context.get("market_snapshot", {})
    updated = snap.get("updatedAt", "")
    venue = str(context.get("venue", "polymarket"))

    # Build enriched context block (crypto prices, time-to-resolution, category notes)
    try:
        from context.market_context import build_context_block
        enriched = build_context_block(
            question=question,
            market_snapshot=snap,
            p_yes_market=p_yes_market,
            venue=venue,
        )
    except Exception:
        enriched = ""

    ctx_lines = [
        f"Question: {question}",
        f"Venue: {venue}",
        f"Current crowd price (P_YES): {p_yes_market:.4f}  ({p_yes_market * 100:.1f}%)",
    ]
    if updated:
        ctx_lines.append(f"Last updated: {updated}")
    if enriched:
        ctx_lines.append("")
        ctx_lines.append(enriched)

    user_prompt = "\n".join(ctx_lines) + (
        "\n\nEstimate the true probability this market resolves YES. "
        "Consider whether the crowd price is well-calibrated. "
        "Only diverge significantly if you have clear, specific reasoning. "
        "Return JSON only."
    )

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=350,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as api_err:
        err_str = str(api_err)
        if "credit balance" in err_str or "402" in err_str or "payment" in err_str.lower():
            raise RuntimeError(f"Anthropic billing error — add credits at console.anthropic.com: {api_err}") from api_err
        raise

    raw = resp.content[0].text.strip()
    # Strip markdown fences if model wraps output despite instructions
    raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
    raw = raw.rstrip("` \n")

    data = json.loads(raw)

    p_yes = float(data["p_yes"])
    confidence = float(data.get("confidence", 0.70))
    rationale = str(data.get("rationale", ""))

    # Hard clamps
    p_yes = max(0.01, min(0.99, p_yes))
    confidence = max(0.50, min(0.95, confidence))

    return (p_yes, confidence, rationale)
