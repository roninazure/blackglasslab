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

    def test_installed_wrapper_uses_active_release_from_any_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime_root = root / "external-runtime"
            wrapper_dir = runtime_root / "bin"
            release = runtime_root / "releases" / "release-sha"
            config_dir = runtime_root / "config"
            command_dir = root / "command-bin"
            unrelated = root / "unrelated"
            for directory in (wrapper_dir, release / "scripts", config_dir, command_dir, unrelated):
                directory.mkdir(parents=True, exist_ok=True)

            shutil.copy2(WRAPPER, wrapper_dir / "swarm-edge")
            (runtime_root / "current").symlink_to(release)
            (command_dir / "swarm-edge").symlink_to(wrapper_dir / "swarm-edge")
            (config_dir / "runtime.env").write_text(
                f"PYTHON_BIN={sys.executable}\nBGL_RUNTIME_ENV_FILE={config_dir / 'runtime.env'}\n",
                encoding="utf-8",
            )
            (release / "scripts" / "operator_console.py").write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "print(Path(__file__).resolve())\n"
                "print(' '.join(sys.argv[1:]))\n",
                encoding="utf-8",
            )

            env = os.environ.copy()
            env.pop("SWARM_EDGE_ROOT", None)
            env.pop("BGL_RUNTIME_ENV_FILE", None)
            expected_script = str((release / "scripts" / "operator_console.py").resolve())
            commands = (
                ("watch",),
                ("watch", "--snapshot"),
                ("portfolio",),
                ("positions",),
                ("revenue-status",),
            )
            working_directories = (ROOT, root, Path("/tmp"), unrelated)
            for cwd in working_directories:
                for args in commands:
                    result = subprocess.run(
                        [str(command_dir / "swarm-edge"), *args],
                        cwd=cwd,
                        env=env,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.splitlines()[0], expected_script)
                    self.assertEqual(result.stdout.splitlines()[1], " ".join(args))

    def test_wrapper_path_resolution_never_uses_home_or_working_directory(self) -> None:
        source = WRAPPER.read_text(encoding="utf-8")
        self.assertNotIn("$HOME", source)
        self.assertNotIn("${HOME", source)
        self.assertNotRegex(source, r"\bPWD\b")
        self.assertIn('WRAPPER_ROOT/current', source)

    def test_wrapper_refuses_unmanaged_run(self) -> None:
        result = subprocess.run([str(WRAPPER), "run"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 64)
        self.assertIn("Refusing unmanaged runner start", result.stderr)

    def test_plist_template_lints(self) -> None:
        result = subprocess.run(
            ["plutil", "-lint", str(PLIST_TEMPLATE)],
            capture_output=True,
            text=True,
            check=False,
        )
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
