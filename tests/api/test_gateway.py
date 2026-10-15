"""共通 gateway の認証と上流への転送。実ネットワークは使わない。"""

import hashlib
import hmac
import importlib.util
import sys
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

NOW = 2_000_000_000


@pytest.fixture
def gateway():
    path = Path(__file__).resolve().parents[2] / "gateway" / "server.py"
    spec = importlib.util.spec_from_file_location("business_shared_gateway", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    upstream = Mock(
        side_effect=lambda request: httpx.Response(
            200, stream=httpx.ByteStream(request.content), headers={"X-Upstream": "fixture"}
        )
    )
    app = module.create_app(
        {
            "credentials": {
                "github_secret": "github-fixture",
                "slack_signing_secret": "slack-fixture",
                "bearer_token": "client-fixture",
            },
            "routes": [
                {
                    "prefix": "/",
                    "upstream": "http://127.0.0.1:18080",
                    "headers": {"X-Webhook-Token": "internal-fixture"},
                }
            ],
        },
        transport=httpx.MockTransport(upstream),
        clock=lambda: NOW,
    )
    with TestClient(app, base_url="https://gateway.example.invalid") as client:
        yield client, upstream


def github_signature(body):
    return "sha256=" + hmac.new(b"github-fixture", body, hashlib.sha256).hexdigest()


def test_h17_bearer_request_forwards_with_internal_auth(gateway):
    client, upstream = gateway
    response = client.get("/api/runs", headers={"Authorization": "Bearer client-fixture"})
    assert response.status_code == 200 and response.headers["X-Upstream"] == "fixture"
    request = upstream.call_args.args[0]
    assert str(request.url) == "http://127.0.0.1:18080/api/runs"
    assert request.headers["X-Webhook-Token"] == "internal-fixture"
    assert "authorization" not in request.headers


def test_h18_invalid_bearer_is_not_forwarded(gateway):
    client, upstream = gateway
    assert client.get("/api/runs", headers={"Authorization": "Bearer wrong"}).status_code == 401
    upstream.assert_not_called()


def test_h19_signed_github_event_reaches_upstream(gateway):
    client, upstream = gateway
    body = b'{"action":"opened"}'
    response = client.post(
        "/webhook/github",
        content=body,
        headers={"X-Hub-Signature-256": github_signature(body), "X-GitHub-Event": "pull_request"},
    )
    assert response.status_code == 200
    request = upstream.call_args.args[0]
    assert request.url.path == "/webhook/github" and request.content == body


def test_h20_signed_slack_event_reaches_upstream(gateway):
    client, upstream = gateway
    body = b'{"type":"event_callback"}'
    signature = hmac.new(b"slack-fixture", f"v0:{NOW}:".encode() + body, hashlib.sha256).hexdigest()
    response = client.post(
        "/webhook/slack",
        content=body,
        headers={"X-Slack-Request-Timestamp": str(NOW), "X-Slack-Signature": "v0=" + signature},
    )
    assert response.status_code == 200
    request = upstream.call_args.args[0]
    assert request.url.path == "/webhook/slack" and request.content == body


def test_h21_modified_signed_body_is_rejected(gateway):
    client, upstream = gateway
    response = client.post(
        "/webhook/github",
        content=b'{"action":"closed"}',
        headers={"X-Hub-Signature-256": github_signature(b'{"action":"opened"}'), "X-GitHub-Event": "pull_request"},
    )
    assert response.status_code == 401
    upstream.assert_not_called()
