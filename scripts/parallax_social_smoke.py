#!/usr/bin/env python3
"""Perform exactly one explicitly authorized operator X publication."""

from __future__ import annotations

import argparse

from parallax.social import SocialPublisher


def main() -> int:
    parser = argparse.ArgumentParser(description="One controlled PARALLAX operator X publication")
    parser.add_argument("--platform", required=True, choices=("x",), help="Publication platform (X only)")
    parser.add_argument("--live", action="store_true", help="Explicitly authorize the live publication")
    parser.add_argument("--text", required=True, help="Exact text to publish")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; refusing to publish")
    result = SocialPublisher().publish_operator_smoke(args.text)
    if result.status == "SENT":
        print("X operator smoke publication sent.")
        return 0
    print("X operator smoke publication failed: " + (result.error_summary or result.error_code or result.status))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
