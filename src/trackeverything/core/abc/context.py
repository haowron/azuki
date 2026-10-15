"""Run-wide mutable context shared by an agent tree."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from trackeverything.core.builtin.domain.errors import ToolFailures

if TYPE_CHECKING:
    from trackeverything.core.abc.agent import BaseAgent

DEFAULT_MAX_STEPS = 12


class AgentLookup(Protocol):
    """run の Agent の取得口。名前から実例を返し、無ければ None。版の選択は取得口が決める。"""

    def agent(self, name: str) -> BaseAgent | None: ...


@dataclass(slots=True)
class AgentContext:
    """Shared data and LLM budget for one complete agent run."""

    data: dict[str, Any]
    max_steps: int = DEFAULT_MAX_STEPS
    run_id: str = field(default_factory=lambda: uuid4().hex)
    _step: int = field(default=0, init=False, repr=False)
    session_id: str = field(default_factory=lambda: uuid4().hex)
    # 依頼が持つ親 session。なければ None。
    parent_session_id: str | None = None
    # queue のメッセージ ID(stream id)。queue を経由しない run は None。
    message_id: str | None = None
    # queue の ACK(XACK と XDEL)。プロセス内の値で、data・LLM・保存 JSON に出さない。
    acknowledger: Callable[[str], Awaitable[None]] | None = field(default=None, repr=False, compare=False)
    # 実行内の承認。dispatch Agent の Python 制御が再検証の後、子 Agent の呼出し前に設定する。
    # data、LLM、保存用 JSON から分離する。
    authorization: object | None = field(default=None, repr=False, compare=False)
    # 実行内の未解決 tool 失敗。run 境界が裁定して消費する(LLM にも保存 JSON にも出さない)。
    tool_failures: ToolFailures = field(default_factory=ToolFailures, init=False, repr=False)
    # Tool が run 内で保持する作業状態(未 commit の変更など)。data・LLM・保存 JSON に出さない。
    tool_state: dict[str, Any] = field(default_factory=dict, init=False, repr=False, compare=False)
    # 子 Agent の取得口。Runtime が run ごとに設定し、子は call_agent がここから名前で得る。data・LLM・保存 JSON に出さない。
    agents: AgentLookup | None = field(default=None, repr=False, compare=False)

    async def ack(self, message_id: str | None) -> None:
        """queue のメッセージを処理済みにし、再び配送されないようにする。queue を経由しない run では何もしない。"""
        if message_id is not None and self.acknowledger is not None:
            await self.acknowledger(message_id)

    def __post_init__(self) -> None:
        if self.max_steps <= 0:
            raise ValueError("max_steps must be positive")

    @property
    def step(self) -> int:
        """Number of LLM turns consumed by this agent tree."""
        return self._step

    def advance_step(self) -> bool:
        """Consume one LLM turn, or return false when the budget is spent."""
        if self._step >= self.max_steps:
            return False
        self._step += 1
        return True
