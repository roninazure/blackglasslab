#!/usr/bin/env python3
"""Generate and validate deterministic release deployment manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_sha(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError:
        # Immutable releases are produced with git archive and intentionally
        # contain no .git directory. Their directory name is the authoritative
        # full commit SHA.
        candidate = root.resolve().name.lower()
        if len(candidate) == 40 and all(
            char in "0123456789abcdef" for char in candidate
        ):
            return candidate
        raise


def build_manifest(root: Path, runtime_env: Path, plist: Path, lock: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "git_sha": git_sha(root),
        "python_version": platform.python_version(),
        "dependency_lock_sha256": sha256_file(lock),
        "runtime_config_sha256": sha256_file(runtime_env),
        "launchd_plist_sha256": sha256_file(plist),
    }


def generate(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    manifest = build_manifest(
        root,
        Path(args.runtime_env).resolve(),
        Path(args.plist).resolve(),
        Path(args.lock).resolve(),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"deployment manifest written: {output}")
    return 0


def validate(args: argparse.Namespace) -> int:
    expected = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    actual = build_manifest(
        Path(args.root).resolve(),
        Path(args.runtime_env).resolve(),
        Path(args.plist).resolve(),
        Path(args.lock).resolve(),
    )
    if expected != actual:
        print("deployment manifest validation failed")
        return 1
    print("deployment manifest valid")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for name, func in (("generate", generate), ("validate", validate)):
        cmd = sub.add_parser(name)
        cmd.set_defaults(func=func)
        cmd.add_argument("--root", default=".")
        cmd.add_argument("--runtime-env", required=True)
        cmd.add_argument("--plist", required=True)
        cmd.add_argument("--lock", required=True)
        if name == "generate":
            cmd.add_argument("--output", required=True)
        else:
            cmd.add_argument("--manifest", required=True)
    return p


if __name__ == "__main__":
    parsed = parser().parse_args()
    raise SystemExit(parsed.func(parsed))
