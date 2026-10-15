"""VR49・AS17: run は RUN_TIMEOUT で取り消され、cancelled として記録される。

VR49 は EventRequest を ``config.DISPATCH_AGENT`` の名前の代役で実行し、子は run の取得口から名前で得る。
"""

import asyncio
import importlib
from unittest.mock import AsyncMock, Mock

import pytest

import trackeverything.config as config
from tests.async_helpers import WAIT, ScriptedAsyncLLM, StaticRepo, StuckLLM, make_runtime
from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.harness import ErrorHarness
from trackeverything.core.abc.llm import Call, Calls
from trackeverything.core.abc.submission import EventRequest
from trackeverything.core.builtin.domain.errors import ErrorNote

DEADLINE = 0.05


class _Entry(BaseAgent):
    """dispatch の代役。子 ``slow`` を名前だけで宣言し、LLM が ``slow`` を呼ぶ。"""

    name = config.DISPATCH_AGENT
    description = "dispatch"
    agents = ("slow",)


class _Slow(BaseAgent):
    """呼ばれた Context を記録してから、止まる LLM で LLM ループを始める子。"""

    name = "slow"
    description = "slow"

    def __init__(self, llm):
        super().__init__(llm)
        self.contexts = []

    async def _execute(self, call):
        self.contexts.append(call.ctx)
        return await super()._execute(call)


async def test_vr49_cancelled_run_is_recorded_with_the_child_resolved_by_name(monkeypatch):
    """期限で取り消した run は cancelled として記録され、子は ctx の取得口から名前で得て、取得口は一度だけ解放される。"""
    monkeypatch.setattr(config, "RUN_TIMEOUT", DEADLINE)
    entry_llm, slow_llm = ScriptedAsyncLLM(Calls((Call("slow", {"request": "dig"}),))), StuckLLM()
    entry, slow = _Entry(entry_llm), _Slow(slow_llm)
    harness = Mock(spec=ErrorHarness)
    harness.name = "failure_notify"
    harness.handle = AsyncMock()
    entry.register_error_harness(harness)
    repo = StaticRepo({entry.name: entry, slow.name: slow}, entrances=(entry.name,))
    store = AsyncMock()
    request = EventRequest(provider="discord", external_id="10", payload={"content": "go"}, session_id="discord:10")
    result = await asyncio.wait_for(make_runtime(store=store, repo=repo).execute(request, "1700000000000-0"), WAIT)
    assert result.status == "cancelled"
    assert result.errors == (ErrorNote(stage="deadline", cause=f"run exceeded {DEADLINE:g}s"),)
    store.begin_run.assert_awaited_once()
    run_id, finished = store.finish_run.await_args.args
    assert run_id == store.begin_run.await_args.args[0]
    assert finished.status == "cancelled" and finished.errors[0].stage == "deadline"
    # 子は束縛ではなく、名前で ctx.agents(この run の取得口)から得た。LLM にも名前で提示した。
    assert [item["name"] for item in entry_llm.requests[0][1]].count("slow") == 1
    [run] = repo.runs
    assert ("version", config.DISPATCH_AGENT) in run.lookups and ("agent", "slow") in run.lookups
    assert slow_llm.calls == 1 and len(slow.contexts) == 1
    ctx = slow.contexts[0]
    assert ctx.agents is run
    assert repo.released == [run]


@pytest.fixture
def load_config(monkeypatch):
    def load(**env):
        if "RUN_TIMEOUT" in env:
            monkeypatch.setenv("RUN_TIMEOUT", env["RUN_TIMEOUT"])
        else:
            monkeypatch.delenv("RUN_TIMEOUT", raising=False)
        return importlib.reload(config)

    yield load
    monkeypatch.undo()
    importlib.reload(config)


def test_as17_deadline_configuration(load_config):
    assert load_config().RUN_TIMEOUT == 3600
    assert load_config(RUN_TIMEOUT="120").RUN_TIMEOUT == 120
    for invalid in ("0", "-5", "soon"):
        with pytest.raises(ValueError, match="RUN_TIMEOUT"):
            load_config(RUN_TIMEOUT=invalid)

