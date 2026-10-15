"""Redis client の境界応答だけを差し替える。疑似 Redis は実装しない。"""

import asyncio
from importlib import import_module
from unittest.mock import AsyncMock

from tests.async_helpers import StaticRepo
from tests.fakes import ScriptedLLM
from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.call import AgentResult

QUEUE, DEAD = "te:{owner}:queue", "te:{owner}:dead"


def module(name):
    return import_module(f"trackeverything.{name}")


def request(**changes):
    """名前指定の実行(``internal``)の ``EventRequest``。``changes`` は最上位の項目を差し替える。"""
    named = {"provider": "internal", "external_id": "cli:general", "payload": {"agent_name": "general", "request": "調査する"}}
    return module("core.abc.submission").EventRequest(**(named | changes))


def redis(*entries):
    """redis-py の非同期 client の代役。

    ``entries`` は ``(stream id, data)``。``XREADGROUP`` は ``client.inbox`` から 1 件ずつ返し、
    空の間は BLOCK の代わりに短く待って空を返す。``XAUTOCLAIM`` は既定で何も取り直さない。
    """
    client = AsyncMock()
    client.inbox = asyncio.Queue()
    for entry in entries:
        client.inbox.put_nowait(entry)

    async def xreadgroup(groupname, consumername, streams, count=None, block=None):
        try:
            message_id, data = await asyncio.wait_for(client.inbox.get(), 0.01)
        except TimeoutError:
            return []
        return [[QUEUE, [(message_id, {"data": data})]]]

    client.xreadgroup.side_effect = xreadgroup
    client.xautoclaim.return_value = ["0-0", [], []]
    client.xpending_range.return_value = []
    client.xadd.return_value = "1700000000000-0"
    return client


def claim(message_id, data, deliveries):
    """``XAUTOCLAIM`` が一度だけ ``message_id`` を取り直し、``XPENDING`` が配送回数を返す設定。"""
    client = redis()
    pending = [["0-0", [(message_id, {"data": data})], []]]
    client.xautoclaim.side_effect = lambda *args, **kwargs: pending.pop(0) if pending else ["0-0", [], []]
    client.xpending_range.return_value = [{"message_id": message_id, "consumer": "runtime", "time_since_delivered": 0, "times_delivered": deliveries}]
    return client


def stream(client):
    queue = module("queue.redis_stream").RedisStream("redis://unused", namespace="owner")
    queue.client = client
    return queue


class Held(BaseAgent):
    """``external_id`` ごとの event が立つまで終わらない dispatch の代役。開始順と Context を記録し、
    dispatch と同じく終わりに ``ctx.ack(ctx.message_id)`` を呼ぶ。"""

    name = "dispatch"

    def __init__(self):
        super().__init__(ScriptedLLM())
        self.started, self.contexts, self._gates = [], [], {}

    def _gate(self, name):
        return self._gates.setdefault(name, asyncio.Event())

    def release(self, *names):
        for name in names:
            self._gate(name).set()

    async def _execute(self, call):
        key = call.ctx.data["event"]["external_id"]
        self.started.append(key)
        self.contexts.append(call.ctx)
        await self._gate(key).wait()
        await call.ctx.ack(call.ctx.message_id)
        return AgentResult(status="ok")


def runtime_for(*agents, client=None, concurrency=4, repo=None):
    """dispatch の代役を一つの版に持ち、読取り元が mock の Redis を使う Runtime(未起動)。

    ``repo`` を渡すとその版庫の代役を使い、渡さなければ ``agents`` を全て入口とする ``StaticRepo`` を作る。
    """
    queue = module("queue.consumer").QueueConsumer(stream(client or redis()))
    if repo is None:
        repo = StaticRepo({agent.name: agent for agent in agents})
    return module("core.builtin.runtime").Runtime(queue, repo, AsyncMock(), concurrency=concurrency)


async def running(client, *agents, concurrency=4, repo=None):
    """Runtime を起動する。読取り loop が ``client`` から読む。"""
    runtime = runtime_for(*agents, client=client, concurrency=concurrency, repo=repo)
    await runtime.start()
    return runtime
