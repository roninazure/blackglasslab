from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class RevenueArchitectureTests(unittest.TestCase):
    def test_revenue_package_has_no_live_execution_dependencies(self) -> None:
        banned = {"adapters", "llm", "agents", "orchestrator", "live_runner"}
        for path in (ROOT / "revenue_poc").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])
            self.assertFalse(imports & banned, f"{path.name} imports {imports & banned}")

    def test_cli_has_no_live_order_option_or_connector_calls(self) -> None:
        text = (ROOT / "scripts" / "revenue_poc.py").read_text(encoding="utf-8")
        self.assertNotIn("submit_order", text)
        self.assertNotIn("get_adapter", text)
        self.assertNotIn('add_argument("--live"', text)

    def test_production_approval_default_remains_and_revenue_is_opt_in(self) -> None:
        wrapper = (ROOT / "scripts" / "run_live.sh").read_text(encoding="utf-8")
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn('BGL_REQUIRE_APPROVAL="${BGL_REQUIRE_APPROVAL:-1}"', wrapper)
        self.assertIn('${BGL_REVENUE_POC_ENABLED:-0}', wrapper)
        self.assertIn("BGL_REVENUE_POC_ENABLED=0", example)


if __name__ == "__main__":
    unittest.main()
