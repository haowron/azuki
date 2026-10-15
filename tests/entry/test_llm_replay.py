"""思考履歴を返さない主モデルから DeepSeek へ切り替える場合の送信契約と、上流の失敗による切替え。"""

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import openai
import pytest

from trackeverything.core.abc.llm import FinalAnswer, LLMClient, Message
from trackeverything.llm.fallback import FallbackLLM
from trackeverything.llm.openai import OpenAIChatLLM


def client(provider="deepseek", thinking="enabled"):
    create = AsyncMock(return_value=SimpleNamespace(model_dump=lambda: {
        "choices": [{"message": {"role": "assistant", "content": "調査完了"}}],
    }))
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return OpenAIChatLLM("test", provider=provider, client=sdk, extra_body={"thinking": {"type": thinking}}), create


def history(reasoning):
    raw = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "native-call", "type": "function", "function": {"name": "read", "arguments": "{}"}},
    ]}
    if reasoning != "missing":
        raw["reasoning_content"] = reasoning
    return [Message("user", "調査"), Message("assistant", json.dumps([{"name": "read", "args": {}}]), raw=raw), Message("tool", "取得済み")]


@pytest.mark.parametrize("reasoning", ["missing", None, "", "保存する推論"])
async def test_thinking_replay_preserves_history_and_normalizes_absent_reasoning(reasoning):
    llm, create = client()
    messages = history(reasoning)
    before = copy.deepcopy(messages)
    response = await llm.respond(messages, [{"name": "read"}])
    sent = create.call_args.kwargs["messages"]
    assert isinstance(response, FinalAnswer)
    assert sent[1]["reasoning_content"] == ("保存する推論" if reasoning == "保存する推論" else "")
    assert sent[1]["tool_calls"][0]["id"] == sent[2]["tool_call_id"] == "native-call"
    assert messages == before


@pytest.mark.parametrize("provider,thinking", [("zai", "enabled"), ("deepseek", "disabled")])
async def test_other_provider_or_disabled_thinking_preserves_native_shape(provider, thinking):
    llm, create = client(provider, thinking)
    messages = history(None)
    await llm.respond(messages, [{"name": "read"}])
    assert create.call_args.kwargs["messages"][1] == messages[1].raw


async def test_transient_primary_failure_reaches_backup_with_complete_replay_fields():
    primary = Mock(spec=LLMClient)
    primary.respond.side_effect = openai.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))
    backup, create = client()

    def endpoint(**kwargs):
        for message in kwargs["messages"]:
            if message["role"] == "assistant":
                assert isinstance(message.get("reasoning_content"), str)
        return SimpleNamespace(model_dump=lambda: {"choices": [{"message": {"content": "継続できた"}}]})

    create.side_effect = endpoint
    result = await FallbackLLM(primary, backup).respond(history(None), [{"name": "read"}])
    assert isinstance(result, FinalAnswer) and result.text == "継続できた"
    primary.respond.assert_called_once()
    create.assert_called_once()


async def test_error_body_without_choices_fails_over_to_backup():
    # Z.AI が自分のキュー上限を超えた要求に返した HTTP 200 の本文そのもの。
    queued_out = {"id": None, "choices": None, "usage": None, "error": {
        "message": "We were unable to start processing your request within the 900-second timeout limit. Please try again later.",
    }}
    primary, primary_create = client("zai")
    primary_create.return_value = SimpleNamespace(model_dump=lambda: queued_out)
    backup, backup_create = client()
    result = await FallbackLLM(primary, backup).respond(history(None), [{"name": "read"}])
    assert isinstance(result, FinalAnswer) and result.text == "調査完了"
    primary_create.assert_called_once()
    backup_create.assert_called_once()


async def test_body_without_choices_or_error_stays_a_parse_error():
    primary, create = client("zai")
    create.return_value = SimpleNamespace(model_dump=lambda: {"id": "empty", "choices": []})
    backup, backup_create = client()
    with pytest.raises(ValueError, match="missing choices"):
        await FallbackLLM(primary, backup).respond(history(None), [{"name": "read"}])
    backup_create.assert_not_called()

