"""Per-call input and output values for agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from trackeverything.core.abc.context import AgentContext
from trackeverything.core.builtin.domain.errors import ErrorNote

type AgentStatus = Literal["ok", "max_steps", "error", "cancelled"]
"""Completion state of one agent invocation.

``"cancelled"`` means the run deadline cancelled the agent tree.
"""


@dataclass(frozen=True, slots=True)
class AgentCall:
    """Input for one agent invocation over a shared run context."""

    ctx: AgentContext
    request: Any = None

    def child(self, request: Any = None) -> AgentCall:
        """Create a child call that references the same run context."""
        return AgentCall(ctx=self.ctx, request=request)


@dataclass(frozen=True, slots=True)
class AgentResult:
    """Output from one completed agent invocation.

    ``errors`` carries the structured failure notes. Every result that is
    not ``"ok"`` has at least one (the run boundary adds it when missing),
    so callers read them instead of parsing ``response``.
    """

    status: AgentStatus
    response: Any = None
    errors: tuple[ErrorNote, ...] = ()
