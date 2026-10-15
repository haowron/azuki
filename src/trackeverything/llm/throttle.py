"""Process-wide concurrency gate for LLM calls on the Host event loop."""

from __future__ import annotations

import asyncio
from typing import Any

from trackeverything.core.abc.llm import LLMClient, LLMResponse, Message


class ThrottledLLM(LLMClient):
    """Concurrency-bounding pass-through around an inner ``LLMClient``.

    Acquires ``semaphore`` for the duration of each ``respond`` call and
    forwards to the inner client unchanged. The caller (the agent loop) sees a
    plain :class:`LLMClient`; the throttle is transparent apart from the wait.
    """

    def __init__(self, inner: LLMClient, semaphore: asyncio.Semaphore) -> None:
        self._inner = inner
        self._sem = semaphore

    async def respond(
        self,
        messages: list[Message],
        callables: list[dict[str, Any]],
    ) -> LLMResponse:
        """Forward to the inner client, holding one semaphore slot for the call."""
        async with self._sem:
            return await self._inner.respond(messages, callables)
