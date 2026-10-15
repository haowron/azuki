"""Transparent execution logging decorator for any LLMClient."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from trackeverything.core.abc.execution_log import LogRecorder
from trackeverything.core.abc.llm import Calls, FinalAnswer, LLMClient, LLMResponse, Message
from trackeverything.core.builtin.domain.execution_log import LogEvent, current_agent_execution_id, current_llm_execution_id, current_run_id, elapsed_clock, elapsed_ms, new_id, snapshot, utc_now
from trackeverything.llm.pricing import calculate_cost_usd


class TrackedLLM(LLMClient):
    """Record every logical LLM request and result without agent involvement."""

    def __init__(self, inner: LLMClient, logger: LogRecorder) -> None:
        self._inner = inner
        self._logger = logger

    async def respond(
        self,
        messages: list[Message],
        callables: list[dict[str, Any]],
    ) -> LLMResponse:
        execution_id = new_id()
        started_at = utc_now()
        started = elapsed_clock()
        request = snapshot({"messages": messages, "callables": callables})
        run_id = current_run_id.get() or new_id()
        parent_id = current_agent_execution_id.get()

        token = current_llm_execution_id.set(execution_id)
        try:
            response = await self._inner.respond(messages, callables)
        except Exception as exc:
            await self._logger.record(
                LogEvent(
                    event_type="llm",
                    run_id=run_id,
                    execution_id=execution_id,
                    parent_execution_id=parent_id,
                    component="llm",
                    started_at=started_at,
                    duration_ms=elapsed_ms(started),
                    status="error",
                    request=request,
                    error={"type": type(exc).__name__, "message": str(exc)},
                )
            )
            raise
        finally:
            current_llm_execution_id.reset(token)

        metadata = response.metadata
        usage = metadata.usage if metadata is not None else None
        provider = metadata.provider if metadata is not None else "unknown"
        model = metadata.model if metadata is not None else "unknown"
        cost = calculate_cost_usd(provider, model, usage) if usage is not None else None
        await self._logger.record(
            LogEvent(
                event_type="llm",
                run_id=run_id,
                execution_id=execution_id,
                parent_execution_id=parent_id,
                component=f"{provider}/{model}",
                started_at=started_at,
                duration_ms=elapsed_ms(started),
                status="ok",
                request=request,
                result=snapshot(_response_data(response)),
                usage=asdict(usage) if usage is not None else None,
                cost_usd=cost,
            )
        )
        return response


def _response_data(response: LLMResponse) -> dict[str, Any]:
    if isinstance(response, FinalAnswer):
        return {"type": "final_answer", "text": response.text, "metadata": response.metadata}
    assert isinstance(response, Calls)
    return {"type": "calls", "calls": response.calls, "raw": response.raw, "metadata": response.metadata}
