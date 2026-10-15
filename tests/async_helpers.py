"""asyncio 用例の固定 LLM・記録・待機。並行の証明は sleep ではなく条件の成立で行う。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

import pytest

from trackeverything.core.abc.llm import LLMClient

WAIT = 5


class ScriptedAsyncLLM(LLMClient):
    """予定した応答を順に返す。尽きたら予定外の呼出しとして失敗させる。"""

    def __init__(self, *responses: Any) -> None:
        self.responses = iter(responses)
        self.requests: list[tuple[list[Any], list[dict[str, Any]]]] = []

    async def respond(self, messages, callables):  # type: ignore[no-untyped-def]
        self.requests.append((list(messages), callables))
        response = next(self.responses, None)
        if response is None:
            pytest.fail("予定外の LLM 呼出し")
        if isinstance(response, Exception):
            raise response
        return response


class StuckLLM(LLMClient):
    """呼ばれたら永久に待つ。期限による取消しの用例が使う。"""

    def __init__(self) -> None:
        self.calls = 0

    async def respond(self, messages, callables):  # type: ignore[no-untyped-def]
        self.calls += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class Recorder:
    """非同期の LogRecorder。記録した LogEvent を順に保持する。"""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def record(self, event: Any) -> None:
        self.events.append(event)

    def of(self, event_type: str) -> list[Any]:
        return [event for event in self.events if event.event_type == event_type]


async def until(predicate: Any, timeout: float = WAIT) -> None:
    """条件が成り立つまで loop へ制御を返す。期限切れは待機の取り違えとして失敗させる。"""
    try:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0)
    except TimeoutError:
        pytest.fail("条件が期限内に成り立たない")


async def settle(rounds: int = 50) -> None:
    """待機中の task が進めるところまで進める(時間は進めない)。"""
    for _ in range(rounds):
        await asyncio.sleep(0)


class StaticRun:
    """``StaticRepo.run()`` の結果。RunVersions と同じく名前で版と Agent を返し、解放後は ``RuntimeError``。"""

    def __init__(self, repo: StaticRepo) -> None:
        self.repo = repo
        self.released = False
        #: 引いた順の ``("version" | "agent", 名前)``。
        self.lookups: list[tuple[str, str]] = []

    def _check(self) -> None:
        if self.released:
            raise RuntimeError("run versions already released")

    def version(self, name: str) -> SimpleNamespace:
        self._check()
        self.lookups.append(("version", name))
        return self.repo.version

    def agent(self, name: str) -> Any:
        self._check()
        self.lookups.append(("agent", name))
        return self.repo.version.graph.agents.get(name)


class StaticRepo:
    """固定の部品グラフを一つの版として返す Repo の代役(Runtime が使う部分だけ)。

    ``agents`` は全て入口(``entrances`` で絞れる)。子も ``run.agent(name)`` で同じ対応表から引く。
    Agent の ``version_label`` は設定しない。
    """

    def __init__(self, agents: dict[str, Any] | None = None, sources: dict[str, Any] | None = None, *, entrances: Iterable[str] | None = None) -> None:
        fixed = dict(agents or {})
        graph = SimpleNamespace(agents=fixed, entrances=frozenset(fixed if entrances is None else entrances), sources=dict(sources or {}))
        commit = SimpleNamespace(id="static000000", parent=None, message="static", created_at=None)
        self.version = SimpleNamespace(commit=commit, id=commit.id, label="c.static00", graph=graph)
        self.refreshes = 0
        self.runs: list[StaticRun] = []
        #: 解放した run(run ごとに一度だけ)。
        self.released: list[StaticRun] = []
        self.closes = 0
        self.closed = False

    async def refresh(self) -> SimpleNamespace:
        """DB の main を読む代わりに数えるだけ。返り値は固定の Commit。"""
        self.refreshes += 1
        return self.version.commit

    def run(self) -> StaticRun:
        run = StaticRun(self)
        self.runs.append(run)
        return run

    def release(self, run: StaticRun) -> None:
        if run.released:
            return
        run.released = True
        self.released.append(run)

    async def close(self) -> None:
        self.closes += 1
        self.closed = True


class FakeQueue:
    """Runtime の読取り元の代役。入れた ``(message_id, request)`` を 1 件ずつ返し、ACK を記録する。"""

    def __init__(self, *deliveries: tuple[str, Any]) -> None:
        self.inbox: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        for delivery in deliveries:
            self.inbox.put_nowait(delivery)
        self.prepared = 0
        self.acked: list[str] = []

    async def prepare(self) -> None:
        self.prepared += 1

    async def next(self) -> tuple[str, Any] | None:
        try:
            return await asyncio.wait_for(self.inbox.get(), 0.01)
        except TimeoutError:
            return None

    async def ack(self, message_id: str) -> None:
        self.acked.append(message_id)


def make_runtime(agents: dict[str, Any] | None = None, *, store: Any = None, queue: Any = None, sources: dict[str, Any] | None = None, repo: Any = None, **options: Any) -> Any:
    """入口の代役を一つの版に持つ Runtime。全ての ``agents`` が入口で、session store は mock。

    ``repo`` を渡すと ``agents``・``sources`` は使わず、その版庫(``StaticRepo`` か実際の ``Repo``)を使う。
    """
    from unittest.mock import AsyncMock

    from trackeverything.core.builtin.runtime import Runtime

    return Runtime(queue or FakeQueue(), repo if repo is not None else StaticRepo(agents, sources), store or AsyncMock(), **options)
