"""LLM client abstract base class + message/response data types.

The agent loop drives the LLM through this single port:

- The agent builds a list of `Message`s (system + user + assistant + tool
  observations), passes them along with a unified list of callables (tools +
  sub-agents), and receives one `LLMResponse` back per turn.
- An `LLMResponse` is a discriminated union with two variants: `FinalAnswer`
  (the LLM is done; return its summary) or `Calls` (the LLM wants to invoke
  one or more callables; dispatch, observe, repeat). Tools and sub-agents are
  NOT distinguished at this layer — the agent loop routes a `Call` to a tool
  or a sub-agent based on the target name.

The concrete runtime ships :class:`~trackeverything.llm.openai.OpenAIChatLLM`
(an OpenAI-compatible client for Z.AI / DeepSeek / OpenAI behind one
parameterized base_url) and :class:`~trackeverything.llm.fallback.FallbackLLM`
(primary + backup with transient-failure failover). The agent core depends
only on this ABC — swap providers by editing the composition root
(``core/builtin/app.py``), never the core.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Message:
    """One turn in the agent-LLM conversation.

    `role` is one of ``"system"``, ``"user"``, ``"assistant"``, or ``"tool"``;
    `content` is the serialized payload (for ``"tool"`` role this is a JSON
    blob carrying the observation).

    `raw` is an optional escape hatch for the provider's native assistant
    message (e.g. the OpenAI message dict carrying ``reasoning_content`` and
    provider-native ``tool_call`` ids). When set on an ``"assistant"`` message,
    the :class:`~trackeverything.core.abc.llm.LLMClient` implementation may replay it
    verbatim instead of rebuilding from `content`. Keeping it here — on the
    object that already represents the turn — avoids the client having to
    maintain a separate per-run cache (fragile, and hostile to concurrency:
    such a cache keyed on message-list object identity cannot survive two
    runs sharing one client). Default ``None``; existing constructors are
    unaffected.
    """

    role: str
    content: str
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class LLMUsage:
    """Provider-reported token usage for one LLM request."""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True)
class LLMMetadata:
    """Provider metadata retained for logging and cost calculation."""

    provider: str
    model: str
    usage: LLMUsage | None = None
    request_id: str | None = None
    raw_response: dict[str, Any] | None = None


@dataclass(frozen=True)
class FinalAnswer:
    """LLM signal: "I'm done — here's the summary."

    When the agent receives this, it stops the loop and returns an
    ``AgentResult`` containing the text and an ``ok`` status.
    """

    text: str
    metadata: LLMMetadata | None = None


@dataclass(frozen=True)
class Call:
    """One invocation request from the LLM.

    `name` matches either a registered tool's name or a callable sub-agent's
    name — the agent loop decides which at routing time. `args` is the opaque
    argument dict whose shape is described by the target's ``arguments``
    schema. The agent core never parses `args`.
    """

    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Calls:
    """LLM signal: "execute these calls, then keep going."

    Each `Call` may target a tool or a sub-agent. The agent loop dispatches
    each one, appends observations to the message history, and re-enters the
    LLM.

    `raw` is the provider's native assistant message that produced these
    calls, when available. The agent loop attaches it to the assistant
    :class:`Message` it appends, so the client can replay it verbatim on the
    next turn (preserving ``reasoning_content`` + provider-native tool_call
    ids that thinking models require). Default ``None``; existing
    constructors are unaffected.
    """

    calls: tuple[Call, ...]
    raw: dict[str, Any] | None = None
    metadata: LLMMetadata | None = None


# Discriminated union — the agent branches on `isinstance(resp, FinalAnswer)`.
LLMResponse = FinalAnswer | Calls
"""One LLM turn's output. Discriminated via isinstance, never via string parsing."""


class LLMClient(ABC):
    """Single LLM port.

    Concrete implementations MUST subclass this and implement :meth:`respond`.
    Forgetting it raises ``TypeError`` at instantiation.

    Implementations are responsible for:

    - serializing `messages` and `callables` into the provider's native format,
    - calling the provider (OpenAI, Anthropic, local, …),
    - parsing the provider's response into either `FinalAnswer` or `Calls`.

    The agent core never sees the provider's wire format — only this ABC.
    Concrete clients live in :mod:`trackeverything.core.builtin`; the
    composition root wires them.
    """

    @abstractmethod
    async def respond(
        self,
        messages: list[Message],
        callables: list[dict[str, Any]],
    ) -> LLMResponse:
        """履歴と callable 一覧から FinalAnswer または Calls を返す。

        callables は Tool と子 Agent の name・description・arguments を持つ。
        この境界は両者を分類せず、Call の振り分けは BaseAgent が行う。
        """
        ...
