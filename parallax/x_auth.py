from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

X_TOKEN_ENDPOINT = "https://api.x.com/2/oauth2/token"
X_SCOPES = "tweet.read tweet.write users.read offline.access"
TOKEN_EXPIRING_SECONDS = 5 * 60


def _pytest_active() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or "pytest" in __import__("sys").modules


@dataclass(frozen=True)
class OAuthResult:
    status: str
    http_status: int | None = None
    body: dict[str, Any] | None = None
    error_summary: str | None = None


class OAuthTransport(Protocol):
    def post_form(self, url: str, form: dict[str, str], headers: dict[str, str]) -> OAuthResult: ...


class HttpOAuthTransport:
    def post_form(self, url: str, form: dict[str, str], headers: dict[str, str]) -> OAuthResult:
        if _pytest_active() or not url.startswith("https://"):
            return OAuthResult("FAILED", error_summary="External OAuth delivery is disabled.")
        try:
            request = Request(url, data=urlencode(form).encode(), headers={**headers, "Content-Type": "application/x-www-form-urlencoded"}, method="POST")
            with urlopen(request, timeout=5) as response:
                return OAuthResult("OK", int(response.status), json.loads(response.read(16384) or b"{}"))
        except HTTPError as exc:
            exc.read(4096)
            return OAuthResult("FAILED", exc.code, error_summary=f"OAuth HTTP {exc.code}")
        except (TimeoutError, URLError, OSError):
            return OAuthResult("FAILED", error_summary="OAuth connection failed.")


@dataclass(frozen=True)
class XCredentials:
    client_id: str | None = None
    client_secret: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: float | None = None


class XCredentialStore:
    """Small private env-file store; credentials never enter SQLite."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def read(self) -> dict[str, str]:
        values: dict[str, str] = {}
        try:
            if self.path.stat().st_mode & 0o077:
                return values
            for line in self.path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip().strip("\"'")
        except OSError:
            pass
        return values

    def update(self, updates: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.path.read_text().splitlines() if self.path.exists() else []
        seen: set[str] = set()
        lines: list[str] = []
        for line in existing:
            key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
            if key in updates:
                lines.append(f"{key}={updates[key]}")
                seen.add(key)
            else:
                lines.append(line)
        if lines and lines[-1] != "":
            lines.append("")
        lines.extend(f"{key}={value}" for key, value in updates.items() if key not in seen)
        content = "\n".join(lines) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class XOAuthClient:
    def __init__(self, transport: OAuthTransport | None = None, now: Callable[[], float] = time.time):
        self.transport = transport or HttpOAuthTransport()
        self.now = now

    def refresh(self, credentials: XCredentials) -> tuple[XCredentials | None, str | None]:
        if not credentials.client_id or not credentials.client_secret or not credentials.refresh_token:
            return None, "X authorization is not configured."
        basic = base64.b64encode(f"{credentials.client_id}:{credentials.client_secret}".encode()).decode()
        result = self.transport.post_form(X_TOKEN_ENDPOINT, {"grant_type": "refresh_token", "refresh_token": credentials.refresh_token}, {"Authorization": f"Basic {basic}"})
        body = result.body or {}
        access = body.get("access_token")
        expires_in = body.get("expires_in")
        if result.status != "OK" or not isinstance(access, str) or not access or not isinstance(expires_in, (int, float)):
            return None, result.error_summary or "X authorization refresh failed."
        rotated = body.get("refresh_token")
        return XCredentials(credentials.client_id, credentials.client_secret, access, rotated if isinstance(rotated, str) and rotated else credentials.refresh_token, self.now() + float(expires_in)), None

    def exchange_code(self, client_id: str, client_secret: str, code: str, verifier: str, redirect_uri: str) -> tuple[XCredentials | None, str | None]:
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        result = self.transport.post_form(X_TOKEN_ENDPOINT, {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri, "code_verifier": verifier}, {"Authorization": f"Basic {basic}"})
        body = result.body or {}
        access = body.get("access_token")
        refresh = body.get("refresh_token")
        expires_in = body.get("expires_in")
        if result.status != "OK" or not isinstance(access, str) or not isinstance(refresh, str) or not isinstance(expires_in, (int, float)):
            return None, result.error_summary or "X authorization exchange failed."
        return XCredentials(client_id, client_secret, access, refresh, self.now() + float(expires_in)), None
