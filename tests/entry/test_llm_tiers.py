"""The Host's named LLM clients (``max``, ``high``, ``low``, ``flash``, ``flash_low``): LT01-LT04.

Only the SDK boundary (``AsyncOpenAI``) is replaced. The cases check the model and reasoning effort each name sends,
the switch to DeepSeek, the fallback to the primary client without Z.AI, and an agent receiving the client it declares.
Real API quality and speed are not part of this check.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import openai
import pytest

import trackeverything.config as config
from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.call import AgentCall
from trackeverything.core.abc.context import AgentContext
from trackeverything.core.abc.llm import FinalAnswer, Message
from trackeverything.core.builtin.compose import build_agents
from trackeverything.llm import compose
from trackeverything.llm import openai as openai_llm

ZAI = config.ZAI_BASE_URL.rstrip("/")
DEEPSEEK = config.DEEPSEEK_BASE_URL.rstrip("/")
NAMES = {"max", "high", "low", "flash", "flash_low"}


def thinking(effort: str) -> dict[str, Any]:
    return {"thinking": {"type": "enabled"}, "reasoning_effort": effort}


@pytest.fixture
def sdk(monkeypatch):
    """Record what every client sends, per endpoint; endpoints in ``failing`` refuse the connection."""
    state = SimpleNamespace(sent=[], failing=set())

    def client(*, base_url: str, **_: Any) -> Any:
        endpoint = base_url.rstrip("/")

        async def create(**kwargs: Any) -> Any:
            state.sent.append((endpoint, kwargs["model"], kwargs["extra_body"], kwargs["messages"][-1]["content"]))
            if endpoint in state.failing:
                raise openai.APIConnectionError(request=httpx.Request("POST", base_url))
            return SimpleNamespace(model_dump=lambda: {"id": "r1", "choices": [{"message": {"role": "assistant", "content": "ok"}}]})

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setattr(openai_llm, "AsyncOpenAI", client)
    return state


def providers(monkeypatch, *, zai: bool = False, deepseek: bool = False) -> None:
    for name, configured in (("ZAI_API_KEY", zai), ("DEEPSEEK_API_KEY", deepseek), ("OPENAI_API_KEY", False)):
        if configured:
            monkeypatch.setenv(name, "test-key")
        else:
            monkeypatch.delenv(name, raising=False)


async def test_lt01_each_zai_name_sends_its_model_and_effort(monkeypatch, sdk):
    providers(monkeypatch, zai=True)
    llms = compose.build_llms(AsyncMock())
    assert set(llms) == NAMES
    for name in sorted(NAMES):
        assert isinstance(await llms[name].respond([Message("user", name)], []), FinalAnswer)
    reasoning, flash = config.ZAI_REASONING_MODEL, config.ZAI_FLASH_MODEL
    assert {text: (endpoint, model, body) for endpoint, model, body, text in sdk.sent} == {
        "max": (ZAI, reasoning, thinking("max")),
        "high": (ZAI, reasoning, thinking("high")),
        "low": (ZAI, reasoning, thinking("low")),
        "flash": (ZAI, flash, thinking("high")),
        "flash_low": (ZAI, flash, thinking("low")),
    }


async def test_lt02_flash_low_switches_to_deepseek_on_a_transient_failure(monkeypatch, sdk):
    providers(monkeypatch, zai=True, deepseek=True)
    sdk.failing.add(ZAI)
    llms = compose.build_llms(AsyncMock())
    assert isinstance(await llms["flash_low"].respond([Message("user", "switch")], []), FinalAnswer)
    assert sdk.sent == [
        (ZAI, config.ZAI_FLASH_MODEL, thinking("low"), "switch"),
        (DEEPSEEK, config.DEEPSEEK_DEFAULT_MODEL, {"thinking": {"type": "enabled"}}, "switch"),
    ]


async def test_lt03_without_zai_flash_low_is_the_primary_client(monkeypatch, sdk):
    providers(monkeypatch, deepseek=True)
    llms = compose.build_llms(AsyncMock())
    assert set(llms) == NAMES
    assert all(llms[name] is llms["max"] for name in NAMES)
    await llms["flash_low"].respond([Message("user", "primary")], [])
    assert [(endpoint, model) for endpoint, model, _, _ in sdk.sent] == [(DEEPSEEK, config.DEEPSEEK_DEFAULT_MODEL)]


class Quick(BaseAgent):
    name = "quick"
    description = "declares flash_low"
    llm = "flash_low"


async def test_lt04_agent_declaring_flash_low_composes_and_calls_it(monkeypatch, sdk):
    providers(monkeypatch, zai=True)
    agents = build_agents((Quick,), compose.build_llms(AsyncMock()))
    result = await agents["quick"].run(AgentCall(AgentContext(data={}, max_steps=2), "go"))
    assert result.status == "ok" and result.response == "ok"
    assert [(endpoint, model, body) for endpoint, model, body, _ in sdk.sent] == [(ZAI, config.ZAI_FLASH_MODEL, thinking("low"))]
