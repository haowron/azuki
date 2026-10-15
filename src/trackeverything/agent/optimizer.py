"""optimizer の入口。名前指定の実行の ``data`` を Host の optimizer へ渡し、報告を返す(LLM を使わない)。

optimizer の本体は Host が所有する :mod:`trackeverything.optimizer` で、版に含めない。
"""

from __future__ import annotations

from typing import ClassVar

from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.call import AgentCall, AgentResult
from trackeverything.optimizer import handle


class OptimizerAgent(BaseAgent):
    name: ClassVar[str] = "optimizer"
    description: ClassVar[str] = "Runs the Host's optimizer once and returns its observation and check reports (no LLM)."

    async def _execute(self, call: AgentCall) -> AgentResult:
        return await handle(call.ctx.data)
