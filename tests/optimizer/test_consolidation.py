"""Optimizer の観点 consolidation: OP23-OP38・OP41・OP43(docs/components/OPTIMIZER.md)。

利用の読取り・schedule と reservation の参照・LLM は代役にし、版庫は代役の store で開いた singleton(実際の Repo)を使う。
fork の読込みと merge は実際に通す。PostgreSQL と実 LLM は使わない。
計画した module は用例の中で import し、実装前は各用例が red になる。
"""

import asyncio
import importlib
import json
from collections.abc import Iterable, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from tests.fakes import ScriptedLLM
from tests.optimizer.conftest import live_store
from tests.versioning.helpers import api, host_services, make_commit, seed
from trackeverything.core.abc.llm import Call, Calls, FinalAnswer, LLMClient
from trackeverything.optimizer import CheckContext, Observation

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=20)
TOOLS = {"list_files", "read_file", "grep", "write_file", "delete_file"}


def consolidation() -> Any:
    return importlib.import_module("trackeverything.optimizer.consolidation")


# ---- 部品の tree


def tool_module(name: str) -> str:
    return f'''
from trackeverything.core.abc.tool import Tool, ToolResult


class Impl(Tool):
    name = "{name}"
    description = "{name} tool"
    arguments = {{"type": "object", "properties": {{"q": {{"type": "string"}}}}}}

    async def run(self, args, *, ctx=None):
        return ToolResult(ok=True, output="{name}")
'''


def _tuple(items: Iterable[str]) -> str:
    values = list(items)
    return "(" + ", ".join(values) + ("," if len(values) == 1 else "") + ")"


def agent_module(name: str, *, tools: Iterable[str] = (), agents: Iterable[str] = (), skills: Iterable[str] = (),
                 body: str | None = None, base: tuple[str, str] | None = None) -> str:
    """class ``Impl`` の Agent。``base`` は (agent の module 名, 基底の類名)。``body`` は ``_execute`` の本体(8 字下げ)。"""
    tools = list(tools)
    imports = [f"from trackeverything.tools.{tool} import Impl as T_{tool}" for tool in tools]
    if base is None:
        imports.insert(0, "from trackeverything.core.abc.agent import BaseAgent")
        parent = "BaseAgent"
    else:
        imports.insert(0, f"from trackeverything.agent.{base[0]} import {base[1]}")
        parent = base[1]
    return "\n".join([
        "from trackeverything.core.abc.call import AgentResult",
        *imports,
        "",
        "",
        f"class Impl({parent}):",
        f'    name = "{name}"',
        f'    description = "{name} agent"',
        f"    tools = {_tuple(f'T_{tool}' for tool in tools)}",
        f"    agents = {tuple(agents)!r}",
        f"    skills = {tuple(skills)!r}",
        "",
        "    async def _execute(self, call):",
        body or f'        return AgentResult(status="ok", response="{name}")',
        "",
    ])


def compose_module(agents: Iterable[str], *, base_tool: str | None = "shared_probe") -> str:
    """dispatch を入口にし、``base_tool`` を全ての Agent に登録する(基本 Tool の代役)。"""
    names = list(agents)
    lines = [
        "from trackeverything.core.builtin.compose import ComponentGraph, build_agents",
        *(f"from trackeverything.agent.{name} import Impl as A_{name}" for name in names),
    ]
    if base_tool is not None:
        lines.append(f"from trackeverything.tools.{base_tool} import Impl as BaseTool")
    lines += [
        "",
        f"AGENTS = {_tuple(f'A_{name}' for name in names)}",
        'ENTRANCES = ("dispatch",)',
        "",
        "",
        "def build(services, *, skills_root=None):",
        "    agents = build_agents(AGENTS, services.llms, logger=services.logger)",
    ]
    if base_tool is not None:
        lines += ["    base = BaseTool()", "    for agent in agents.values():", "        agent.register(base)"]
    lines += ["    return ComponentGraph(agents=agents, entrances=frozenset(ENTRANCES), sources={})", ""]
    return "\n".join(lines)


def skill_file(name: str) -> str:
    return f"---\nname: {name}\ndescription: {name} skill\n---\n# {name}\n"


def scene(agents: Mapping[str, Mapping[str, Any]], *, tools: Iterable[str] = (), skills: Iterable[str] = (),
          composed: Iterable[str] | None = None, extra: Mapping[str, str | None] | None = None) -> dict[str, bytes]:
    """``agents``(名前 → ``agent_module`` の引数)・``tools``・``skills`` の tree。全ての Agent に登録する Tool ``shared_probe`` を含む(Host の基本 Tool とは別の名前)。"""
    files: dict[str, str | None] = {
        "agent/__init__.py": "",
        "tools/__init__.py": "",
        "tools/shared_probe.py": tool_module("shared_probe"),
        **{f"tools/{tool}.py": tool_module(tool) for tool in tools},
        **{f"agent/{name}.py": agent_module(name, **spec) for name, spec in agents.items()},
        **{f"skills/{skill}/SKILL.md": skill_file(skill) for skill in skills},
        "agent/compose.py": compose_module(composed if composed is not None else agents),
    }
    files.update(extra or {})
    return {path: content.encode() for path, content in files.items() if content is not None}


# ---- 実行と応答


def usage(**fields: Any) -> Any:
    return consolidation().Usage(**fields)


async def check_run(llm: LLMClient, *, used: Any = None, refs: Iterable[str] = (), merge: bool = True) -> tuple[list[Any], AsyncMock, AsyncMock]:
    """観点 consolidation を一度実行する。観測時刻は ``NOW``。(findings, 利用の読取り, 参照の読取り)。"""
    read_usage = AsyncMock(return_value=used if used is not None else usage())
    read_refs = AsyncMock(return_value=frozenset(refs))
    check = consolidation().ConsolidationCheck(read_usage=read_usage, read_refs=read_refs)
    ctx = CheckContext(observation=Observation(observed_at=NOW, main_version="", runs=()), llms={"max": llm}, merge=merge)
    return list(await check.run(ctx)), read_usage, read_refs


def survey(*groups: Iterable[str]) -> FinalAnswer:
    return FinalAnswer(json.dumps({"groups": [{"targets": list(group), "reason": "same purpose"} for group in groups]}))


def verdict(action: str, targets: Iterable[str], *, survivor: str | None = None, reason: str = "because") -> FinalAnswer:
    return FinalAnswer(json.dumps({"action": action, "targets": list(targets), "survivor": survivor, "reason": reason}))


def calls(*items: tuple[str, dict[str, Any]]) -> Calls:
    return Calls(tuple(Call(name, args) for name, args in items))


def write(path: str, content: str) -> tuple[str, dict[str, Any]]:
    return ("write_file", {"path": path, "content": content})


def delete(path: str) -> tuple[str, dict[str, Any]]:
    return ("delete_file", {"path": path})


def grep(pattern: str, prefix: str = "") -> tuple[str, dict[str, Any]]:
    return ("grep", {"pattern": pattern, "prefix": prefix})


def user_json(request: tuple[list[Any], list[dict[str, Any]]]) -> dict[str, Any]:
    messages, _ = request
    return json.loads(next(message.content for message in messages if message.role == "user"))


def tool_results(request: tuple[list[Any], list[dict[str, Any]]], count: int) -> list[dict[str, Any]]:
    messages, _ = request
    tail = messages[-count:]
    assert [message.role for message in tail] == ["tool"] * count
    return [json.loads(message.content) for message in tail]


def saved(store: Any) -> list[tuple[Any, dict[str, bytes]]]:
    """fork で保持(keep)した版の ``(Commit, tree)``。"""
    kept = [call.args[0] for call in store.keep.await_args_list]
    return [(store.written[version_id][0], dict(store.written[version_id][1])) for version_id in kept]


def outcomes(findings: list[Any]) -> list[tuple[str, str, str]]:
    return [(finding.detail["kind"], finding.detail["targets"][0], finding.detail["outcome"]) for finding in findings]


def current_main() -> str:
    return api().repo.current_repo().main.id


@pytest.fixture
async def opened(tmp_path):
    """``await opened(files, ancestor=None)`` は files の版を main とする singleton の版庫を開く。

    ``ancestor`` があれば、その tree の版(``OLD`` に作成)を main の親にする(年齢の判定用)。``(版庫, store, main の Commit)``。
    """
    repos: list[Any] = []

    async def open_(files: dict[str, bytes], *, ancestor: Mapping[str, bytes] | None = None) -> tuple[Any, Any, Any]:
        modules = api()
        parent = make_commit(dict(ancestor), message="old", created_at=OLD) if ancestor is not None else None
        main = make_commit(files, parent=parent.id if parent is not None else None)
        store = live_store(main, files)
        if parent is not None and ancestor is not None:
            seed(store, (parent, dict(ancestor)))
        repo = modules.repo.open_repo(modules.compose.prepare_components(host_services()), store=store)
        repos.append(repo)
        await repo.start()
        return repo, store, main

    yield open_
    for repo in reversed(repos):
        with suppress(RuntimeError):
            await repo.close()


# ---- 用例


def test_op23_consolidation_is_registered_after_errors():
    """OP23: CHECKS は errors・consolidation・additions・tuning・behavior の順で、checks を省くと全てをこの順に実行する。"""
    from trackeverything.optimizer import CHECKS, Optimizer

    assert [check.name for check in CHECKS] == ["errors", "consolidation", "additions", "tuning", "behavior"]
    assert isinstance(CHECKS[1], consolidation().ConsolidationCheck)
    optimizer = Optimizer(llms={}, read_runs=AsyncMock(return_value=[]))
    assert [check.name for check in optimizer.select(None)] == ["errors", "consolidation", "additions", "tuning", "behavior"]
    module = consolidation()
    assert (timedelta(days=30), timedelta(days=14), 20) == (module.WINDOW, module.GRACE, module.MIN_RUNS)
    assert (module.MAX_SURVEY, module.MAX_GROUPS, module.MAX_TURNS, module.MAX_FORKS, module.LLM_NAME) == (5, 5, 40, 2, "max")


async def test_op24_the_survey_receives_the_catalog_with_usage(opened):
    """OP24: 利用は観測の 30 日前を下限に一度読み、survey は Tool のない一回の呼出しで、宣言・上位・到達・利用を持つ目録を受け取る。"""
    files = scene(
        {"dispatch": {"agents": ("a",)}, "a": {"tools": ("t1", "t2"), "agents": ("b",), "skills": ("s",)}, "b": {}},
        tools=("t1", "t2"), skills=("s",),
    )
    await opened(files)
    used = usage(runs={"dispatch": 30, "a": 25, "b": 3}, last_run={"a": "2026-10-12T00:00:00+00:00"},
                 calls={("dispatch", "a"): 25, ("a", "t1"): 4, ("a", "b"): 3}, reads={("a", "s"): 2})
    llm = ScriptedLLM(survey())

    findings, read_usage, read_refs = await check_run(llm, used=used)

    assert findings == []
    read_usage.assert_awaited_once_with(since=NOW - timedelta(days=30))
    read_refs.assert_awaited_once_with()
    (request,) = llm.requests
    messages, callables = request
    assert messages[0].role == "system" and callables == []
    data = user_json(request)
    assert data["groups"] == []
    agents = {entry["name"]: entry for entry in data["catalog"]["agents"]}
    tools = {entry["name"]: entry for entry in data["catalog"]["tools"]}
    skills = {entry["name"]: entry for entry in data["catalog"]["skills"]}
    a = agents["a"]
    assert (a["tools"], a["children"], a["skills"], a["parents"]) == (["t1", "t2"], ["b"], ["s"], ["dispatch"])
    assert (a["reachable"], a["runs"], a["last_run"], a["old"], a["protected"], a["module"]) == (
        True, 25, "2026-10-12T00:00:00+00:00", False, False, "trackeverything.agent.a")
    assert agents["b"]["parents"] == ["a"] and agents["dispatch"]["protected"] is True
    assert (tools["t1"]["declared_by"], tools["t1"]["calls"], tools["t2"]["calls"], tools["t1"]["arguments"]) == (["a"], 4, 0, ["q"])
    assert tools["shared_probe"]["protected"] is True and tools["t1"]["protected"] is False
    assert (skills["s"]["declared_by"], skills["s"]["reads"], skills["s"]["description"]) == (["a"], 2, "s skill")


async def test_op25_an_unreachable_agent_is_deleted_and_merged(opened):
    """OP25: 入口から届かない Agent は unreachable の組になり、削除の fork を merge して merged を報告する。"""
    files = scene({"dispatch": {"agents": ("a",)}, "a": {}, "orphan": {}})
    _, store, commit = await opened(files)
    llm = ScriptedLLM(
        survey(),
        calls(delete("agent/orphan.py"), write("agent/compose.py", compose_module(["dispatch", "a"]))),
        verdict("delete", ["orphan"], reason="nobody reaches it"),
    )

    findings, _, _ = await check_run(llm)

    ((candidate, tree),) = saved(store)
    assert candidate.parent == commit.id and candidate.message == "optimizer:consolidation unreachable delete orphan"
    assert "agent/orphan.py" not in tree and current_main() == candidate.id
    (finding,) = findings
    assert finding.summary.startswith("unreachable orphan: merged")
    assert finding.evidence == ()
    assert finding.detail == {"kind": "unreachable", "targets": ["orphan"], "action": "delete", "survivor": None,
                              "outcome": "merged", "reason": "nobody reaches it", "base": commit.id,
                              "candidate": candidate.id, "paths": ["agent/compose.py", "agent/orphan.py"]}
    messages, callables = llm.requests[1]
    assert messages[0].role == "system" and {item["name"] for item in callables} == TOOLS
    data = user_json(llm.requests[1])
    assert (data["kind"], data["targets"]) == ("unreachable", ["orphan"])


async def test_op26_unconnected_declarations_are_unreachable(opened):
    """OP26: 登録されない Tool 類・合成されない Agent 類・宣言されない Skill は unreachable。名前のない基底の類は対象外。
    後の組は進んだ main から fork する。"""
    files = scene(
        {"dispatch": {"agents": ("a",)}, "a": {"tools": ("t1",), "base": ("shared", "Shared")}},
        tools=("t1", "t9"), skills=("lone",),
        extra={
            "agent/shared.py": "from trackeverything.core.abc.agent import BaseAgent\n\n\nclass Shared(BaseAgent):\n    pass\n",
            "agent/ghost.py": agent_module("ghost"),
        },
    )
    _, store, commit = await opened(files)
    llm = ScriptedLLM(
        survey(),
        calls(delete("agent/ghost.py")), verdict("delete", ["ghost"]),
        calls(delete("tools/t9.py")), verdict("delete", ["t9"]),
        calls(delete("skills/lone/SKILL.md")), verdict("delete", ["lone"]),
    )

    findings, _, _ = await check_run(llm)

    assert outcomes(findings) == [("unreachable", "ghost", "merged"), ("unreachable", "t9", "merged"), ("unreachable", "lone", "merged")]
    (first, _), (second, _), (third, tree) = saved(store)
    assert first.parent == commit.id and second.parent == first.id and third.parent == second.id
    assert "agent/ghost.py" not in tree and "tools/t9.py" not in tree and "skills/lone/SKILL.md" not in tree
    assert "agent/shared.py" in tree
    assert current_main() == third.id


async def test_op27_only_old_unprotected_agents_without_runs_are_unused(opened):
    """OP27: 0 回の Agent は、14 日前の版にある(a)か記録の初出が 14 日より前(c)なら unused。新しい(b)・schedule の参照(d)は対象外。"""
    files = scene({"dispatch": {"agents": ("a", "b", "c", "d")}, "a": {}, "b": {}, "c": {}, "d": {}})
    await opened(files, ancestor={"agent/a.py": b"# old a\n"})
    used = usage(runs={"dispatch": 50}, first_seen={"c": OLD, "d": OLD})
    llm = ScriptedLLM(survey(), verdict("keep", ["a"]), verdict("keep", ["c"]))

    findings, _, _ = await check_run(llm, used=used, refs={"d"})

    assert outcomes(findings) == [("unused", "a", "kept"), ("unused", "c", "kept")]
    assert [user_json(request)["targets"] for request in llm.requests[1:]] == [["a"], ["c"]]
    agents = {entry["name"]: entry for entry in user_json(llm.requests[0])["catalog"]["agents"]}
    assert (agents["a"]["old"], agents["b"]["old"], agents["c"]["old"], agents["d"]["protected"]) == (True, False, True, True)


def _edges_scene(runs: int) -> tuple[dict[str, bytes], Any]:
    files = scene(
        {"dispatch": {"agents": ("a",)},
         "a": {"tools": ("t1", "t2", "t3"), "agents": ("c",),
               "body": '        await self.call_tool(call, "t3", {})\n        return AgentResult(status="ok", response="a")'},
         "c": {}},
        tools=("t1", "t2", "t3"),
    )
    used = usage(runs={"dispatch": runs, "a": runs}, calls={("dispatch", "a"): runs, ("a", "t1"): 5},
                 first_seen={name: OLD for name in ("dispatch", "a", "t1", "t2", "t3")})
    return files, used


async def test_op28_declarations_never_called_by_a_busy_agent_are_pruned(opened):
    """OP28 (a): 25 回の Agent の、呼ばれない古い宣言(t2)だけが組になる。Python が名前で呼ぶ t3・新しい子 c は対象外。"""
    files, used = _edges_scene(25)
    _, store, commit = await opened(files)
    pruned = agent_module("a", tools=("t1", "t3"), agents=("c",),
                          body='        await self.call_tool(call, "t3", {})\n        return AgentResult(status="ok", response="a")')
    llm = ScriptedLLM(survey(), calls(write("agent/a.py", pruned)), verdict("prune", ["a", "t2"]))

    findings, _, _ = await check_run(llm, used=used)

    (finding,) = findings
    assert (finding.detail["kind"], finding.detail["targets"], finding.detail["action"], finding.detail["outcome"]) == (
        "unused_edges", ["a", "t2"], "prune", "merged")
    assert user_json(llm.requests[1])["targets"] == ["a", "t2"]
    ((candidate, tree),) = saved(store)
    assert candidate.parent == commit.id and "T_t2" not in tree["agent/a.py"].decode()


async def test_op28_an_agent_below_the_run_threshold_keeps_its_declarations(opened):
    """OP28 (b): 19 回の Agent の呼ばれない宣言は組にならない。"""
    files, used = _edges_scene(19)
    await opened(files)
    llm = ScriptedLLM(survey())

    findings, _, _ = await check_run(llm, used=used)

    assert findings == [] and len(llm.requests) == 1


async def test_op29_a_declared_skill_never_read_is_an_unused_edge(opened):
    """OP29: 読まれない古い Skill(s)は宣言した Agent の unused_edges に入り、一度読まれた Skill(r)は入らない。"""
    files = scene({"dispatch": {"agents": ("a",)}, "a": {"skills": ("s", "r")}}, skills=("s", "r"))
    await opened(files, ancestor={"skills/s/SKILL.md": b"old", "skills/r/SKILL.md": b"old"})
    used = usage(runs={"dispatch": 25, "a": 25}, calls={("dispatch", "a"): 25}, reads={("a", "r"): 1},
                 first_seen={"dispatch": OLD, "a": OLD})
    llm = ScriptedLLM(survey(), verdict("keep", ["a", "s"]))

    findings, _, _ = await check_run(llm, used=used)

    (finding,) = findings
    assert (finding.detail["kind"], finding.detail["targets"], finding.detail["outcome"]) == ("unused_edges", ["a", "s"], "kept")


async def test_op30_survey_groups_are_cleaned_capped_and_follow_unreachable(opened):
    """OP30 (a): 目録にない名前を外し、空の組を捨て、有効な先頭 5 組を unreachable の後に処理する。上限を超えた組は over limit。"""
    files = scene({"dispatch": {"agents": ("a", "b", "x")}, "a": {}, "b": {}, "x": {}, "orphan": {}})
    await opened(files)
    llm = ScriptedLLM(
        survey(("a", "b"), ("a", "nope"), ("nope",), ("b", "x"), ("a", "x"), ("x", "a"), ("b", "a")),
        *(verdict("keep", ["any"]) for _ in range(5)),
    )

    findings, _, _ = await check_run(llm)

    assert [(finding.detail["kind"], finding.detail["targets"], finding.detail["outcome"]) for finding in findings] == [
        ("unreachable", ["orphan"], "kept"),
        ("duplicate", ["a", "b"], "kept"),
        ("duplicate", ["a"], "kept"),
        ("duplicate", ["b", "x"], "kept"),
        ("duplicate", ["a", "x"], "kept"),
        ("duplicate", ["x", "a"], "skipped"),
    ]
    assert findings[-1].detail["reason"] == "over limit"
    assert [group["kind"] for group in user_json(llm.requests[0])["groups"]] == ["unreachable"]


@pytest.mark.parametrize("answer", [FinalAnswer("not json"), RuntimeError("llm down")], ids=["not-json", "llm-error"])
async def test_op30_a_failed_survey_still_handles_python_groups(opened, answer):
    """OP30 (b): survey の失敗は kind survey の failed になり、Python の組は処理する。"""
    files = scene({"dispatch": {"agents": ("a",)}, "a": {}, "orphan": {}})
    await opened(files)
    llm = ScriptedLLM(answer, verdict("keep", ["orphan"]))

    findings, _, _ = await check_run(llm)

    survey_finding, group = findings
    assert (survey_finding.detail["kind"], survey_finding.detail["outcome"]) == ("survey", "failed")
    assert (group.detail["kind"], group.detail["targets"], group.detail["outcome"]) == ("unreachable", ["orphan"], "kept")


async def test_op31_duplicates_are_merged_into_the_survivor_on_the_advancing_main(opened):
    """OP31: 重複の組は survivor へ統合して merge し、次の組は進んだ main から fork する。"""
    files = scene({"dispatch": {"agents": ("p",)}, "p": {"agents": ("a", "b")}, "a": {}, "b": {}})
    _, store, commit = await opened(files)
    survivor = agent_module("a", body='        # also does what b did\n        return AgentResult(status="ok", response="a")')
    llm = ScriptedLLM(
        survey(("a", "b"), ("p",)),
        calls(write("agent/p.py", agent_module("p", agents=("a",))), write("agent/a.py", survivor), delete("agent/b.py"),
              write("agent/compose.py", compose_module(["dispatch", "p", "a"]))),
        verdict("merge", ["a", "b"], survivor="a"),
        calls(write("agent/p.py", agent_module("p", agents=("a",), body='        return AgentResult(status="ok", response="p2")'))),
        verdict("demote", ["p"]),
    )

    findings, _, _ = await check_run(llm)

    assert outcomes(findings) == [("duplicate", "a", "merged"), ("duplicate", "p", "merged")]
    assert (findings[0].detail["action"], findings[0].detail["survivor"]) == ("merge", "a")
    (first, _), (second, tree) = saved(store)
    assert first.parent == commit.id and second.parent == first.id
    assert first.message == "optimizer:consolidation duplicate merge a,b"
    assert "agent/b.py" not in tree and tree["agent/a.py"] == survivor.encode() and b"p2" in tree["agent/p.py"]
    assert current_main() == second.id


async def test_op32_a_reference_check_failure_is_returned_to_the_llm(opened):
    """OP32 (a): 消した Agent を call_agent の文字列で呼ぶ行が残れば LLM へ返り、直した 2 回目を merge する。"""
    caller = '        return await self.call_agent(call, "x", "go")'
    files = scene({"dispatch": {"agents": ("a",)}, "a": {"agents": ("x",), "body": caller}, "x": {}})
    _, store, commit = await opened(files)
    llm = ScriptedLLM(
        survey(("x",)),
        calls(delete("agent/x.py"), write("agent/compose.py", compose_module(["dispatch", "a"])),
              write("agent/a.py", agent_module("a", body=caller))),
        verdict("delete", ["x"]),
        calls(write("agent/a.py", agent_module("a"))),
        verdict("delete", ["x"]),
    )

    findings, _, _ = await check_run(llm)

    messages, _ = llm.requests[3]
    assert messages[-1].role == "user" and '"x"' in messages[-1].content and "agent/a.py" in messages[-1].content
    ((candidate, tree),) = saved(store)
    assert candidate.parent == commit.id and "agent/x.py" not in tree
    assert findings[0].detail["outcome"] == "merged"


def _refs_case() -> tuple[dict[str, bytes], tuple[Any, ...], set[str], str]:
    files = scene({"dispatch": {"agents": ("a", "s1")}, "a": {}, "s1": {}})
    change = calls(delete("agent/s1.py"), write("agent/compose.py", compose_module(["dispatch", "a"])),
                   write("agent/dispatch.py", agent_module("dispatch", agents=("a",))))
    return files, (survey(("s1",)), change, verdict("delete", ["s1"]), change, verdict("delete", ["s1"])), {"s1"}, "s1"


def _protected_case() -> tuple[dict[str, bytes], tuple[Any, ...], set[str], str]:
    files = scene({"dispatch": {"agents": ("a",)}, "a": {}})
    change = calls(delete("tools/shared_probe.py"), write("agent/compose.py", compose_module(["dispatch", "a"], base_tool=None)))
    return files, (survey(("shared_probe",)), change, verdict("delete", ["shared_probe"]), change, verdict("delete", ["shared_probe"])), set(), "shared_probe"


@pytest.mark.parametrize("case", [_refs_case, _protected_case], ids=["schedule-reference", "protected-base-tool"])
async def test_op32_two_reference_check_failures_are_rejected(opened, case):
    """OP32 (b)(c): schedule が名指しする Agent・基本 Tool を消す変更は 2 回とも参照の検査に落ち、rejected で fork せず main は変わらない。"""
    files, script, refs, name = case()
    _, store, commit = await opened(files)
    llm = ScriptedLLM(*script)

    findings, _, _ = await check_run(llm, refs=refs)

    (finding,) = findings
    assert finding.detail["outcome"] == "rejected" and name in finding.detail["reason"]
    assert len(llm.requests) == 5
    store.keep.assert_not_awaited()
    store.push.assert_not_awaited()
    assert current_main() == commit.id


async def test_op33_writes_and_deletes_are_limited_and_grep_is_capped(opened):
    """OP33: agent/・tools/・skills/ の外への書込みと削除・無いファイルの削除は失敗。grep は ``path:line: text`` で 200 行まで。"""
    files = scene({"dispatch": {"agents": ("a",)}, "a": {}}, extra={"tools/big.txt": "x\n" * 300})
    _, store, _ = await opened(files)
    llm = ScriptedLLM(
        survey(("a",)),
        calls(write("harnesses/x.py", "X = 1\n"), delete("sources/x.py"), delete("../x.py"), write("core/x.py", "X = 1\n"),
              delete("agent/missing.py"), grep("^x$", "tools/"), grep("class Impl", "agent/")),
        verdict("keep", ["a"]),
    )

    findings, _, _ = await check_run(llm)

    results = tool_results(llm.requests[2], 7)
    assert [result["ok"] for result in results] == [False] * 5 + [True, True]
    assert all(result["error"] for result in results[:5])
    capped = results[5]["output"].splitlines()
    assert len(capped) == 200 and capped[0] == "tools/big.txt:1: x"
    assert "agent/a.py:" in results[6]["output"] and "class Impl" in results[6]["output"]
    assert findings[0].detail["outcome"] == "kept"
    store.write.assert_not_awaited()


@pytest.mark.parametrize(
    ("script", "reason"),
    [
        ((calls(write("agent/a.py", agent_module("a", body='        return AgentResult(status="ok", response="z")'))),
          verdict("keep", ["a"], reason="fine")), "fine"),
        ((verdict("delete", ["a"], reason="dead"),), "delete without changes: dead"),
        ((FinalAnswer("書込み {失敗} のため keep にします。\n```json\n"
                      + verdict("keep", ["a"], reason="prompt only is not true").text + "\n```"),),
         "prompt only is not true"),
    ],
    ids=["keep-with-writes", "delete-without-changes", "text-around-the-json"],
)
async def test_op34_keep_or_no_changes_is_kept_without_a_fork(opened, script, reason):
    """OP34: keep の書込みは捨て、変更のない delete は kept。どちらも fork しない。最終回答の JSON の前後の文は読み飛ばす。"""
    files = scene({"dispatch": {"agents": ("a",)}, "a": {}})
    _, store, commit = await opened(files)
    llm = ScriptedLLM(survey(("a",)), *script)

    findings, _, _ = await check_run(llm)

    (finding,) = findings
    assert (finding.detail["outcome"], finding.detail["reason"]) == ("kept", reason)
    store.write.assert_not_awaited()
    assert current_main() == commit.id


async def test_op35_a_fork_failure_is_returned_to_the_llm_once(opened):
    """OP35 (a): 読み込めない tree の fork の失敗は LLM へ返り、直した 2 回目を merge する。"""
    files = scene({"dispatch": {"agents": ("a",)}, "a": {}, "orphan": {}})
    _, store, commit = await opened(files)
    llm = ScriptedLLM(
        survey(),
        calls(delete("agent/orphan.py")), verdict("delete", ["orphan"]),
        calls(write("agent/compose.py", compose_module(["dispatch", "a"]))), verdict("delete", ["orphan"]),
    )

    findings, _, _ = await check_run(llm)

    messages, _ = llm.requests[3]
    assert messages[-1].role == "user" and "orphan" in messages[-1].content
    ((candidate, _),) = saved(store)
    assert candidate.parent == commit.id and findings[0].detail["outcome"] == "merged"


async def test_op35_two_fork_failures_leave_main_unchanged(opened):
    """OP35 (b): fork が 2 回とも失敗すれば fork_failed で、main は変わらない。"""
    files = scene({"dispatch": {"agents": ("a",)}, "a": {}, "orphan": {}})
    _, store, commit = await opened(files)
    llm = ScriptedLLM(
        survey(),
        calls(delete("agent/orphan.py")), verdict("delete", ["orphan"]),
        calls(write("agent/compose.py", "def broken(:\n")), verdict("delete", ["orphan"]),
    )

    findings, _, _ = await check_run(llm)

    assert findings[0].detail["outcome"] == "fork_failed"
    assert len(llm.requests) == 5
    store.keep.assert_not_awaited()
    store.push.assert_not_awaited()
    assert current_main() == commit.id


def _moves_shared_main_after_the_fork(store: Any, other: Any) -> None:
    """共有の main は、fork が版を書いた直後に他の push で ``other`` へ動く。"""
    writing = store.write.side_effect

    async def write_then_move(*args: Any) -> Any:
        commit = await writing(*args)
        store.fetch.return_value = other.id
        return commit

    store.write.side_effect = write_then_move


async def test_op36_a_merge_after_the_shared_main_moved_is_rebased(opened):
    """OP36 (a): fork の後に共有の main が動けば、候補を載せ直して merged、``rebased_from`` に元の候補を報告する。"""
    files = scene({"dispatch": {"agents": ("a",)}, "a": {}, "orphan": {}})
    _, store, commit = await opened(files)
    other_files = {**files, "tools/other.txt": b"other"}
    other = make_commit(other_files, parent=commit.id, message="person")
    seed(store, (other, other_files))
    _moves_shared_main_after_the_fork(store, other)
    combined = {path: data for path, data in other_files.items() if path != "agent/orphan.py"}
    combined["agent/compose.py"] = compose_module(["dispatch", "a"]).encode()
    store.merge_tree.return_value = combined
    llm = ScriptedLLM(
        survey(),
        calls(delete("agent/orphan.py"), write("agent/compose.py", compose_module(["dispatch", "a"]))),
        verdict("delete", ["orphan"]),
    )

    findings, _, _ = await check_run(llm)

    (candidate, _), (rebased, tree) = saved(store)
    assert candidate.parent == commit.id and rebased.parent == other.id and tree == combined
    detail = findings[0].detail
    assert (detail["outcome"], detail["candidate"], detail["rebased_from"]) == ("merged", rebased.id, candidate.id)
    assert current_main() == rebased.id


async def test_op36_a_conflicting_merge_fails(opened):
    """OP36 (b): 載せ直しが衝突すれば failed(``merge: MergeConflict: …``)で、mirror の main は変わらない。"""
    files = scene({"dispatch": {"agents": ("a",)}, "a": {}, "orphan": {}})
    _, store, commit = await opened(files)
    other_files = {**files, "agent/compose.py": b"# changed by a person\n" + files["agent/compose.py"]}
    other = make_commit(other_files, parent=commit.id, message="person")
    seed(store, (other, other_files))
    _moves_shared_main_after_the_fork(store, other)
    store.merge_tree.side_effect = api().repo.MergeConflict(["agent/compose.py"])
    llm = ScriptedLLM(
        survey(),
        calls(delete("agent/orphan.py"), write("agent/compose.py", compose_module(["dispatch", "a"]))),
        verdict("delete", ["orphan"]),
    )

    findings, _, _ = await check_run(llm)

    detail = findings[0].detail
    assert detail["outcome"] == "failed" and detail["reason"].startswith("merge: MergeConflict")
    assert "rebased_from" not in detail
    store.push.assert_not_awaited()
    assert current_main() == commit.id


async def test_op37_groups_over_the_limit_are_skipped(opened):
    """OP37 (a): 7 組なら先頭 5 組を処理し、残る 2 組は skipped(over limit)。"""
    names = [f"o{index}" for index in range(1, 8)]
    files = scene({"dispatch": {"agents": ("a",)}, "a": {}, **{name: {} for name in names}})
    await opened(files)
    llm = ScriptedLLM(survey(), *(verdict("keep", [name]) for name in names[:5]))

    findings, _, _ = await check_run(llm)

    assert outcomes(findings) == [*(("unreachable", name, "kept") for name in names[:5]),
                                  ("unreachable", "o6", "skipped"), ("unreachable", "o7", "skipped")]
    assert all(finding.detail["reason"] == "over limit" for finding in findings[5:])
    assert len(llm.requests) == 6


async def test_op37_a_group_whose_target_is_gone_is_skipped(opened):
    """OP37 (b): 先の merge で対象が消えた組は会話を始めず skipped(gone)。"""
    files = scene({"dispatch": {"agents": ("a",)}, "a": {}, "o1": {}})
    await opened(files)
    llm = ScriptedLLM(
        survey(("o1",)),
        calls(delete("agent/o1.py"), write("agent/compose.py", compose_module(["dispatch", "a"]))),
        verdict("delete", ["o1"]),
    )

    findings, _, _ = await check_run(llm)

    assert outcomes(findings) == [("unreachable", "o1", "merged"), ("duplicate", "o1", "skipped")]
    assert findings[1].detail["reason"] == "gone"
    assert len(llm.requests) == 3


@pytest.mark.parametrize(
    ("first", "reason"),
    [
        ((RuntimeError("llm down"),), "RuntimeError: llm down"),
        (tuple(calls(("list_files", {"prefix": ""})) for _ in range(40)), "40"),
        ((FinalAnswer("done"),), "JSON"),
    ],
    ids=["llm-error", "turn-limit", "not-json"],
)
async def test_op38_a_failed_conversation_does_not_stop_the_next_group(opened, first, reason):
    """OP38 (a)-(c): LLM の例外・応答の上限・JSON でない最終回答は failed で fork せず、次の組を処理する。"""
    files = scene({"dispatch": {"agents": ("a",)}, "a": {}, "o1": {}, "o2": {}})
    _, store, _ = await opened(files)
    llm = ScriptedLLM(survey(), *first, verdict("keep", ["o2"]))

    findings, _, _ = await check_run(llm)

    assert outcomes(findings) == [("unreachable", "o1", "failed"), ("unreachable", "o2", "kept")]
    assert reason in findings[0].detail["reason"]
    store.write.assert_not_awaited()


async def test_op38_cancellation_propagates(opened):
    """OP38 (d): LLM の応答中の取消しは伝わる。"""
    await opened(scene({"dispatch": {"agents": ("a",)}, "a": {}}))
    started = asyncio.Event()

    class Blocking(LLMClient):
        async def respond(self, messages, callables):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(check_run(Blocking()))
    await asyncio.wait_for(started.wait(), 1)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_op38_no_groups_means_no_findings(opened):
    """OP38 (e): Python の組がなく survey も組を返さなければ、finding は空で会話も fork もしない。"""
    _, store, _ = await opened(scene({"dispatch": {"agents": ("a",)}, "a": {}}))
    llm = ScriptedLLM(survey())

    findings, _, _ = await check_run(llm, used=usage(runs={"dispatch": 3, "a": 3}))

    assert findings == []
    assert len(llm.requests) == 1
    store.write.assert_not_awaited()


async def test_op41_without_merge_a_change_is_only_forked(opened):
    """OP41: ``merge`` が偽なら変更は fork で候補を残すだけで main は変わらない(outcome ``forked``)。"""
    files = scene({"dispatch": {"agents": ("a",)}, "a": {}, "orphan": {}})
    _, store, commit = await opened(files)
    llm = ScriptedLLM(
        survey(),
        calls(delete("agent/orphan.py"), write("agent/compose.py", compose_module(["dispatch", "a"]))),
        verdict("delete", ["orphan"], reason="nobody reaches it"),
    )

    findings, _, _ = await check_run(llm, merge=False)

    ((candidate, _),) = saved(store)
    (finding,) = findings
    assert finding.detail["outcome"] == "forked" and finding.detail["candidate"] == candidate.id
    assert current_main() == commit.id
    store.push.assert_not_awaited()


async def test_op43_the_system_preamble_can_be_neither_written_nor_deleted(opened):
    """OP43: agent/SYSTEM.md への write_file と delete_file は失敗の結果で tree を変えない。変更のない delete は kept で fork しない。"""
    system = "agent/SYSTEM.md"
    files = scene({"dispatch": {"agents": ("a",)}, "a": {}}, extra={system: "RULES\n"})
    _, store, commit = await opened(files)
    llm = ScriptedLLM(survey(("a",)), calls(write(system, "OTHER RULES\n"), delete(system)), verdict("delete", ["a"], reason="dead"))

    findings, _, _ = await check_run(llm)

    results = tool_results(llm.requests[2], 2)
    assert [result["ok"] for result in results] == [False, False]
    assert all(system in result["error"] for result in results)
    assert findings[0].detail["outcome"] == "kept" and findings[0].detail["reason"] == "delete without changes: dead"
    store.write.assert_not_awaited()
    assert current_main() == commit.id
