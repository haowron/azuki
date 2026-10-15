"""既定の dispatch。queue の全てのメッセージを受け取る唯一の入口で、LLM を使わない。

``internal`` の payload が ``agent_name`` を持つ名前指定の実行は、宣言した子(``general``・``optimizer``)へ ``data`` を
入力として委譲する。それ以外の全てのイベント(provider によらない)は ``general`` へ渡す。queue の ACK はここが処理の終わりに行う。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, ClassVar

from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.call import AgentCall, AgentResult
from trackeverything.core.builtin.domain.errors import ErrorNote

#: general へ渡すイベントの本文の上限(文字)。
EVENT_CHARS = 20_000


def _failed(cause: str) -> AgentResult:
    return AgentResult(status="error", response=cause, errors=(ErrorNote("dispatch", cause),))


def _request(event: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    """general への依頼。本文(``content``・``request``)があればそれを、なければイベントの JSON を渡す。"""
    for key in ("content", "request"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    text = json.dumps(payload, ensure_ascii=False, default=str)[:EVENT_CHARS]
    return f"An event arrived from {event.get('provider')} ({event.get('external_id')}). Decide what to do with it:\n{text}"


class DispatchAgent(BaseAgent):
    """名前指定の実行を宣言した子へ、他の全てのイベントを general へ委譲する。"""

    name: ClassVar[str] = "dispatch"
    description: ClassVar[str] = "Routes every queue message: a named run goes to the declared child it names, everything else to general."
    agents: ClassVar[tuple[str, ...]] = ("general", "optimizer")

    async def _execute(self, call: AgentCall) -> AgentResult:
        result = await self._handle(call)
        # 処理の終わりに queue のメッセージを ACK する(実行しなかったイベントも含む)。
        await call.ctx.ack(call.ctx.message_id)
        return result

    async def _handle(self, call: AgentCall) -> AgentResult:
        event = call.ctx.data.get("event")
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(event, dict) or not isinstance(payload, dict):
            return _failed("dispatch requires an event with a payload object")
        if event.get("provider") == "internal" and "agent_name" in payload:
            name, data = payload["agent_name"], payload.get("data", {})
            if not isinstance(name, str) or name not in type(self).agents:
                return _failed(f"agent is not declared by dispatch: {name!r}")
            if not isinstance(data, dict):
                return _failed("named run data must be an object")
            call.ctx.data.clear()
            call.ctx.data.update(data)
            return await self.call_agent(call, name, payload.get("request"))
        return await self.call_agent(call, "general", _request(event, payload))
