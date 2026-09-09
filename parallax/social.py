from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import timestamp, utcnow

SOCIAL_DB_PATH = Path.home() / "Library" / "Application Support" / "SwarmEdge" / "state" / "parallax_social.sqlite"
SOCIAL_CONFIG_PATH = Path.home() / "Library" / "Application Support" / "SwarmEdge" / "parallax-social.env"
PLATFORMS = ("x", "linkedin", "instagram")
PUBLIC_COOLDOWN_SECONDS = 30 * 60
MAX_ATTEMPTS = 2


def _pytest_active() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules


def _env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        if path.stat().st_mode & 0o077:
            return values
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    except OSError:
        pass
    return values


@dataclass(frozen=True)
class SocialConfig:
    mode: str = "disabled"
    x_enabled: bool = False
    linkedin_enabled: bool = False
    instagram_enabled: bool = False
    x_token: str | None = None
    linkedin_token: str | None = None
    linkedin_org: str | None = None
    linkedin_version: str | None = None
    instagram_token: str | None = None
    instagram_account: str | None = None
    instagram_media_base_url: str | None = None

    @classmethod
    def load(cls) -> SocialConfig:
        keys = {
            "PARALLAX_SOCIAL_MODE", "PARALLAX_SOCIAL_X_ENABLED", "PARALLAX_SOCIAL_LINKEDIN_ENABLED",
            "PARALLAX_SOCIAL_INSTAGRAM_ENABLED", "PARALLAX_X_ACCESS_TOKEN", "PARALLAX_LINKEDIN_ACCESS_TOKEN",
            "PARALLAX_LINKEDIN_ORGANIZATION_URN", "PARALLAX_LINKEDIN_VERSION", "PARALLAX_INSTAGRAM_ACCESS_TOKEN",
            "PARALLAX_INSTAGRAM_ACCOUNT_ID", "PARALLAX_INSTAGRAM_MEDIA_BASE_URL",
        }
        values = _env_file(SOCIAL_CONFIG_PATH)
        values.update({k: v for k, v in os.environ.items() if k in keys})
        if _pytest_active():
            return cls()
        mode = values.get("PARALLAX_SOCIAL_MODE", "disabled").casefold()
        if mode not in {"disabled", "dry_run", "live"}:
            mode = "disabled"
        flag = lambda key: values.get(key, "false").casefold() in {"1", "true", "yes", "on"}
        return cls(mode, flag("PARALLAX_SOCIAL_X_ENABLED"), flag("PARALLAX_SOCIAL_LINKEDIN_ENABLED"), flag("PARALLAX_SOCIAL_INSTAGRAM_ENABLED"), values.get("PARALLAX_X_ACCESS_TOKEN"), values.get("PARALLAX_LINKEDIN_ACCESS_TOKEN"), values.get("PARALLAX_LINKEDIN_ORGANIZATION_URN"), values.get("PARALLAX_LINKEDIN_VERSION"), values.get("PARALLAX_INSTAGRAM_ACCESS_TOKEN"), values.get("PARALLAX_INSTAGRAM_ACCOUNT_ID"), values.get("PARALLAX_INSTAGRAM_MEDIA_BASE_URL"))


class SocialTransport(Protocol):
    def post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> TransportResult: ...


@dataclass(frozen=True)
class TransportResult:
    status: str
    http_status: int | None = None
    external_post_id: str | None = None
    error_code: str | None = None
    error_summary: str | None = None


class HttpSocialTransport:
    def post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> TransportResult:
        if _pytest_active() or not url.startswith("https://"):
            return TransportResult("FAILED", error_code="NETWORK_DISABLED", error_summary="External social delivery is disabled.")
        try:
            req = Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
            with urlopen(req, timeout=5) as response:
                body = response.read(16384)
                data = json.loads(body or b"{}")
                return TransportResult("SENT", int(response.status), str(data.get("data", {}).get("id")) if data.get("data", {}).get("id") else None)
        except HTTPError as exc:
            exc.read(4096)
            return TransportResult("FAILED", exc.code, error_code="HTTP_5XX" if exc.code >= 500 else "HTTP_4XX", error_summary=f"HTTP {exc.code}")
        except (TimeoutError, URLError, OSError):
            return TransportResult("UNKNOWN", error_code="NETWORK_UNKNOWN", error_summary="Connection outcome is unknown.")


class XAdapter:
    endpoint = "https://api.x.com/2/tweets"
    def __init__(self, config: SocialConfig | None = None, transport: SocialTransport | None = None):
        self.config = config or SocialConfig.load(); self.transport = transport or HttpSocialTransport()
    def publish(self, text: str) -> TransportResult:
        if not self.config.x_token: return TransportResult("FAILED", error_code="MISSING_X_ACCESS_TOKEN", error_summary="X access token is not configured.")
        return self.transport.post_json(self.endpoint, {"text": text}, {"Authorization": f"Bearer {self.config.x_token}", "Content-Type": "application/json"})


class LinkedInAdapter:
    endpoint = "https://api.linkedin.com/rest/posts"
    def __init__(self, config: SocialConfig | None = None, transport: SocialTransport | None = None):
        self.config = config or SocialConfig.load(); self.transport = transport or HttpSocialTransport()
    def publish(self, text: str) -> TransportResult:
        if not all((self.config.linkedin_token, self.config.linkedin_org, self.config.linkedin_version)):
            return TransportResult("FAILED", error_code="MISSING_LINKEDIN_CONFIGURATION", error_summary="LinkedIn organization and API configuration are required.")
        headers = {"Authorization": f"Bearer {self.config.linkedin_token}", "Linkedin-Version": str(self.config.linkedin_version), "X-Restli-Protocol-Version": "2.0.0", "Content-Type": "application/json"}
        return self.transport.post_json(self.endpoint, {"author": self.config.linkedin_org, "commentary": {"text": text}}, headers)


class InstagramAdapter:
    def __init__(self, config: SocialConfig | None = None): self.config = config or SocialConfig.load()
    def publish(self, content: dict[str, Any]) -> TransportResult:
        if not self.config.instagram_media_base_url:
            return TransportResult("FAILED", error_code="MEDIA_HOST_NOT_CONFIGURED", error_summary="Instagram feed publishing requires a public media URL.")
        return TransportResult("FAILED", error_code="MEDIA_HOST_NOT_CONFIGURED", error_summary="Instagram media delivery is intentionally not implemented in V1.")


def _money(value: Any) -> str:
    try: return f"${float(value):,.0f}"
    except (TypeError, ValueError): return ""


def format_x(item: dict[str, Any]) -> str:
    actionable = item.get("attention_class") == "ACTIONABLE_PLAY"
    if actionable:
        examples = {float(e.get("stake")): e for e in (item.get("economics") or {}).get("examples", []) if e.get("available", True)}
        lines = ["PARALLAX PLAY", "", str(item.get("market", "")), str(item.get("verdict", "")) + (f" · {item.get('trade_confidence')} confidence" if item.get("trade_confidence") else "")]
        price = (item.get("current_market") or {}).get("current_executable_buy_prices", {}).get(str(item.get("verdict", "").removeprefix("BUY ")))
        if price is not None: lines.append(f"Price: {round(float(price) * 100):.0f}¢")
        for stake in (25.0, 50.0, 100.0):
            if stake in examples: lines.append(f"{_money(stake)} → potential +{_money(examples[stake].get('gross_profit_if_correct'))}")
        lines += ["", "Before fees/costs. Max loss = amount spent.", "swarmaxis.ai/parallax"]
    else:
        source = item.get("source_signal") or {}
        lines = ["PARALLAX SIGNAL", "", str(item.get("market", "")), "", f"{source.get('side', '')} moved {source.get('formatted_previous_value', '')} → {source.get('formatted_current_value', '')} in {source.get('formatted_window', '')}.", "", "WATCH — market movement, not a BUY recommendation.", "", "swarmaxis.ai/parallax"]
    return "\n".join(lines)[:270]


def format_linkedin(item: dict[str, Any]) -> str:
    if item.get("attention_class") == "ACTIONABLE_PLAY":
        economics = item.get("economics") or {}
        examples = "; ".join(f"{_money(e.get('stake'))}: +{_money(e.get('gross_profit_if_correct'))}" for e in economics.get("examples", []) if e.get("available", True))
        return "\n".join(["PARALLAX PLAY", "", f"Market:\n{item.get('market', '')}", f"\nVerdict:\n{item.get('verdict', '')}", f"\nWhat PARALLAX sees:\n{item.get('why') or item.get('what_it_means', '')}", f"\nTrade confidence:\n{item.get('trade_confidence', 'Not available')}", f"\nIllustrative stake economics:\n{examples or 'Not available'}", f"\nRisks / invalidation:\n{item.get('invalidation') or item.get('operator_instruction', '')}", "\nBefore fees/costs. Maximum loss is amount spent.", "\nPARALLAX by Swarm Axis"])
    return "\n".join(["PARALLAX MARKET SIGNAL", "", f"Market\n{item.get('market', '')}", f"\nWhat happened\n{item.get('what_happened', '')}", f"\nWhat it means\n{item.get('what_it_means', '')}", "\nWhat it does NOT mean\nThis is not a trade recommendation.", "\nWATCH\nNot a BUY recommendation.", "\nPARALLAX by Swarm Axis"])


def instagram_content(item: dict[str, Any]) -> dict[str, Any]:
    actionable = item.get("attention_class") == "ACTIONABLE_PLAY"
    return {"caption": format_linkedin(item), "card_title": "PARALLAX PLAY" if actionable else "PARALLAX MARKET SIGNAL", "market_title": item.get("market", ""), "verdict": item.get("verdict", "WATCH"), "primary_metric": item.get("market_activity_strength", ""), "secondary_text": item.get("what_happened", ""), "disclosure": "" if actionable else "NOT A BUY RECOMMENDATION"}


class SocialStore:
    def __init__(self, path: str | Path = SOCIAL_DB_PATH):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS social_outbox (outbox_id TEXT PRIMARY KEY, source_type TEXT NOT NULL, source_id TEXT NOT NULL, venue TEXT NOT NULL, market_id TEXT NOT NULL, publication_class TEXT NOT NULL, created_at TEXT NOT NULL, publish_after TEXT NOT NULL, expires_at TEXT, status TEXT NOT NULL, content_fingerprint TEXT NOT NULL, content_json TEXT NOT NULL, UNIQUE(source_type, source_id, publication_class)); CREATE TABLE IF NOT EXISTS social_publications (publication_id TEXT PRIMARY KEY, outbox_id TEXT NOT NULL, platform TEXT NOT NULL, status TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, last_attempt_at TEXT, sent_at TEXT, external_post_id TEXT, error_code TEXT, error_summary TEXT, UNIQUE(platform, outbox_id));""")

    def _rows(self, table: str, limit: int) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 100)),))]

    def outbox(self, limit=20): return self._rows("social_outbox", limit)
    def publications(self, limit=20): return self._rows("social_publications", limit)

    def counts(self) -> dict[str, int]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT status, count(*) FROM social_outbox GROUP BY status").fetchall()
        return {str(k).lower(): int(v) for k, v in rows}

    def add(self, item: dict[str, Any], now: str) -> str | None:
        source_type = "parallax_signal"; source_id = str(item.get("source_id", "")); cls = str(item.get("attention_class", ""))
        if not source_id or cls not in {"ACTIONABLE_PLAY", "PUBLIC_WORTHY"}: return None
        raw = json.dumps(item, sort_keys=True, default=str).encode(); fingerprint = hashlib.sha256(raw).hexdigest()
        oid = "outbox-" + hashlib.sha256(f"{source_type}:{source_id}:{cls}".encode()).hexdigest()[:24]
        with sqlite3.connect(self.path) as db:
            if cls == "PUBLIC_WORTHY":
                recent = False
                for (created,) in db.execute("SELECT created_at FROM social_outbox WHERE market_id=? AND publication_class IN ('PUBLIC_WORTHY','ACTIONABLE_PLAY')", (str(item.get("market_id", "")),)):
                    created_at = timestamp(created); now_at = timestamp(now)
                    if created_at and now_at and 0 <= (now_at - created_at).total_seconds() < PUBLIC_COOLDOWN_SECONDS:
                        recent = True; break
                if recent: return None
            db.execute("INSERT OR IGNORE INTO social_outbox VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (oid, source_type, source_id, str(item.get("venue", "")), str(item.get("market_id", "")), cls, now, now, item.get("expires_at"), "PENDING", fingerprint, json.dumps(item, sort_keys=True, default=str)))
        return oid


class SocialPublisher:
    def __init__(self, store: SocialStore | None = None, config: SocialConfig | None = None, transport: SocialTransport | None = None):
        self.store = store or SocialStore(); self.config = config or SocialConfig.load(); self.transport = transport or HttpSocialTransport(); self.started_at = utcnow().isoformat()

    def enqueue(self, item: dict[str, Any]) -> None:
        if self.config.mode == "disabled" or item.get("demo") is True: return
        now = utcnow().isoformat()
        oid = self.store.add(item, now)
        if oid and self.config.mode == "dry_run":
            with sqlite3.connect(self.store.path) as db:
                db.execute("UPDATE social_outbox SET status='DRY_RUN' WHERE outbox_id=?", (oid,))
                for platform in PLATFORMS:
                    enabled, configured, blocker = self._platform_config(platform)
                    if not enabled:
                        continue
                    publication_id = "pub-" + hashlib.sha256(f"{oid}:{platform}".encode()).hexdigest()[:24]
                    status = "DRY_RUN" if platform != "instagram" or configured else "BLOCKED"
                    error_code = None if status == "DRY_RUN" else blocker
                    db.execute("INSERT OR IGNORE INTO social_publications VALUES (?,?,?,?,?,?,?,?,?,?,?)", (publication_id, oid, platform, status, 0, now, None, None, None, error_code, "Media host is not configured." if error_code else None))

    def _platform_config(self, platform: str) -> tuple[bool, bool, str | None]:
        c = self.config
        if platform == "x": return c.x_enabled, bool(c.x_token), None
        if platform == "linkedin": return c.linkedin_enabled, bool(c.linkedin_token and c.linkedin_org and c.linkedin_version), None
        return c.instagram_enabled, bool(c.instagram_token and c.instagram_account and c.instagram_media_base_url), "MEDIA_HOST_NOT_CONFIGURED"

    def status(self) -> dict[str, Any]:
        counts = self.store.counts(); platforms = {}
        for p in PLATFORMS:
            enabled, configured, blocker = self._platform_config(p); platforms[p] = {"enabled": enabled, "configured": configured, "status": "disabled" if not enabled else "ready" if configured else "blocked", **({"blocker": blocker} if blocker and enabled and not configured else {})}
        pubs = self.store.publications(1); last = pubs[0].get("sent_at") if pubs and pubs[0].get("status") == "SENT" else None
        return {"mode": self.config.mode, "platforms": platforms, "pending": counts.get("pending", 0), "dry_run": counts.get("dry_run", 0), "sent": counts.get("sent", 0), "failed": counts.get("failed", 0), "unknown": counts.get("unknown", 0), "expired": counts.get("expired", 0), "last_publication_at": last}

    def publish_pending(self) -> None:
        if self.config.mode != "live" or (_pytest_active() and isinstance(self.transport, HttpSocialTransport)): return
        for item in self.store.outbox(100):
            if item["status"] != "PENDING": continue
            created_at = timestamp(item.get("created_at"))
            started_at = timestamp(self.started_at)
            if created_at is not None and started_at is not None and created_at < started_at:
                with sqlite3.connect(self.store.path) as db: db.execute("UPDATE social_outbox SET status='SKIPPED' WHERE outbox_id=?", (item["outbox_id"],))
                continue
            exp = timestamp(item.get("expires_at")); now = utcnow()
            if exp and exp <= now:
                with sqlite3.connect(self.store.path) as db: db.execute("UPDATE social_outbox SET status='EXPIRED' WHERE outbox_id=?", (item["outbox_id"],))
                continue
            # Each enabled platform has an independent ledger identity.
            for platform in PLATFORMS:
                enabled, configured, _blocker = self._platform_config(platform)
                if not enabled or not configured: continue
                with sqlite3.connect(self.store.path) as db:
                    if db.execute("SELECT 1 FROM social_publications WHERE platform=? AND outbox_id=?", (platform, item["outbox_id"])).fetchone(): continue
                item_payload = json.loads(item.get("content_json") or "{}")
                content = format_x(item_payload) if platform == "x" else format_linkedin(item_payload) if platform == "linkedin" else instagram_content(item_payload)
                if platform == "instagram": continue
                url = "https://api.x.com/2/tweets" if platform == "x" else "https://api.linkedin.com/rest/posts"
                headers = {"Authorization": f"Bearer {self.config.x_token if platform == 'x' else self.config.linkedin_token}", "Content-Type": "application/json"}
                if platform == "linkedin": headers.update({"Linkedin-Version": str(self.config.linkedin_version), "X-Restli-Protocol-Version": "2.0.0"})
                result = self.transport.post_json(url, {"text": content} if platform == "x" else {"author": self.config.linkedin_org, "commentary": {"text": content}}, headers)
                status = result.status; attempts = 1
                if status == "FAILED" and result.error_code == "HTTP_5XX":
                    result = self.transport.post_json(url, {"text": content} if platform == "x" else {"author": self.config.linkedin_org, "commentary": {"text": content}}, headers); status = result.status; attempts = 2
                pid = "pub-" + hashlib.sha256(f"{item['outbox_id']}:{platform}".encode()).hexdigest()[:24]
                with sqlite3.connect(self.store.path) as db: db.execute("INSERT OR IGNORE INTO social_publications VALUES (?,?,?,?,?,?,?,?,?,?,?)", (pid, item["outbox_id"], platform, status, attempts, now.isoformat(), now.isoformat(), now.isoformat() if status == "SENT" else None, result.external_post_id, result.error_code, result.error_summary))
            with sqlite3.connect(self.store.path) as db:
                statuses = [r["status"] for r in self.store.publications(100) if r["outbox_id"] == item["outbox_id"]]
                final = "SENT" if "SENT" in statuses else "UNKNOWN" if "UNKNOWN" in statuses else "FAILED"
                db.execute("UPDATE social_outbox SET status=? WHERE outbox_id=?", (final, item["outbox_id"]))
