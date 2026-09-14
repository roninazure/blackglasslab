from __future__ import annotations

import os

from parallax.dashboard import render_dashboard
from parallax.social import (
    SocialConfig,
    SocialPublisher,
    SocialStore,
    TransportResult,
    XAdapter,
)
from parallax.x_auth import OAuthResult, XCredentials, XCredentialStore, XOAuthClient


class FakeTransport:
    def __init__(self, posts=None, token_body=None):
        self.posts = []
        self.forms = []
        self.post_results = list(posts or [TransportResult("SENT", 201, "post-1")])
        self.token_body = token_body

    def post_json(self, url, payload, headers):
        self.posts.append((url, payload, headers))
        return self.post_results.pop(0)

    def post_form(self, url, form, headers):
        self.forms.append((url, form, headers))
        if self.token_body is None:
            return OAuthResult("FAILED", 400, error_summary="refresh failed")
        return OAuthResult("OK", 200, self.token_body)


def config(**kwargs):
    return SocialConfig(
        x_enabled=True,
        x_token="access-old",
        x_client_id="client-test",
        x_client_secret="secret-test",
        x_refresh_token="refresh-old",
        **kwargs,
    )


def test_unexpired_token_does_not_refresh(tmp_path):
    transport = FakeTransport()
    adapter = XAdapter(config(x_expires_at=10_000), transport, transport, XCredentialStore(tmp_path / "social.env"), now=lambda: 1)
    assert adapter.publish("hello").status == "SENT"
    assert transport.forms == []
    assert transport.posts[0][2]["Authorization"] == "Bearer access-old"


def test_refreshes_before_post_and_persists_rotation_atomically(tmp_path):
    path = tmp_path / "social.env"
    path.write_text("UNRELATED=preserve\n")
    os.chmod(path, 0o600)
    transport = FakeTransport(token_body={"access_token": "access-new", "refresh_token": "refresh-new", "expires_in": 3600})
    adapter = XAdapter(config(x_expires_at=100), transport, transport, XCredentialStore(path), now=lambda: 1)
    assert adapter.publish("hello").status == "SENT"
    assert transport.posts[0][2]["Authorization"] == "Bearer access-new"
    assert "UNRELATED=preserve" in path.read_text()
    assert "PARALLAX_X_REFRESH_TOKEN=refresh-new" in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600


def test_refresh_failure_fails_closed_without_post(tmp_path):
    transport = FakeTransport()
    adapter = XAdapter(config(x_expires_at=100), transport, transport, XCredentialStore(tmp_path / "social.env"), now=lambda: 1)
    result = adapter.publish("hello")
    assert result.status == "FAILED"
    assert transport.posts == []


def test_expired_post_refreshes_once_and_does_not_loop(tmp_path):
    transport = FakeTransport(
        posts=[TransportResult("FAILED", 401, error_code="HTTP_4XX"), TransportResult("FAILED", 401, error_code="HTTP_4XX")],
        token_body={"access_token": "access-new", "refresh_token": "refresh-new", "expires_in": 3600},
    )
    adapter = XAdapter(config(x_expires_at=10_000), transport, transport, XCredentialStore(tmp_path / "social.env"), now=lambda: 1)
    assert adapter.publish("hello").status == "FAILED"
    assert len(transport.posts) == 2
    assert len(transport.forms) == 1


def test_pytest_config_and_status_never_expose_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("PARALLAX_X_ACCESS_TOKEN", "leaked-access")
    monkeypatch.setenv("PARALLAX_X_REFRESH_TOKEN", "leaked-refresh")
    assert SocialConfig.load() == SocialConfig()
    social = SocialPublisher(config=SocialConfig(), store=SocialStore(tmp_path / "social.sqlite"))
    status = str(social.status())
    html = render_dashboard({}, {}, {}, {}, social.status())
    assert "leaked-access" not in status + html
    assert "leaked-refresh" not in status + html


def test_refresh_client_uses_confidential_basic_auth_without_printing_tokens():
    transport = FakeTransport(token_body={"access_token": "access-new", "refresh_token": "refresh-new", "expires_in": 3600})
    credentials, error = XOAuthClient(transport, now=lambda: 100).refresh(XCredentials("client-test", "secret-test", "access-old", "refresh-old", 1))
    assert error is None
    assert credentials and credentials.access_token == "access-new"
    assert transport.forms[0][1] == {"grant_type": "refresh_token", "refresh_token": "refresh-old"}
    assert "access-new" not in str(transport.forms[0])
