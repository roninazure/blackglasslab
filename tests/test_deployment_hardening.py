from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "bin" / "swarm-edge"
PLIST_TEMPLATE = ROOT / "deploy" / "launchd" / "com.swarmedge.runner.plist.in"


class DeploymentHardeningTests(unittest.TestCase):
    def test_wrapper_uses_launchctl_and_has_no_broad_process_control(self) -> None:
        source = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("launchctl kickstart", source)
        self.assertIn("launchctl kill", source)
        self.assertIn("launchctl print", source)
        self.assertNotRegex(source, r"\b(?:pgrep|pkill|nohup)\b")

    def test_wrapper_status_and_start_call_the_launchd_job(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            calls = root / "launchctl.calls"
            fake_launchctl = fake_bin / "launchctl"
            fake_launchctl.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CALLS\"\n"
                "if [ \"$1\" = print ]; then echo 'state = running'; echo 'pid = 123'; fi\n",
                encoding="utf-8",
            )
            fake_launchctl.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{fake_bin}:{env['PATH']}",
                    "CALLS": str(calls),
                    "SWARM_EDGE_LAUNCHD_DOMAIN": "gui/test",
                }
            )
            for command in ("status", "start"):
                result = subprocess.run(
                    [str(WRAPPER), command], env=env, capture_output=True, text=True, check=False
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            call_text = calls.read_text(encoding="utf-8")
            self.assertIn("print gui/test/com.swarmedge.runner", call_text)
            self.assertIn("kickstart gui/test/com.swarmedge.runner", call_text)

    def test_wrapper_refuses_unmanaged_run(self) -> None:
        result = subprocess.run([str(WRAPPER), "run"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 64)
        self.assertIn("Refusing unmanaged runner start", result.stderr)

    def test_plist_template_lints(self) -> None:
        result = subprocess.run(["plutil", "-lint", str(PLIST_TEMPLATE)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_manifest_generation_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime_env = root / ".env.runtime"
            plist = root / "runner.plist"
            lock = root / "requirements.lock"
            runtime_env.write_text("BGL_PRODUCTION_MODE=1\n", encoding="utf-8")
            plist.write_text(PLIST_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
            lock.write_text("example==1.0\n", encoding="utf-8")
            outputs = []
            for name in ("one.json", "two.json"):
                output = root / name
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts" / "deployment_manifest.py"),
                        "generate",
                        "--root",
                        str(ROOT),
                        "--runtime-env",
                        str(runtime_env),
                        "--plist",
                        str(plist),
                        "--lock",
                        str(lock),
                        "--output",
                        str(output),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                outputs.append(output.read_bytes())
            self.assertEqual(outputs[0], outputs[1])
            manifest = json.loads(outputs[0])
            self.assertEqual(manifest["dependency_lock_sha256"], hashlib.sha256(lock.read_bytes()).hexdigest())

    def test_dependency_metadata_is_pinned_and_reproducible(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        lock_lines = [line for line in (ROOT / "requirements.lock").read_text().splitlines() if line]
        self.assertIn('requires-python = ">=3.12,<3.13"', pyproject)
        self.assertEqual(lock_lines, sorted(lock_lines, key=str.casefold))
        self.assertTrue(all("==" in line for line in lock_lines))


if __name__ == "__main__":
    unittest.main()
