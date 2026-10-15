"""Fallback LLM — wraps a primary + backup `LLMClient` for runtime failover.

`FallbackLLM` holds two `LLMClient` instances (both typically
:class:`~trackeverything.llm.openai.OpenAIChatLLM` pointed at different
providers). Each :meth:`respond` call tries the primary first; if it raises an
exception classified as "transient / server-side" by :func:`_should_fallback`,
the identical call is retried against the backup. If the backup also fails, its
exception wins (the primary's is logged, not swallowed).

This is the seam that lets the runtime keep tracking when the main LLM provider
has a blip (rate limit, 5xx, network timeout, an error body in place of
choices): the runtime's run-failure
isolation catches *any* exception from a single ``agent.run(call)`` anyway, but
falling over *within* one run avoids dropping a whole tracked entity just
because Z.AI returned a 503 for a few seconds.

Which errors fail over (and which don't) is deliberately conservative — see
:func:`_should_fallback`'s docstring. The goal is to switch only when the
backup is genuinely likely to do better, never to mask a configuration bug.

The concrete ``LLMClient`` ships as :class:`~trackeverything.llm.openai.OpenAIChatLLM`
(thin wrapper over the official ``openai`` SDK), so the error taxonomy below
speaks the SDK's exception types: ``openai.APIStatusError`` (HTTP status,
carries ``.status_code``) and ``openai.APIConnectionError`` (network-layer
base; :class:`openai.APITimeoutError` is a subclass), plus the adapter's own
:class:`~trackeverything.llm.openai.UpstreamResponseError`. Swapping the
adapter for a non-SDK client means rethinking this classifier — it is
SDK-shaped, not protocol-shaped.

Naming invariant: "Track / Tracking" everywhere, never "trace".
"""

from __future__ import annotations

import logging
from typing import Any

import openai

from trackeverything.core.abc.llm import LLMClient, LLMResponse, Message
from trackeverything.core.builtin.domain.execution_log import current_agent_execution_id, current_llm_execution_id, current_run_id, elapsed_clock, elapsed_ms
from trackeverything.llm.openai import UpstreamResponseError

log = logging.getLogger(__name__)

#: HTTP status codes that indicate a transient or server-side problem at the
#: primary provider — switching to the backup is likely to help. Client-side
#: errors (400/401/403/404/422) are deliberately excluded: those mean the
#: *request* is wrong (bad key, bad schema), and the backup would fail the same
#: way or for want of credentials. Letting them surface exposes the real bug.
_FALLBACK_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


def _should_fallback(exc: BaseException) -> bool:
    """Return True if `exc` is a failure worth retrying on the backup.

    - :class:`openai.APIStatusError` with status in :data:`_FALLBACK_STATUSES`
      → True (rate-limited / server error).
    - Any :class:`openai.APIConnectionError` subclass (timeout, connection
      error, …) → True (network-layer, endpoint-specific). Note
      :class:`openai.APITimeoutError` is a subclass, so it is covered.
    - :class:`UpstreamResponseError` (HTTP 200 whose body is an ``error``
      without choices, e.g. Z.AI's queue limit) → True.
    - Any other :class:`openai.APIStatusError` (4xx other than above) → False.
    - :class:`ValueError` (response-parse failure) → False: most likely our own
      parser bug or an odd response shape; switching providers is unpredictable.
    - Anything else → False (conservative — don't hide unknown bugs).
    """
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code in _FALLBACK_STATUSES
    if isinstance(exc, UpstreamResponseError):
        return True
    # APIConnectionError is the common base for connection / read errors and
    # timeouts (APITimeoutError subclasses it). Checking the base covers the
    # whole family without enumerating each subclass.
    return isinstance(exc, openai.APIConnectionError)


class FallbackLLM(LLMClient):
    """An LLM client that retries on the backup when the primary fails transiently.

    Both ``primary`` and ``fallback`` must be valid :class:`LLMClient`
    instances. They are used as-is — :class:`FallbackLLM` does no translation
    (both are expected to speak the same agent-core protocol; typically both
    are :class:`~trackeverything.llm.openai.OpenAIChatLLM`).
    """

    def __init__(self, primary: LLMClient, fallback: LLMClient) -> None:
        self._primary = primary
        self._fallback = fallback

    async def respond(
        self,
        messages: list[Message],
        callables: list[dict[str, Any]],
    ) -> LLMResponse:
        """Try the primary; on a fallback-classified error, retry on the backup.

        Non-fallback errors propagate immediately from the primary. If the
        backup also fails, its exception propagates (chained `from` the
        primary's, so neither context is lost).
        """
        started = elapsed_clock()
        try:
            return await self._primary.respond(messages, callables)
        except Exception as primary_exc:
            if not _should_fallback(primary_exc):
                raise
            log.warning(
                "primary LLM failed (%s); falling back to backup (run_id=%s, parent_execution_id=%s, llm_execution_id=%s, primary_ms=%s)",
                type(primary_exc).__name__, current_run_id.get(), current_agent_execution_id.get(), current_llm_execution_id.get(), elapsed_ms(started),
            )
            try:
                return await self._fallback.respond(messages, callables)
            except Exception as fallback_exc:
                raise fallback_exc from primary_exc
