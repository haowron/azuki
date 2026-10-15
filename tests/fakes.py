"""固定 LLM 応答と並行場面の短い待機。"""

import asyncio
import threading

import pytest

from trackeverything.core.abc.call import AgentCall
from trackeverything.core.abc.context import AgentContext
from trackeverything.core.abc.llm import LLMClient


class ScriptedLLM(LLMClient):
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    async def respond(self, messages, callables):
        self.requests.append((list(messages), callables))
        response = next(self.responses, None)
        if response is None:
            pytest.fail("予定外の LLM 呼出し")
        if isinstance(response, Exception):
            raise response
        return response


def agent_call(data=None, request=None, **context):
    return AgentCall(AgentContext(data=data or {}, **context), request)


def in_thread(target, *args, **kwargs):
    """並行の場面で target を別 thread で始める。結果と例外は finish で受け取る。"""
    outcome = {}

    def run():
        try:
            outcome["value"] = target(*args, **kwargs)
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.outcome = outcome
    thread.start()
    return thread


def finish(thread, timeout=5):
    """thread の終了を待ち、期限切れは待機の取り違え(deadlock を含む)として失敗させる。"""
    thread.join(timeout)
    if thread.is_alive():
        pytest.fail("並行の処理が期限内に終わらない")
    if "error" in thread.outcome:
        raise thread.outcome["error"]
    return thread.outcome.get("value")


def in_task(target, *args, **kwargs):
    """並行の場面で coroutine 関数 target を task として始める。結果は finish_task で受け取る。"""
    return asyncio.create_task(target(*args, **kwargs))


async def finish_task(task, timeout=5):
    """task の終了を待ち、期限切れは待機の取り違え(deadlock を含む)として失敗させる。"""
    try:
        return await asyncio.wait_for(task, timeout)
    except TimeoutError:
        pytest.fail("並行の処理が期限内に終わらない")
