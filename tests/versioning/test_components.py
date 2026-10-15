"""Components under the version repository: VR28, VR30, VR31, VR35, VR43.

The store is an ``AsyncMock`` answering from fixed data (``version_store``). ``persist`` replaces those
fixed answers with what the Repo wrote, between steps, as the mirror and GitHub would answer after a fork or a merge.
"""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.async_helpers import Recorder, make_runtime
from tests.fakes import ScriptedLLM, finish_task
from tests.versioning.helpers import (
    DISPATCH,
    api,
    compose_source,
    event,
    host_services,
    make_commit,
    minimal_tree,
    namespace_entries,
    namespace_of,
    seed,
    version_store,
    with_marker,
)
from trackeverything.core.abc.call import AgentCall
from trackeverything.core.abc.context import AgentContext
from trackeverything.core.abc.llm import Call, Calls, FinalAnswer
from trackeverything.core.shared_state import shared

WAIT = 5


def _arguments(recorded: Any, *names: str) -> list[Any]:
    """The arguments of one recorded call in ``names`` order, positional or keyword."""
    values = list(recorded.args)
    return values + [recorded.kwargs[name] for name in names[len(values):]]


def persist(store: Any) -> None:
    """Let the mocked store answer later reads with the versions and main the Repo wrote (GitHub's main follows the mirror's)."""
    for commit, files in store.written.values():
        seed(store, (commit, files))
    if store.move_main.await_args_list:
        store.read_main.return_value = _arguments(store.move_main.await_args_list[-1], "expected", "new")[1]
        store.fetch.return_value = store.read_main.return_value


async def started(repos: Any, files: dict[str, bytes], tmp_path: Path, **options: Any) -> tuple[Any, Any, Any]:
    """A Repo started on a store whose main holds ``files``: (repo, store, main Commit)."""
    main = make_commit(files, message="v1")
    store = version_store(main=main, versions=[(main, files)])
    repo = await repos(store, **options)
    assert (await repo.start()).id == main.id
    return repo, store, main


async def merge_files(repo: Any, store: Any, files: dict[str, bytes], message: str) -> Any:
    """Fork ``files`` from the current main, merge it, and let the mocked store answer with the new main."""
    candidate = await repo.fork(repo.main.id, files, message)
    tree, parent, written_message = _arguments(store.write.await_args, "tree", "parent", "message")[:3]
    assert dict(tree) == files and (parent, written_message) == (repo.main.id, message)
    assert store.written[candidate.id][0] == candidate
    persist(store)
    merged = await repo.merge(candidate.id)
    persist(store)
    assert merged.id == candidate.id == repo.main.id
    return merged


class StampingLLM(ScriptedLLM):
    """A ScriptedLLM that also notes ``stamp()`` (the current code version) at every request."""

    def __init__(self, stamp: Any, *responses: Any) -> None:
        super().__init__(*responses)
        self.stamp = stamp
        self.stamps: list[str] = []

    async def respond(self, messages, callables):
        self.stamps.append(self.stamp())
        return await super().respond(messages, callables)


# ---- VR28

PROBE_28 = '''
import asyncio

from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.call import AgentResult


class Probe(BaseAgent):
    name = "probe"
    description = "probe"
    agents = ("k1", "k2")

    async def _execute(self, call):
        note = call.ctx.data["note"]
        note("probe")
        statuses = []
        for step in call.ctx.data["plan"]:
            if step == "hook":
                await call.ctx.data["hook"]()
            elif step == "gather":
                both = await asyncio.gather(self.call_agent(call, "k1", "gathered"), self.call_agent(call, "k2", "gathered"))
                statuses += [result.status for result in both]
            else:
                statuses.append((await self.call_agent(call, step, "solo")).status)
            note("probe")
        return AgentResult(status="ok" if all(status == "ok" for status in statuses) else "error", response=",".join(statuses))
'''

KIDS_28 = '''
from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.call import AgentResult
from trackeverything.tools.vr28_tool import StampTool


class K1(BaseAgent):
    name = "k1"
    description = "k1"

    async def _execute(self, call):
        note = call.ctx.data["note"]
        note("k1")
        gate = call.ctx.data.get("gate")
        if gate is not None and call.request == "gathered":
            await gate["e1"].wait()
            note("k1")
            gate["e2"].set()
        return AgentResult(status="ok", response="k1")


class K2(BaseAgent):
    name = "k2"
    description = "k2"
    tools = (StampTool,)

    async def _execute(self, call):
        note = call.ctx.data["note"]
        note("k2")
        gate = call.ctx.data["gate"]
        gate["e1"].set()
        await gate["e2"].wait()
        note("k2")
        return await self._execute_llm_loop(call)
'''

TOOL_28 = '''
from trackeverything.core.abc.tool import Tool, ToolResult


class StampTool(Tool):
    name = "vr28_tool"
    description = "notes the code version"
    arguments = {"type": "object", "properties": {}}

    async def run(self, args, *, ctx=None):
        ctx.data["note"]("tool")
        return ToolResult(ok=True, output="stamped")
'''


async def test_vr28_each_agent_records_the_version_it_runs_on(repos, tmp_path):
    """VR28: Values, spans and LLM calls of each Agent carry '<host>+c.<id8>' of that Agent's own version."""
    from trackeverything.core.builtin import code_version

    records: list[tuple[str, str, str, str]] = []

    async def record(event):
        records.append((event.run_id, event.event_type, event.component, code_version.current_code_version()))

    logger = AsyncMock()
    logger.record.side_effect = record
    k2_script = (Calls((Call("vr28_tool", {}),)), FinalAnswer("k2 done"))
    llm = StampingLLM(code_version.current_code_version, *k2_script, *k2_script)
    services = host_services(llms={name: llm for name in ("max", "high", "low")}, logger=logger)
    values: list[tuple[str, str, str]] = []

    def noter(run: str) -> Any:
        return lambda who: values.append((run, who, code_version.current_code_version()))

    files1 = minimal_tree(
        (),
        extra={"agent/vr28_probe.py": PROBE_28, "agent/vr28_kids.py": KIDS_28, "tools/vr28_tool.py": TOOL_28},
        compose=compose_source(["agent.vr28_probe.Probe", "agent.vr28_kids.K1", "agent.vr28_kids.K2"], ["probe"]),
    )
    files2 = with_marker(files1, "vr28_v2")
    files3 = {**files2, "agent/vr28_kids.py": files2["agent/vr28_kids.py"] + b"\nREVISION = 3\n"}
    code_version.override_code_version("hostsha")
    try:
        repo, store, v1 = await started(repos, files1, tmp_path, services=services)
        # Part A: run one holds v1 across the merge of v2; run two runs wholly on v2.
        reached, go = asyncio.Event(), asyncio.Event()

        async def hold() -> None:
            reached.set()
            await go.wait()

        run1 = repo.run()
        ctx1 = AgentContext(data={"note": noter("one"), "plan": ["k1", "hook", "k1"], "hook": hold}, agents=run1)
        first = asyncio.create_task(run1.agent("probe").run(AgentCall(ctx1, "one")))
        await asyncio.wait_for(reached.wait(), WAIT)
        v2 = await merge_files(repo, store, files2, "v2")
        run2 = repo.run()
        ctx2 = AgentContext(data={"note": noter("two"), "plan": ["k1"]}, agents=run2)
        second = await asyncio.wait_for(run2.agent("probe").run(AgentCall(ctx2, "two")), WAIT)
        go.set()
        first_result = await finish_task(first)
        # Part B: probe and k1 are pinned to v2, then v3 becomes main before k2 is first asked for.
        merged: list[Any] = []

        async def merge_v3() -> None:
            merged.append(await merge_files(repo, store, files3, "v3"))

        run3 = repo.run()
        gate = {"e1": asyncio.Event(), "e2": asyncio.Event()}
        ctx3 = AgentContext(data={"note": noter("three"), "plan": ["k1", "hook", "gather", "k2"], "hook": merge_v3, "gate": gate}, agents=run3)
        third = await asyncio.wait_for(run3.agent("probe").run(AgentCall(ctx3, "three")), WAIT)
        outside = code_version.current_code_version()
    finally:
        code_version.override_code_version(None)
    assert (first_result.status, second.status, third.status) == ("ok", "ok", "ok"), (first_result, second, third)
    (v3,) = merged
    one, two, three = (f"hostsha+c.{commit.id[:8]}" for commit in (v1, v2, v3))

    def seen(run: str, who: str | None = None) -> set[str]:
        return {value for name, author, value in values if name == run and who in (None, author)}

    def logged(ctx: Any, event_type: str | None = None, component: str | None = None) -> set[str]:
        return {value for run_id, kind, name, value in records if run_id == ctx.run_id and event_type in (None, kind) and component in (None, name)}

    assert {author for name, author, _ in values if name == "one"} == {"probe", "k1"} and seen("one") == {one}
    assert seen("two") == {two}
    assert seen("three", "probe") == {two} and seen("three", "k1") == {two}
    assert seen("three", "k2") == {three} and seen("three", "tool") == {three}
    # The parent's value after the gather and after the sequential k2 call is still its own version.
    assert [value for name, author, value in values if name == "three" and author == "probe"] == [two] * 5
    assert {name for run_id, _, name, _ in records if run_id == ctx1.run_id} == {"probe", "k1"} and logged(ctx1) == {one}
    assert logged(ctx2) == {two}
    assert logged(ctx3, "agent", "probe") == {two} and logged(ctx3, "agent", "k1") == {two}
    assert logged(ctx3, "agent", "k2") == {three} and logged(ctx3, "tool", "vr28_tool") == {three}
    assert llm.stamps == [three] * 4
    assert outside == "hostsha"
    for run in (run1, run2, run3):
        repo.release(run)
    assert namespace_entries(v1.id) == [] and namespace_entries(v2.id) == []
    assert namespace_entries(v3.id) != [] and repo.main.id == v3.id


# ---- VR30

PARENTS_30 = '''
from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.call import AgentResult
from trackeverything.core.shared_state import shared

SINK = shared(SINK_NAME, list)
HOOKS = shared(HOOKS_NAME, dict)


def _outcome(result):
    return [result.status, result.response, [note.stage for note in result.errors]]


class _Delegating(BaseAgent):
    agents = ("kid",)
    #: (child name, request) in call order.
    calls = (("kid", None),)

    async def _execute(self, call):
        SINK.append({"who": self.name, "agent": id(self), "ctx": id(call.ctx), "module": type(self).__module__})
        hook = HOOKS.get(self.name)
        if hook is not None:
            await hook()
        outcomes = [_outcome(await self.call_agent(call, name, request)) for name, request in self.calls]
        SINK.append({"who": self.name, "outcomes": outcomes, "has_kid": self.has_agent("kid"), "has_stranger": self.has_agent("stranger")})
        return AgentResult(status="ok", response="delegated")


class Router(BaseAgent):
    name = DISPATCH_NAME
    description = "runs the parent the internal event names"
    agents = ("p1", "p2", "p3")

    async def _execute(self, call):
        return await self.call_agent(call, call.ctx.data["event"]["payload"]["agent_name"], None)


class P1(_Delegating):
    name = "p1"
    calls = (("kid", "r1"), ("stranger", None))


class P2(_Delegating):
    name = "p2"


class P3(_Delegating):
    name = "p3"


class Kid(BaseAgent):
    name = "kid"
    description = "kid"

    async def _execute(self, call):
        SINK.append({"who": "kid", "agent": id(self), "ctx": id(call.ctx), "module": type(self).__module__})
        return AgentResult(status="ok", response="kid ok")


class Stranger(BaseAgent):
    name = "stranger"
    description = "nobody declares it"

    async def _execute(self, call):
        SINK.append({"who": "stranger"})
        return AgentResult(status="ok", response="stranger ok")
'''

P3_ALONE_30 = '''
from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.call import AgentResult
from trackeverything.core.shared_state import shared

SINK = shared(SINK_NAME, list)


class P3(BaseAgent):
    name = "p3"
    description = "p3 without children"

    async def _execute(self, call):
        SINK.append({"who": "p3", "version": "v2"})
        return AgentResult(status="ok", response="v2")
'''


def _tree_30(source: str, sink: str, hooks: str, agents: list[str], entrances: list[str]) -> dict[str, bytes]:
    header = f"SINK_NAME = {sink!r}\nHOOKS_NAME = {hooks!r}\nDISPATCH_NAME = {DISPATCH!r}\n"
    return minimal_tree((), extra={"agent/vr30.py": header + source}, compose=compose_source([f"agent.vr30.{name}" for name in agents], entrances))


async def test_vr30_call_agent_runs_declared_children_from_the_run_lookup_only(repos, tmp_path, monkeypatch):
    """VR30: call_agent gets declared children through ctx.agents (one shared instance per name) and rejects every other name.

    Each event's dispatch stand-in (``Router``) runs the parent its payload names, so every parent is a child of the Runtime's run.
    """
    modules = api()
    sink, hooks = f"vr30_sink_{uuid4().hex}", f"vr30_hooks_{uuid4().hex}"
    files1 = _tree_30(PARENTS_30, sink, hooks, ["Router", "P1", "P2", "P3", "Kid", "Stranger"], [DISPATCH])
    files2 = _tree_30(P3_ALONE_30, sink, hooks, ["P3"], ["p3"])
    logger = Recorder()
    repo, store, v1 = await started(repos, files1, tmp_path, services=host_services(logger=logger))
    v2 = await repo.fork(v1.id, files2, "v2")
    persist(store)
    asked: list[str] = []
    lookup = modules.repo.RunVersions.agent

    def recording(self, name):
        asked.append(name)
        return lookup(self, name)

    monkeypatch.setattr(modules.repo.RunVersions, "agent", recording)
    runtime = make_runtime(repo=repo)
    entries = shared(sink, list)

    # (a) P1 delegates to its declared kid on the same ctx; the undeclared stranger is never looked up.
    first = await runtime.execute(event(agent_name="p1"), "1700000000000-0")
    assert first.status == "ok", first
    assert [entry["who"] for entry in entries] == ["p1", "kid", "p1"]
    p1, kid, outcome = entries
    assert kid["ctx"] == p1["ctx"] and kid["module"].startswith(namespace_of(v1) + ".")
    assert outcome == {
        "who": "p1",
        "outcomes": [["ok", "kid ok", []], ["error", "agent not registered: stranger", ["delegation"]]],
        "has_kid": True,
        "has_stranger": False,
    }
    assert "kid" in asked and "stranger" not in asked
    # build_agents hands the logger to every instance; nothing propagates it from the parent.
    assert "kid" in {event.component for event in logger.of("agent")}

    # (b) Another parent declaring the same child gets the same instance.
    second = await runtime.execute(event(agent_name="p2"), "1700000000000-1")
    assert second.status == "ok", second
    assert [entry["who"] for entry in entries[3:]] == ["p2", "kid", "p2"]
    p2, shared_kid, outcome = entries[3:]
    assert shared_kid["agent"] == kid["agent"] and shared_kid["ctx"] == p2["ctx"]
    assert outcome["outcomes"] == [["ok", "kid ok", []]]

    # (c) P3 is resolved from v1; its first lookup of kid happens after v2 (no kid) became main.
    async def merge_v2() -> None:
        merged = await repo.merge(v2.id)
        assert merged.id == v2.id
        persist(store)

    shared(hooks, dict)["p3"] = merge_v2
    asked.clear()
    third = await runtime.execute(event(agent_name="p3"), "1700000000000-2")
    assert third.status == "ok", third
    assert [entry["who"] for entry in entries[6:]] == ["p3", "p3"]
    p3, outcome = entries[6:]
    assert p3["module"].startswith(namespace_of(v1) + ".") and repo.main.id == v2.id
    assert outcome == {"who": "p3", "outcomes": [["error", "agent not registered: kid", ["delegation"]]], "has_kid": True, "has_stranger": False}
    assert "kid" in asked
    assert {"who": "stranger"} not in entries and {"who": "p3", "version": "v2"} not in entries


# ---- VR31

PARENT_31 = '''
from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.harness import PreHarness
from trackeverything.tools.vl35_tool import Flaky

CHECKS = []


class Gate(PreHarness):
    name = "gate"

    async def check(self, call):
        CHECKS.append(call.ctx.data.get("marker"))
        return True


class Parent(BaseAgent):
    name = "parent"
    description = "parent"
    tools = (Flaky,)
    agents = ("child",)
    pre = (Gate,)
'''

TOOL_31 = '''
from trackeverything.core.abc.tool import Tool


class Flaky(Tool):
    name = "flaky"
    description = "always fails"
    arguments = {"type": "object", "properties": {}}

    async def run(self, args, *, ctx=None):
        await ctx.data["hook"]()
        raise RuntimeError("tool exploded")
'''


def _child_31(description: str, seen: str) -> str:
    return f'''
from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.call import AgentResult


class Child(BaseAgent):
    name = "child"
    description = {description!r}

    async def _execute(self, call):
        call.ctx.data["seen_by_child"] = {seen}
        return AgentResult(status="ok", response="child ok")
'''


def _tree_31(child: str) -> dict[str, bytes]:
    return minimal_tree(
        (),
        extra={"agent/vl35_parent.py": PARENT_31, "agent/vl35_child.py": child, "tools/vl35_tool.py": TOOL_31},
        compose=compose_source(["agent.vl35_parent.Parent", "agent.vl35_child.Child"], ["parent"]),
    )


async def test_vr31_child_offered_to_the_llm_is_pinned_when_offered(repos, tmp_path):
    """VR31: The child offered at step 1 keeps its version after a merge, and the run keeps the Agent contract."""
    llm = ScriptedLLM(
        Calls((Call("flaky", {}),)),
        Calls((Call("child", {"request": "help"}),)),
        Calls((Call("child", {"request": "again"}),)),
        Calls((Call("child", {"request": "more"}),)),
    )
    logger = Recorder()
    services = host_services(llms={name: llm for name in ("max", "high", "low")}, logger=logger)
    repo, store, v1 = await started(repos, _tree_31(_child_31("child v1", 'call.ctx.data.get("marker")')), tmp_path, services=services)
    v2 = await repo.fork(v1.id, _tree_31(_child_31("child v2", '"v2"')), "v2")
    persist(store)

    async def merge_v2() -> None:
        merged = await repo.merge(v2.id)
        assert merged.id == v2.id
        persist(store)

    run = repo.run()
    ctx = AgentContext(data={"marker": "m-1", "hook": merge_v2}, max_steps=4, agents=run)
    parent = run.agent("parent")
    result = await asyncio.wait_for(parent.run(AgentCall(ctx, "work")), WAIT)
    assert result.status == "max_steps", result
    assert repo.main.id == v2.id
    assert ctx.data["seen_by_child"] == "m-1"
    assert [{item["name"]: item["description"] for item in callables}.get("child") for _, callables in llm.requests] == ["child v1"] * 4
    assert sys.modules[type(parent).__module__].CHECKS == ["m-1"]
    failures = [json.loads(message.content) for message in llm.requests[1][0] if message.role == "tool"]
    assert failures and failures[0]["name"] == "flaky" and not failures[0]["ok"]
    v1_prefix = namespace_of(v1) + "."
    assert type(parent).__module__.startswith(v1_prefix)
    assert run.agent("parent") is parent and type(run.agent("child")).__module__.startswith(v1_prefix)
    assert {"parent", "child"} <= {event.component for event in logger.of("agent")}
    repo.release(run)
    assert namespace_entries(v1.id) == [] and namespace_entries(v2.id) != []


# ---- VR35


def _catalog_agent(cls: str, name: str, children: tuple[str, ...]) -> str:
    return (
        "from trackeverything.core.abc.agent import BaseAgent\n\n\n"
        f"class {cls}(BaseAgent):\n    name = {name!r}\n    description = {name + ' agent'!r}\n    agents = {children!r}\n"
    )


def _catalog_tree(alpha_children: tuple[str, ...], *, gamma: bool) -> dict[str, bytes]:
    agents = {
        "agent/vl36_alpha.py": _catalog_agent("Alpha", "alpha", alpha_children),
        "agent/vl36_beta.py": _catalog_agent("Beta", "beta", ("alpha",)),
        "agent/vl36_orphan.py": _catalog_agent("Orphan", "orphan", ("beta",)),
    }
    references = ["agent.vl36_alpha.Alpha", "agent.vl36_beta.Beta", "agent.vl36_orphan.Orphan"]
    if gamma:
        agents["agent/vl36_gamma.py"] = _catalog_agent("Gamma", "gamma", ())
        references.append("agent.vl36_gamma.Gamma")
    return minimal_tree((), extra=agents, compose=compose_source(references, ["alpha"]))


def _catalog_agents(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["name"]: entry for entry in snapshot["agents"]}


async def test_vr35_one_catalog_describes_one_version(repos, tmp_path):
    """VR35: A catalog snapshot is built from one main's graph agents and class declarations and survives that version's unload."""
    from trackeverything.api.catalog import create_snapshot_catalog_router
    from trackeverything.core.builtin import app as composition

    repo, store, v1 = await started(repos, _catalog_tree(("beta",), gamma=False), tmp_path)
    entered, go = asyncio.Event(), asyncio.Event()

    async def save_catalog(snapshot):
        if not entered.is_set():
            entered.set()
            await go.wait()

    stream = AsyncMock()
    stream.save_catalog.side_effect = save_catalog
    runtime = SimpleNamespace(repo=repo, producer=SimpleNamespace(stream=stream))
    first = asyncio.create_task(composition.publish_catalog(runtime))
    await asyncio.wait_for(entered.wait(), WAIT)
    v2 = await merge_files(repo, store, _catalog_tree(("beta", "gamma"), gamma=True), "v2")
    # v1 is neither main nor used by a run, so the merge unloaded it while its snapshot waits to be saved.
    assert namespace_entries(v1.id) == []
    go.set()
    await finish_task(first)
    await composition.publish_catalog(runtime)
    old, new = (recorded.args[0] for recorded in stream.save_catalog.await_args_list)

    agents = _catalog_agents(old)
    assert set(agents) == {"alpha", "beta", "orphan"}
    assert sorted(agents["alpha"]["entrances"]) == ["alpha"] and agents["beta"]["entrances"] == [] and agents["orphan"]["entrances"] == []
    assert sorted(agents["beta"]["parents"]) == ["alpha", "orphan"] and sorted(agents["alpha"]["parents"]) == ["beta"]
    assert sorted(agents["alpha"]["children"]) == ["beta"] and sorted(agents["orphan"]["children"]) == ["beta"]
    assert sorted(agents["beta"]["children"]) == ["alpha"]
    assert old["code_version"] == f"c.{v1.id[:8]}"
    agents = _catalog_agents(new)
    assert {"alpha", "beta", "orphan", "gamma"} <= set(agents)
    assert sorted(agents["gamma"]["parents"]) == ["alpha"] and sorted(agents["alpha"]["children"]) == ["beta", "gamma"]
    assert new["code_version"] == f"c.{v2.id[:8]}"
    assert "trackeverything.agent.vl36_alpha" not in sys.modules
    app = FastAPI()
    app.include_router(create_snapshot_catalog_router(AsyncMock(return_value=new)))
    assert TestClient(app).get("/api/catalog").json() == new


# ---- VR43

READER = """
from pathlib import Path

HERE = Path(__file__).resolve().parent


def stylesheet():
    return (HERE / "assets" / "report.css").read_text()
"""

RESOURCE_COMPOSE = """
from trackeverything.agent.main import Entry
from trackeverything.core.builtin.compose import ComponentGraph, build_agents


def build(services, *, skills_root=None):
    agents = build_agents((Entry,), services.llms, logger=services.logger)
    agents["main"].skills_root = skills_root
    return ComponentGraph(agents=agents, entrances=frozenset({"main"}), sources={})
"""


def _resource_tree(number: int) -> dict[str, bytes]:
    """A version whose Skill text, Skill script and module resource all carry ``number``; compose hands ``skills_root`` to ``main``."""
    return minimal_tree(
        extra={
            "tools/reader.py": READER,
            "tools/assets/report.css": f"/* V{number} */\n",
            "skills/s/SKILL.md": f"BODY_V{number}\n",
            "skills/s/scripts/run.py": f"print('V{number} SCRIPT')\n",
        },
        compose=RESOURCE_COMPOSE,
    )


async def test_vr43_skill_files_and_resources_come_from_each_versions_file_view(repos, tmp_path):
    """VR43: Skill text, Skill scripts and module resources are read from the file view of the version a run pinned."""
    repo, store, _ = await started(repos, _resource_tree(1), tmp_path)
    run1 = repo.run()
    v1 = run1.version("main")
    v2c = await merge_files(repo, store, _resource_tree(2), "v2")
    run2 = repo.run()
    v2 = run2.version("main")
    assert v2.id == v2c.id != v1.id

    for version, number in ((v1, 1), (v2, 2)):
        root = version.graph.agents["main"].skills_root
        assert root == version.view_path("skills")
        assert (root / "s/SKILL.md").read_text() == f"BODY_V{number}\n"
        assert (root / "s/scripts/run.py").read_text() == f"print('V{number} SCRIPT')\n"
        assert version.module("trackeverything.tools.reader").stylesheet() == f"/* V{number} */\n"

    skill = v1.view_path("skills/s/SKILL.md")
    repo.release(run1)
    assert namespace_entries(v1) == [] and not skill.exists()
    repo.release(run2)
    assert namespace_entries(v2) != []
