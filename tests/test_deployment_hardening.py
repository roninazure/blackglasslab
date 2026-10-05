from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPORTS_PLIST_TEMPLATE = ROOT / "deploy" / "launchd" / "com.swarmedge.parallax-sports.plist.in"


def test_parallax_sports_plist_template_lints() -> None:
    result = subprocess.run(
        ["plutil", "-lint", str(SPORTS_PLIST_TEMPLATE)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_parallax_sports_plist_owns_unattended_runner() -> None:
    source = SPORTS_PLIST_TEMPLATE.read_text(encoding="utf-8")
    assert "com.swarmedge.parallax-sports" in source
    assert "@SWARM_EDGE_ROOT@/scripts/parallax_unattended.py" in source
    assert "@SWARM_EDGE_PYTHON@" in source
    assert "@PARALLAX_STATE_DIR@" in source
    assert "@RELEASE_SHA@" in source
    assert "<string>5</string>" in source


def test_manifest_generation_is_deterministic() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        runtime_env = root / ".env.runtime"
        plist = root / "parallax-sports.plist"
        lock = root / "requirements.lock"
        runtime_env.write_text("PARALLAX_ALERT_MODE=ntfy\n", encoding="utf-8")
        plist.write_text(
            SPORTS_PLIST_TEMPLATE.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
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
                    "--entrypoint",
                    str(ROOT / "scripts" / "parallax_unattended.py"),
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, result.stderr
            outputs.append(output.read_bytes())

        assert outputs[0] == outputs[1]
        manifest = json.loads(outputs[0])
        assert manifest["dependency_lock_sha256"] == hashlib.sha256(
            lock.read_bytes()
        ).hexdigest()


def test_dependency_metadata_is_pinned_and_reproducible() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lock_lines = [
        line
        for line in (ROOT / "requirements.lock").read_text().splitlines()
        if line
    ]
    assert 'requires-python = ">=3.12,<3.13"' in pyproject
    assert lock_lines == sorted(lock_lines, key=str.casefold)
    assert all("==" in line for line in lock_lines)


def test_manifest_git_sha_falls_back_to_immutable_release_name(tmp_path) -> None:
    from scripts.deployment_manifest import git_sha

    sha = "a" * 40
    release = tmp_path / sha
    release.mkdir()

    assert git_sha(release) == sha
