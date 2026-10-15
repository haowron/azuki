"""RI01〜RI05、Q29: 受信側の原形投入。"""

import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from slack_sdk.socket_mode.request import SocketModeRequest

import trackeverything.config as config
from tests.queue.helpers import module
from trackeverything.integration.slack import SlackGateway, slack_connector
from trackeverything.integration.webhook import create_webhook_app
from trackeverything.integration.webhook.github import github_connector


def publisher():
    return AsyncMock(return_value=module("queue.contracts").PublishReceipt(message_id="1700000000000-0", accepted_at=1700000000.0))


def webhook(*connectors):
    publish = publisher()
    return TestClient(create_webhook_app(publish, connectors=list(connectors))), publish


PR_EVENT = {
    "action": "opened",
    "repository": {"name": "r", "owner": {"login": "o"}},
    "pull_request": {"number": 42, "user": {"login": "owner"}, "body": "説明"},
}


def test_ri01_github_event_is_queued_verbatim_with_allowed_headers(monkeypatch):
    monkeypatch.setattr(config, "WEBHOOK_TOKEN", "secret")
    client, publish = webhook(github_connector())
    headers = {"X-Webhook-Token": "secret", "X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d-1", "X-GitHub-Hook-ID": "9", "X-Forwarded-User": "gateway"}
    response = client.post("/webhook/github", headers=headers, json=PR_EVENT)
    assert response.status_code == 202 and response.json()["run"] == "queued"
    request = publish.await_args.args[0]
    assert (request.provider, request.external_id, request.session_id) == ("github", "d-1", None)
    assert request.payload["body"] == PR_EVENT
    assert request.payload["headers"] == {"x-github-event": "pull_request", "x-github-delivery": "d-1", "x-github-hook-id": "9"}


def test_ri02_ping_and_successful_workflow_run_are_not_ignored_by_intake():
    client, publish = webhook(github_connector())
    run = {"action": "completed", "workflow_run": {"id": 1, "conclusion": "success"}, "repository": PR_EVENT["repository"]}
    for event, body in (("ping", {"zen": "Keep it logically awesome."}), ("workflow_run", run)):
        response = client.post("/webhook/github", headers={"X-GitHub-Event": event, "X-GitHub-Delivery": f"d-{event}"}, json=body)
        assert response.status_code == 202
    assert [call.args[0].payload["body"] for call in publish.await_args_list] == [{"zen": "Keep it logically awesome."}, run]


@pytest.mark.parametrize(("body", "headers"), [
    ("not json", {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d-1"}),
    ([1, 2], {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d-1"}),
    (PR_EVENT, {"X-GitHub-Event": "pull_request"}),
    (PR_EVENT, {"X-GitHub-Delivery": "d-1"}),
], ids=["not-json", "json-array", "no-delivery", "no-event"])
def test_ri03_malformed_github_delivery_is_rejected(body, headers):
    client, publish = webhook(github_connector())
    content = body if isinstance(body, str) else json.dumps(body)
    response = client.post("/webhook/github", headers={**headers, "Content-Type": "application/json"}, content=content)
    assert response.status_code == 422
    publish.assert_not_awaited()


def test_ri04_slack_events_api_answers_handshake_and_queues_other_callbacks_verbatim():
    client, publish = webhook(slack_connector(None))
    handshake = client.post("/webhook/slack", json={"type": "url_verification", "challenge": "c-1", "token": "verification"})
    assert handshake.status_code == 200 and handshake.json() == {"challenge": "c-1"}
    publish.assert_not_awaited()
    callback = {"token": "verification", "type": "event_callback", "team_id": "T", "event_id": "Ev1",
                "event": {"type": "message", "channel": "C", "user": "U", "text": "通常の投稿"}}
    assert client.post("/webhook/slack", json=callback).status_code == 202
    request = publish.await_args.args[0]
    assert (request.provider, request.external_id, request.session_id) == ("slack", "Ev1", None)
    assert request.payload == {key: value for key, value in callback.items() if key != "token"}
    missing = client.post("/webhook/slack", json={"type": "event_callback", "event": {"type": "message"}})
    assert missing.status_code == 422 and publish.await_count == 1


def socket_payload(channel="C"):
    return {"token": "verification", "type": "event_callback", "team_id": "T", "api_app_id": "A", "event_id": "E",
            "event": {"type": "message", "channel": channel, "user": "U", "text": "hello", "ts": "1000.1"}}


def slack_receiver():
    producer, client = AsyncMock(), AsyncMock()
    order = []
    producer.publish.side_effect = lambda *args, **kwargs: order.append("publish")
    client.send_socket_mode_response.side_effect = lambda *args, **kwargs: order.append("slack_ack")
    return SlackGateway(producer, app_token="app", token="bot", team_id="T", channel_id="C"), producer, client, order


async def test_ri05_socket_mode_acks_target_only_after_publish_and_skips_other_channels():
    gateway, producer, client, order = slack_receiver()
    await gateway.on_request(client, SocketModeRequest(type="events_api", envelope_id="env", payload=socket_payload()))
    assert order == ["publish", "slack_ack"]
    order.clear()
    producer.publish.side_effect = RuntimeError("not stored")
    await gateway.on_request(client, SocketModeRequest(type="events_api", envelope_id="env", payload=socket_payload()))
    assert order == []
    producer.publish.reset_mock()
    await gateway.on_request(client, SocketModeRequest(type="events_api", envelope_id="other", payload=socket_payload("OTHER")))
    assert order == ["slack_ack"]
    producer.publish.assert_not_awaited()


async def test_q29_socket_mode_publishes_raw_payload_before_ack():
    gateway, producer, client, order = slack_receiver()
    payload = socket_payload()
    await gateway.on_request(client, SocketModeRequest(type="events_api", envelope_id="env", payload=payload))
    request = producer.publish.await_args.args[0]
    assert (request.provider, request.external_id) == ("slack", "T:E")
    assert request.payload == {key: value for key, value in payload.items() if key != "token"}
    assert order == ["publish", "slack_ack"]
    assert client.send_socket_mode_response.await_args.args[0].envelope_id == "env"
