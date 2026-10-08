#!/usr/bin/env python3
"""Explicit, reversible launchd cutover to the one customer publisher.

Never invoked by the publisher. Operators must pass --apply with an immutable,
clean release checkout and its expected SHA. The default invocation prints help.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

OLD = (
    "com.swarmaxis.parallax-nfl-publisher",
    "com.swarmaxis.parallax-cfb-publisher",
    "com.swarmaxis.parallax-mlb-publisher",
)
NEW = "com.swarmaxis.parallax-customer-publisher"
LABELS = (*OLD, NEW)
DAEMONS = Path("/Library/LaunchDaemons")
DEFAULT_USER = os.environ.get("SUDO_USER") or Path.home().name
DEFAULT_BACKUPS = Path("/Users") / DEFAULT_USER / "Library/Application Support/SwarmEdge/backups"


class Deployment:
    def __init__(self, daemons: Path = DAEMONS, backups: Path = DEFAULT_BACKUPS):
        self.daemons, self.backups = daemons, backups

    def command(self, *argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(argv, text=True, capture_output=True)
        if check and result.returncode:
            raise RuntimeError(f"{argv[0]} failed: {(result.stderr or result.stdout).strip()[:400]}")
        return result

    def plist(self, label: str) -> Path:
        return self.daemons / f"{label}.plist"

    def loaded(self, label: str) -> bool:
        return self.command("/bin/launchctl", "print", f"system/{label}", check=False).returncode == 0

    def bootout(self, label: str) -> None:
        if self.loaded(label):
            self.command("/bin/launchctl", "bootout", f"system/{label}")

    def bootstrap(self, label: str) -> None:
        self.command("/bin/launchctl", "bootstrap", "system", str(self.plist(label)))

    def preflight(self, release: Path, sha: str, worktree: Path, user: str) -> None:
        if not release.is_absolute() or not worktree.is_absolute() or len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("absolute release/worktree paths and full expected SHA required")
        publisher = release / "scripts/parallax_customer_publisher.py"
        if not publisher.is_file():
            raise ValueError("release has no canonical publisher")
        if self.command("/usr/bin/git", "-C", str(release), "rev-parse", "HEAD").stdout.strip() != sha:
            raise ValueError("release SHA differs from expected SHA")
        if self.command("/usr/bin/git", "-C", str(release), "status", "--porcelain").stdout.strip():
            raise ValueError("release is dirty")
        if self.command("/usr/bin/git", "-C", str(worktree), "branch", "--show-current").stdout.strip() != "parallax-live-data":
            raise ValueError("wrong customer worktree branch")
        if self.command("/usr/bin/git", "-C", str(worktree), "status", "--porcelain", "--untracked-files=all").stdout.strip():
            raise ValueError("customer worktree is dirty")
        local = self.command("/usr/bin/git", "-C", str(worktree), "rev-parse", "HEAD").stdout.strip()
        remote = self.command("/usr/bin/git", "-C", str(worktree), "ls-remote", "--heads", "origin", "parallax-live-data").stdout.split()[0]
        if local != remote:
            raise ValueError("customer worktree is behind/ahead of remote")
        state = Path("/Users") / user / "Library/Application Support/SwarmEdge/state"
        self.command("/usr/bin/python3", str(publisher), "--dry-run", "--worktree", str(worktree),
                     "--public-feed", str(state / "parallax/public_feed"), "--db", str(state / "parallax_inbox.sqlite"))

    def new_plist(self, release: Path, user: str, sha: str) -> bytes:
        log_dir = Path("/Users") / user / "Library/Logs/SwarmEdge"
        return plistlib.dumps({
            "Label": NEW, "UserName": user, "RunAtLoad": True,
            "StartInterval": 60, "ProcessType": "Background",
            "WorkingDirectory": str(release),
            "EnvironmentVariables": {"HOME": str(Path("/Users") / user),
                                     "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                                     "PARALLAX_PUBLISHER_SHA": sha},
            "ProgramArguments": ["/usr/bin/python3", str(release / "scripts/parallax_customer_publisher.py")],
            "StandardOutPath": str(log_dir / "parallax-customer-publisher.log"),
            "StandardErrorPath": str(log_dir / "parallax-customer-publisher.err.log"),
        })

    def verify_health(self, user: str, started: datetime, sha: str) -> None:
        health_path = Path("/Users") / user / "Library/Application Support/SwarmEdge/state/parallax/customer_publisher_health.json"
        for _ in range(15):
            try:
                health = json.loads(health_path.read_text())
                checked = datetime.fromisoformat(health["checked_at"])
                if checked >= started:
                    if health.get("state") == "HEALTHY" and health.get("publisher_release_sha") == sha and all(
                        health.get("lanes", {}).get(lane, {}).get("source_buy") ==
                        health.get("lanes", {}).get(lane, {}).get("customer_buy")
                        for lane in ("nfl", "cfb", "mlb")
                    ):
                        return
                    raise RuntimeError(f"publisher health failed: {health.get('error', 'count mismatch')}")
            except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError):
                pass
            time.sleep(2)
        raise RuntimeError("publisher did not write healthy state after cutover")

    def restore(self, backup: Path, loaded: list[str]) -> None:
        for label in LABELS:
            self.bootout(label)
        for label in LABELS:
            target = self.plist(label)
            if target.exists():
                target.unlink()
            saved = backup / target.name
            if saved.exists():
                shutil.copy2(saved, target)
        for label in loaded:
            if self.plist(label).exists():
                self.bootstrap(label)

    def apply(self, release: Path, sha: str, worktree: Path, user: str) -> Path:
        self.preflight(release, sha, worktree, user)
        backup = self.backups / ("parallax-customer-cutover-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        backup.mkdir(parents=True, exist_ok=False)
        loaded = [label for label in LABELS if self.loaded(label)]
        for label in LABELS:
            source = self.plist(label)
            if source.exists():
                shutil.copy2(source, backup / source.name)
        (backup / "manifest.json").write_text(json.dumps({"release_sha": sha, "loaded": loaded}, indent=2) + "\n")
        try:
            for label in LABELS:
                self.bootout(label)
            for label in OLD:
                self.plist(label).unlink(missing_ok=True)
                script = label.removeprefix("com.swarmaxis.parallax-").removesuffix("-publisher")
                if self.command("/usr/bin/pgrep", "-f", f"/parallax-publisher/publish_{script}.py", check=False).returncode == 0:
                    raise RuntimeError(f"legacy publisher process still running: {label}")
            if self.command("/usr/bin/pgrep", "-f", "/parallax-publisher/publish_customer_unified.py", check=False).returncode == 0:
                raise RuntimeError("previous unified publisher process still running")
            target = self.plist(NEW)
            temporary = target.with_suffix(".plist.tmp")
            temporary.write_bytes(self.new_plist(release, user, sha))
            os.chmod(temporary, 0o644)
            os.replace(temporary, target)
            started = datetime.now(timezone.utc)
            self.bootstrap(NEW)
            for label in OLD:
                if self.loaded(label):
                    raise RuntimeError(f"legacy publisher still loaded: {label}")
            if not self.loaded(NEW):
                raise RuntimeError("canonical publisher did not load")
            self.verify_health(user, started, sha)
        except Exception:
            self.restore(backup, loaded)
            raise
        return backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--apply", action="store_true")
    group.add_argument("--rollback", type=Path, metavar="BACKUP_DIR")
    parser.add_argument("--release-dir", type=Path)
    parser.add_argument("--sha")
    parser.add_argument("--worktree", type=Path, default=Path.home() / "swarm-runtime/swarm-edge-live-data")
    parser.add_argument("--user", default=DEFAULT_USER)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("run explicitly as root after review")
    backups = Path("/Users") / args.user / "Library/Application Support/SwarmEdge/backups"
    deploy = Deployment(backups=backups)
    if args.rollback:
        backup = args.rollback.resolve()
        if backup.parent != backups.resolve() or not (backup / "manifest.json").is_file():
            parser.error("rollback requires a cutover backup in the backup directory")
        manifest = json.loads((backup / "manifest.json").read_text())
        deploy.restore(backup, manifest["loaded"])
        print(f"ROLLED_BACK {backup}")
    else:
        if not args.release_dir or not args.sha:
            parser.error("--apply requires --release-dir and --sha")
        backup = deploy.apply(args.release_dir.resolve(), args.sha, args.worktree.resolve(), args.user)
        print(f"CUTOVER_OK backup={backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
