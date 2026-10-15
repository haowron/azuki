"""The Runtime over a real Repo: VR24-VR26, VR29.

Every message is an ``EventRequest``. Each run refreshes the database main, takes ``repo.run()``, looks up
``config.DISPATCH_AGENT`` in it, starts that Agent only if it is an entrance of the version, and releases the
run at its end. The version store is an ``AsyncMock`` with fixed answers (``version_store``); a scene that
needs a later read to differ changes ``store.read_main.return_value`` or seeds rows.
"""

import asyncio
import inspect
import json
import threading
from collections.abc import Iterable
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import trackeverything.config as config
from tests.async_helpers import WAIT, FakeQueue, Recorder, make_runtime, settle, until
from tests.fakes import ScriptedLLM, finish_task, in_task
from tests.versioning.helpers import (
    DISPATCH,
    EMPTY_STATE,
    ROOT,
    api,
    compose_source,
    event,
    host_services,
    loaded_state,
    make_commit,
    minimal_tree,
    namespace_entries,
    namespace_of,
    seed,
    version_store,
)
from trackeverything.core.abc.call import AgentResult
from trackeverything.core.abc.llm import Call, Calls, FinalAnswer
from trackeverything.core.abc.submission import REQUEST_ID, EventRequest
from trackeverything.core.shared_state import shared

FILES_AT = "files@2026-10-13T00:00:00Z"

#: VR24's agents. Each records its runs in a uuid-named shared list. The dispatch stand-in delegates
#: through the LLM only when the payload asks for it, and ACKs its message as the real dispatch does.
VR24_AGENTS = '''
from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.call import AgentResult
from trackeverything.core.shared_state import shared


def seen():
    return shared({seen!r}, list)


class Disp(BaseAgent):
    name = {dispatch!r}
    description = "event entrance"
    agents = ("kid",)

    async def _execute(self, call):
        seen().append(("run", self.name, __name__))
        seen().append(("agents", call.ctx.agents))
        try:
            if call.ctx.data["event"]["payload"].get("delegate"):
                return await super()._execute(call)
            return AgentResult(status="ok", response="dispatched")
        finally:
            await call.ctx.ack(call.ctx.message_id)


class Kid(BaseAgent):
    name = "kid"
    description = "Python-controlled child"

    async def _execute(self, call):
        seen().append(("run", self.name, __name__))
        seen().append(("kid is itself", call.ctx.agents.agent("kid") is self))
        seen().append(("ctx", call.ctx))
        call.ctx.data.setdefault("trail", []).append("kid ran")
        return AgentResult(status="ok", response="kid done")
'''

#: VR26's entrance and child. The test drives them through hooks in the event payload.
PROBE = '''
from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.call import AgentResult


class Probe(BaseAgent):
    name = {dispatch!r}
    description = "probe entrance"
    agents = ("kid",)

    async def _execute(self, call):
        return await call.ctx.data["event"]["payload"]["script"](self, call)


class Kid(BaseAgent):
    name = "kid"
    description = "returns its module name"

    async def _execute(self, call):
        await call.ctx.data["event"]["payload"]["kid_hook"](self, call)
        return AgentResult(status="ok", response=__name__)
'''

SLOW = '''
import threading

from trackeverything.core.shared_state import shared

shared({loads!r}, list).append(1)
if not shared({gate!r}, threading.Event).wait(5):
    raise TimeoutError("the gate stayed closed")
'''


def _calls(store: Any, method: str) -> list[tuple[Any, ...]]:
    """Arguments of each awaited call of a VersionStore method, in declaration order."""
    signature = inspect.signature(getattr(api().store.VersionStore, method))
    return [tuple(signature.bind(None, *call.args, **call.kwargs).arguments.values())[1:] for call in getattr(store, method).await_args_list]


def _ran_on(result: Any, version: Any, value: int = 1) -> None:
    """The ``Entry`` of ``version`` answered, seeing dependency VERSION ``value`` before and after its hook."""
    assert result.status == "ok", result
    assert result.response.startswith(f"{value}->{value}|{namespace_of(version)}."), result.response


def _unknown(result: Any, name: str) -> None:
    assert result.status == "error" and result.response == f"unknown entrance: {name}", result
    assert result.errors[0].stage == "entrance"


def _finished(sessions: Any) -> Any:
    """The result of the last ``finish_run`` record."""
    call = sessions.finish_run.await_args
    return call.args[1] if len(call.args) > 1 else call.kwargs["result"]


def _view_file(version: Any) -> Any:
    """A file of a loaded version's file view; it must disappear when the version is unloaded."""
    path = version.view_path("agent/compose.py")
    assert path.is_file(), path
    return path


def _holding() -> tuple[Any, asyncio.Event, asyncio.Event]:
    """A hook that sets ``reached`` and then waits for ``go``."""
    reached, go = asyncio.Event(), asyncio.Event()

    async def hook() -> None:
        reached.set()
        await asyncio.wait_for(go.wait(), WAIT)

    return hook, reached, go


def _stored(files: dict[str, bytes]) -> tuple[Any, Any]:
    """A store whose main is a version of ``files`` written earlier."""
    main = make_commit(files, message=FILES_AT)
    return main, version_store(main=main, versions=[(main, files)])


async def _started(repos: Any, tmp_path: Any, store: Any, **options: Any) -> Any:
    """A Repo started from the mirror's main."""
    repo = await repos(store, **options)
    await repo.start()
    return repo


def _vr24_tree(seen: str, entrances: tuple[str, ...]) -> dict[str, bytes]:
    return minimal_tree(
        entries=(),
        extra={"agent/vr24.py": VR24_AGENTS.format(seen=seen, dispatch=DISPATCH)},
        compose=compose_source(["agent.vr24.Disp", "agent.vr24.Kid"], entrances),
    )


def _runs(seen: list[Any]) -> list[tuple[str, str]]:
    """(entrance or child name, module name) of each recorded run, in order."""
    return [(item[1], item[2]) for item in seen if item[0] == "run"]


def _trace(monkeypatch: Any, seen: list[Any]) -> list[Any]:
    """Note in ``seen`` each finished ``Repo.refresh`` (with main's id), each ``Repo.run`` and each ``RunVersions.version`` name.

    Returns the list of the runs ``Repo.run`` handed out, in order.
    """
    modules = api()
    refresh, run, version = modules.repo.Repo.refresh, modules.repo.Repo.run, modules.repo.RunVersions.version
    runs: list[Any] = []

    async def refreshing(self: Any) -> Any:
        commit = await refresh(self)
        seen.append(("refreshed", commit.id))
        return commit

    def running(self: Any) -> Any:
        created = run(self)
        runs.append(created)
        seen.append(("new run",))
        return created

    def looking(self: Any, name: str) -> Any:
        seen.append(("version", name))
        return version(self, name)

    monkeypatch.setattr(modules.repo.Repo, "refresh", refreshing)
    monkeypatch.setattr(modules.repo.Repo, "run", running)
    monkeypatch.setattr(modules.repo.RunVersions, "version", looking)
    return runs


def _steps(seen: list[Any]) -> list[tuple[Any, ...]]:
    """The traced steps and runs of ``seen``, each cut to (kind, value)."""
    return [item[:2] for item in seen if item[0] in ("refreshed", "new run", "version", "run")]


def _in_order(steps: list[tuple[Any, ...]], expected: Iterable[tuple[Any, ...]]) -> bool:
    """Whether ``expected`` occurs in ``steps`` in this order (other steps may come between)."""
    remaining = iter(steps)
    return all(any(step == wanted for step in remaining) for wanted in expected)


async def test_vr24_each_event_refreshes_then_runs_the_dispatch_entrance_of_the_main(repos, tmp_path, monkeypatch):
    """VR24: each EventRequest refreshes, looks up config.DISPATCH_AGENT in a new run and starts it only as an entrance of that version, with the run on the ctx and nothing about versions in data."""
    modules = api()
    seen_name = f"vr24-seen-{uuid4().hex}"
    seen = shared(seen_name, list)
    tree1 = _vr24_tree(seen_name, (DISPATCH,))
    v1, store = _stored(tree1)
    llm = ScriptedLLM(Calls((Call("kid", {"request": "help"}),)), FinalAnswer("done"))
    recorder = Recorder()
    services = host_services(llms={"max": llm, "high": ScriptedLLM(), "low": ScriptedLLM()}, logger=recorder)
    repo = await _started(repos, tmp_path, store, services=services)
    runs = _trace(monkeypatch, seen)
    queue, sessions = FakeQueue(), AsyncMock()
    runtime = make_runtime(repo=repo, queue=queue, store=sessions)

    # (a) dispatch runs from the run the Runtime took after its refresh, and delegates to its child through that run.
    request = EventRequest(provider="internal", external_id="vr24-a", payload={"delegate": True, "topic": "vr24"})
    result = await runtime.execute(request, "1700000000000-1")
    assert result.status == "ok" and result.response == "done"
    assert [name for name, _ in _runs(seen)] == [DISPATCH, "kid"]
    assert all(module.startswith(f"{namespace_of(v1)}.") for _, module in _runs(seen))
    steps = _steps(seen)
    assert steps[:3] == [("refreshed", v1.id), ("new run",), ("version", DISPATCH)], steps
    assert _in_order(steps, [("version", DISPATCH), ("run", DISPATCH), ("run", "kid")]), steps
    [agents] = [item[1] for item in seen if item[0] == "agents"]
    assert runs == [agents] and isinstance(agents, modules.repo.RunVersions)
    assert ("kid is itself", True) in seen
    [ctx] = [item[1] for item in seen if item[0] == "ctx"]
    assert ctx.agents is agents
    assert ctx.data == {"event": {"provider": "internal", "external_id": "vr24-a", "payload": {"delegate": True, "topic": "vr24"}}, "trail": ["kid ran"]}
    assert queue.acked == ["1700000000000-1"]
    assert len(llm.requests) == 2 and recorder.events
    texts = [json.dumps([[message.content for message in messages], callables], default=str, ensure_ascii=False) for messages, callables in llm.requests]
    texts += [repr(call) for call in sessions.begin_run.await_args_list]
    texts += [json.dumps([logged.request, logged.result, logged.error], default=str, ensure_ascii=False) for logged in recorder.events]
    texts.append(repr(_finished(sessions)))
    assert [text for text in texts if any(leak in text for leak in (v1.id, ROOT, "RunVersions"))] == []
    with pytest.raises(RuntimeError):
        agents.agent("kid")

    # (b) Each event reads the database main before its run is taken, so a main moved elsewhere runs next.
    tree2 = _vr24_tree(seen_name, (DISPATCH, "kid"))
    v2 = make_commit(tree2, parent=v1.id, message=FILES_AT)
    seed(store, (v2, tree2))
    store.read_main.return_value = v2.id
    seen.clear()
    runs.clear()
    for number in (2, 3):
        result = await runtime.execute(event(), f"1700000000000-{number}")
        assert result.status == "ok" and result.response == "dispatched", result
    steps = _steps(seen)
    one_event = [("refreshed", v2.id), ("new run",), ("version", DISPATCH), ("run", DISPATCH)]
    assert _in_order(steps, one_event * 2), steps
    assert [step for step in steps if step[0] in ("refreshed", "new run")] == [("refreshed", v2.id), ("new run",)] * 2
    assert len(runs) == 2 and [item[1] for item in seen if item[0] == "agents"] == runs
    assert all(module.startswith(f"{namespace_of(v2)}.") for _, module in _runs(seen))
    assert queue.acked == ["1700000000000-1", "1700000000000-2", "1700000000000-3"]
    assert namespace_entries(v1) == []

    # (c) A main whose graph has the dispatch Agent but not as an entrance starts nothing.
    tree3 = _vr24_tree(seen_name, ("kid",))
    v3 = make_commit(tree3, parent=v2.id, message=FILES_AT)
    seed(store, (v3, tree3))
    store.read_main.return_value = v3.id
    seen.clear()
    runs.clear()
    result = await runtime.execute(event(), "1700000000000-4")
    _unknown(result, DISPATCH)
    assert _finished(sessions) == result
    graph = repo.main_version.graph
    assert repo.main.id == v3.id and DISPATCH in graph.agents and DISPATCH not in graph.entrances
    assert _in_order(_steps(seen), [("refreshed", v3.id), ("new run",), ("version", DISPATCH)])
    assert len(runs) == 1 and _runs(seen) == []
    assert "1700000000000-4" not in queue.acked
    assert namespace_entries(v1) == [] and namespace_entries(v2) == []


async def test_vr25_every_run_exit_releases_its_run_exactly_once(repos, tmp_path, monkeypatch):
    """VR25: every way a run ends releases its version use once; a witness run keeps the old main loaded until it is released."""
    files = minimal_tree(entries=(DISPATCH, "kid"), compose=compose_source([f"agent.{DISPATCH}.Entry", "agent.kid.Entry"], [DISPATCH]))
    v1, store = _stored(files)
    repo = await _started(repos, tmp_path, store)
    v1_view = _view_file(repo.main_version)
    witness = repo.run()
    assert witness.version(DISPATCH).id == v1.id
    queue, sessions = FakeQueue(), AsyncMock()
    runtime = make_runtime(repo=repo, queue=queue, store=sessions)

    for number, status in enumerate(("ok", "error", "max_steps"), start=1):
        result = await runtime.execute(event(status=status), f"1700000000000-{number}")
        assert result.status == status

    def boom() -> None:
        raise RuntimeError("boom")

    result = await runtime.execute(event(hook=boom), "1700000000000-4")
    assert result.status == "error" and result.errors[0].stage == "execution"
    sessions.begin_run.side_effect = OSError("database down")
    result = await runtime.execute(event(), "1700000000000-5")
    assert result.status == "error" and result.errors[0].stage == "session"
    sessions.begin_run.side_effect = None
    # kid is an Agent of the version but not an entrance.
    monkeypatch.setattr(config, "DISPATCH_AGENT", "kid")
    _unknown(await runtime.execute(event(), "1700000000000-6"), "kid")
    monkeypatch.setattr(config, "DISPATCH_AGENT", DISPATCH)

    hook, reached, _ = _holding()
    cancelled = in_task(runtime.execute, event(hook=hook), "1700000000000-7")
    await asyncio.wait_for(reached.wait(), WAIT)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert "1700000000000-7" not in queue.acked

    async def stuck() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(config, "RUN_TIMEOUT", 0.05)
    result = await runtime.execute(event(hook=stuck), "1700000000000-8")
    assert result.status == "cancelled" and result.errors[0].stage == "deadline"

    tree2 = {**files, "tools/vl_dep.py": b"VERSION = 2\n"}
    v2 = await repo.fork(v1.id, tree2, "v2")
    seed(store, (v2, tree2))
    merged = await repo.merge(v2.id)
    assert merged.id == v2.id
    store.read_main.return_value = v2.id
    store.fetch.return_value = v2.id
    assert repo.main.id == v2.id
    # Only the witness still uses v1: no exit released more than its own use.
    assert namespace_entries(v1) and v1_view.exists()
    repo.release(witness)
    # Every exit released: nothing else keeps v1.
    assert namespace_entries(v1) == [] and not v1_view.exists()
    repo.release(witness)
    assert namespace_entries(v2) and repo.main.id == v2.id


async def test_vr26_version_pins_and_request_id_stay_inside_one_run(repos, tmp_path):
    """VR26: a run keeps the version each name first resolved to, and neither its pins nor its REQUEST_ID reach the next run."""
    tree1 = minimal_tree(entries=(), extra={"agent/probe.py": PROBE.format(dispatch=DISPATCH)}, compose=compose_source(["agent.probe.Probe", "agent.probe.Kid"], [DISPATCH]))
    v1, store = _stored(tree1)
    repo = await _started(repos, tmp_path, store)
    tree2 = {**tree1, "tools/vl_dep.py": b"VERSION = 2\n"}
    v2 = await repo.fork(v1.id, tree2, "v2")
    seed(store, (v2, tree2))
    queue = FakeQueue()
    runtime = make_runtime(repo=repo, queue=queue)
    record: dict[str, Any] = {}
    kids: list[tuple[str, Any, str | None]] = []
    merged, second_done = asyncio.Event(), asyncio.Event()

    async def kid_hook(kid: Any, call: Any) -> None:
        kids.append((call.request, kid, REQUEST_ID.get()))
        if call.request == "k1":
            record["merge"] = await repo.merge(v2.id)
            store.read_main.return_value = v2.id
            merged.set()

    async def second(probe: Any, call: Any) -> AgentResult:
        outcome = await probe.call_agent(call, "kid", "k3")
        record["second"] = (probe, outcome.response, REQUEST_ID.get())
        second_done.set()
        return AgentResult(status="ok")

    async def first(probe: Any, call: Any) -> AgentResult:
        record["first"] = (probe, REQUEST_ID.get())

        async def after_merge() -> Any:
            await asyncio.wait_for(merged.wait(), WAIT)
            return await probe.call_agent(call, "kid", "k2")

        record["first:kids"] = await asyncio.gather(probe.call_agent(call, "kid", "k1"), after_merge())
        # The running message enqueues the next one, as automatic development does.
        queue.inbox.put_nowait(("1700000000000-1", event(script=second, kid_hook=kid_hook)))
        await asyncio.wait_for(second_done.wait(), WAIT)
        record["first:after"] = (call.ctx.agents.agent(DISPATCH), call.ctx.agents.agent("kid"), REQUEST_ID.get())
        return AgentResult(status="ok")

    token = REQUEST_ID.set("outer")
    try:
        await runtime.start()
        queue.inbox.put_nowait(("1700000000000-0", event(script=first, kid_hook=kid_hook)))
        await until(lambda: "first:after" in record)
        await until(lambda: not namespace_entries(v1))
        assert namespace_entries(v2) and repo.main.id == v2.id
        assert REQUEST_ID.get() == "outer"
    finally:
        REQUEST_ID.reset(token)
        await asyncio.wait_for(runtime.stop(), WAIT)

    merged_main = record["merge"]
    assert merged_main.id == v2.id
    old, new = f"{namespace_of(v1)}.", f"{namespace_of(v2)}."
    probe1, request1 = record["first"]
    assert type(probe1).__module__.startswith(old) and request1 == "1700000000000-0"
    assert [request for request, _, _ in kids] == ["k1", "k2", "k3"]
    (_, kid1, id1), (_, kid2, id2), (_, kid3, id3) = kids
    assert kid1 is kid2 and type(kid1).__module__.startswith(old) and id1 == id2 == "1700000000000-0"
    assert all(outcome.response.startswith(old) for outcome in record["first:kids"])
    after_probe, after_kid, after_id = record["first:after"]
    assert after_probe is probe1 and after_kid is kid1 and after_id == "1700000000000-0"
    probe2, kid_answer, request2 = record["second"]
    assert type(probe2).__module__.startswith(new) and kid_answer.startswith(new) and request2 == "1700000000000-1"
    assert type(kid3).__module__.startswith(new) and id3 == "1700000000000-1"


async def test_vr29_shutdown_waits_for_a_running_run_then_unloads_every_version(repos, tmp_path):
    """VR29: stop waits for an accepted run on the old main, then unloads every version without writing to the store."""
    tree1 = minimal_tree((DISPATCH,))
    v1, store = _stored(tree1)
    repo = await _started(repos, tmp_path, store)
    v1_view = _view_file(repo.main_version)
    queue, sessions = FakeQueue(), AsyncMock()
    runtime = make_runtime(repo=repo, queue=queue, store=sessions)
    hook, reached, go = _holding()
    await runtime.start()
    queue.inbox.put_nowait(("1700000000000-0", event(hook=hook)))
    await asyncio.wait_for(reached.wait(), WAIT)
    tree2 = {**tree1, "tools/vl_dep.py": b"VERSION = 2\n"}
    v2 = await repo.fork(v1.id, tree2, "v2")
    seed(store, (v2, tree2))
    merged = await repo.merge(v2.id)
    assert merged.id == v2.id
    store.read_main.return_value = v2.id
    store.fetch.return_value = v2.id
    v2_view = _view_file(repo.main_version)
    writes = (list(store.write.await_args_list), list(store.move_main.await_args_list))

    stopping = in_task(runtime.stop)
    await settle()
    assert not stopping.done()
    assert namespace_entries(v1) and namespace_entries(v2)
    go.set()
    await finish_task(stopping)
    _ran_on(_finished(sessions), v1)
    assert namespace_entries(v1) == [] and namespace_entries(v2) == []
    assert not v1_view.exists() and not v2_view.exists()
    assert (list(store.write.await_args_list), list(store.move_main.await_args_list)) == writes
    assert _calls(store, "move_main") == [(v1.id, v2.id)]


async def test_vr29_shutdown_waits_for_a_run_still_loading_the_new_main(repos, tmp_path):
    """VR29: stop waits for an accepted run whose refresh is still loading the new main, then unloads everything."""
    tree1 = minimal_tree((DISPATCH,))
    v1, store = _stored(tree1)
    repo = await _started(repos, tmp_path, store)
    loads_name, gate_name = f"vr29-loads-{uuid4().hex}", f"vr29-gate-{uuid4().hex}"
    loads, gate = shared(loads_name, list), shared(gate_name, threading.Event)
    tree2 = minimal_tree(
        (DISPATCH,),
        extra={
            "tools/vl_dep.py": "from trackeverything.tools import slow  # noqa: F401\nVERSION = 2\n",
            "tools/slow.py": SLOW.format(loads=loads_name, gate=gate_name),
        },
    )
    v2 = make_commit(tree2, parent=v1.id, message=FILES_AT)
    seed(store, (v2, tree2))
    store.read_main.return_value = v2.id
    queue, sessions = FakeQueue(), AsyncMock()
    runtime = make_runtime(repo=repo, queue=queue, store=sessions)
    await runtime.start()
    try:
        queue.inbox.put_nowait(("1700000000000-0", event()))
        await until(lambda: len(loads) == 1)
        stopping = in_task(runtime.stop)
        await settle()
        assert not stopping.done()
    finally:
        gate.set()
    await finish_task(stopping)
    _ran_on(_finished(sessions), v2, 2)
    assert loaded_state(repos.view_root) == EMPTY_STATE
    assert store.write.await_count == 0 and store.move_main.await_count == 0
