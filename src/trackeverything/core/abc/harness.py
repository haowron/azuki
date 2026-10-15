"""Pre/post harness ABCs — forced gates around agent execution.

A harness is a gate the agent cannot bypass. Unlike tools (which the LLM
chooses to call mid-loop), harnesses are evaluated by ``BaseAgent.run``
outside the loop, and the LLM never sees them: harnesses are absent from
prompts and from the callables list. That invisibility is what makes the
gate "forced" — the model cannot route around it or negotiate with it.

Two protocols, one phase each:

- :class:`PreHarness` — evaluated in order before ``_execute``. The first
  ``check`` returning False (or raising) blocks execution: the run returns
  ``AgentResult(status="error")`` with a ``harness:<name>`` note without
  spending a single LLM call, and the post chain does not run.
- :class:`PostHarness` — evaluated in order after ``_execute`` returns
  ``status="ok"``. The first ``check`` returning False (or raising) vetoes
  the result: the run returns ``AgentResult(status="error")`` while the
  original result is preserved in the harness log event.

Both chains are ordered (declaration order first, then registration order)
and fail-fast: the first blocking/vetoing harness short-circuits the rest.

Conventions for implementers (not enforced by the framework):

- **Fail-closed.** A raised exception counts as a block (pre) / veto (post).
  A harness is a lock; a broken lock must not open.
- **Single side per class.** The two ``check`` signatures differ, so one
  class implements one phase. Need both sides? Write two harnesses.
- **LLM-powered harnesses must budget.** Call ``call.ctx.advance_step()``
  before every ``self._llm.respond(...)`` and return False when it is
  exhausted — the shared step budget stays the run-wide cost ceiling.
- **``ctx.data`` is read-only.** Harness side effects go to the log, never
  into the shared context the agent reads.
- **Never dispatch agents.** Delegation stays agent-to-agent; a harness
  that spawned agents would recurse (the child's own harnesses would run).
  LLM judgment is a direct ``LLMClient`` call, not an agent loop.
- **Stateless across runs.** Instances live for the process lifetime;
  cross-run state belongs in config/data files, re-read per check.

Concrete harnesses live in the outer ``trackeverything.harnesses`` package.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from trackeverything.core.abc.call import AgentCall, AgentResult
from trackeverything.core.abc.llm import LLMClient


class PreHarness(ABC):
    """Execution gate evaluated before the agent loop starts."""

    # Subclasses MUST override these class attributes.
    name: ClassVar[str]
    description: ClassVar[str]

    def __init__(self, llm: LLMClient) -> None:
        """Store the host agent's LLM client for shared use.

        Deterministic harnesses simply ignore ``self._llm``; LLM-powered
        ones call it directly (gated on ``call.ctx.advance_step()``).
        """
        self._llm = llm

    @abstractmethod
    async def check(self, call: AgentCall) -> bool:
        """Decide whether the agent may execute this call.

        Return True to allow (the chain continues), False to block (the
        run short-circuits to ``status="error"``). Exceptions count as
        a block: harnesses fail closed.
        """
        ...


class PostHarness(ABC):
    """Result gate evaluated after the agent loop returns "ok"."""

    # Subclasses MUST override these class attributes.
    name: ClassVar[str]
    description: ClassVar[str]

    def __init__(self, llm: LLMClient) -> None:
        """Store the host agent's LLM client for shared use.

        Deterministic harnesses simply ignore ``self._llm``; LLM-powered
        ones call it directly (gated on ``call.ctx.advance_step()``).
        """
        self._llm = llm

    @abstractmethod
    async def check(self, call: AgentCall, result: AgentResult) -> bool:
        """Decide whether the agent's result may stand.

        Observes ``(call, result)`` without mutating either. Return True
        to approve, False to veto (the run becomes ``status="error"``;
        the original result is snapshotted into the harness log event).
        Exceptions count as a veto: harnesses fail closed.
        """
        ...


class ErrorHarness(ABC):
    """Reaction hook evaluated by ``BaseAgent.run`` when a run fails.

    The third harness protocol: unlike the pre/post gates (which decide
    whether execution may run or stand), an error harness only reacts to a
    failed result — typically by delivering a notification. It runs after
    the failure is recorded, ordered like the other chains, and fail-closed
    in the sense that its own exceptions are swallowed and logged: a broken
    reaction must never mask the original failure.

    Conventions for implementers:

    - **Read-only on the result.** The returned ``AgentResult`` is final;
      the notes on ``result.errors`` are the verbatim causes to surface.
    - **``ctx.data`` may be read.** Entrances can stage delivery targets
      (e.g. a channel id) there before failing; mutating it changes nothing.
    - **Deliver through plain tool/transport calls, never an agent loop.**
      Delegation stays agent-to-agent; a harness that spawned agents would
      recurse. LLM-powered translation is a direct ``LLMClient`` call.
    - **Degrade gracefully.** An exhausted step budget or a failing LLM
      means skipping the polish (translate less), never skipping the
      delivery and never raising.
    """

    # Subclasses MUST override these class attributes.
    name: ClassVar[str]
    description: ClassVar[str]

    def __init__(self, llm: LLMClient) -> None:
        """Store the host agent's LLM client for shared use."""
        self._llm = llm

    @abstractmethod
    async def handle(self, call: AgentCall, result: AgentResult) -> None:
        """React to a failed run without mutating the result.

        Exceptions are swallowed and logged by the boundary — a failed
        reaction never replaces the failure it reports on.
        """
        ...
