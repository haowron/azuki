"""The version-owned system preamble ``agent/SYSTEM.md``: VR56-VR61.

Every LLM call an agent makes starts with the version's ``agent/SYSTEM.md``; harnesses calling their client
directly do not. The store is an ``AsyncMock`` answering from fixed data (``version_store``); one ScriptedLLM
serves every LLM name and records what each call sent.
"""

import asyncio
from typing import Any

import pytest

from tests.fakes import ScriptedLLM
from tests.versioning.helpers import host_services, minimal_tree
from tests.versioning.test_components import merge_files, started
from trackeverything.core.abc.call import AgentCall
from trackeverything.core.abc.context import AgentContext
from trackeverything.core.abc.llm import Call, Calls, FinalAnswer, Message

WAIT = 5

AGENTS = '''
from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.call import AgentResult
from trackeverything.core.abc.harness import PreHarness
from trackeverything.core.abc.llm import Message
from trackeverything.core.abc.tool import Tool, ToolResult


class Echo(Tool):
    name = "sm_echo"
    description = "echo"
    arguments = {"type": "object", "properties": {}}

    async def run(self, args, *, ctx=None):
        return ToolResult(ok=True, output="echoed")


class Judge(PreHarness):
    name = "judge"
    description = "asks its LLM directly when the run asks for it"

    async def check(self, call):
        if call.ctx.data.get("judge"):
            await self._llm.respond([Message("system", "JUDGE"), Message("user", "verdict?")], [])
        return True


class Loop(BaseAgent):
    name = "loop"
    description = "the standard LLM loop"
    tools = (Echo,)
    pre = (Judge,)

    def get_prompt(self):
        return "OWN PROMPT"


class Direct(BaseAgent):
    name = "direct"
    description = "builds its own messages"

    async def _execute(self, call):
        built = [Message(role, content) for role, content in call.ctx.data["messages"]]
        before = list(built)
        await self._respond_one_step(call.ctx, built)
        await self._respond_one_step(call.ctx, built)
        call.ctx.data["unchanged"] = built == before
        return AgentResult(status="ok", response="done")
'''

COMPOSE = '''
from trackeverything.agent.sm import Direct, Loop
from trackeverything.core.builtin.compose import ComponentGraph, build_agents


def build(services, *, skills_root=None):
    agents = build_agents((Loop, Direct), services.llms, logger=services.logger)
    agents["loop"].configure_skills("SKILL CATALOG")
    return ComponentGraph(agents=agents, entrances=frozenset({"loop"}), sources={})
'''


def tree(system: str | bytes | None) -> dict[str, bytes]:
    """A version with the agents ``loop`` and ``direct``; ``system`` is ``agent/SYSTEM.md`` (None: no such file)."""
    extra: dict[str, str | bytes | None] = {"agent/sm.py": AGENTS}
    if system is not None:
        extra["agent/SYSTEM.md"] = system
    return minimal_tree((), extra=extra, compose=COMPOSE)


def llm_services(*responses: Any) -> tuple[ScriptedLLM, Any]:
    llm = ScriptedLLM(*responses)
    return llm, host_services(llms={name: llm for name in ("max", "high", "low")})


async def ask(run: Any, name: str, data: dict[str, Any]) -> AgentContext:
    """Run agent ``name`` from the run handle ``run``; return its context."""
    ctx = AgentContext(data=data, max_steps=8, agents=run)
    result = await asyncio.wait_for(run.agent(name).run(AgentCall(ctx, "go")), WAIT)
    assert result.status == "ok", result
    return ctx


async def ask_once(repo: Any, name: str, data: dict[str, Any]) -> AgentContext:
    run = repo.run()
    try:
        return await ask(run, name, data)
    finally:
        repo.release(run)


def sent(llm: ScriptedLLM) -> list[list[Message]]:
    return [messages for messages, _ in llm.requests]


async def test_vr56_the_standard_loop_starts_every_call_with_the_preamble_once(repos, tmp_path):
    """VR56: Both calls of a tool-then-answer loop start with SYSTEM.md, then the agent's prompt, then the skill catalog, once."""
    llm, services = llm_services(Calls((Call("sm_echo", {}),)), FinalAnswer("done"))
    repo, _, _ = await started(repos, tree("\nRULES_V1\n\n"), tmp_path, services=services)

    await ask_once(repo, "loop", {})

    assert len(llm.requests) == 2
    for messages in sent(llm):
        assert messages[0] == Message("system", "RULES_V1\n\nOWN PROMPT\n\nSKILL CATALOG")
        assert sum(message.content.count("RULES_V1") for message in messages) == 1
    assert [message.role for message in sent(llm)[1]] == ["system", "user", "assistant", "tool"]


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ([("system", "MINE"), ("user", "hi")], [Message("system", "RULES_V1\n\nMINE"), Message("user", "hi")]),
        ([("user", "hi")], [Message("system", "RULES_V1"), Message("user", "hi")]),
    ],
    ids=["own-system-first", "no-system"],
)
async def test_vr57_own_messages_get_the_preamble_without_being_changed(repos, tmp_path, given, expected):
    """VR57: Messages an agent builds itself get SYSTEM.md at send time; its own list is unchanged and a second call adds it once again."""
    llm, services = llm_services(FinalAnswer("one"), FinalAnswer("two"))
    repo, _, _ = await started(repos, tree("RULES_V1\n"), tmp_path, services=services)

    ctx = await ask_once(repo, "direct", {"messages": given})

    assert sent(llm) == [expected, expected]
    assert ctx.data["unchanged"] is True


@pytest.mark.parametrize("system", [None, "", " \n\t\n"], ids=["missing", "empty", "blank"])
async def test_vr58_no_preamble_leaves_every_call_as_before(repos, tmp_path, system):
    """VR58: Without SYSTEM.md, or with an empty or blank one, nothing is added: no text, no separator, no extra message."""
    llm, services = llm_services(FinalAnswer("done"), FinalAnswer("one"), FinalAnswer("two"))
    repo, _, _ = await started(repos, tree(system), tmp_path, services=services)

    await ask_once(repo, "loop", {})
    await ask_once(repo, "direct", {"messages": [("user", "hi")]})

    loop, first, second = sent(llm)
    assert loop[0] == Message("system", "OWN PROMPT\n\nSKILL CATALOG")
    assert first == second == [Message("user", "hi")]


async def test_vr59_each_run_sends_the_preamble_of_its_own_version(repos, tmp_path):
    """VR59: Changing only SYSTEM.md makes a new version; a run pinned before the merge keeps V1 and a new run sends V2."""
    llm, services = llm_services(*(FinalAnswer(str(number)) for number in range(4)))
    repo, store, v1 = await started(repos, tree("RULES_V1\n"), tmp_path, services=services)
    run1 = repo.run()
    run1.agent("direct")

    v2 = await merge_files(repo, store, tree("RULES_V2\n"), "v2")
    run2 = repo.run()
    try:
        await ask(run1, "direct", {"messages": [("user", "hi")]})
        await ask(run2, "direct", {"messages": [("user", "hi")]})
    finally:
        repo.release(run1)
        repo.release(run2)

    assert v2.id != v1.id
    assert [messages[0].content for messages in sent(llm)] == ["RULES_V1", "RULES_V1", "RULES_V2", "RULES_V2"]


async def test_vr60_a_preamble_of_exactly_8000_characters_loads(repos, tmp_path):
    """VR60 (a): 8,000 characters after stripping (24,000 UTF-8 bytes) load and are sent whole."""
    text = "あ" * 8000
    llm, services = llm_services(FinalAnswer("one"), FinalAnswer("two"))
    repo, store, _ = await started(repos, tree(None), tmp_path, services=services)

    await repo.validate(tree(f"\n{text}\n"))
    await merge_files(repo, store, tree(f"\n{text}\n"), "v2")
    await ask_once(repo, "direct", {"messages": [("user", "hi")]})

    assert sent(llm)[0] == [Message("system", text), Message("user", "hi")]


@pytest.mark.parametrize("system", ["x" * 8001, b"\xff\xfe not utf-8\n"], ids=["over-8000", "not-utf-8"])
async def test_vr60_an_invalid_preamble_fails_the_version(repos, tmp_path, system):
    """VR60 (b)(c): Over 8,000 characters or not UTF-8, the version fails to load in fork and in validate (the check CLI and hooks); main stays."""
    repo, _, v1 = await started(repos, tree("RULES_V1\n"), tmp_path)

    with pytest.raises(ValueError, match=r"agent/SYSTEM\.md"):
        await repo.fork(v1.id, tree(system), "v2")
    with pytest.raises(ValueError, match=r"agent/SYSTEM\.md"):
        await repo.validate(tree(system))

    assert repo.main.id == v1.id


async def test_vr61_a_harness_calling_its_llm_gets_no_preamble(repos, tmp_path):
    """VR61: In one run, the pre harness's direct LLM call is sent as written and the agent's call starts with SYSTEM.md."""
    llm, services = llm_services(FinalAnswer("allow"), FinalAnswer("done"))
    repo, _, _ = await started(repos, tree("RULES_V1\n"), tmp_path, services=services)

    await ask_once(repo, "loop", {"judge": True})

    judge, agent = sent(llm)
    assert judge == [Message("system", "JUDGE"), Message("user", "verdict?")]
    assert agent[0].content.startswith("RULES_V1\n\nOWN PROMPT")
