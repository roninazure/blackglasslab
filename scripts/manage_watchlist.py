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
    POLICY_BANNED,
    POLICY_CORE,
    POLICY_RESEARCH,
    POLICY_WATCH,
    SPECULATIVE,
    UNKNOWN_REQUIRES_REVIEW,
    InstitutionalUniverseConfig,
    detect_banned_class,
    evaluate_market,
)


WATCHLIST_PATH = ROOT / "markets" / "polymarket_watchlist.json"
DB_PATH = ROOT / "memory" / "runs.sqlite"
REPORTS_DIR = ROOT / "reports"
REJECTION_JSON_PATH = REPORTS_DIR / "phase3_2_rejection_analysis.json"
REJECTION_MD_PATH = REPORTS_DIR / "phase3_2_rejection_analysis.md"
EXPANSION_JSON_PATH = REPORTS_DIR / "phase3_2_universe_expansion.json"
EXPANSION_MD_PATH = REPORTS_DIR / "phase3_2_universe_expansion.md"
ARCHIVE_DIR = (
    ROOT / "archive" / "phase3_2_institutional_discovery_expansion"
)
GAMMA_BASE = "https://gamma-api.polymarket.com"
HEADERS = {
    "User-Agent": "SwarmEdge/3.2 (institutional-paper-research)",
    "Accept": "application/json",
    "Referer": "https://polymarket.com/",
}
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

Fetcher = Callable[[str], dict[str, Any]]
PageFetcher = Callable[[int], list[dict[str, Any]]]
CandidateFetcher = Callable[
    [str, int, Optional[str]],
    list[dict[str, Any]],
]


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
                item.get("market_id")
                or item.get("slug")
                or item.get("id")
                or ""
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


def _event_context(event: dict[str, Any]) -> dict[str, Any]:
    return {
        field: event.get(field)
        for field in (
            "id",
            "slug",
            "title",
            "category",
            "subcategory",
            "endDate",
            "volume",
            "liquidity",
        )
        if event.get(field) is not None
    }


def fetch_candidate_page(
    sort_mode: str,
    offset: int,
    hint: Optional[str] = None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "limit": 100,
        "offset": offset,
        "active": "true",
        "closed": "false",
        "order": sort_mode,
        "ascending": "true" if sort_mode == "endDate" else "false",
    }
    if hint:
        params["title_search"] = hint
    request = urllib.request.Request(
        f"{GAMMA_BASE}/events?{urllib.parse.urlencode(params)}",
        headers=HEADERS,
    )
    try:
        with _OPENER.open(request, timeout=20) as response:
            payload = json.loads(response.read())
    except Exception as exc:
        print(
            f"  fetch error sort={sort_mode} offset={offset}"
            f" hint={hint or '-'}: {str(exc)[:180]}"
        )
        return []
    if not isinstance(payload, list):
        return []

    markets: list[dict[str, Any]] = []
    for event in payload:
        if not isinstance(event, dict):
            continue
        context = _event_context(event)
        event_markets = event.get("markets")
        if not isinstance(event_markets, list):
            continue
        for raw in event_markets:
            if not isinstance(raw, dict):
                continue
            market = dict(raw)
            market["_discovery_event"] = context
            markets.append(market)
    return markets


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
    banned_class = detect_banned_class(
        fallback_question,
        market_id,
        config,
    )
    tier = POLICY_BANNED
    classification = BANNED_JUNK
    reason = "unknown_requires_review"
    if banned_class:
        reason = (
            "malformed_market"
            if banned_class == "malformed_market"
            else "banned_market_class"
        )
    return {
        "market_id": market_id,
        "question": fallback_question,
        "category": "novelty/other",
        "institutional_category": "novelty/other",
        "policy_tier": tier,
        "classification": classification,
        "policy_allowed": False,
        "policy_reason": reason,
        "institutional_quality_score": 0.0,
        "banned_class": banned_class or "api_lookup_failed",
        "reason_codes": ["fetch_failed", reason],
        "time_bucket": "unknown",
        "metrics": {"metadata_missing": ["market_snapshot"]},
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
            record = evaluate_market(
                market,
                config=config,
                now=audit_time,
            ).as_dict()
        except Exception as exc:
            record = _unknown_audit_row(
                market_id,
                str(exc)[:300],
                config,
            )
        records.append(record)
        if pause_seconds:
            time.sleep(pause_seconds)

    return {
        "report_type": "current_watchlist_phase3_2_audit",
        "ts_utc": audit_time.isoformat(),
        "policy_mode": config.mode,
        "watchlist_size": len(entries),
        "summary": {
            "tier_counts": dict(Counter(row["policy_tier"] for row in records)),
            "category_counts": dict(
                Counter(row["institutional_category"] for row in records)
            ),
            "policy_allowed": sum(
                1 for row in records if row["policy_allowed"]
            ),
            "policy_rejected": sum(
                1 for row in records if not row["policy_allowed"]
            ),
        },
        "markets": records,
    }


def _horizon_caps(target_size: int) -> dict[str, int]:
    return {
        "2-14d": max(2, math.ceil(target_size * 0.30)),
        "15-45d": max(2, math.ceil(target_size * 0.30)),
        "46-120d": max(2, math.ceil(target_size * 0.30)),
        "121-365d": max(1, math.ceil(target_size * 0.20)),
    }


def discover_market_universe(
    *,
    config: InstitutionalUniverseConfig,
    candidate_fetcher: CandidateFetcher = fetch_candidate_page,
    page_fetcher: Optional[PageFetcher] = None,
    now: Optional[datetime] = None,
    open_positions: Optional[set[str]] = None,
    pause_seconds: float = 0.0,
) -> dict[str, Any]:
    scan_time = (now or now_utc()).astimezone(timezone.utc)
    exposures = open_positions or set()
    evaluated: list[dict[str, Any]] = []
    seen: set[str] = set()
    query_counts: Counter[str] = Counter()
    raw_candidates = 0

    if page_fetcher is not None:
        queries = [("fixture", None)]

        def get_page(
            _sort_mode: str,
            offset: int,
            _hint: Optional[str],
        ) -> list[dict[str, Any]]:
            return page_fetcher(offset)

    else:
        queries = [
            (sort_mode, hint)
            for sort_mode in config.sort_modes
            for hint in (config.category_hints or (None,))
        ]
        get_page = candidate_fetcher

    per_query_budget = max(
        1,
        math.ceil(config.max_candidates / max(1, len(queries))),
    )
    for sort_mode, hint in queries:
        query_key = f"{sort_mode}:{hint or 'all'}"
        query_unique = 0
        for page in range(config.max_pages):
            markets = get_page(sort_mode, page * 100, hint)
            if not markets:
                break
            query_counts[query_key] += 1
            raw_candidates += len(markets)
            for market in markets:
                market_id = str(
                    market.get("slug")
                    or market.get("market_id")
                    or ""
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
                query_unique += 1
                if query_unique >= per_query_budget:
                    break
                if len(evaluated) >= config.max_candidates:
                    break
            if (
                query_unique >= per_query_budget
                or len(evaluated) >= config.max_candidates
            ):
                break
            if pause_seconds:
                time.sleep(pause_seconds)
    eligible = [row for row in evaluated if row["policy_allowed"]]
    eligible.sort(
        key=lambda row: (
            row["policy_tier"] == POLICY_CORE,
            row["institutional_quality_score"],
            row["metrics"].get("liquidity") or 0.0,
            row["metrics"].get("volume") or 0.0,
        ),
        reverse=True,
    )

    category_counts: Counter[str] = Counter()
    event_counts: Counter[str] = Counter()
    horizon_counts: Counter[str] = Counter()
    horizon_caps = _horizon_caps(config.target_size)
    selected: list[dict[str, Any]] = []
    balance_rejections: Counter[str] = Counter()

    def event_key(row: dict[str, Any]) -> str:
        metrics = row.get("metrics") or {}
        return str(
            metrics.get("event_id")
            or metrics.get("event_slug")
            or row["market_id"]
        )

    def try_select(
        row: dict[str, Any],
        *,
        enforce_horizon_cap: bool,
    ) -> bool:
        if len(selected) >= config.target_size:
            return False
        category = row["institutional_category"]
        bucket = row["time_bucket"]
        if category_counts[category] >= config.max_per_category:
            balance_rejections["category_cap"] += 1
            return False
        key = event_key(row)
        if event_counts[key] >= config.max_per_event:
            balance_rejections["event_cap"] += 1
            return False
        if (
            enforce_horizon_cap
            and horizon_counts[bucket] >= horizon_caps.get(bucket, 0)
        ):
            balance_rejections["horizon_cap"] += 1
            return False
        selected.append(row)
        category_counts[category] += 1
        horizon_counts[bucket] += 1
        event_counts[key] += 1
        return True

    for row in eligible:
        try_select(row, enforce_horizon_cap=True)
        if len(selected) >= config.target_size:
            break

    selected_ids = {row["market_id"] for row in selected}
    for row in eligible:
        if row["market_id"] in selected_ids:
            continue
        if try_select(row, enforce_horizon_cap=False):
            selected_ids.add(row["market_id"])
        if len(selected) >= config.target_size:
            break

    if config.include_watch_in_watchlist:
        watch_rows = [
            row for row in evaluated if row["policy_tier"] == POLICY_WATCH
        ]
        watch_rows.sort(
            key=lambda row: row["institutional_quality_score"],
            reverse=True,
        )
        for row in watch_rows:
            if len(selected) >= config.target_size:
                break
            category = row["institutional_category"]
            if category_counts[category] >= config.max_per_category:
                continue
            key = event_key(row)
            if event_counts[key] >= config.max_per_event:
                continue
            selected.append(row)
            category_counts[category] += 1
            event_counts[key] += 1

    tier_counts = Counter(row["policy_tier"] for row in evaluated)
    selected_tiers = Counter(row["policy_tier"] for row in selected)
    insufficient = len(selected) < config.target_size
    return {
        "scan": {
            "queries": [
                {"sort_mode": sort_mode, "category_hint": hint}
                for sort_mode, hint in queries
            ],
            "pages_fetched_by_query": dict(query_counts),
            "raw_candidates": raw_candidates,
            "unique_markets_scanned": len(evaluated),
            "eligible_core": tier_counts.get(POLICY_CORE, 0),
            "eligible_research": tier_counts.get(POLICY_RESEARCH, 0),
            "watch_markets": tier_counts.get(POLICY_WATCH, 0),
            "banned_markets": tier_counts.get(POLICY_BANNED, 0),
            "eligible_markets": len(eligible),
            "selected_markets": len(selected),
        },
        "selected": selected,
        "selection_summary": {
            "category_counts": dict(category_counts),
            "horizon_counts": dict(horizon_counts),
            "event_counts": dict(event_counts),
            "tier_counts": dict(selected_tiers),
            "classification_counts": dict(
                Counter(row["classification"] for row in selected)
            ),
            "balance_rejections": dict(balance_rejections),
            "insufficient_clean_markets": insufficient,
            "insufficient_reason": (
                "insufficient_clean_markets" if insufficient else None
            ),
        },
        "evaluated": evaluated,
    }


_ANALYSIS_REASON_MAP = {
    "category_ban": {
        "sports_prop",
        "thin_local_primary",
        "entertainment_celebrity",
        "novelty_meme",
        "product_release",
        "product_release_comparison",
    },
    "unknown_category": {"unknown_category"},
    "missing_or_unclear_deadline": {"missing_resolution_deadline"},
    "liquidity_below_threshold": {"low_liquidity"},
    "volume_below_threshold": {"low_volume"},
    "spread_too_wide": {"wide_spread"},
    "probability_out_of_band": {"probability_out_of_band"},
    "weak_resolution_quality": {
        "weak_resolution_quality",
        "unclean_binary_resolution",
    },
    "malformed_question": {"malformed_market"},
    "duplicate_exposure": {"duplicate_position"},
    "horizon_outside_range": {
        "horizon_outside_range",
        "long_horizon_category_not_allowed",
    },
    "quality_score_below_threshold": {
        "institutional_score_below_minimum"
    },
    "api_metadata_missing": {
        "missing_resolution_deadline",
        "missing_market_probability",
        "missing_executable_spread",
    },
}


def build_rejection_analysis(
    discovery: dict[str, Any],
    *,
    config: InstitutionalUniverseConfig,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    analysis_time = (now or now_utc()).astimezone(timezone.utc)
    records = discovery["evaluated"]
    reason_code_counts = Counter(
        code for row in records for code in row["reason_codes"]
    )
    breakdown: dict[str, dict[str, Any]] = {}
    for label, codes in _ANALYSIS_REASON_MAP.items():
        matching = [
            row
            for row in records
            if codes.intersection(row["reason_codes"])
        ]
        breakdown[label] = {
            "count": len(matching),
            "market_ids": [row["market_id"] for row in matching[:20]],
        }

    tier_counts = Counter(row["policy_tier"] for row in records)
    categories = Counter(row["institutional_category"] for row in records)
    banned_classes = Counter(
        row["banned_class"] for row in records if row.get("banned_class")
    )
    return {
        "report_type": "phase3_2_rejection_analysis",
        "ts_utc": analysis_time.isoformat(),
        "policy_mode": config.mode,
        "markets_scanned": len(records),
        "tier_counts": dict(tier_counts),
        "institutional_category_counts": dict(categories),
        "reason_code_counts": dict(reason_code_counts),
        "banned_class_counts": dict(banned_classes),
        "rejection_breakdown": breakdown,
        "root_causes": [
            (
                "Phase 3.1 used question-only keyword classification, leaving "
                "most active markets as novelty/other."
            ),
            (
                "Discovery used one volume-sorted market query and discarded "
                "event category/title context."
            ),
            (
                "One threshold set forced serious near-threshold markets into "
                "the same rejection path as weak markets."
            ),
            (
                "The previous malformed check treated missing terminal "
                "punctuation as malformed even when grammar was otherwise clear."
            ),
        ],
    }


def render_rejection_markdown(analysis: dict[str, Any]) -> str:
    lines = [
        "# Phase 3.2 Rejection Analysis",
        "",
        f"- Timestamp: `{analysis['ts_utc']}`",
        f"- Policy: `{analysis['policy_mode']}`",
        f"- Unique markets scanned: {analysis['markets_scanned']}",
        "- Tier counts: "
        + json.dumps(analysis["tier_counts"], sort_keys=True),
        "",
        "## Root Causes",
        "",
    ]
    lines.extend(f"- {cause}" for cause in analysis["root_causes"])
    lines.extend(
        [
            "",
            "## Rejection Breakdown",
            "",
            "| Cause | Count |",
            "| --- | ---: |",
        ]
    )
    for label, payload in analysis["rejection_breakdown"].items():
        lines.append(f"| {label} | {payload['count']} |")
    lines.extend(
        [
            "",
            "Counts overlap because one market can fail multiple quality "
            "requirements. Hard-banned classes remain separate from WATCH "
            "quality failures.",
            "",
            "## Institutional Categories",
            "",
            "```json",
            json.dumps(
                analysis["institutional_category_counts"],
                indent=2,
                sort_keys=True,
            ),
            "```",
        ]
    )
    return "\n".join(lines)


def build_expansion_report(
    *,
    audit: dict[str, Any],
    discovery: dict[str, Any],
    rejection_analysis: dict[str, Any],
    config: InstitutionalUniverseConfig,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    report_time = (now or now_utc()).astimezone(timezone.utc)
    selected = discovery["selected"]
    old_ids = {row["market_id"] for row in audit["markets"]}
    selected_ids = {row["market_id"] for row in selected}
    selected_scores = [
        float(row["institutional_quality_score"]) for row in selected
    ]
    selected_has_banned = any(
        row["policy_tier"] == POLICY_BANNED
        or row.get("banned_class")
        or not row["policy_allowed"]
        for row in selected
    )
    materially_better = (
        len(selected) > audit["watchlist_size"]
        and not selected_has_banned
        and all(
            row["policy_tier"] in {POLICY_CORE, POLICY_RESEARCH}
            for row in selected
        )
    )
    return {
        "report_type": "phase3_2_universe_expansion",
        "ts_utc": report_time.isoformat(),
        "policy_mode": config.mode,
        "policy_config": config.as_dict(),
        "dry_run": True,
        "applied": False,
        "materially_better": materially_better,
        "old_watchlist_size": audit["watchlist_size"],
        "new_watchlist_size": len(selected),
        "comparison_to_phase3_1": {
            "phase3_1_watchlist_size": audit["watchlist_size"],
            "size_change": len(selected) - audit["watchlist_size"],
            "retained_market_ids": sorted(old_ids & selected_ids),
            "removed_market_ids": sorted(old_ids - selected_ids),
            "added_market_ids": sorted(selected_ids - old_ids),
        },
        "scan": discovery["scan"],
        "eligible_tier_counts": {
            POLICY_CORE: discovery["scan"]["eligible_core"],
            POLICY_RESEARCH: discovery["scan"]["eligible_research"],
            POLICY_WATCH: discovery["scan"]["watch_markets"],
            POLICY_BANNED: discovery["scan"]["banned_markets"],
        },
        "selection_summary": {
            **discovery["selection_summary"],
            "average_quality": (
                round(sum(selected_scores) / len(selected_scores), 2)
                if selected_scores
                else 0.0
            ),
            "contains_banned_class": selected_has_banned,
            "rejected_reason_distribution": rejection_analysis[
                "reason_code_counts"
            ],
        },
        "selected_markets": selected,
        "apply_backup_path": None,
        "apply_blocked_reason": None,
    }


def render_expansion_markdown(report: dict[str, Any]) -> str:
    summary = report["selection_summary"]
    tiers = report["eligible_tier_counts"]
    lines = [
        "# Phase 3.2 Institutional Universe Expansion",
        "",
        f"- Timestamp: `{report['ts_utc']}`",
        f"- Policy: `{report['policy_mode']}`",
        f"- Mode: `{'APPLIED' if report['applied'] else 'DRY_RUN'}`",
        f"- Materially better: `{str(report['materially_better']).lower()}`",
        f"- Old watchlist: {report['old_watchlist_size']}",
        f"- New watchlist: {report['new_watchlist_size']}",
        f"- Unique markets scanned: {report['scan']['unique_markets_scanned']}",
        f"- CORE eligible: {tiers.get(POLICY_CORE, 0)}",
        f"- RESEARCH eligible: {tiers.get(POLICY_RESEARCH, 0)}",
        f"- WATCH: {tiers.get(POLICY_WATCH, 0)}",
        f"- BANNED: {tiers.get(POLICY_BANNED, 0)}",
        f"- Average selected quality: {summary['average_quality']:.1f}",
        f"- Contains banned class: `{str(summary['contains_banned_class']).lower()}`",
        "",
        "## Selected Universe",
        "",
        "| Market | Tier | Category | Horizon | Quality |",
        "| --- | --- | --- | --- | ---: |",
    ]
    for row in report["selected_markets"]:
        lines.append(
            f"| `{row['market_id']}` | {row['policy_tier']} | "
            f"{row['institutional_category']} | {row['time_bucket']} | "
            f"{row['institutional_quality_score']:.1f} |"
        )
    if not report["selected_markets"]:
        lines.append("| _No clean markets selected_ | | | | |")
    lines.extend(
        [
            "",
            "## Selection Balance",
            "",
            "- Tiers: "
            + json.dumps(summary["tier_counts"], sort_keys=True),
            "- Categories: "
            + json.dumps(summary["category_counts"], sort_keys=True),
            "- Horizons: "
            + json.dumps(summary["horizon_counts"], sort_keys=True),
            "- Balance rejections: "
            + json.dumps(summary["balance_rejections"], sort_keys=True),
            "",
            "## Phase 3.1 Comparison",
            "",
            f"- Added: {len(report['comparison_to_phase3_1']['added_market_ids'])}",
            f"- Retained: {len(report['comparison_to_phase3_1']['retained_market_ids'])}",
            f"- Removed: {len(report['comparison_to_phase3_1']['removed_market_ids'])}",
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
                "The policy produced a smaller-than-target universe rather than "
                "adding WATCH or BANNED markets.",
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
    backup_path = (
        archive_dir / f"polymarket_watchlist.pre_apply.{timestamp}.json"
    )
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


def write_rejection_reports(analysis: dict[str, Any]) -> None:
    _write_json(REJECTION_JSON_PATH, analysis)
    _write_text(REJECTION_MD_PATH, render_rejection_markdown(analysis))


def write_expansion_reports(report: dict[str, Any]) -> None:
    _write_json(EXPANSION_JSON_PATH, report)
    _write_text(EXPANSION_MD_PATH, render_expansion_markdown(report))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply only when expansion is larger and contains no banned tier.",
    )
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help="Audit the current watchlist without discovery.",
    )
    parser.add_argument("--max-pages", "--pages", type=int, default=None)
    parser.add_argument("--max-candidates", type=int, default=None)
    parser.add_argument("--target-size", type=int, default=None)
    args = parser.parse_args()

    config = InstitutionalUniverseConfig.from_env()
    if any(
        value is not None
        for value in (
            args.max_pages,
            args.max_candidates,
            args.target_size,
        )
    ):
        values = config.as_dict()
        if args.max_pages is not None:
            values["max_pages"] = max(0, args.max_pages)
        if args.max_candidates is not None:
            values["max_candidates"] = max(0, args.max_candidates)
        if args.target_size is not None:
            values["target_size"] = max(0, args.target_size)
        for field in (
            "sort_modes",
            "category_hints",
            "allowed_categories",
            "novelty_whitelist",
        ):
            values[field] = tuple(values[field])
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
    print(
        "Current tiers: "
        + json.dumps(audit["summary"]["tier_counts"], sort_keys=True)
    )
    if args.audit_only:
        return 0

    print(
        f"Scanning active events across {len(config.sort_modes)} sort modes; "
        f"max_pages={config.max_pages} "
        f"max_candidates={config.max_candidates} "
        f"target={config.target_size}..."
    )
    discovery = discover_market_universe(
        config=config,
        open_positions=open_position_slugs(),
        pause_seconds=0.02,
    )
    rejection_analysis = build_rejection_analysis(
        discovery,
        config=config,
    )
    report = build_expansion_report(
        audit=audit,
        discovery=discovery,
        rejection_analysis=rejection_analysis,
        config=config,
    )
    write_rejection_reports(rejection_analysis)
    write_expansion_reports(report)

    print(
        f"Dry run: unique={report['scan']['unique_markets_scanned']} "
        f"core={report['scan']['eligible_core']} "
        f"research={report['scan']['eligible_research']} "
        f"watch={report['scan']['watch_markets']} "
        f"banned={report['scan']['banned_markets']} "
        f"selected={report['new_watchlist_size']} "
        f"materially_better={report['materially_better']}"
    )
    if not args.apply:
        print("DRY RUN - watchlist unchanged.")
        print(
            f"Reports: {REJECTION_MD_PATH} {EXPANSION_MD_PATH}"
        )
        return 0

    if not report["materially_better"]:
        report["apply_blocked_reason"] = "dry_run_not_materially_better"
        write_expansion_reports(report)
        print("APPLY BLOCKED - dry run was not materially better.")
        return 2

    backup_path = apply_rebuild(report["selected_markets"])
    report["dry_run"] = False
    report["applied"] = True
    report["apply_backup_path"] = str(backup_path.relative_to(ROOT))
    write_expansion_reports(report)
    print(
        f"APPLIED - watchlist now contains {report['new_watchlist_size']} "
        "CORE/RESEARCH markets."
    )
    print(f"Backup: {backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
