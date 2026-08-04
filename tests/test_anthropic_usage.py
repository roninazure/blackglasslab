from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from llm import usage


class AnthropicUsageTests(unittest.TestCase):
    def test_real_response_usage_and_cost_estimate_are_persisted(self) -> None:
        response = SimpleNamespace(
            model="claude-haiku-4-5-20251001",
            usage=SimpleNamespace(
                input_tokens=1000,
                output_tokens=200,
                cache_creation_input_tokens=100,
                cache_read_input_tokens=50,
            ),
        )
        with tempfile.TemporaryDirectory() as td:
            paths = SimpleNamespace(signals_dir=Path(td))
            with mock.patch.object(usage, "RUNTIME_PATHS", paths):
                payload = usage.capture_usage(response, operation="forecast")
            self.assertEqual(payload["model"], response.model)
            self.assertEqual(payload["input_tokens"], 1000)
            self.assertEqual(payload["cache_read_input_tokens"], 50)
            self.assertAlmostEqual(payload["estimated_cost_usd"], 0.00213)
            self.assertEqual(len(list((Path(td) / "anthropic_usage").glob("*.json"))), 1)

    def test_unknown_model_cost_remains_unknown(self) -> None:
        response = SimpleNamespace(
            model="future-model",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(usage, "RUNTIME_PATHS", SimpleNamespace(signals_dir=Path(td))):
                payload = usage.capture_usage(response, operation="skeptic")
        self.assertIsNone(payload["estimated_cost_usd"])
        self.assertEqual(payload["pricing_source"], "unknown_model_pricing")


if __name__ == "__main__":
    unittest.main()
