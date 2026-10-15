"""PC01・PC02: 部品が実行中のメッセージを親として新しい実行依頼を投入する Host の投入口(``queue/child.py``)。"""

from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from tests.queue.helpers import module
from trackeverything.core.abc.submission import REQUEST_ID, EventRequest


@pytest.fixture
def child():
    """``queue.child`` を Producer 未登録の状態で返し、終わりに登録を外す。"""
    found = module("queue.child")
    found.install(None)
    yield found
    found.install(None)


def _producer():
    producer = Mock()
    producer.publish = AsyncMock(return_value=Mock(message_id="1700000000001-0"))
    return producer


async def test_pc01_publish_child_queues_an_internal_request_under_the_running_message(child):
    """PC01: 実行中のメッセージと登録済みの Producer で、``<親>:<key>`` の internal の依頼を一度投入して受付を返す。"""
    producer = _producer()
    child.install(producer)
    payload = {"agent_name": "general", "request": "Issue を調べる"}
    token = REQUEST_ID.set("1700000000000-0")
    try:
        receipt = await child.publish_child(payload, "auto-start", session_id="s-1")
    finally:
        REQUEST_ID.reset(token)

    producer.publish.assert_awaited_once()
    (request,) = producer.publish.await_args.args
    assert isinstance(request, EventRequest)
    assert (request.provider, request.external_id, request.session_id) == ("internal", "1700000000000-0:auto-start", "s-1")
    assert request.payload == payload
    assert receipt is producer.publish.return_value

    token = REQUEST_ID.set("1700000000000-1")
    try:
        await child.publish_child({"agent_name": "sentry_autofix_item"}, "sentry-item:42")
    finally:
        REQUEST_ID.reset(token)
    second = producer.publish.await_args.args[0]
    assert (second.external_id, second.session_id) == ("1700000000000-1:sentry-item:42", None)


async def test_pc02_publish_child_refuses_without_a_running_message_or_a_producer(child):
    """PC02: 実行の外・Producer の未登録・256 文字を超える ``external_id`` はいずれも例外で、何も投入しない。"""
    producer = _producer()
    child.install(producer)
    assert REQUEST_ID.get() is None
    with pytest.raises(RuntimeError):
        await child.publish_child({"agent_name": "general"}, "auto-start")

    token = REQUEST_ID.set("1700000000000-0")
    try:
        with pytest.raises((ValidationError, ValueError)):
            await child.publish_child({"agent_name": "general"}, "k" * 256)
        child.install(None)
        with pytest.raises(RuntimeError):
            await child.publish_child({"agent_name": "general"}, "auto-start")
    finally:
        REQUEST_ID.reset(token)
    producer.publish.assert_not_awaited()
