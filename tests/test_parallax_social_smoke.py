from __future__ import annotations

from parallax.social import SocialConfig, SocialPublisher, SocialStore, TransportResult


class FakeTransport:
    def __init__(self):
        self.calls = []

    def post_json(self, url, payload, headers):
        self.calls.append((url, payload, headers))
        return TransportResult("SENT", 201, "x-post-123")


def test_operator_smoke_isolated_and_records_x_post(tmp_path):
    transport = FakeTransport()
    publisher = SocialPublisher(
        store=SocialStore(tmp_path / "social.sqlite"),
        config=SocialConfig(mode="live", x_enabled=True, x_token="token", x_refresh_token="refresh", x_client_id="client", x_client_secret="secret"),
        transport=transport,
    )
    result = publisher.publish_operator_smoke("controlled smoke")
    assert result.status == "SENT"
    assert len(transport.calls) == 1
    outbox = publisher.store.outbox()[0]
    publication = publisher.store.publications()[0]
    assert outbox["source_type"] == "OPERATOR_SMOKE"
    assert publication["status"] == "SENT"
    assert publication["external_post_id"] == "x-post-123"
    assert outbox["publication_class"] == "OPERATOR_SMOKE"
    assert outbox["content_json"] == '{"source_type": "OPERATOR_SMOKE", "text": "controlled smoke"}'
