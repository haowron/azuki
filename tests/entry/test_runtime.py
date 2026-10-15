"""RS02・RS03・RS06・RS08・RS09・SR02・VR46: Runtime の 1 件の実行(dispatch への引渡し・session・記録・取消し)と自動開発の投入口。

Runtime は依頼の中身によらず dispatch を実行する。dispatch は ``"dispatch"`` に登録した代役で、Context と request を観測する。
"""

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import trackeverything.core.builtin.app as entry
from tests.async_helpers import WAIT, FakeQueue, make_runtime
from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.call import AgentResult
from trackeverything.core.abc.submission import REQUEST_ID, EventRequest
from trackeverything.core.builtin.domain.errors import ErrorNote

HEX32 = re.compile(r"[0-9a-f]{32}")
ID = "1700000000000-0"


def _agent(result=None):
    agent = Mock(spec=BaseAgent)
    agent.run.return_value = result or AgentResult(status="ok", response="done")
    return agent


def _request(**changes):
    return EventRequest(**({"provider": "github", "external_id": "delivery-1", "payload": {"action": "opened"}} | changes))


async def test_rs02_missing_session_id_starts_a_new_session():
    agent, store = _agent(), AsyncMock()
    await make_runtime({"dispatch": agent}, store=store).execute(_request(), ID)
    run_id, session_id = store.begin_run.await_args.args
    assert HEX32.fullmatch(run_id) and HEX32.fullmatch(session_id) and session_id != run_id
    ctx = agent.run.await_args.args[0].ctx
    assert (ctx.run_id, ctx.session_id) == (run_id, session_id)


async def test_rs03_given_session_is_used_with_its_parent():
    agent, store = _agent(), AsyncMock()
    # 名前指定の実行でも Runtime は payload を解釈せず、dispatch へ原形で渡す。
    payload = {"agent_name": "general", "request": "go", "data": {"pr_url": "https://github.com/o/r/pull/1"}}
    request = _request(provider="internal", external_id="cli:1", payload=payload, session_id="discord:2", parent_session_id="discord:1")
    await make_runtime({"dispatch": agent}, store=store).execute(request, ID)
    event = {"provider": "internal", "external_id": "cli:1", "payload": payload}
    begun = store.begin_run.await_args
    assert begun.args[1] == "discord:2"
    assert begun.kwargs == {"agent_name": "dispatch", "data": {"event": event}, "request": None, "parent_session_id": "discord:1"}
    call = agent.run.await_args.args[0]
    assert call.request is None
    assert (call.ctx.session_id, call.ctx.parent_session_id, call.ctx.message_id) == ("discord:2", "discord:1", ID)
    assert call.ctx.data == {"event": event} and call.ctx.authorization is None


async def test_rs06_failed_start_record_does_not_run_the_agent():
    agent, store = _agent(), AsyncMock()
    store.begin_run.side_effect = RuntimeError("db down")
    result = await make_runtime({"dispatch": agent}, store=store).execute(_request(), ID)
    assert result.status == "error" and [note.stage for note in result.errors] == ["session"]
    agent.run.assert_not_called()
    store.finish_run.assert_not_called()


async def test_rs08_version_without_dispatch_is_an_error_and_recorded():
    store = AsyncMock()
    result = await make_runtime({}, store=store).execute(_request(), ID)
    assert result.status == "error" and result.errors == (ErrorNote("entrance", "unknown entrance: dispatch"),)
    store.finish_run.assert_awaited_once_with(store.begin_run.await_args.args[0], result)


async def test_rs09_unexpected_exception_is_an_execution_error():
    agent, store = _agent(), AsyncMock()
    agent.run.side_effect = KeyError("pr_url")
    result = await make_runtime({"dispatch": agent}, store=store).execute(_request(), ID)
    assert result.status == "error" and result.errors == (ErrorNote("execution", "KeyError: 'pr_url'"),)
    store.finish_run.assert_awaited_once_with(store.begin_run.await_args.args[0], result)


async def test_sr02_cancelled_run_is_recorded_and_not_acknowledged():
    started, store, queue = asyncio.Event(), AsyncMock(), FakeQueue()

    async def hold(call):
        started.set()
        await asyncio.Event().wait()

    agent = _agent()
    agent.run.side_effect = hold
    task = asyncio.create_task(make_runtime({"dispatch": agent}, store=store, queue=queue).execute(_request(), ID))
    await asyncio.wait_for(started.wait(), WAIT)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    run_id, result = store.finish_run.await_args.args
    assert run_id == store.begin_run.await_args.args[0] and result.status == "cancelled"
    assert queue.acked == []


async def test_vr46_prepare_runtime_state_only_installs_the_producer(monkeypatch):
    """VR46: Host の起動処理は Producer を ``queue/child.py`` に登録するだけで、部品の module を呼ばない。投入は実行中のメッセージを親とする。"""
    # Host の起動処理は版の作業単位(unit_of_work・component)を経由せず、部品の module も束縛しない。
    assert [name for name in ("unit_of_work", "component") if hasattr(entry, name)] == []
    assert [name for name in ("migration", "project_store", "issue_workflow", "sentry_chain", "list_project_labels") if hasattr(entry, name)] == []
    from trackeverything.queue import child

    producer = AsyncMock()
    try:
        await entry.prepare_runtime_state(SimpleNamespace(producer=producer))
        payload = {"issue_url": "https://github.com/o/r/issues/5", "channel_id": "10", "session_id": "s-5", "request": "開始"}
        with pytest.raises(RuntimeError):
            await child.publish_child(payload, "auto-start", session_id="s-5")
        producer.publish.assert_not_awaited()
        token = REQUEST_ID.set("5-0")
        try:
            await child.publish_child(payload, "auto-start", session_id="s-5")
        finally:
            REQUEST_ID.reset(token)
        producer.publish.assert_awaited_once_with(EventRequest(provider="internal", external_id="5-0:auto-start", session_id="s-5", payload=payload))
    finally:
        child.install(None)
