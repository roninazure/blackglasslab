"""
Compatibility shim — live_runner.py imports from llm.openai_client.
Phase 3 routes all calls through the Claude client.
"""
from llm.claude_client import claude_enabled as openai_enabled, forecast_yes_probability  # noqa: F401
