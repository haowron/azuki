"""Q01〜Q03・Q08・RI12・SQ01〜SQ02・UR01: 投入口は Producer.publish(EventRequest) 一つで、XADD 一回で受理し、重複排除をしない。"""

import json

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from tests.queue.helpers import QUEUE, module, redis, request, stream
from trackeverything.core.abc.submission import EventRequest
from trackeverything.integration.webhook import create_webhook_app
from trackeverything.integration.webhook.github import github_connector

PR_EVENT = {"action": "opened", "repository": {"name": "r", "owner": {"login": "o"}}, "pull_request": {"number": 42}}
# CLI に渡す名前指定の実行(internal)の JSON。Q02 はこの一部を差し替えて拒否される入力を作る。
NAMED_RUN = {"provider": "internal", "external_id": "cli:general", "payload": {"agent_name": "general", "request": "調査する"}}


def producer():
    client = redis()
    return module("queue.producer").Producer(stream(client)), client


def saved(client):
    """``XADD`` に渡した field ``data`` の実行依頼(呼出し順)。"""
    return [EventRequest.model_validate_json(call.args[1]["data"]) for call in client.xadd.await_args_list]


def github(pub):
    return TestClient(create_webhook_app(pub.publish, connectors=[github_connector()]))


async def test_q01_sdk_and_cli_share_publish_without_runtime(tmp_path):
    pub, client = producer()
    client.xadd.side_effect = ["1700000000000-0", "1700000000000-1"]
    sdk = await pub.publish(request())
    path = tmp_path / "message.json"
    path.write_text(json.dumps(request().model_dump(mode="json")))
    cli = await module("queue.cli").publish_file(pub, path)
    assert (sdk.message_id, cli.message_id) == ("1700000000000-0", "1700000000000-1")
    assert saved(client) == [request(), request()]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        NAMED_RUN | {"kind": "other"},
        {"kind": "agent.run", "agent_name": "general", "request": "調査する"},
        {"kind": "source.run", "agent_name": "project_memory", "source": "github_pull_request", "source_kwargs": {"owner": "o", "repo": "r", "number": 1}, "project": None},
        NAMED_RUN | {"provider": "gitlab"},
        {"provider": "internal", "payload": {"agent_name": "general"}},
        NAMED_RUN | {"authorization": {"confirmed": True}},
        NAMED_RUN | {"run_id": "forged"},
    ],
    ids=["empty", "unknown-kind", "old-agent-run", "old-source-run", "unknown-provider", "missing-external-id", "authorization", "run-id"],
)
async def test_q02_invalid_or_trusted_input_cannot_be_published(tmp_path, payload):
    pub, client = producer()
    path = tmp_path / "message.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        await module("queue.cli").publish_file(pub, path)
    client.xadd.assert_not_awaited()


async def test_q03_undeclared_agent_name_is_admitted_without_resolving_agent():
    pub, client = producer()
    await pub.publish(request(payload={"agent_name": "not_registered"}))
    assert saved(client)[0].payload == {"agent_name": "not_registered"}


def test_q08_unavailable_queue_never_reports_acceptance():
    pub, client = producer()
    client.xadd.side_effect = RedisConnectionError("redis is down")
    response = github(pub).post("/webhook/github", headers={"X-GitHub-Event": "ping", "X-GitHub-Delivery": "d-1"}, json={"zen": "z"})
    assert response.status_code == 503
    assert "message_id" not in response.json()


async def test_ri12_one_mebibyte_event_is_accepted():
    pub, client = producer()
    body = {"text": "x" * (1024 * 1024)}
    large = EventRequest(provider="github", external_id="large", payload={"headers": {"x-github-event": "push"}, "body": body})
    receipt = await pub.publish(large)
    assert receipt.message_id == "1700000000000-0"
    assert saved(client)[0].payload["body"] == body


async def test_sq01_each_publish_is_one_xadd_and_the_stream_id_is_the_message_id():
    pub, client = producer()
    client.xadd.side_effect = ["1700000000000-0", "1700000000000-1"]
    first, second = await pub.publish(request()), await pub.publish(request())
    assert [call.args[0] for call in client.xadd.await_args_list] == [QUEUE, QUEUE]
    assert all(set(call.args[1]) == {"data"} and call.kwargs == {} for call in client.xadd.await_args_list)
    assert saved(client) == [request(), request()]
    assert first.model_dump() == {"message_id": "1700000000000-0", "accepted_at": 1700000000.0}
    assert second.message_id == "1700000000000-1"


def test_sq02_github_webhook_reaches_redis_as_an_event_request():
    pub, client = producer()
    response = github(pub).post("/webhook/github", headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d-1"}, json=PR_EVENT)
    assert response.status_code == 202
    assert response.json() == {"ok": True, "run": "queued", "message_id": "1700000000000-0", "accepted_at": 1700000000.0}
    [event] = saved(client)
    assert isinstance(event, EventRequest)
    assert (event.provider, event.external_id, event.payload["body"]) == ("github", "d-1", PR_EVENT)
    assert client.xadd.await_args.args[0] == QUEUE


async def test_ur01_cli_republishes_dead_stream_data_as_is(tmp_path):
    """dead stream の ``data``(GitHub イベントの EventRequest の JSON)を、そのまま CLI で投入する。"""
    pub, client = producer()
    dead = EventRequest(provider="github", external_id="d-1", payload={"headers": {"x-github-event": "pull_request", "x-github-delivery": "d-1"}, "body": PR_EVENT})
    data = dead.model_dump_json()
    path = tmp_path / "message.json"
    path.write_text(data)
    receipt = await module("queue.cli").publish_file(pub, path)
    assert receipt.message_id == "1700000000000-0"
    client.xadd.assert_awaited_once()
    assert client.xadd.await_args.args[0] == QUEUE
    assert json.loads(client.xadd.await_args.args[1]["data"]) == json.loads(data)
    assert saved(client) == [dead]
