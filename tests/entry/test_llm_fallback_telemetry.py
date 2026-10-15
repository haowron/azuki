"""fallback の診断相関・計測・秘匿化。実 API と Agent の業務判断は対象外。"""

import asyncio
import re
from contextlib import contextmanager
from unittest.mock import Mock

import httpx
import openai
import pytest

from trackeverything.core.abc.execution_log import LogRecorder
from trackeverything.core.abc.llm import FinalAnswer, LLMClient, Message
from trackeverything.core.builtin.domain import execution_log
from trackeverything.llm import fallback
from trackeverything.llm.tracking import TrackedLLM

PRIVATE = "検査用の非公開本文"


@contextmanager
def correlation(run_id, parent_id):
    run = execution_log.current_run_id.set(run_id)
    parent = execution_log.current_agent_execution_id.set(parent_id)
    try:
        yield
    finally:
        execution_log.current_agent_execution_id.reset(parent)
        execution_log.current_run_id.reset(run)


def rate_limit():
    return openai.RateLimitError(PRIVATE, response=httpx.Response(429, request=httpx.Request("POST", "https://example.invalid")), body=None)


def clients():
    primary, backup = Mock(spec=LLMClient), Mock(spec=LLMClient)
    primary.respond.side_effect = rate_limit()
    backup.respond.return_value = FinalAnswer("継続")
    return primary, backup


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(fallback, "elapsed_clock", lambda: 12.0, raising=False)
    monkeypatch.setattr(execution_log, "elapsed_clock", lambda: 14.5)


@pytest.mark.parametrize("run_id,parent_id", [("run-a", "agent-a"), (None, None)])
async def test_fallback_records_correlation_and_primary_time_without_private_text(caplog, run_id, parent_id):
    primary, backup = clients()
    messages, callables = [Message("user", PRIVATE)], [{"name": "fixture"}]
    with correlation(run_id, parent_id):
        result = await fallback.FallbackLLM(primary, backup).respond(messages, callables)
    assert result is backup.respond.return_value
    primary.respond.assert_awaited_once_with(messages, callables)
    backup.respond.assert_awaited_once_with(messages, callables)
    assert len(caplog.records) == 1
    assert f"run_id={run_id}" in caplog.text and f"parent_execution_id={parent_id}" in caplog.text
    assert "primary_ms=2500" in caplog.text and "RateLimitError" in caplog.text
    assert "llm_execution_id=None" in caplog.text
    assert PRIVATE not in caplog.text and "example.invalid" not in caplog.text


async def test_parallel_fallbacks_keep_each_run_correlation(caplog):
    primary, backup = clients()
    entered, proceed = asyncio.Event(), asyncio.Event()
    entered_count = 0

    async def fail(messages, callables):
        nonlocal entered_count
        entered_count += 1
        if entered_count == 2:
            entered.set()
        await proceed.wait()
        raise rate_limit()

    primary.respond.side_effect = fail
    llm = fallback.FallbackLLM(primary, backup)

    async def respond(run_id):
        with correlation(run_id, f"agent-{run_id}"):
            return await llm.respond([], [])

    first = asyncio.create_task(respond("one"))
    second = asyncio.create_task(respond("two"))
    await asyncio.wait_for(entered.wait(), 1)
    proceed.set()
    await asyncio.wait_for(asyncio.gather(first, second), 1)
    assert len(caplog.records) == 2 and backup.respond.await_count == 2
    for run_id in ("one", "two"):
        assert sum(f"run_id={run_id}, parent_execution_id=agent-{run_id}" in record.getMessage() for record in caplog.records) == 1


async def test_backup_failure_preserves_exception_chain_and_primary_diagnostic(caplog):
    primary, backup = clients()
    failed = RuntimeError(PRIVATE)
    backup.respond.side_effect = failed
    with correlation("run-failed", "agent-failed"), pytest.raises(RuntimeError) as caught:
        await fallback.FallbackLLM(primary, backup).respond([], [])
    assert caught.value is failed and isinstance(failed.__cause__, openai.RateLimitError)
    backup.respond.assert_awaited_once()
    assert "run_id=run-failed" in caplog.text and "primary_ms=2500" in caplog.text
    assert PRIVATE not in caplog.text


async def test_primary_success_never_records_fallback(caplog):
    primary, backup = clients()
    primary.respond.side_effect = None
    primary.respond.return_value = FinalAnswer("主モデルの結果")
    assert await fallback.FallbackLLM(primary, backup).respond([], []) is primary.respond.return_value
    backup.respond.assert_not_awaited()
    assert not caplog.records


@pytest.mark.parametrize("failure", [ValueError(PRIVATE), asyncio.CancelledError()])
async def test_non_fallback_error_and_cancellation_never_call_backup(caplog, failure):
    primary, backup = clients()
    primary.respond.side_effect = failure
    with pytest.raises(type(failure)) as caught:
        await fallback.FallbackLLM(primary, backup).respond([], [])
    assert caught.value is failure
    backup.respond.assert_not_awaited()
    assert not caplog.records


async def test_parallel_fallbacks_in_one_agent_match_distinct_llm_events(caplog):
    primary, backup = clients()
    logger = Mock(spec=LogRecorder)
    entered, proceed = asyncio.Event(), asyncio.Event()
    count = 0

    async def fail(messages, callables):
        nonlocal count
        count += 1
        if count == 2:
            entered.set()
        await proceed.wait()
        raise rate_limit()

    primary.respond.side_effect = fail
    llm = TrackedLLM(fallback.FallbackLLM(primary, backup), logger)
    with correlation("same-run", "same-agent"):
        tasks = [asyncio.create_task(llm.respond([Message("user", PRIVATE)], [])) for _ in range(2)]
        await asyncio.wait_for(entered.wait(), 1)
        proceed.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
    events = [call.args[0] for call in logger.record.await_args_list]
    warning_ids = re.findall(r"llm_execution_id=([0-9a-f]{32})", caplog.text)
    assert len(events) == len(warning_ids) == len(set(warning_ids)) == 2
    assert set(warning_ids) == {event.execution_id for event in events}
    assert all(event.run_id == "same-run" and event.parent_execution_id == "same-agent" for event in events)
    assert primary.respond.await_count == backup.respond.await_count == 2
    assert PRIVATE not in caplog.text


@pytest.mark.parametrize("outcome", ["success", "primary_error", "backup_error", "cancelled", "logger_error"])
async def test_tracked_call_restores_correlation_on_every_exit(outcome):
    primary, backup = clients()
    logger = Mock(spec=LogRecorder)
    failure = RuntimeError("記録失敗") if outcome == "logger_error" else ValueError("呼出し失敗")
    seen = []

    async def respond(messages, callables):
        seen.append(execution_log.current_llm_execution_id.get())
        if outcome == "success":
            return FinalAnswer("成功")
        if outcome == "cancelled":
            raise asyncio.CancelledError()
        if outcome == "backup_error":
            raise rate_limit()
        raise failure

    primary.respond.side_effect = respond
    if outcome == "backup_error":
        backup.respond.side_effect = failure
    if outcome == "logger_error":
        logger.record.side_effect = failure
    assert execution_log.current_llm_execution_id.get() is None
    llm = TrackedLLM(fallback.FallbackLLM(primary, backup), logger)
    if outcome == "success":
        assert (await llm.respond([], [])).text == "成功"
    else:
        with pytest.raises(asyncio.CancelledError if outcome == "cancelled" else type(failure)):
            await llm.respond([], [])
    assert len(seen) == 1 and re.fullmatch(r"[0-9a-f]{32}", seen[0])
    assert execution_log.current_llm_execution_id.get() is None
    if outcome != "cancelled":
        assert logger.record.await_args.args[0].execution_id == seen[0]
    else:
        logger.record.assert_not_awaited()


async def test_nested_tracked_calls_restore_the_outer_call_id():
    inner, outer = Mock(spec=LLMClient), Mock(spec=LLMClient)
    logger = Mock(spec=LogRecorder)
    observed = []

    async def inside(messages, callables):
        observed.append(execution_log.current_llm_execution_id.get())
        return FinalAnswer("内側")

    async def outside(messages, callables):
        observed.append(execution_log.current_llm_execution_id.get())
        result = await TrackedLLM(inner, logger).respond(messages, callables)
        observed.append(execution_log.current_llm_execution_id.get())
        return result

    inner.respond.side_effect = inside
    outer.respond.side_effect = outside
    assert (await TrackedLLM(outer, logger).respond([], [])).text == "内側"
    assert observed[0] == observed[2] and observed[0] != observed[1]
    assert {call.args[0].execution_id for call in logger.record.await_args_list} == set(observed)
    assert execution_log.current_llm_execution_id.get() is None
