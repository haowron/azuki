"""SQ04・SQ05・RI10〜RI11: ACK の所有者は dispatch だけであることと、provider によらない dispatch。"""

import asyncio
import re
from unittest.mock import AsyncMock, Mock, call

import pytest

from tests.async_helpers import WAIT, until
from tests.fakes import ScriptedLLM
from tests.queue.helpers import QUEUE, module, redis, request, runtime_for
from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.call import AgentResult

ID = "1700000000000-0"
HEX32 = re.compile(r"[0-9a-f]{32}")


def event(provider, payload, session_id=None, parent_session_id=None, external_id="e1"):
    return module("core.abc.submission").EventRequest(provider=provider, external_id=external_id, payload=payload,
                                                      session_id=session_id, parent_session_id=parent_session_id)


class Dispatch(BaseAgent):
    """Context の queue ID を記録し、指定された場合だけ ACK する dispatch の代役。"""

    name = "dispatch"

    def __init__(self, acknowledge):
        super().__init__(ScriptedLLM())
        self.acknowledge, self.seen = acknowledge, []

    async def _execute(self, call):
        self.seen.append(call.ctx.message_id)
        if self.acknowledge:
            await call.ctx.ack(call.ctx.message_id)
        return AgentResult(status="ok")


@pytest.mark.parametrize("acknowledge", [True, False], ids=["dispatch-acks", "dispatch-does-not-ack"])
async def test_sq04_event_is_acknowledged_only_by_the_dispatch_agent(acknowledge):
    client, agent = redis(), Dispatch(acknowledge)
    ping = event("github", {"headers": {"x-github-event": "ping", "x-github-delivery": "d-1"}, "body": {"zen": "z"}})
    await runtime_for(agent, client=client).execute(ping, ID)
    assert agent.seen == [ID]
    if acknowledge:
        assert client.xack.await_args_list == [call(QUEUE, "runtime", ID)] and client.xdel.await_args_list == [call(QUEUE, ID)]
    else:
        client.xack.assert_not_awaited()
        client.xdel.assert_not_awaited()


@pytest.mark.parametrize("case", ["dispatch-ok", "dispatch-error", "dispatch-exception"])
async def test_sq05_runtime_never_acknowledges_when_dispatch_does_not(case):
    client = redis()

    async def run(agent_call):
        # dispatch の代役は ctx.ack を呼ばずに終わる。
        if case == "dispatch-exception":
            raise RuntimeError("dispatch failed")
        return AgentResult(status="ok" if case == "dispatch-ok" else "error")

    agent = Mock(spec=BaseAgent)
    agent.name = "dispatch"
    agent.run = AsyncMock(side_effect=run)
    await runtime_for(agent, client=client).execute(request(), ID)
    agent.run.assert_awaited_once()
    assert agent.run.await_args.args[0].ctx.message_id == ID
    client.xack.assert_not_awaited()
    client.xdel.assert_not_awaited()


@pytest.mark.parametrize(("provider", "payload", "session_id", "parent_session_id"), [
    ("github", {"headers": {"x-github-event": "ping", "x-github-delivery": "d-1"}, "body": {"zen": "z"}}, None, None),
    ("discord", {"id": "20", "channel_id": "11", "author": {"id": "1"}, "content": "質問"}, "session-11", "session-9"),
    ("slack", {"type": "event_callback", "event_id": "E", "event": {"type": "message"}}, None, None),
    ("internal", {"issue_url": "https://github.com/o/r/issues/5", "channel_id": "10", "session_id": "s", "request": "開始"}, "s", None),
])
async def test_ri10_every_provider_starts_dispatch_with_the_event_unchanged(provider, payload, session_id, parent_session_id):
    agent = Mock(spec=BaseAgent)
    agent.name = "dispatch"
    agent.run = AsyncMock(return_value=AgentResult(status="ok"))
    result = await runtime_for(agent).execute(event(provider, payload, session_id, parent_session_id), ID)
    assert result.status == "ok"
    started = agent.run.await_args.args[0]
    assert started.request is None
    assert started.ctx.data == {"event": {"provider": provider, "external_id": "e1", "payload": payload}}
    assert started.ctx.parent_session_id == parent_session_id and started.ctx.message_id == ID
    assert started.ctx.session_id == session_id if session_id is not None else HEX32.fullmatch(started.ctx.session_id)


class HeldDispatch(BaseAgent):
    """開始した event を記録し、解放されるまで終わらない dispatch。"""

    name = "dispatch"

    def __init__(self):
        super().__init__(ScriptedLLM())
        self.started, self.release = [], asyncio.Event()

    async def _execute(self, call):
        self.started.append(call.ctx.data["event"]["external_id"])
        await self.release.wait()
        return AgentResult(status="ok")


async def test_ri11_same_session_and_same_pr_without_session_run_concurrently():
    agent = HeldDispatch()
    events = runtime_for(agent)
    message = {"id": "20", "channel_id": "10", "author": {"id": "1"}, "content": "質問"}
    body = {"action": "synchronize", "repository": {"name": "r", "owner": {"login": "o"}}, "pull_request": {"number": 1}}
    same_session = [event("discord", message, "s", external_id=key) for key in ("a", "b")]
    same_pr = [event("github", {"headers": {"x-github-event": "pull_request", "x-github-delivery": key}, "body": body}, external_id=key) for key in ("c", "d")]
    for group, ids in ((same_session, ("1", "2")), (same_pr, ("3", "4"))):
        agent.started, agent.release = [], asyncio.Event()
        tasks = [asyncio.create_task(events.execute(item, f"1700000000000-{key}")) for item, key in zip(group, ids, strict=True)]
        # 先の run が終わる前に後の run も始まる(session による直列化はない)。
        await until(lambda group=group: sorted(agent.started) == [item.external_id for item in group])
        agent.release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), WAIT)
