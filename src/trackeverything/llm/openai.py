"""OpenAI-compatible concrete `LLMClient`.

`OpenAIChatLLM` talks to any endpoint speaking the OpenAI Chat Completions
format (OpenAI, DeepSeek, Z.AI, vLLM — selected by the composition root). Thin
adapter over the official ``openai`` SDK; this is the only place that knows
the OpenAI message shape.

Translation:
- system/user → ``{role, content}`` verbatim.
- assistant carrying a JSON list of ``{name, args}`` → ``{role:"assistant",
  tool_calls:[...]}`` with sequential ids.
- tool → ``{role:"tool", tool_call_id, content}``, ids paired by position.
- Thinking models (``reasoning_content``) replay the provider's raw assistant
  message verbatim via ``Message.raw`` — no client-side cache.

Failures: an HTTP 200 body carrying ``error`` without choices raises
:class:`UpstreamResponseError`, on which ``FallbackLLM`` fails over.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from openai import NOT_GIVEN, AsyncOpenAI

from trackeverything.config import (
    DEFAULT_TIMEOUT,
    OPENAI_BASE_URL,
    OPENAI_DEFAULT_MODEL,
)
from trackeverything.core.abc.llm import (
    Call,
    Calls,
    FinalAnswer,
    LLMClient,
    LLMMetadata,
    LLMResponse,
    LLMUsage,
    Message,
)

if TYPE_CHECKING:
    import httpx


class UpstreamResponseError(RuntimeError):
    """The provider answered HTTP 200 with an ``error`` object and no choices.

    Z.AI does this when a request waits in its queue past its own limit. It is a
    provider-side failure rather than a parse failure.
    """


class OpenAIChatLLM(LLMClient):
    """OpenAI-compatible Chat Completions client.

    Construct directly; endpoint/model/timeout policy comes from
    ``config.py`` and the credential is passed in by the composition root.
    ``client`` injects a stub (tests); ``http_client`` forwards a custom httpx
    transport to the SDK.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = OPENAI_BASE_URL,
        model: str = OPENAI_DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        client: Any = None,
        http_client: httpx.AsyncClient | None = None,
        extra_body: dict[str, Any] | None = None,
        provider: str | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must be a non-empty string")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._provider = provider or self._infer_provider(self._base_url)
        self._client: Any = client or AsyncOpenAI(
            api_key=api_key,
            base_url=self._base_url,
            timeout=timeout,
            http_client=http_client,
        )
        self._extra_body: dict[str, Any] = dict(extra_body) if extra_body else {}

    async def respond(
        self,
        messages: list[Message],
        callables: list[dict[str, Any]],
    ) -> LLMResponse:
        tools = self._to_openai_tools(callables)
        completion = await self._client.chat.completions.create(
            model=self._model,
            messages=self._to_openai_messages(messages),
            tools=tools or NOT_GIVEN,
            tool_choice="auto" if tools else NOT_GIVEN,
            extra_body=self._extra_body or None,
        )
        body: dict[str, Any] = completion.model_dump()
        raw = self._raw_assistant_from_body(body)
        metadata = LLMMetadata(
            provider=self._provider,
            model=self._model,
            usage=self._parse_usage(body.get("usage")),
            request_id=str(body["id"]) if body.get("id") is not None else None,
            raw_response=body,
        )
        return self._parse_choice(body, raw, metadata)

    @staticmethod
    def _infer_provider(base_url: str) -> str:
        if "z.ai" in base_url:
            return "zai"
        if "deepseek" in base_url:
            return "deepseek"
        if "openai" in base_url:
            return "openai"
        return "unknown"

    @staticmethod
    def _parse_usage(raw: Any) -> LLMUsage | None:
        if not isinstance(raw, dict):
            return None
        prompt = _as_int(raw.get("prompt_tokens"))
        completion = _as_int(raw.get("completion_tokens"))
        total = _as_int(raw.get("total_tokens"), prompt + completion)
        prompt_details = raw.get("prompt_tokens_details")
        completion_details = raw.get("completion_tokens_details")
        cached = _as_int(raw.get("prompt_cache_hit_tokens"))
        if isinstance(prompt_details, dict):
            cached = _as_int(prompt_details.get("cached_tokens"), cached)
        reasoning = _as_int(raw.get("reasoning_tokens"))
        if isinstance(completion_details, dict):
            reasoning = _as_int(completion_details.get("reasoning_tokens"), reasoning)
        return LLMUsage(
            input_tokens=prompt,
            output_tokens=completion,
            total_tokens=total,
            cached_input_tokens=cached,
            reasoning_tokens=reasoning,
        )

    @staticmethod
    def _raw_assistant_from_body(body: dict[str, Any]) -> dict[str, Any] | None:
        """Assistant message to replay next turn, or None. Only tool_calls /
        reasoning_content turns need replay."""
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        message = choices[0].get("message")
        if not isinstance(message, dict):
            return None
        if message.get("tool_calls") or message.get("reasoning_content"):
            return dict(message)
        return None

    # ---- messages ----

    def _to_openai_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        pending_ids: list[str] = []
        counter = 0
        for msg in messages:
            if msg.role == "assistant":
                calls = self._try_parse_calls(msg.content)
                if calls is None:
                    out.append({"role": "assistant", "content": msg.content})
                    continue
                raw = msg.raw
                if raw is not None and isinstance(raw.get("tool_calls"), list):
                    # Replay the provider's raw message (preserves
                    # reasoning_content + native tool_call ids).
                    pending_ids = [str((tc or {}).get("id", f"call_{counter + i}")) for i, tc in enumerate(raw["tool_calls"])]
                    counter += len(raw["tool_calls"])
                    out.append(dict(raw))
                else:
                    tool_calls: list[dict[str, Any]] = []
                    pending_ids = []
                    for c in calls:
                        call_id = f"call_{counter}"
                        counter += 1
                        pending_ids.append(call_id)
                        tool_calls.append(
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": str(c.get("name", "")),
                                    "arguments": json.dumps(c.get("args", {})),
                                },
                            }
                        )
                    out.append({"role": "assistant", "content": None, "tool_calls": tool_calls})
            elif msg.role == "tool":
                call_id = pending_ids.pop(0) if pending_ids else f"call_{counter}"
                out.append({"role": "tool", "tool_call_id": call_id, "content": msg.content})
            else:
                out.append({"role": msg.role, "content": msg.content})
        if self._provider == "deepseek" and self._extra_body.get("thinking", {}).get("type") == "enabled":
            # 主モデルが推論を返さなかった履歴も思考モードの送信形式に合わせる。
            # 既存の推論は保持し、共有 Message.raw は変更しない。
            for item in out:
                if item["role"] == "assistant" and item.get("reasoning_content") is None:
                    item["reasoning_content"] = ""
        return out

    @staticmethod
    def _try_parse_calls(content: str) -> list[dict[str, Any]] | None:
        """Return the list of {name, args} if content is such a JSON list, else None."""
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError, TypeError:
            return None
        if not isinstance(parsed, list):
            return None
        if parsed and not all(isinstance(x, dict) and "name" in x for x in parsed):
            return None
        return parsed

    # ---- tools ----

    @staticmethod
    def _to_openai_tools(callables: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": c["name"],
                    "description": c.get("description", ""),
                    "parameters": c.get("arguments") or {"type": "object", "properties": {}},
                },
            }
            for c in callables
        ]

    # ---- response ----

    @staticmethod
    def _parse_choice(
        body: dict[str, Any],
        raw: dict[str, Any] | None,
        metadata: LLMMetadata | None = None,
    ) -> LLMResponse:
        choices = body.get("choices")
        if not choices or not isinstance(choices, list):
            if body.get("error"):
                raise UpstreamResponseError(f"LLM provider returned an error instead of choices: {body['error']!r}")
            raise ValueError(f"OpenAI response missing choices: {body!r}")
        message = choices[0].get("message") or {}
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            calls: list[Call] = []
            for tc in tool_calls:
                fn = (tc or {}).get("function") or {}
                name = str(fn.get("name", ""))
                raw_args = fn.get("arguments")
                if isinstance(raw_args, dict):
                    args = raw_args
                elif isinstance(raw_args, str) and raw_args.strip():
                    try:
                        parsed = json.loads(raw_args)
                        args = parsed if isinstance(parsed, dict) else {"_value": parsed}
                    except json.JSONDecodeError:
                        args = {}
                else:
                    args = {}
                calls.append(Call(name=name, args=args))
            return Calls(calls=tuple(calls), raw=raw, metadata=metadata)
        content = message.get("content")
        return FinalAnswer(
            text=content if isinstance(content, str) else "",
            metadata=metadata,
        )


def _as_int(value: Any, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default
