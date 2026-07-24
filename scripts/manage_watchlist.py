#!/usr/bin/env python3
"""Audit and rebuild the Polymarket watchlist under institutional policy."""
from __future__ import annotations

import argparse
import json
import math
import shutil
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.polymarket_adapter import PolymarketAdapter
from market_universe.policy import (
    ACCEPTABLE_RESEARCH,
    BANNED_JUNK,
    INSTITUTIONAL_CORE,
    SPECULATIVE,
    UNKNOWN_REQUIRES_REVIEW,
    InstitutionalUniverseConfig,
    detect_banned_class,
    evaluate_market,
)


WATCHLIST_PATH = ROOT / "markets" / "polymarket_watchlist.json"
DB_PATH = ROOT / "memory" / "runs.sqlite"
REPORTS_DIR = ROOT / "reports"
AUDIT_JSON_PATH = REPORTS_DIR / "current_watchlist_audit.json"
AUDIT_MD_PATH = REPORTS_DIR / "current_watchlist_audit.md"
REBUILD_JSON_PATH = REPORTS_DIR / "market_universe_rebuild.json"
REBUILD_MD_PATH = REPORTS_DIR / "market_universe_rebuild.md"
ARCHIVE_DIR = ROOT / "archive" / "phase3_1_market_universe_reset"
GAMMA_BASE = "https://gamma-api.polymarket.com"
HEADERS = {
    "User-Agent": "SwarmEdge/3.1 (institutional-paper-research)",
    "Accept": "application/json",
    "Referer": "https://polymarket.com/",
}

Fetcher = Callable[[str], dict[str, Any]]
PageFetcher = Callable[[int], list[dict[str, Any]]]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_watchlist(path: Path = WATCHLIST_PATH) -> list[dict[str, str]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"watchlist must be a list: {path}")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in payload:
        if isinstance(item, str):
            market_id = item.strip()
        elif isinstance(item, dict):
            market_id = str(
                item.get("market_id") or item.get("slug") or item.get("id") or ""
            ).strip()
        else:
            market_id = ""
        if market_id and market_id not in seen:
            result.append({"market_id": market_id})
            seen.add(market_id)
    return result


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def fetch_page(offset: int) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "limit": 100,
            "offset": offset,
            "active": "true",
            "closed": "false",
            "order": "volume",
            "ascending": "false",
        }
    )
    request = urllib.request.Request(
        f"{GAMMA_BASE}/markets?{query}", headers=HEADERS
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read())
    except Exception as exc:
        print(f"  fetch error offset={offset}: {str(exc)[:180]}")
        return []
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]


def open_position_slugs(db_path: Path = DB_PATH) -> set[str]:
    if not db_path.exists():
        return set()
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT market_id FROM paper_trades "
            "WHERE status IN ('OPEN','PENDING')"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return set()
    return {str(row[0]) for row in rows if row and row[0]}


def _unknown_audit_row(
    market_id: str,
    error: str,
    config: InstitutionalUniverseConfig,
) -> dict[str, Any]:
    fallback_question = market_id.replace("-", " ").strip().capitalize() + "?"
    banned_class = detect_banned_class(fallback_question, market_id, config)
    if banned_class:
        return {
            "market_id": market_id,
            "question": fallback_question,
            "category": "unknown",
            "classification": BANNED_JUNK,
            "policy_allowed": False,
            "policy_reason": (
                "malformed_market"
                if banned_class == "malformed_market"
                else "banned_market_class"
            ),
            "institutional_quality_score": 0.0,
            "banned_class": banned_class,
            "reason_codes": ["fetch_failed", banned_class],
            "time_bucket": "unknown",
            "metrics": {},
            "fetch_error": error,
        }
    return {
        "market_id": market_id,
        "question": fallback_question,
        "category": "unknown",
        "classification": UNKNOWN_REQUIRES_REVIEW,
        "policy_allowed": False,
        "policy_reason": "unknown_requires_review",
        "institutional_quality_score": 0.0,
        "banned_class": None,
        "reason_codes": ["fetch_failed", "unknown_requires_review"],
        "time_bucket": "unknown",
        "metrics": {},
        "fetch_error": error,
    }


def audit_watchlist(
    entries: list[dict[str, str]],
    *,
    fetcher: Fetcher,
    config: InstitutionalUniverseConfig,
    now: Optional[datetime] = None,
    pause_seconds: float = 0.0,
) -> dict[str, Any]:
    audit_time = (now or now_utc()).astimezone(timezone.utc)
    records: list[dict[str, Any]] = []
    for entry in entries:
        market_id = entry["market_id"]
        try:
            market = fetcher(market_id)
            evaluation = evaluate_market(
                market,
                config=config,
                now=audit_time,
            )
            record = evaluation.as_dict()
        except Exception as exc:
            record = _unknown_audit_row(market_id, str(exc)[:300], config)
        records.append(record)
        if pause_seconds:
            time.sleep(pause_seconds)

    classification_counts = Counter(
        row["classification"] for row in records
    )
    category_counts = Counter(row["category"] for row in records)
    banned_counts = Counter(
        row["banned_class"] for row in records if row.get("banned_class")
    )
    return {
        "report_type": "current_watchlist_audit",
        "ts_utc": audit_time.isoformat(),
        "policy_mode": config.mode,
        "policy_config": config.as_dict(),
        "watchlist_size": len(entries),
        "summary": {
            "classification_counts": dict(classification_counts),
            "category_counts": dict(category_counts),
            "banned_class_counts": dict(banned_counts),
            "policy_allowed": sum(
                1 for row in records if row["policy_allowed"]
            ),
            "policy_rejected": sum(
                1 for row in records if not row["policy_allowed"]
            ),
        },
        "markets": records,
    }


def render_audit_markdown(audit: dict[str, Any]) -> str:
    summary = audit["summary"]
    classes = summary["classification_counts"]
    lines = [
        "# Current Watchlist Audit",
        "",
        f"- Timestamp: `{audit['ts_utc']}`",
        f"- Policy: `{audit['policy_mode']}`",
        f"- Markets audited: {audit['watchlist_size']}",
        f"- Policy allowed: {summary['policy_allowed']}",
        f"- Policy rejected: {summary['policy_rejected']}",
        (
            "- Classifications: "
            + ", ".join(
                f"{name}={classes.get(name, 0)}"
                for name in (
                    INSTITUTIONAL_CORE,
                    ACCEPTABLE_RESEARCH,
                    SPECULATIVE,
                    BANNED_JUNK,
                    UNKNOWN_REQUIRES_REVIEW,
                )
            )
        ),
        "",
        "## Market Decisions",
        "",
        "| Market | Classification | Category | Score | Decision reason | Banned class |",
        "| --- | --- | --- | ---: | --- | --- |",
    ]
    for row in audit["markets"]:
        lines.append(
            f"| `{row['market_id']}` | {row['classification']} | "
            f"{row['category']} | {row['institutional_quality_score']:.1f} | "
            f"{row['policy_reason']} | {row.get('banned_class') or ''} |"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "Every source watchlist entry is represented above. Fetch failures are "
            "classified as UNKNOWN_REQUIRES_REVIEW unless the slug itself proves a "
            "hard-banned class.",
        ]
    )
    return "\n".join(lines)


def _horizon_caps(target_size: int) -> dict[str, int]:
    return {
        "2-14d": max(1, math.ceil(target_size * 0.25)),
        "15-45d": max(1, math.ceil(target_size * 0.30)),
        "46-120d": max(1, math.ceil(target_size * 0.30)),
        "121-365d": max(1, math.ceil(target_size * 0.15)),
    }


def discover_market_universe(
    *,
    config: InstitutionalUniverseConfig,
    page_fetcher: PageFetcher = fetch_page,
    now: Optional[datetime] = None,
    open_positions: Optional[set[str]] = None,
    pause_seconds: float = 0.0,
) -> dict[str, Any]:
    scan_time = (now or now_utc()).astimezone(timezone.utc)
    exposures = open_positions or set()
    evaluated: list[dict[str, Any]] = []
    seen: set[str] = set()
    pages_fetched = 0

    for page in range(config.scan_pages):
        markets = page_fetcher(page * 100)
        if not markets:
            break
        pages_fetched += 1
        for market in markets:
            market_id = str(
                market.get("slug") or market.get("market_id") or ""
            ).strip()
            if not market_id or market_id in seen:
                continue
            seen.add(market_id)
            evaluation = evaluate_market(
                market,
                config=config,
                now=scan_time,
                duplicate_position=market_id in exposures,
            )
            evaluated.append(evaluation.as_dict())
        if pause_seconds:
            time.sleep(pause_seconds)

    eligible = [row for row in evaluated if row["policy_allowed"]]
    eligible.sort(
        key=lambda row: (
            row["classification"] == INSTITUTIONAL_CORE,
            row["institutional_quality_score"],
            row["metrics"].get("liquidity") or 0.0,
            row["metrics"].get("volume") or 0.0,
        ),
        reverse=True,
    )

    category_counts: Counter[str] = Counter()
    horizon_counts: Counter[str] = Counter()
    horizon_caps = _horizon_caps(config.target_size)
    selected: list[dict[str, Any]] = []
    balance_rejections: Counter[str] = Counter()

    for row in eligible:
        if len(selected) >= config.target_size:
            break
        category = row["category"]
        bucket = row["time_bucket"]
        if category_counts[category] >= config.max_per_category:
            balance_rejections["category_cap"] += 1
            continue
        if horizon_counts[bucket] >= horizon_caps.get(bucket, 0):
            balance_rejections["horizon_cap"] += 1
            continue
        selected.append(row)
        category_counts[category] += 1
        horizon_counts[bucket] += 1

    rejection_reasons: Counter[str] = Counter(
        row["policy_reason"] for row in evaluated if not row["policy_allowed"]
    )
    insufficient = len(selected) < config.target_size
    return {
        "scan": {
            "pages_requested": config.scan_pages,
            "pages_fetched": pages_fetched,
            "markets_scanned": len(evaluated),
            "eligible_markets": len(eligible),
            "selected_markets": len(selected),
        },
        "selected": selected,
        "selection_summary": {
            "category_counts": dict(category_counts),
            "horizon_counts": dict(horizon_counts),
            "classification_counts": dict(
                Counter(row["classification"] for row in selected)
            ),
            "rejection_reasons": dict(rejection_reasons),
            "balance_rejections": dict(balance_rejections),
            "insufficient_clean_markets": insufficient,
            "insufficient_reason": (
                "insufficient_clean_markets" if insufficient else None
            ),
        },
        "evaluated": evaluated,
    }


def build_rebuild_report(
    *,
    audit: dict[str, Any],
    discovery: dict[str, Any],
    config: InstitutionalUniverseConfig,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    report_time = (now or now_utc()).astimezone(timezone.utc)
    old_classes = audit["summary"]["classification_counts"]
    old_unacceptable = sum(
        old_classes.get(name, 0)
        for name in (SPECULATIVE, BANNED_JUNK, UNKNOWN_REQUIRES_REVIEW)
    )
    selected = discovery["selected"]
    materially_cleaner = (
        bool(selected)
        and old_unacceptable > 0
        and all(row["policy_allowed"] for row in selected)
        and not any(row.get("banned_class") for row in selected)
    )
    selected_ids = {row["market_id"] for row in selected}
    old_ids = {row["market_id"] for row in audit["markets"]}
    return {
        "report_type": "market_universe_rebuild",
        "ts_utc": report_time.isoformat(),
        "policy_mode": config.mode,
        "policy_config": config.as_dict(),
        "dry_run": True,
        "applied": False,
        "materially_cleaner": materially_cleaner,
        "old_watchlist_size": audit["watchlist_size"],
        "new_watchlist_size": len(selected),
        "removed_market_ids": sorted(old_ids - selected_ids),
        "retained_market_ids": sorted(old_ids & selected_ids),
        "added_market_ids": sorted(selected_ids - old_ids),
        "scan": discovery["scan"],
        "selection_summary": discovery["selection_summary"],
        "selected_markets": selected,
        "apply_backup_path": None,
        "apply_blocked_reason": None,
    }


def render_rebuild_markdown(report: dict[str, Any]) -> str:
    summary = report["selection_summary"]
    lines = [
        "# Market Universe Rebuild",
        "",
        f"- Timestamp: `{report['ts_utc']}`",
        f"- Policy: `{report['policy_mode']}`",
        f"- Mode: `{'APPLIED' if report['applied'] else 'DRY_RUN'}`",
        f"- Materially cleaner: `{str(report['materially_cleaner']).lower()}`",
        f"- Old watchlist: {report['old_watchlist_size']}",
        f"- New watchlist: {report['new_watchlist_size']}",
        f"- Markets scanned: {report['scan']['markets_scanned']}",
        f"- Eligible before balance: {report['scan']['eligible_markets']}",
        f"- Insufficient clean markets: `{str(summary['insufficient_clean_markets']).lower()}`",
        "",
        "## Selected Universe",
        "",
        "| Market | Classification | Category | Horizon | Score |",
        "| --- | --- | --- | --- | ---: |",
    ]
    for row in report["selected_markets"]:
        lines.append(
            f"| `{row['market_id']}` | {row['classification']} | "
            f"{row['category']} | {row['time_bucket']} | "
            f"{row['institutional_quality_score']:.1f} |"
        )
    if not report["selected_markets"]:
        lines.append("| _No clean markets selected_ | | | | |")
    lines.extend(
        [
            "",
            "## Balance",
            "",
            "- Categories: "
            + json.dumps(summary["category_counts"], sort_keys=True),
            "- Horizons: "
            + json.dumps(summary["horizon_counts"], sort_keys=True),
            "- Policy rejections: "
            + json.dumps(summary["rejection_reasons"], sort_keys=True),
            "",
            "## Changes",
            "",
            f"- Removed: {len(report['removed_market_ids'])}",
            f"- Retained: {len(report['retained_market_ids'])}",
            f"- Added: {len(report['added_market_ids'])}",
        ]
    )
    if report.get("apply_backup_path"):
        lines.append(f"- Apply backup: `{report['apply_backup_path']}`")
    if report.get("apply_blocked_reason"):
        lines.append(f"- Apply blocked: `{report['apply_blocked_reason']}`")
    if summary["insufficient_clean_markets"]:
        lines.extend(
            [
                "",
                "The policy intentionally produced a smaller watchlist rather than "
                "filling remaining slots with speculative or banned markets.",
            ]
        )
    return "\n".join(lines)


def apply_rebuild(
    selected: list[dict[str, Any]],
    *,
    watchlist_path: Path = WATCHLIST_PATH,
    archive_dir: Path = ARCHIVE_DIR,
    now: Optional[datetime] = None,
) -> Path:
    apply_time = (now or now_utc()).astimezone(timezone.utc)
    archive_dir.mkdir(parents=True, exist_ok=True)
    timestamp = apply_time.strftime("%Y%m%dT%H%M%SZ")
    backup_path = archive_dir / f"polymarket_watchlist.pre_apply.{timestamp}.json"
    if watchlist_path.exists():
        shutil.copy2(watchlist_path, backup_path)
    else:
        backup_path.write_text("[]\n", encoding="utf-8")

    payload = [{"market_id": row["market_id"]} for row in selected]
    watchlist_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = watchlist_path.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(watchlist_path)
    return backup_path


def write_audit_reports(audit: dict[str, Any]) -> None:
    _write_json(AUDIT_JSON_PATH, audit)
    _write_text(AUDIT_MD_PATH, render_audit_markdown(audit))


def write_rebuild_reports(report: dict[str, Any]) -> None:
    _write_json(REBUILD_JSON_PATH, report)
    _write_text(REBUILD_MD_PATH, render_rebuild_markdown(report))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply only when the generated report is materially cleaner.",
    )
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help="Audit every current watchlist market without discovery.",
    )
    parser.add_argument("--pages", type=int, default=None)
    parser.add_argument("--target-size", type=int, default=None)
    args = parser.parse_args()

    config = InstitutionalUniverseConfig.from_env()
    if args.pages is not None or args.target_size is not None:
        values = config.as_dict()
        if args.pages is not None:
            values["scan_pages"] = max(0, args.pages)
        if args.target_size is not None:
            values["target_size"] = max(0, args.target_size)
        values["allowed_categories"] = tuple(values["allowed_categories"])
        values["novelty_whitelist"] = tuple(values["novelty_whitelist"])
        config = InstitutionalUniverseConfig(**values)

    current = load_watchlist()
    adapter = PolymarketAdapter()
    print(
        f"MARKET UNIVERSE - {now_utc().strftime('%Y-%m-%d %H:%M UTC')} "
        f"policy={config.mode}"
    )
    print(f"Auditing {len(current)} current markets...")
    audit = audit_watchlist(
        current,
        fetcher=adapter.get_market,
        config=config,
        pause_seconds=0.05,
    )
    write_audit_reports(audit)
    print(
        f"Audit: allowed={audit['summary']['policy_allowed']} "
        f"rejected={audit['summary']['policy_rejected']}"
    )
    print(f"Audit reports: {AUDIT_MD_PATH} {AUDIT_JSON_PATH}")

    if args.audit_only:
        return 0

    print(
        f"Scanning up to {config.scan_pages * 100} active markets; "
        f"target={config.target_size}..."
    )
    discovery = discover_market_universe(
        config=config,
        open_positions=open_position_slugs(),
        pause_seconds=0.05,
    )
    report = build_rebuild_report(
        audit=audit,
        discovery=discovery,
        config=config,
    )
    write_rebuild_reports(report)
    print(
        f"Dry run: scanned={report['scan']['markets_scanned']} "
        f"eligible={report['scan']['eligible_markets']} "
        f"selected={report['new_watchlist_size']} "
        f"materially_cleaner={report['materially_cleaner']}"
    )

    if not args.apply:
        print("DRY RUN - watchlist unchanged.")
        print(f"Rebuild reports: {REBUILD_MD_PATH} {REBUILD_JSON_PATH}")
        return 0

    if not report["materially_cleaner"]:
        report["apply_blocked_reason"] = "dry_run_not_materially_cleaner"
        write_rebuild_reports(report)
        print("APPLY BLOCKED - dry run was not materially cleaner.")
        return 2

    backup_path = apply_rebuild(report["selected_markets"])
    report["dry_run"] = False
    report["applied"] = True
    report["apply_backup_path"] = str(backup_path.relative_to(ROOT))
    write_rebuild_reports(report)
    print(
        f"APPLIED - watchlist now contains {report['new_watchlist_size']} "
        f"clean markets."
    )
    print(f"Backup: {backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
