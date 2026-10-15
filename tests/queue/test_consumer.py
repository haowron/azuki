"""SQ03・SQ06〜SQ09・VR52: Runtime の読取り loop と、EventRequest の取り直し・dead・並行上限・停止。"""

import asyncio
import json
from unittest.mock import call

import pytest

from tests.async_helpers import WAIT, StaticRepo, settle, until
from tests.fakes import ScriptedLLM
from tests.queue.helpers import DEAD, QUEUE, Held, claim, redis, request, running
from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.call import AgentResult

ID = "1700000000000-0"


class Recording(BaseAgent):
    """Context に渡った queue の ID を記録し、dispatch と同じく終わりに ACK する dispatch の代役。"""

    name = "dispatch"

    def __init__(self):
        super().__init__(ScriptedLLM())
        self.seen = []

    async def _execute(self, call):
        self.seen.append(call.ctx.message_id)
        await call.ctx.ack(call.ctx.message_id)
        return AgentResult(status="ok")


def queue_commands(client):
    return [name for name, *_ in client.mock_calls if name in {"xadd", "xack", "xdel"}]


async def test_sq03_runtime_hands_the_stream_id_to_the_agent():
    agent = Recording()
    client = redis((ID, request().model_dump_json()))
    runtime = await running(client, agent)
    await until(lambda: agent.seen == [ID])
    read = client.xreadgroup.await_args_list[0]
    assert read.args == ("runtime", "runtime", {QUEUE: ">"}) and read.kwargs == {"count": 1, "block": 1000}
    await until(lambda: client.xdel.await_count == 1)
    await runtime.stop()


async def test_sq06_idle_pending_message_is_claimed_and_run_again():
    agent = Recording()
    client = claim(ID, request().model_dump_json(), deliveries=3)
    runtime = await running(client, agent)
    await until(lambda: agent.seen == [ID])
    claimed = client.xautoclaim.await_args_list[0]
    assert claimed.args == (QUEUE, "runtime", "runtime", 1_800_000) and claimed.kwargs == {"start_id": "0-0", "count": 1}
    client.xpending_range.assert_awaited_with(QUEUE, "runtime", min=ID, max=ID, count=1)
    await runtime.stop()


async def test_sq07_fourth_delivery_moves_to_dead_without_running():
    agent = Recording()
    data = request().model_dump_json()
    client = claim(ID, data, deliveries=4)
    runtime = await running(client, agent)
    await until(lambda: client.xdel.await_count == 1)
    await runtime.stop()
    assert agent.seen == []
    dead = client.xadd.await_args
    assert dead.args[0] == DEAD and dead.args[1]["id"] == ID and dead.args[1]["data"] == data and dead.args[1]["reason"]
    assert client.xack.await_args_list == [call(QUEUE, "runtime", ID)] and client.xdel.await_args_list == [call(QUEUE, ID)]
    assert queue_commands(client) == ["xadd", "xack", "xdel"]


# 削除した AgentRequest・SourceRequest が投入していた旧形式の data。
OLD_AGENT_RUN = {"kind": "agent.run", "agent_name": "general", "request": "調査する", "data": {}, "session_id": None, "parent_session_id": None}
OLD_SOURCE_RUN = {"kind": "source.run", "agent_name": "project_memory", "source": "github_pull_request",
                  "source_kwargs": {"owner": "o", "repo": "r", "number": 1}, "project": None}


@pytest.mark.parametrize("data", ["not json", '{"kind": "unknown.kind"}', json.dumps(OLD_AGENT_RUN), json.dumps(OLD_SOURCE_RUN)],
                         ids=["not-json", "unknown-kind", "old-agent-run", "old-source-run"])
async def test_sq08_unreadable_message_moves_to_dead_without_running(data):
    agent = Recording()
    client = redis((ID, data))
    runtime = await running(client, agent)
    await until(lambda: client.xdel.await_count == 1)
    await runtime.stop()
    assert agent.seen == []
    assert queue_commands(client) == ["xadd", "xack", "xdel"]
    assert client.xadd.await_args.args[0] == DEAD and client.xadd.await_args.args[1]["data"] == data


async def test_sq09_no_more_than_run_concurrency_messages_run_at_once():
    agent = Held()
    client = redis(*[(f"1700000000000-{index}", request(external_id=f"m{index}").model_dump_json()) for index in range(3)])
    runtime = await running(client, agent, concurrency=2)
    await until(lambda: len(agent.started) == 2)
    await settle()
    assert agent.started == ["m0", "m1"] and client.inbox.qsize() == 1
    agent.release("m0")
    await until(lambda: len(agent.started) == 3)
    assert agent.started[2] == "m2"
    agent.release("m1", "m2")
    await until(lambda: client.xdel.await_count == 3)
    await runtime.stop()


async def test_vr52_stop_stops_reading_waits_for_running_work_then_closes_the_repo():
    """停止は読取りを止め、実行中のメッセージの終了と run の解放を待ってから版庫を一度だけ閉じる。"""
    agent = Held()
    repo = StaticRepo({agent.name: agent})
    client = redis((ID, request(external_id="m0").model_dump_json()))
    runtime = await running(client, repo=repo)
    await until(lambda: agent.started == ["m0"])
    stopping = asyncio.create_task(runtime.stop())
    await asyncio.sleep(0)
    reads = client.xreadgroup.await_count
    await settle()
    assert not stopping.done(), "stop waits for the running message"
    assert repo.closes == 0 and repo.released == []
    agent.release("m0")
    await asyncio.wait_for(stopping, WAIT)
    assert client.xreadgroup.await_count == reads
    assert client.xack.await_args_list == [call(QUEUE, "runtime", ID)]
    assert len(repo.runs) == 1 and repo.released == repo.runs
    assert repo.closes == 1 and repo.closed
