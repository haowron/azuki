"""休眠を挟んだ実行ログの経過時間。実 API・DB・プロセスは使わない。"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.call import AgentCall
from trackeverything.core.abc.context import AgentContext
from trackeverything.core.abc.llm import Call, Calls, FinalAnswer, LLMClient
from trackeverything.core.abc.tool import Tool, ToolResult
from trackeverything.core.builtin.domain import execution_log
from trackeverything.llm.tracking import TrackedLLM


@pytest.mark.parametrize(('platform', 'constants', 'clock_id'), [
    ('darwin', {'CLOCK_MONOTONIC_RAW': 4}, 4),
    ('linux', {'CLOCK_BOOTTIME': 7, 'CLOCK_MONOTONIC_RAW': 4}, 7),
    ('other', {}, None),
])
def test_elapsed_clock_uses_platform_suspend_clock(monkeypatch, platform, constants, clock_id):
    clocks = SimpleNamespace(clock_gettime=Mock(return_value=1200.0), monotonic=Mock(return_value=1200.0), **constants)
    monkeypatch.setattr(execution_log, 'time', clocks)
    monkeypatch.setattr(execution_log, 'sys', SimpleNamespace(platform=platform), raising=False)
    assert execution_log.elapsed_clock() == 1200.0
    if clock_id is None:
        clocks.monotonic.assert_called_once()
        clocks.clock_gettime.assert_not_called()
    else:
        clocks.clock_gettime.assert_called_once_with(clock_id)
        clocks.monotonic.assert_not_called()


@pytest.fixture
def sleep_clock(monkeypatch):
    clock = [1000.0, 100.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[1])
    monkeypatch.setattr(execution_log, 'sys', SimpleNamespace(platform='darwin'), raising=False)
    monkeypatch.setattr(execution_log, 'time', SimpleNamespace(CLOCK_MONOTONIC_RAW=4, clock_gettime=lambda _: clock[0], monotonic=lambda: clock[1]))

    def advance(awake=1.0, suspended=60.0):
        clock[0] += awake + suspended
        clock[1] += awake

    return advance


async def test_agent_llm_tool_and_harness_logs_include_suspend(sleep_clock):
    logger, inner = AsyncMock(), Mock(spec=LLMClient)
    responses = iter([Calls((Call('wait_tool', {}),)), FinalAnswer('完了')])

    def respond(*args):
        sleep_clock(.5, 30)
        return next(responses)

    inner.respond.side_effect = respond
    tool = Mock(spec=Tool, name='wait_tool')
    tool.name = 'wait_tool'
    tool.description = '待機する'
    tool.arguments = {'type': 'object', 'properties': {}}
    tool.run.side_effect = lambda *a, **kw: (sleep_clock(), ToolResult(ok=True))[1]
    tool.llm_args.side_effect = lambda args: args
    tool.error_key.return_value = None
    agent = BaseAgent(TrackedLLM(inner, logger), logger=logger)
    agent.register(tool)
    for register in (agent.register_pre_harness, agent.register_post_harness):
        harness = Mock()
        harness.name = 'check'
        harness.check = AsyncMock(side_effect=lambda *a: (sleep_clock(), True)[1])
        register(harness)
    result = await agent.run(AgentCall(AgentContext(data={})))
    assert result.status == 'ok', result
    events = [call.args[0] for call in logger.record.call_args_list]
    assert [e.duration_ms for e in events if e.event_type == 'agent'] == [244000]
    assert [e.duration_ms for e in events if e.event_type == 'llm'] == [30500, 30500]
    assert [e.duration_ms for e in events if e.event_type == 'tool'] == [61000]
    assert [e.duration_ms for e in events if e.event_type == 'harness'] == [61000, 61000]


async def test_failed_llm_keeps_error_and_suspend_duration(sleep_clock):
    logger, inner = AsyncMock(), Mock(spec=LLMClient)

    def fail(*args):
        sleep_clock()
        raise RuntimeError('offline')

    inner.respond.side_effect = fail
    with pytest.raises(RuntimeError, match='offline'):
        await TrackedLLM(inner, logger).respond([], [])
    event = logger.record.call_args.args[0]
    assert event.status == 'error' and event.duration_ms == 61000
