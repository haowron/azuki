"""Optimizer の骨格: OP01-OP09・OP39(docs/components/OPTIMIZER.md)。

実行記録の読取り・時刻・観点は代役にし、PostgreSQL と実 LLM を使わない。main の版 id は代役の store で開いた
singleton の版庫から得る(docs/components/OPTIMIZER.md)。
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from tests.fakes import ScriptedLLM
from trackeverything import optimizer
from trackeverything.core.builtin.domain.execution_log import current_run_id
from trackeverything.core.builtin.execution_log import RunTrace
from trackeverything.optimizer import Finding, Optimizer

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)


class Recording:
    """固定の finding か例外を返し、受け取った Context を記録する観点。"""

    def __init__(self, name, *findings, error=None):
        self.name = name
        self.findings = findings
        self.error = error
        self.seen = []

    async def run(self, ctx):
        self.seen.append(ctx)
        if self.error is not None:
            raise self.error
        return list(self.findings)


def trace(run_id):
    return RunTrace(run_id=run_id, events=[], record=None)


@pytest.fixture
def llms():
    return {"max": ScriptedLLM()}


@pytest.fixture
async def main_version(singleton):
    """singleton の版庫を開き、その main の版 id を返す。"""
    _, _, main = await singleton()
    return main.id


@pytest.fixture
def installed(llms, main_version):
    """``Optimizer`` を作って install し、終了時に外す。"""

    def make(*checks, runs=("r1", "r2", "r3"), read=None):
        read = read or AsyncMock(return_value=[trace(run_id) for run_id in runs])
        optimizer.install(Optimizer(llms=llms, read_runs=read, checks=checks, clock=lambda: NOW))
        return read

    yield make
    optimizer.install(None)


async def test_op01_checks_share_one_observation_and_report_in_order(installed, llms, main_version):
    """OP01: 読取りは limit=100 で一度、観点は登録順に同じ観測を受け取り、報告は登録順に finding を持つ。"""
    a = Recording("a", Finding(summary="a-1", evidence=("r1",)))
    b = Recording("b", Finding(summary="b-1"), Finding(summary="b-2", evidence=("r2", "r3")))
    read = installed(a, b)

    result = await optimizer.handle({})

    read.assert_awaited_once_with(limit=100)
    assert result.status == "ok" and result.errors == ()
    assert len(a.seen) == len(b.seen) == 1
    observation = a.seen[0].observation
    assert b.seen[0].observation is observation
    assert [run.run_id for run in observation.runs] == ["r1", "r2", "r3"]
    assert observation.main_version == main_version and observation.observed_at == NOW
    assert a.seen[0].llms is llms
    assert result.response == {
        "observed_at": "2026-10-13T09:00:00+00:00",
        "main_version": main_version,
        "run_count": 3,
        "checks": [
            {"name": "a", "status": "ok", "findings": [{"summary": "a-1", "evidence": ["r1"]}]},
            {"name": "b", "status": "ok", "findings": [{"summary": "b-1", "evidence": []},
                                                       {"summary": "b-2", "evidence": ["r2", "r3"]}]},
        ],
    }


async def test_op02_a_failed_check_is_recorded_and_the_next_check_still_runs(installed):
    """OP02: 失敗した観点は failed として記録し、次の観点を実行する。status は error、response は報告全体。"""
    a = Recording("a", error=RuntimeError("boom"))
    b = Recording("b", Finding(summary="b-1"))
    installed(a, b)

    result = await optimizer.handle({})

    assert len(b.seen) == 1
    assert result.status == "error"
    assert [(note.stage, note.cause) for note in result.errors] == [("check:a", "RuntimeError: boom")]
    assert result.response["checks"] == [
        {"name": "a", "status": "failed", "error": "RuntimeError: boom", "findings": []},
        {"name": "b", "status": "ok", "findings": [{"summary": "b-1", "evidence": []}]},
    ]


async def test_op03_selected_checks_run_alone_and_unknown_names_are_rejected(installed):
    """OP03: checks で選んだ観点だけを実行する。未登録の名前は読取りも観点も呼ばずに error。"""
    a, b = Recording("a"), Recording("b")
    read = installed(a, b)

    selected = await optimizer.handle({"checks": ["b"]})

    assert selected.status == "ok"
    assert a.seen == [] and len(b.seen) == 1
    assert [check["name"] for check in selected.response["checks"]] == ["b"]

    read.reset_mock()
    b.seen.clear()
    unknown = await optimizer.handle({"checks": ["x"]})

    assert unknown.status == "error"
    assert [note.stage for note in unknown.errors] == ["input"] and "x" in unknown.errors[0].cause
    read.assert_not_awaited()
    assert a.seen == [] and b.seen == []


@pytest.mark.parametrize(
    "data",
    [
        {"limit": 0},
        {"limit": 1001},
        {"limit": "100"},
        {"limit": 100, "since": "yesterday"},
        {"checks": ["a", "a"]},
        {"merge": "no"},
    ],
    ids=["limit-0", "limit-1001", "limit-text", "unknown-field", "duplicate-check", "merge-text"],
)
async def test_op04_invalid_input_reads_nothing(installed, data):
    """OP04: 不正な入力は読取りも観点も呼ばずに error。"""
    a = Recording("a")
    read = installed(a)

    result = await optimizer.handle(data)

    assert result.status == "error" and [note.stage for note in result.errors] == ["input"]
    read.assert_not_awaited()
    assert a.seen == []


async def test_op05_the_optimizer_run_itself_is_not_observed(installed):
    """OP05: 読取り結果から現在の run を除く。"""
    a = Recording("a")
    installed(a, runs=("r1", "self", "r3"))
    token = current_run_id.set("self")
    try:
        result = await optimizer.handle({})
    finally:
        current_run_id.reset(token)

    assert [run.run_id for run in a.seen[0].observation.runs] == ["r1", "r3"]
    assert result.response["run_count"] == 2


async def test_op06_a_read_failure_runs_no_check(installed):
    """OP06: 実行記録の読取りが失敗したら観点を呼ばずに error。"""
    a = Recording("a")
    installed(a, read=AsyncMock(side_effect=OSError("database unreachable")))

    result = await optimizer.handle({})

    assert result.status == "error"
    assert [(note.stage, note.cause) for note in result.errors] == [("observe", "OSError: database unreachable")]
    assert a.seen == []


async def test_op07_handle_without_install_is_an_error():
    """OP07: install されていないプロセスでは error。"""
    optimizer.install(None)

    result = await optimizer.handle({})

    assert result.status == "error"
    assert [(note.stage, note.cause) for note in result.errors] == [("optimizer", "optimizer is not installed in this process")]


async def test_op08_cancellation_propagates_and_stops_later_checks(installed):
    """OP08: 観点の実行中の取消しは failed にせず伝え、後の観点を呼ばない。"""
    started = asyncio.Event()

    class Blocking:
        name = "a"

        async def run(self, ctx):
            started.set()
            await asyncio.Event().wait()

    b = Recording("b")
    installed(Blocking(), b)
    task = asyncio.create_task(optimizer.handle({}))
    await asyncio.wait_for(started.wait(), 1)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert b.seen == []


def test_op09_duplicate_check_names_are_rejected(llms):
    """OP09: 同じ名前の観点を二つ渡すと作成時に ValueError。"""
    with pytest.raises(ValueError, match="a"):
        Optimizer(llms=llms, read_runs=AsyncMock(), checks=(Recording("a"), Recording("a")))


async def test_op39_merge_reaches_every_check(installed):
    """OP39: ``merge`` の既定は真で、``{"merge": false}`` なら全ての観点の Context が偽を受け取る。"""
    a = Recording("a")
    b = Recording("b")
    installed(a, b)

    await optimizer.handle({})
    await optimizer.handle({"merge": False})

    assert [ctx.merge for ctx in a.seen] == [True, False]
    assert [ctx.merge for ctx in b.seen] == [True, False]
