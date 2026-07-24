from __future__ import annotations

import json
import math
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional


INSTITUTIONAL_CORE = "INSTITUTIONAL_CORE"
ACCEPTABLE_RESEARCH = "ACCEPTABLE_RESEARCH"
SPECULATIVE = "SPECULATIVE"
BANNED_JUNK = "BANNED_JUNK"
UNKNOWN_REQUIRES_REVIEW = "UNKNOWN_REQUIRES_REVIEW"

POLICY_CORE = "CORE"
POLICY_RESEARCH = "RESEARCH"
POLICY_WATCH = "WATCH"
POLICY_BANNED = "BANNED"

DEFAULT_ALLOWED_CATEGORIES = (
    "macro/fed",
    "macro/econ",
    "inflation/CPI",
    "rates",
    "recession",
    "jobs/employment",
    "GDP/economic growth",
    "major elections",
    "geopolitics",
    "crypto majors",
    "commodities/energy",
    "oil/gas",
    "legal/regulatory",
    "central banks",
    "major market indices",
    "corporate/regulatory events",
    "high-liquidity clean events",
)

LONG_HORIZON_CATEGORIES = set(DEFAULT_ALLOWED_CATEGORIES)

_DISTRICT_RE = re.compile(
    r"\b(?:al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|"
    r"ma|mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|"
    r"tn|tx|ut|vt|va|wa|wv|wi|wy)[ -]?\d{1,2}\b",
    re.I,
)
_MALFORMED_PATTERNS = (
    re.compile(
        r"\bwill\b.{0,80}\b(?:invades|wins|loses|becomes|happens)\b",
        re.I,
    ),
    re.compile(r"\bwill will\b", re.I),
    re.compile(r"\?\s*\?", re.I),
)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default)).strip()))
    except (AttributeError, TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)).strip())
    except (AttributeError, TypeError, ValueError):
        return default


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if not raw:
        return default
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    return values or default


def _env_with_fallback(
    primary: str,
    fallback: str,
    default: float,
) -> float:
    if primary in os.environ:
        return _env_float(primary, default)
    return _env_float(fallback, default)


@dataclass(frozen=True)
class InstitutionalUniverseConfig:
    mode: str = "institutional_v2"
    target_size: int = 24
    max_pages: int = 8
    max_candidates: int = 5_000
    sort_modes: tuple[str, ...] = (
        "volume24hr",
        "volume",
        "liquidity",
        "endDate",
    )
    category_hints: tuple[str, ...] = ()
    core_min_liquidity: float = 25_000.0
    core_min_volume: float = 250_000.0
    core_max_spread: float = 0.02
    core_min_probability: float = 0.05
    core_max_probability: float = 0.95
    core_min_quality_score: float = 75.0
    research_min_liquidity: float = 5_000.0
    research_min_volume: float = 50_000.0
    research_max_spread: float = 0.04
    research_min_probability: float = 0.025
    research_max_probability: float = 0.975
    research_min_quality_score: float = 55.0
    max_per_category: int = 4
    max_per_event: int = 2
    min_days_to_resolution: float = 2.0
    max_days_to_resolution: float = 365.0
    require_deadline: bool = True
    allow_sports: bool = False
    allow_thin_primaries: bool = False
    include_watch_in_watchlist: bool = False
    allowed_categories: tuple[str, ...] = DEFAULT_ALLOWED_CATEGORIES
    novelty_whitelist: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "InstitutionalUniverseConfig":
        return cls(
            mode=os.environ.get(
                "BGL_MARKET_UNIVERSE_POLICY_MODE",
                "institutional_v2",
            ).strip(),
            target_size=_env_int("BGL_UNIVERSE_TARGET_SIZE", 24),
            max_pages=_env_int(
                "BGL_UNIVERSE_MAX_PAGES",
                _env_int("BGL_UNIVERSE_SCAN_PAGES", 8),
            ),
            max_candidates=_env_int("BGL_UNIVERSE_MAX_CANDIDATES", 5_000),
            sort_modes=_env_csv(
                "BGL_UNIVERSE_SORT_MODES",
                ("volume24hr", "volume", "liquidity", "endDate"),
            ),
            category_hints=_env_csv("BGL_UNIVERSE_CATEGORY_HINTS", ()),
            core_min_liquidity=_env_float(
                "BGL_UNIVERSE_CORE_MIN_LIQUIDITY",
                25_000.0,
            ),
            core_min_volume=_env_float(
                "BGL_UNIVERSE_CORE_MIN_VOLUME",
                250_000.0,
            ),
            core_max_spread=_env_float(
                "BGL_UNIVERSE_CORE_MAX_SPREAD",
                0.02,
            ),
            core_min_probability=_env_float(
                "BGL_UNIVERSE_CORE_MIN_PROBABILITY",
                0.05,
            ),
            core_max_probability=_env_float(
                "BGL_UNIVERSE_CORE_MAX_PROBABILITY",
                0.95,
            ),
            core_min_quality_score=_env_float(
                "BGL_UNIVERSE_CORE_MIN_QUALITY_SCORE",
                75.0,
            ),
            research_min_liquidity=_env_with_fallback(
                "BGL_UNIVERSE_RESEARCH_MIN_LIQUIDITY",
                "BGL_UNIVERSE_MIN_LIQUIDITY",
                5_000.0,
            ),
            research_min_volume=_env_with_fallback(
                "BGL_UNIVERSE_RESEARCH_MIN_VOLUME",
                "BGL_UNIVERSE_MIN_VOLUME",
                50_000.0,
            ),
            research_max_spread=_env_with_fallback(
                "BGL_UNIVERSE_RESEARCH_MAX_SPREAD",
                "BGL_UNIVERSE_MAX_SPREAD",
                0.04,
            ),
            research_min_probability=_env_with_fallback(
                "BGL_UNIVERSE_RESEARCH_MIN_PROBABILITY",
                "BGL_UNIVERSE_MIN_PROBABILITY",
                0.025,
            ),
            research_max_probability=_env_with_fallback(
                "BGL_UNIVERSE_RESEARCH_MAX_PROBABILITY",
                "BGL_UNIVERSE_MAX_PROBABILITY",
                0.975,
            ),
            research_min_quality_score=_env_with_fallback(
                "BGL_UNIVERSE_RESEARCH_MIN_QUALITY_SCORE",
                "BGL_UNIVERSE_MIN_QUALITY_SCORE",
                55.0,
            ),
            max_per_category=_env_int("BGL_UNIVERSE_MAX_PER_CATEGORY", 4),
            max_per_event=_env_int("BGL_UNIVERSE_MAX_PER_EVENT", 2),
            min_days_to_resolution=_env_float(
                "BGL_UNIVERSE_MIN_DAYS_TO_RESOLUTION",
                2.0,
            ),
            max_days_to_resolution=_env_float(
                "BGL_UNIVERSE_MAX_DAYS_TO_RESOLUTION",
                365.0,
            ),
            require_deadline=_env_bool(
                "BGL_UNIVERSE_REQUIRE_DEADLINE",
                True,
            ),
            allow_sports=_env_bool("BGL_UNIVERSE_ALLOW_SPORTS", False),
            allow_thin_primaries=_env_bool(
                "BGL_UNIVERSE_ALLOW_THIN_PRIMARIES",
                False,
            ),
            include_watch_in_watchlist=_env_bool(
                "BGL_UNIVERSE_INCLUDE_WATCH",
                False,
            ),
            allowed_categories=_env_csv(
                "BGL_UNIVERSE_ALLOWED_CATEGORIES",
                DEFAULT_ALLOWED_CATEGORIES,
            ),
            novelty_whitelist=_env_csv(
                "BGL_UNIVERSE_NOVELTY_WHITELIST",
                (),
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for field in (
            "sort_modes",
            "category_hints",
            "allowed_categories",
            "novelty_whitelist",
        ):
            payload[field] = list(payload[field])
        return payload


@dataclass(frozen=True)
class MarketPolicyEvaluation:
    market_id: str
    question: str
    category: str
    institutional_category: str
    policy_tier: str
    classification: str
    policy_allowed: bool
    policy_reason: str
    institutional_quality_score: float
    banned_class: Optional[str]
    reason_codes: tuple[str, ...]
    time_bucket: str
    metrics: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reason_codes"] = list(self.reason_codes)
        return payload


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_json_list(value: Any) -> Optional[list[Any]]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, list) else None
    return None


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _market_end(market: Mapping[str, Any]) -> Optional[datetime]:
    for field in ("endDate", "endDateIso", "end_date", "resolutionDate"):
        parsed = _parse_datetime(market.get(field))
        if parsed is not None:
            return parsed
    return None


def _yes_probability_and_spread(
    market: Mapping[str, Any],
) -> tuple[Optional[float], Optional[float]]:
    bid = _number(market.get("bestBid"), -1.0)
    ask = _number(market.get("bestAsk"), -1.0)
    if 0 <= bid <= ask <= 1:
        return ((bid + ask) / 2.0, ask - bid)

    outcomes = _parse_json_list(market.get("outcomes"))
    prices = _parse_json_list(market.get("outcomePrices"))
    if outcomes and prices and len(outcomes) == len(prices):
        for index, outcome in enumerate(outcomes):
            if str(outcome).strip().lower() == "yes":
                probability = _number(prices[index], -1.0)
                if 0 <= probability <= 1:
                    return probability, None
    return None, None


def has_clean_binary_resolution(market: Mapping[str, Any]) -> bool:
    outcomes = _parse_json_list(market.get("outcomes"))
    if not outcomes or len(outcomes) != 2:
        return False
    return {str(value).strip().lower() for value in outcomes} == {"yes", "no"}


def is_malformed_question(question: str) -> bool:
    text = (question or "").strip()
    if len(text) < 12:
        return True
    return any(pattern.search(text) for pattern in _MALFORMED_PATTERNS)


def _metadata_text(market: Optional[Mapping[str, Any]]) -> str:
    if not market:
        return ""
    values: list[str] = []
    for field in (
        "slug",
        "category",
        "subcategory",
        "groupItemTitle",
        "marketType",
    ):
        if market.get(field):
            values.append(str(market[field]))
    events = market.get("events")
    if isinstance(events, list):
        for event in events[:3]:
            if not isinstance(event, Mapping):
                continue
            for field in ("title", "category", "subcategory", "slug"):
                if event.get(field):
                    values.append(str(event[field]))
    event = market.get("_discovery_event")
    if isinstance(event, Mapping):
        for field in ("title", "category", "subcategory", "slug"):
            if event.get(field):
                values.append(str(event[field]))
    return " ".join(values)


def classify_institutional_category(
    question: str,
    market: Optional[Mapping[str, Any]] = None,
) -> str:
    text = f" {(question or '').lower()} {_metadata_text(market).lower()} "

    if any(
        term in text
        for term in (
            "federal reserve",
            " fomc ",
            "fed funds",
            "fed interest rate",
            "fed rate",
            "fed decrease",
            "fed increase",
        )
    ):
        return "macro/fed"
    if any(term in text for term in (" cpi ", "inflation", " pce ", "consumer price")):
        return "inflation/CPI"
    if any(
        term in text
        for term in (
            "unemployment",
            "nonfarm payroll",
            "payrolls",
            "jobs report",
            "jobless claims",
            "employment report",
            "labor force",
        )
    ):
        return "jobs/employment"
    if any(
        term in text
        for term in (
            " gdp ",
            "gross domestic product",
            "economic growth",
            "growth rate",
        )
    ):
        return "GDP/economic growth"
    if "recession" in text:
        return "recession"
    if any(
        term in text
        for term in (
            "interest rate",
            "rate cut",
            "rate hike",
            "treasury yield",
            "basis point",
            " bps ",
            "yield curve",
        )
    ):
        return "rates"
    if any(
        term in text
        for term in (
            "european central bank",
            " ecb ",
            "bank of england",
            " boe ",
            "bank of japan",
            " boj ",
            "reserve bank of australia",
            "bank of canada",
            "swiss national bank",
            "people's bank of china",
            "central bank",
        )
    ):
        return "central banks"
    if any(
        term in text
        for term in (
            "presidential election",
            "general election",
            "senate control",
            "house control",
            "control of the senate",
            "control of the house",
            "parliament",
            "referendum",
            "elected president",
            "prime minister",
            "national election",
        )
    ):
        return "major elections"
    if any(
        term in text
        for term in (
            "invasion",
            "invade",
            "ceasefire",
            "peace agreement",
            "nuclear",
            "missile",
            "sanction",
            "regime",
            "military strike",
            "armed conflict",
            " war ",
            " nato ",
            "annex",
            "diplomatic relations",
            "recognize palestine",
            "gulf cooperation council",
            "territorial control",
            "territorial acquisition",
            " greenland",
            "troop withdrawal",
        )
    ):
        return "geopolitics"
    if any(
        term in text
        for term in (
            "bitcoin",
            " btc ",
            "ethereum",
            " ether ",
            " eth ",
        )
    ):
        return "crypto majors"
    if any(
        term in text
        for term in (
            "crude oil",
            " brent ",
            " wti ",
            "oil price",
            "natural gas",
            "gas price",
            " opec ",
        )
    ):
        return "oil/gas"
    if any(
        term in text
        for term in (
            "gold price",
            "copper price",
            "commodity price",
            "energy price",
        )
    ):
        return "commodities/energy"
    if any(
        term in text
        for term in (
            "s&p 500",
            "s&p500",
            "nasdaq",
            "dow jones",
            "russell 2000",
            "nikkei 225",
            "ftse 100",
            "dax index",
            "major market index",
        )
    ):
        return "major market indices"
    if any(
        term in text
        for term in (
            "quarterly earnings",
            "quarterly revenue",
            "quarterly total net inflows",
            "merger",
            "acquisition",
            "acquire ",
            "ipo day",
            "initial public offering",
            "market cap",
            "valuation hit",
            "bankruptcy",
            "regulatory approval",
            "shareholder vote",
        )
    ):
        return "corporate/regulatory events"
    if any(
        term in text
        for term in (
            "supreme court",
            " scotus ",
            "court ruling",
            "lawsuit",
            "convicted",
            "indictment",
            " sec ",
            " cftc ",
            " ftc ",
            "department of justice",
            "regulation",
            "regulatory",
            "antitrust",
            "executive order",
        )
    ):
        return "legal/regulatory"
    if any(
        term in text
        for term in (
            "government shutdown",
            "debt ceiling",
            "national emergency",
            "trade agreement",
            "tariff rate",
            "export ban",
            "cabinet resignation",
            "impeachment",
        )
    ):
        return "high-liquidity clean events"
    if any(
        term in text
        for term in (
            "unemployment rate",
            "consumer confidence",
            "retail sales",
            "industrial production",
            "economic output",
        )
    ):
        return "macro/econ"
    return "novelty/other"


def detect_banned_class(
    question: str,
    slug: str,
    config: InstitutionalUniverseConfig,
    market: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    text = (
        f" {(question or '').lower()} {(slug or '').lower()} "
        f"{_metadata_text(market).lower()} "
    )
    if slug in config.novelty_whitelist:
        return None
    if "gta vi" in text or "gta-vi" in text or "gta 6" in text:
        return "product_release_comparison"
    if any(
        term in text
        for term in (
            " album",
            " top artist",
            " spotify",
            " netflix",
            " box office",
            " celebrity",
            " kardashian",
            " taylor swift",
            " rihanna",
            " playboi carti",
            " grammy",
            " oscar",
            " emmy",
            " movie this week",
            " tv show",
        )
    ):
        return "entertainment_celebrity"
    if any(
        term in text
        for term in (
            "will aliens",
            "aliens exist",
            "rapture",
            "second coming",
            "flat earth",
            "lizard people",
            " meme ",
            "memecoin",
        )
    ):
        return "novelty_meme"
    if any(
        term in text
        for term in (
            "gpt-6",
            "gpt 6",
            "iphone release",
            "product release",
        )
    ):
        return "product_release"
    if (
        (" primary" in text or " nominee" in text)
        and _DISTRICT_RE.search(text)
        and not config.allow_thin_primaries
    ):
        return "thin_local_primary"
    sports_terms = (
        " nba ",
        " nfl ",
        " nhl ",
        " mlb ",
        " mls ",
        " fifa ",
        "formula 1",
        " f1 ",
        "world series",
        "super bowl",
        "world cup",
        "grand prix",
        "playoff",
        "goalscorer",
        "touchdown",
        "points scored",
        " esports",
        " e-sports",
        "counter-strike",
        " dota ",
        " league of legends",
        " lcs ",
        " cricket",
    )
    if any(term in text for term in sports_terms) and not config.allow_sports:
        return "sports_prop"
    if is_malformed_question(question):
        return "malformed_market"
    return None


def _time_bucket(days: Optional[float]) -> str:
    if days is None:
        return "unknown"
    if 2 <= days <= 14:
        return "2-14d"
    if 14 < days <= 45:
        return "15-45d"
    if 45 < days <= 120:
        return "46-120d"
    if 120 < days <= 365:
        return "121-365d"
    if days < 2:
        return "under-2d"
    return "over-365d"


def _log_quality(value: float, floor: float, target: float, points: float) -> float:
    if value <= floor:
        return 0.0
    ratio = math.log10(value / floor) / math.log10(target / floor)
    return points * min(1.0, max(0.0, ratio))


def _quality_score(
    *,
    liquidity: float,
    volume: float,
    spread: Optional[float],
    probability: Optional[float],
    days: Optional[float],
    category: str,
    config: InstitutionalUniverseConfig,
) -> float:
    liquidity_score = _log_quality(
        liquidity,
        config.research_min_liquidity,
        150_000.0,
        20.0,
    )
    volume_score = _log_quality(
        volume,
        config.research_min_volume,
        5_000_000.0,
        20.0,
    )
    spread_score = (
        0.0
        if spread is None
        else 15.0
        * (
            1.0
            - min(
                1.0,
                max(0.0, spread / config.research_max_spread),
            )
        )
    )
    probability_score = (
        0.0
        if probability is None
        else 10.0
        * max(0.0, 1.0 - abs(probability - 0.5) / 0.475)
    )
    horizon_score = {
        "2-14d": 15.0,
        "15-45d": 14.0,
        "46-120d": 12.0,
        "121-365d": 8.0,
    }.get(_time_bucket(days), 0.0)
    category_score = 20.0 if category in {
        "macro/fed",
        "macro/econ",
        "inflation/CPI",
        "rates",
        "recession",
        "jobs/employment",
        "GDP/economic growth",
        "major elections",
        "geopolitics",
        "central banks",
        "legal/regulatory",
    } else 16.0
    return round(
        liquidity_score
        + volume_score
        + spread_score
        + probability_score
        + horizon_score
        + category_score,
        2,
    )


def _classification_for_tier(tier: str) -> str:
    return {
        POLICY_CORE: INSTITUTIONAL_CORE,
        POLICY_RESEARCH: ACCEPTABLE_RESEARCH,
        POLICY_WATCH: SPECULATIVE,
        POLICY_BANNED: BANNED_JUNK,
    }[tier]


def _evaluation(
    *,
    market_id: str,
    question: str,
    category: str,
    tier: str,
    allowed: bool,
    reason: str,
    score: float,
    banned_class: Optional[str],
    reason_codes: list[str],
    bucket: str,
    metrics: dict[str, Any],
) -> MarketPolicyEvaluation:
    return MarketPolicyEvaluation(
        market_id=market_id,
        question=question,
        category=category,
        institutional_category=category,
        policy_tier=tier,
        classification=_classification_for_tier(tier),
        policy_allowed=allowed,
        policy_reason=reason,
        institutional_quality_score=score,
        banned_class=banned_class,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        time_bucket=bucket,
        metrics=metrics,
    )


def _threshold_failures(
    *,
    liquidity: float,
    volume: float,
    spread: float,
    probability: float,
    score: float,
    min_liquidity: float,
    min_volume: float,
    max_spread: float,
    min_probability: float,
    max_probability: float,
    min_score: float,
) -> list[str]:
    failures: list[str] = []
    if liquidity < min_liquidity:
        failures.append("low_liquidity")
    if volume < min_volume:
        failures.append("low_volume")
    if spread > max_spread:
        failures.append("wide_spread")
    if not min_probability <= probability <= max_probability:
        failures.append("probability_out_of_band")
    if score < min_score:
        failures.append("institutional_score_below_minimum")
    return failures


def evaluate_market(
    market: Mapping[str, Any],
    *,
    config: Optional[InstitutionalUniverseConfig] = None,
    now: Optional[datetime] = None,
    duplicate_position: bool = False,
) -> MarketPolicyEvaluation:
    cfg = config or InstitutionalUniverseConfig.from_env()
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    market_id = str(
        market.get("slug")
        or market.get("market_id")
        or market.get("id")
        or ""
    )
    question = str(market.get("question") or market_id).strip()
    category = classify_institutional_category(question, market)

    if cfg.mode.lower() in {"off", "legacy", "disabled"}:
        return _evaluation(
            market_id=market_id,
            question=question,
            category=category,
            tier=POLICY_RESEARCH,
            allowed=True,
            reason="policy_disabled",
            score=100.0,
            banned_class=None,
            reason_codes=[],
            bucket="not_evaluated",
            metrics={},
        )

    banned_class = detect_banned_class(
        question,
        market_id,
        cfg,
        market,
    )
    if banned_class is not None:
        reason = (
            "malformed_market"
            if banned_class == "malformed_market"
            else "banned_market_class"
        )
        return _evaluation(
            market_id=market_id,
            question=question,
            category=category,
            tier=POLICY_BANNED,
            allowed=False,
            reason=reason,
            score=0.0,
            banned_class=banned_class,
            reason_codes=[reason, banned_class],
            bucket="not_eligible",
            metrics={},
        )

    if category not in cfg.allowed_categories:
        return _evaluation(
            market_id=market_id,
            question=question,
            category=category,
            tier=POLICY_BANNED,
            allowed=False,
            reason="banned_market_class",
            score=0.0,
            banned_class="low_signal_other",
            reason_codes=["banned_market_class", "unknown_category"],
            bucket="not_eligible",
            metrics={},
        )

    outcomes_clean = has_clean_binary_resolution(market)
    end_date = _market_end(market)
    days = (
        None
        if end_date is None
        else (end_date - now_utc).total_seconds() / 86_400.0
    )
    bucket = _time_bucket(days)
    probability, spread = _yes_probability_and_spread(market)
    liquidity = max(0.0, _number(market.get("liquidity")))
    volume = max(0.0, _number(market.get("volume")))
    metadata_missing: list[str] = []
    if end_date is None:
        metadata_missing.append("deadline")
    if probability is None:
        metadata_missing.append("probability")
    if spread is None:
        metadata_missing.append("executable_spread")
    event_context = market.get("_discovery_event")
    if not isinstance(event_context, Mapping):
        event_context = {}
    metrics = {
        "liquidity": liquidity,
        "volume": volume,
        "spread": spread,
        "p_yes_market": probability,
        "end_date": end_date.isoformat() if end_date else None,
        "days_to_resolution": round(days, 2) if days is not None else None,
        "active": market.get("active"),
        "closed": bool(market.get("closed")),
        "clean_binary_resolution": outcomes_clean,
        "duplicate_position": duplicate_position,
        "metadata_missing": metadata_missing,
        "event_id": event_context.get("id"),
        "event_slug": event_context.get("slug"),
    }

    resolution_failures: list[str] = []
    if not outcomes_clean:
        resolution_failures.append("unclean_binary_resolution")
    if cfg.require_deadline and end_date is None:
        resolution_failures.append("missing_resolution_deadline")
    if probability is None:
        resolution_failures.append("missing_market_probability")
    if spread is None:
        resolution_failures.append("missing_executable_spread")
    if resolution_failures:
        return _evaluation(
            market_id=market_id,
            question=question,
            category=category,
            tier=POLICY_BANNED,
            allowed=False,
            reason="weak_resolution_quality",
            score=0.0,
            banned_class="weak_resolution_quality",
            reason_codes=["weak_resolution_quality", *resolution_failures],
            bucket=bucket,
            metrics=metrics,
        )

    assert probability is not None
    assert spread is not None
    score = _quality_score(
        liquidity=liquidity,
        volume=volume,
        spread=spread,
        probability=probability,
        days=days,
        category=category,
        config=cfg,
    )

    common_failures: list[str] = []
    if market.get("active") is False or bool(market.get("closed")):
        common_failures.append("inactive_or_closed")
    if duplicate_position:
        common_failures.append("duplicate_position")
    if days is None or days < cfg.min_days_to_resolution:
        common_failures.append("horizon_outside_range")
    if days is not None and days > cfg.max_days_to_resolution:
        common_failures.append("horizon_outside_range")
    if bucket == "121-365d" and category not in LONG_HORIZON_CATEGORIES:
        common_failures.append("long_horizon_category_not_allowed")

    core_failures = _threshold_failures(
        liquidity=liquidity,
        volume=volume,
        spread=spread,
        probability=probability,
        score=score,
        min_liquidity=cfg.core_min_liquidity,
        min_volume=cfg.core_min_volume,
        max_spread=cfg.core_max_spread,
        min_probability=cfg.core_min_probability,
        max_probability=cfg.core_max_probability,
        min_score=cfg.core_min_quality_score,
    )
    if not common_failures and not core_failures:
        return _evaluation(
            market_id=market_id,
            question=question,
            category=category,
            tier=POLICY_CORE,
            allowed=True,
            reason="institutional_core_pass",
            score=score,
            banned_class=None,
            reason_codes=[],
            bucket=bucket,
            metrics=metrics,
        )

    research_failures = _threshold_failures(
        liquidity=liquidity,
        volume=volume,
        spread=spread,
        probability=probability,
        score=score,
        min_liquidity=cfg.research_min_liquidity,
        min_volume=cfg.research_min_volume,
        max_spread=cfg.research_max_spread,
        min_probability=cfg.research_min_probability,
        max_probability=cfg.research_max_probability,
        min_score=cfg.research_min_quality_score,
    )
    if not common_failures and not research_failures:
        return _evaluation(
            market_id=market_id,
            question=question,
            category=category,
            tier=POLICY_RESEARCH,
            allowed=True,
            reason="institutional_research_pass",
            score=score,
            banned_class=None,
            reason_codes=[],
            bucket=bucket,
            metrics=metrics,
        )

    return _evaluation(
        market_id=market_id,
        question=question,
        category=category,
        tier=POLICY_WATCH,
        allowed=False,
        reason="low_institutional_quality",
        score=score,
        banned_class=None,
        reason_codes=[
            "low_institutional_quality",
            *common_failures,
            *research_failures,
        ],
        bucket=bucket,
        metrics=metrics,
    )
