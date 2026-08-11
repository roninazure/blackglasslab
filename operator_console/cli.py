from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from rich.console import Console

from operator_console.data import OperatorDataSource
from operator_console.render import render_command

COMMANDS = ("watch", "portfolio", "positions", "revenue-status", "discovery-breakdown", "alpha-leaderboard", "investment-report")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Swarm Edge read-only operator console"
    )
    subparsers = result.add_subparsers(dest="command", required=True)
    watch = subparsers.add_parser(
        "watch", help="Open the live full-screen operator console"
    )
    watch.add_argument(
        "--snapshot",
        action="store_true",
        help="Render one non-interactive Revenue status snapshot and exit",
    )
    subparsers.add_parser("portfolio", help="Print a concise Revenue portfolio summary")
    subparsers.add_parser("positions", help="Print the Revenue positions table")
    subparsers.add_parser("revenue-status", help="Print detailed Revenue POC health")
    subparsers.add_parser(
        "discovery-breakdown", help="Print the latest read-only discovery funnel"
    )
    subparsers.add_parser(
        "alpha-leaderboard", help="Print the read-only realized-alpha scoreboard"
    )
    subparsers.add_parser(
        "investment-report", help="Print the read-only institutional operating report"
    )
    return result


def main(
    argv: Sequence[str] | None = None, *, source: OperatorDataSource | None = None
) -> int:
    args = parser().parse_args(argv)
    data_source = source or OperatorDataSource()
    if args.command == "watch" and not args.snapshot:
        from operator_console.app import run_console

        run_console(data_source)
        return 0

    snapshot = data_source.read(include_logs=True, include_reports=True)
    command = "revenue-status" if args.command == "watch" else args.command
    render_command(command, snapshot, Console())
    return 0 if snapshot.system.database_status.startswith("OK") else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
