"""Startup and the mirror's main: VR19, VR21-VR23, VR45, VR54, VG18, VG21, VG22.

The version store is an ``AsyncMock`` with fixed answers (``version_store``). Writes never change
what it reads, so a scene that needs the mirror's main to move sets ``store.read_main.return_value``
or seeds versions itself, as ``sync`` or a merge would. Starting from GitHub when the mirror has no
main is checked against real git in ``test_git_repo.py`` (VG04, VG05). The working copy that a local
runtime makes at startup (VG21, VG22) is checked with a real git store.
"""

import asyncio
import importlib.util
import inspect
import logging
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

import trackeverything.config as config
from tests.async_helpers import WAIT, FakeQueue, make_runtime, until
from tests.fakes import ScriptedLLM, finish_task, in_task
from tests.versioning.gitrepo import git
from tests.versioning.helpers import (
    DISPATCH,
    EMPTY_STATE,
    ROOT,
    api,
    compose_source,
    event,
    loaded_state,
    make_commit,
    minimal_tree,
    namespace_entries,
    namespace_of,
    seed,
    version_store,
)
from trackeverything.core.abc.call import AgentCall
from trackeverything.core.abc.context import AgentContext
from trackeverything.core.shared_state import shared

FILES_AT = "files@2026-10-13T00:00:00Z"

TURBO = '''
from trackeverything.core.abc.agent import BaseAgent


class Turbo(BaseAgent):
    name = "turbo_agent"
    description = "asks for an LLM the Host does not have"
    llm = "turbo"
'''

#: Imported by the version's ``tools/vl_dep.py``. Counts its imports and blocks until the gate opens.
SLOW = '''
import threading

from trackeverything.core.shared_state import shared

shared({loads!r}, list).append(1)
if not shared({gate!r}, threading.Event).wait(5):
    raise TimeoutError("the gate stayed closed")
'''

SLOW_DEP = "from trackeverything.tools import slow  # noqa: F401\nVERSION = {version}\n"

def _calls(store: Any, method: str) -> list[tuple[Any, ...]]:
    """Arguments of each awaited call of a VersionStore method, in declaration order."""
    signature = inspect.signature(getattr(api().store.VersionStore, method))
    return [tuple(signature.bind(None, *call.args, **call.kwargs).arguments.values())[1:] for call in getattr(store, method).await_args_list]


def _ran_on(result: Any, version: Any, value: int = 1) -> None:
    """The ``Entry`` of ``version`` answered, seeing dependency VERSION ``value`` before and after its hook."""
    assert result.status == "ok", result
    assert result.response.startswith(f"{value}->{value}|{namespace_of(version)}."), result.response


def _view_file(version: Any) -> Any:
    """A file of a loaded version's file view; it must disappear when the version is unloaded."""
    path = version.view_path("agent/compose.py")
    assert path.is_file(), path
    return path


def _root_attribute(version: Any) -> bool:
    """Whether the ``_te_versions`` package still holds the version's namespace as an attribute."""
    root = sys.modules.get(ROOT)
    return root is not None and hasattr(root, namespace_of(version).rpartition(".")[2])


def _holding() -> tuple[Any, asyncio.Event, asyncio.Event]:
    """A hook that sets ``reached`` and then waits for ``go``."""
    reached, go = asyncio.Event(), asyncio.Event()

    async def hook() -> None:
        reached.set()
        await asyncio.wait_for(go.wait(), WAIT)

    return hook, reached, go


def _slow() -> tuple[str, list[int], threading.Event]:
    """``tools/slow.py`` with uuid-named shared counters, plus the import counter and the (closed) gate."""
    loads, gate = f"vr-loads-{uuid4().hex}", f"vr-gate-{uuid4().hex}"
    return SLOW.format(loads=loads, gate=gate), shared(loads, list), shared(gate, threading.Event)


def _slow_tree() -> tuple[dict[str, bytes], list[int], threading.Event]:
    """A tree whose load blocks in the worker thread until the gate opens (``tools.vl_dep.VERSION`` is 2), with ``_slow``'s counter and gate."""
    slow, loads, gate = _slow()
    return minimal_tree(extra={"tools/vl_dep.py": SLOW_DEP.format(version=2), "tools/slow.py": slow}), loads, gate


def _seed_slow(store: Any, parent: Any) -> tuple[Any, list[int], threading.Event]:
    """A version of ``_slow_tree()`` forked from ``parent``, stored by another process."""
    files, loads, gate = _slow_tree()
    version = make_commit(files, parent=parent.id, message=FILES_AT)
    seed(store, (version, files))
    return version, loads, gate


def _unbuildable() -> dict[str, bytes]:
    """A tree that loads but whose compose fails under the test HostServices (llm ``turbo``)."""
    return minimal_tree(extra={"agent/turbo.py": TURBO}, compose=compose_source(["agent.main.Entry", "agent.turbo.Turbo"], ["main"]))


async def _tick(event: asyncio.Event) -> None:
    event.set()


async def _loop_runs_while_closed(gate: threading.Event) -> None:
    """A separate task on the event loop completes while the gate is still closed."""
    ticked = asyncio.Event()
    other = asyncio.create_task(_tick(ticked))
    await asyncio.wait_for(ticked.wait(), WAIT)
    await other
    assert not gate.is_set()


async def _cancel_while_loading(repos: Any, operation: Any, loads: list[int]) -> dict[str, list[str]]:
    """Run the coroutine ``operation`` until its load blocks in the worker thread, cancel it and return the state before it.

    The cancellation propagates at once: the thread still holds the half-loaded version.
    """
    before = loaded_state(repos.view_root)
    task = asyncio.create_task(operation)
    await until(lambda: len(loads) == 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert loaded_state(repos.view_root) != before
    return before


async def _started(repos: Any, tmp_path: Any, store: Any, **options: Any) -> Any:
    """A Repo started from the mirror's main."""
    repo = await repos(store, **options)
    await repo.start()
    return repo


def _stored(files: dict[str, bytes]) -> tuple[Any, Any]:
    """A store whose main is a version of ``files`` written earlier."""
    main = make_commit(files, message=FILES_AT)
    return main, version_store(main=main, versions=[(main, files)])


async def _run_main(repo: Any, main: Any) -> None:
    """Resolve the entrance ``main`` through one run and execute it."""
    run = repo.run()
    try:
        agent = run.agent("main")
        _ran_on(await agent.run(AgentCall(AgentContext(data={}, agents=run), "go")), main)
    finally:
        repo.release(run)


async def test_vr19_startup_aborts_when_the_stored_main_does_not_build(repos):
    """VR19: a mirror main the current Host cannot compose aborts startup; nothing is fetched, written or moved."""
    _, store = _stored(_unbuildable())
    repo = await repos(store)
    with pytest.raises(ValueError, match="turbo"):
        await repo.start()
    assert store.write.await_count == 0 and store.move_main.await_count == 0 and store.fetch.await_count == 0
    assert loaded_state(repos.view_root) == EMPTY_STATE


async def test_vr19_startup_aborts_when_the_stored_main_has_no_commit(repos):
    """VR19: a mirror main that names no stored version aborts startup with an error naming it."""
    missing = "0123456789ab" * 3 + "0123"
    store = version_store(main=missing)
    repo = await repos(store)
    # The case fixes the message, not the exception type.
    with pytest.raises(Exception, match=missing):
        await repo.start()
    assert store.write.await_count == 0 and store.move_main.await_count == 0
    assert loaded_state(repos.view_root) == EMPTY_STATE


async def test_vr21_each_run_start_follows_the_mirror_main(repos, tmp_path):
    """VR21: new runs use a main adopted into the mirror, running runs finish on their version, and an idle old main is unloaded at the switch."""
    v1, store = _stored(minimal_tree((DISPATCH,)))
    repo = await _started(repos, tmp_path, store)
    v1_view = _view_file(repo.main_version)
    for method in (store.write, store.move_main, store.read_tree):
        method.reset_mock()
    runtime = make_runtime(repo=repo)

    # m1 blocks on v1.
    hook1, reached1, go1 = _holding()
    m1 = in_task(runtime.execute, event(hook=hook1), "1700000000000-1")
    await asyncio.wait_for(reached1.wait(), WAIT)
    _ran_on(await runtime.execute(event(), "1700000000000-2"), v1)

    # The mirror's main moves to v2 (adopted by sync or a merge).
    tree2 = minimal_tree((DISPATCH,), extra={"tools/vl_dep.py": "VERSION = 2\n"})
    v2 = make_commit(tree2, parent=v1.id, message=FILES_AT)
    seed(store, (v2, tree2))
    store.read_main.return_value = v2.id
    _ran_on(await runtime.execute(event(), "1700000000000-3"), v2, 2)
    assert repo.main.id == v2.id
    assert namespace_entries(v1), "m1 still runs on v1"
    go1.set()
    _ran_on(await finish_task(m1), v1)
    assert namespace_entries(v1) == [] and not v1_view.exists()

    # An unchanged mirror main loads nothing.
    v2_version = repo.main_version
    v2_dep = v2_version.module("trackeverything.tools.vl_dep")
    store.read_main.reset_mock()
    store.read_tree.reset_mock()
    _ran_on(await runtime.execute(event(), "1700000000000-4"), v2, 2)
    assert store.read_main.await_count >= 1 and store.read_tree.await_count == 0
    assert repo.main_version is v2_version and v2_version.module("trackeverything.tools.vl_dep") is v2_dep

    # m5 holds v2 while main goes back to the unloaded v1 and then forward to v2 again.
    hook5, reached5, go5 = _holding()
    m5 = in_task(runtime.execute, event(hook=hook5), "1700000000000-5")
    await asyncio.wait_for(reached5.wait(), WAIT)
    store.read_main.return_value = v1.id
    _ran_on(await runtime.execute(event(), "1700000000000-6"), v1)
    assert repo.main.id == v1.id
    seen: dict[str, Any] = {}

    def look() -> None:
        seen.update(modules=namespace_entries(v1), attribute=_root_attribute(v1))

    store.read_main.return_value = v2.id
    _ran_on(await runtime.execute(event(hook=look), "1700000000000-7"), v2, 2)
    assert seen == {"modules": [], "attribute": False}
    assert repo.main_version is v2_version and v2_version.module("trackeverything.tools.vl_dep") is v2_dep
    go5.set()
    _ran_on(await finish_task(m5), v2, 2)
    assert store.write.await_count == 0 and store.move_main.await_count == 0


async def test_vr22_runs_load_a_new_main_off_the_event_loop_once(repos, tmp_path):
    """VR22: runs that start together load a new database main once, in a worker thread, before any entrance runs."""
    v1, store = _stored(minimal_tree((DISPATCH,)))
    repo = await _started(repos, tmp_path, store)
    slow, loads, gate = _slow()
    tree2 = minimal_tree((DISPATCH,), extra={"tools/vl_dep.py": SLOW_DEP.format(version=2), "tools/slow.py": slow})
    v2 = make_commit(tree2, parent=v1.id, message=FILES_AT)
    seed(store, (v2, tree2))
    store.read_main.return_value = v2.id
    runtime = make_runtime(repo=repo, concurrency=4)
    entered: list[int] = []
    try:
        runs = [in_task(runtime.execute, event(hook=lambda: entered.append(1)), f"1700000000000-{number}") for number in range(3)]
        await until(lambda: len(loads) == 1)
        await _loop_runs_while_closed(gate)
        assert entered == [] and not any(run.done() for run in runs)
    finally:
        gate.set()
    for run in runs:
        _ran_on(await finish_task(run), v2, 2)
    assert len(loads) == 1 and len(entered) == 3 and repo.main.id == v2.id


async def test_vr22_fork_loads_off_the_event_loop(repos, tmp_path):
    """VR22: fork loads a new tree in a worker thread, once."""
    v1, store = _stored(minimal_tree())
    repo = await _started(repos, tmp_path, store)
    slow, loads, gate = _slow()
    candidate = minimal_tree(extra={"tools/vl_dep.py": SLOW_DEP.format(version=2), "tools/slow.py": slow})
    try:
        forking = in_task(repo.fork, v1.id, candidate, "slow candidate")
        await until(lambda: len(loads) == 1)
        await _loop_runs_while_closed(gate)
        assert not forking.done()
    finally:
        gate.set()
    fork = await finish_task(forking)
    assert fork.parent == v1.id
    [(files, parent, message, _)] = _calls(store, "write")
    assert dict(files) == candidate and (parent, message) == (v1.id, "slow candidate")
    assert _calls(store, "keep") == [(fork.id,)]
    assert len(loads) == 1


async def test_vr22_merge_loads_the_candidate_off_the_event_loop(repos, tmp_path):
    """VR22: merge loads a candidate forked from GitHub's main in a worker thread, once, and then pushes it and moves main to it."""
    v1, store = _stored(minimal_tree())
    repo = await _started(repos, tmp_path, store)
    slow, loads, gate = _slow()
    candidate = minimal_tree(extra={"tools/vl_dep.py": SLOW_DEP.format(version=2), "tools/slow.py": slow})
    # Another process forked the candidate, so this process has not loaded it.
    k = make_commit(candidate, parent=v1.id, message="candidate")
    seed(store, (k, candidate))
    try:
        merging = in_task(repo.merge, k.id)
        await until(lambda: len(loads) == 1)
        await _loop_runs_while_closed(gate)
        assert not merging.done() and store.push.await_count == 0 and store.move_main.await_count == 0
    finally:
        gate.set()
    merged = await finish_task(merging)
    assert merged == k
    assert _calls(store, "push") == [(k.id,)]
    assert _calls(store, "move_main") == [(v1.id, k.id)] and store.write.await_count == 0
    assert len(loads) == 1 and repo.main.id == k.id


async def test_vr54_a_cancelled_refresh_releases_the_new_main_when_its_thread_ends(repos, tmp_path):
    """VR54: a refresh cancelled while it loads v2 keeps v1; v2 is released when the thread ends and the next refresh loads it again."""
    v1, store = _stored(minimal_tree())
    repo = await _started(repos, tmp_path, store)
    v2, loads, gate = _seed_slow(store, v1)
    store.read_main.return_value = v2.id
    before = await _cancel_while_loading(repos, repo.refresh(), loads)
    gate.set()
    await until(lambda: loaded_state(repos.view_root) == before)
    assert repo.main.id == v1.id
    assert (await repo.refresh()).id == v2.id and len(loads) == 2


async def test_vr54_a_refresh_waits_for_the_cancelled_load_of_the_same_version(repos, tmp_path):
    """VR54: a refresh started while a cancelled load of v2 still runs waits for its release, then loads v2 afresh and intact."""
    v1, store = _stored(minimal_tree())
    repo = await _started(repos, tmp_path, store)
    v2, loads, gate = _seed_slow(store, v1)
    store.read_main.return_value = v2.id
    await _cancel_while_loading(repos, repo.refresh(), loads)
    try:
        refreshing = in_task(repo.refresh)
        await _loop_runs_while_closed(gate)
        assert not refreshing.done()
    finally:
        gate.set()
    assert (await finish_task(refreshing)).id == v2.id and len(loads) == 2
    assert repo.main_version.module("trackeverything.tools.vl_dep").VERSION == 2
    _view_file(repo.main_version)


async def test_vr54_a_cancelled_fork_saves_nothing_and_releases_its_load(repos, tmp_path):
    """VR54: a fork cancelled while it loads keeps nothing (no fork ref); its load is released when the thread ends and the same fork then succeeds."""
    v1, store = _stored(minimal_tree())
    repo = await _started(repos, tmp_path, store)
    files, loads, gate = _slow_tree()
    before = await _cancel_while_loading(repos, repo.fork(v1.id, files, "slow candidate"), loads)
    gate.set()
    await until(lambda: loaded_state(repos.view_root) == before)
    assert store.keep.await_count == 0
    fork = await repo.fork(v1.id, files, "slow candidate")
    assert _calls(store, "keep") == [(fork.id,)] and len(loads) == 2


async def test_vr54_a_cancelled_merge_keeps_main_and_the_next_merge_loads_the_candidate_again(repos, tmp_path):
    """VR54: a merge cancelled while it loads a candidate forked by another process keeps main; the next merge loads the candidate again."""
    v1, store = _stored(minimal_tree())
    repo = await _started(repos, tmp_path, store)
    k, loads, gate = _seed_slow(store, v1)
    before = await _cancel_while_loading(repos, repo.merge(k.id), loads)
    gate.set()
    await until(lambda: loaded_state(repos.view_root) == before)
    assert store.push.await_count == 0 and store.move_main.await_count == 0 and repo.main.id == v1.id
    assert await repo.merge(k.id) == k
    assert _calls(store, "push") == [(k.id,)]
    assert _calls(store, "move_main") == [(v1.id, k.id)] and len(loads) == 2


async def test_vr54_close_waits_for_a_cancelled_load(repos, tmp_path):
    """VR54: close after a refresh was cancelled mid-load returns only when the thread ends, with nothing left loaded."""
    v1, store = _stored(minimal_tree())
    repo = await _started(repos, tmp_path, store)
    v2, loads, gate = _seed_slow(store, v1)
    store.read_main.return_value = v2.id
    await _cancel_while_loading(repos, repo.refresh(), loads)
    try:
        closing = in_task(repo.close)
        await _loop_runs_while_closed(gate)
        assert not closing.done()
    finally:
        gate.set()
    await finish_task(closing)
    assert loaded_state(repos.view_root) == EMPTY_STATE


async def test_vr23_a_database_main_that_fails_to_load_keeps_the_loaded_main(repos, tmp_path, caplog):
    """VR23: a mirror main that fails to load is logged once, leaves nothing loaded, and runs stay on the loaded main."""
    v1, store = _stored(minimal_tree((DISPATCH,)))
    repo = await _started(repos, tmp_path, store)
    runtime = make_runtime(repo=repo)
    v1_view = _view_file(repo.main_version)
    before = loaded_state(repos.view_root)
    broken = minimal_tree((DISPATCH,), extra={"tools/vl_dep.py": "from trackeverything.tools import b  # noqa: F401\nVERSION = 1\n", "tools/b.py": "VALUE = 0\nRESULT = 1 / 0\n"})
    b1 = make_commit(broken, parent=v1.id, message=FILES_AT)
    seed(store, (b1, broken))
    store.read_main.return_value = b1.id
    caplog.set_level(logging.ERROR)
    for number in (1, 2):
        _ran_on(await runtime.execute(event(), f"1700000000000-{number}"), v1)
        assert repo.main.id == v1.id
        assert namespace_entries(b1) == [] and loaded_state(repos.view_root) == before
    # The exception is read from the record, never formatted here: formatting would cache the view's lines again.
    reports = [record for record in caplog.records if record.levelno >= logging.ERROR and b1.id[:8] in record.getMessage()]
    assert len(reports) == 1
    error = reports[0].exc_info[1] if reports[0].exc_info else None
    assert isinstance(error, ZeroDivisionError) or "ZeroDivisionError" in reports[0].getMessage()
    assert store.write.await_count == 0 and store.move_main.await_count == 0

    tree3 = minimal_tree((DISPATCH,), extra={"tools/vl_dep.py": "VERSION = 3\n"})
    v3 = make_commit(tree3, parent=v1.id, message=FILES_AT)
    seed(store, (v3, tree3))
    store.read_main.return_value = v3.id
    _ran_on(await runtime.execute(event(), "1700000000000-3"), v3, 3)
    assert repo.main.id == v3.id and namespace_entries(v1) == [] and not v1_view.exists()



async def test_vr23_a_failed_read_of_the_new_main_is_retried_on_the_next_run(repos, tmp_path, caplog):
    """VR23: a store read that fails while loading the mirror's main is only a warning; the next run loads that main."""
    v1, store = _stored(minimal_tree((DISPATCH,)))
    repo = await _started(repos, tmp_path, store)
    runtime = make_runtime(repo=repo)
    tree2 = minimal_tree((DISPATCH,), extra={"tools/vl_dep.py": "VERSION = 2\n"})
    v2 = make_commit(tree2, parent=v1.id, message=FILES_AT)
    seed(store, (v2, tree2))
    store.read_main.return_value = v2.id
    reads = store.read_tree.side_effect

    async def fail_once(version_id):
        store.read_tree.side_effect = reads
        raise ConnectionError("mirror busy")

    store.read_tree.side_effect = fail_once
    caplog.set_level(logging.WARNING)
    _ran_on(await runtime.execute(event(), "1700000000000-1"), v1)
    assert repo.main.id == v1.id and namespace_entries(v2) == []
    assert [record.levelno for record in caplog.records if v2.id in record.getMessage()] == [logging.WARNING]
    _ran_on(await runtime.execute(event(), "1700000000000-2"), v2, 2)
    assert repo.main.id == v2.id
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]

def _host(monkeypatch: Any, tmp_path: Any, store: Any) -> SimpleNamespace:
    """Replace the Host factories of ``core.builtin.app`` and the git store factory; return the mocks."""
    from trackeverything.core.builtin import app

    modules = api()
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://vr45@localhost/vr45")
    monkeypatch.setattr(config, "COMPONENT_REPO_DIR", str(tmp_path / "components.git"))
    monkeypatch.setattr(config, "COMPONENT_REPO_URL", str(tmp_path / "github.git"))
    monkeypatch.setattr(app, "open_execution_logger", Mock(return_value=AsyncMock()))
    monkeypatch.setattr(app, "build_llms", Mock(return_value={name: ScriptedLLM() for name in ("max", "high", "low")}))
    monkeypatch.setattr(app, "open_session_store", Mock(return_value=Mock()))
    producer = Mock()
    producer.publish = AsyncMock()
    producer.close = AsyncMock()
    producer.stream.heartbeat = AsyncMock()
    producer.stream.save_catalog = AsyncMock()
    monkeypatch.setattr(app.Producer, "from_env", Mock(return_value=producer))
    # The store factory is replaced wherever the Host may construct it.
    factory = Mock(return_value=store)
    monkeypatch.setattr(modules.store, "GitVersionStore", factory)
    for module in (app, modules.repo):
        monkeypatch.setattr(module, "GitVersionStore", factory, raising=False)
    return SimpleNamespace(app=app, producer=producer, factory=factory)


def _versions_loaded() -> list[str]:
    return [name for name in list(sys.modules) if name.startswith(f"{ROOT}.")]


#: Names the Host used to bind for its direct calls into component modules.
CANONICAL_CALLS = ("workspace_pool", "pr_retire_sweep", "migration", "project_store", "issue_workflow", "sentry_chain")


async def test_vr45_build_runtime_starts_from_the_mirror_main_and_ticks_only_host_work(monkeypatch, tmp_path):
    """VR45: build_runtime loads the mirror's main without fetching, and its tick only publishes the catalog and the heartbeat.

    The Host binds no component module. Its repository is the process singleton (``current_repo()``),
    released when the repository closes.
    """
    v1, store = _stored(minimal_tree())
    host = _host(monkeypatch, tmp_path, store)
    assert not any(hasattr(host.app, name) for name in CANONICAL_CALLS)
    runtime = await host.app.build_runtime()
    try:
        assert isinstance(runtime, host.app.TrackingRuntime)
        assert api().repo.current_repo() is runtime.repo
        assert runtime.repo.main.id == v1.id
        arguments = host.factory.call_args
        given = [*arguments.args, *arguments.kwargs.values()]
        assert config.COMPONENT_REPO_DIR in given and config.COMPONENT_REPO_URL in given
        assert store.write.await_count == 0 and store.move_main.await_count == 0 and store.fetch.await_count == 0

        await runtime._tick_hook()
        host.producer.publish.assert_not_awaited()
        host.producer.stream.save_catalog.assert_awaited_once()
        catalog = host.producer.stream.save_catalog.await_args.args[0]
        assert catalog["entrances"] == ["main"] and catalog["code_version"] == f"c.{v1.id[:8]}"
        host.producer.stream.heartbeat.assert_awaited_once_with()
        assert store.fetch.await_count == 0
        assert importlib.util.find_spec("trackeverything.core.versioning.watcher") is None
    finally:
        await runtime.repo.close()
    assert _versions_loaded() == []
    with pytest.raises(RuntimeError):
        api().repo.current_repo()


async def test_vr45_build_runtime_aborts_on_a_mirror_main_that_does_not_build(monkeypatch, tmp_path):
    """VR45: build_runtime raises when the mirror's main does not compose, and no runtime is returned."""
    _, store = _stored(_unbuildable())
    host = _host(monkeypatch, tmp_path, store)
    with pytest.raises(ValueError, match="turbo"):
        await host.app.build_runtime()
    assert store.write.await_count == 0 and store.move_main.await_count == 0
    assert _versions_loaded() == []
    with pytest.raises(RuntimeError):
        api().repo.current_repo()


async def test_vg18_runtime_syncs_the_component_repository_every_interval(monkeypatch, tmp_path):
    """VG18: the runtime calls ``repo.sync()`` every ``COMPONENT_FETCH_INTERVAL``, keeps going after a failed sync, and stops syncing before it closes the repository."""
    _, store = _stored(minimal_tree())
    host = _host(monkeypatch, tmp_path, store)
    monkeypatch.setattr(config, "COMPONENT_FETCH_INTERVAL", 0.01)
    runtime = await host.app.build_runtime()
    runtime.queue = FakeQueue()
    repo = runtime.repo
    events: list[str] = []
    close = repo.close

    async def sync() -> Any:
        events.append("sync")
        if events.count("sync") == 1:
            raise ConnectionError("github unreachable")
        return repo.main

    async def closing() -> None:
        events.append("close")
        await close()

    monkeypatch.setattr(repo, "sync", sync)
    monkeypatch.setattr(repo, "close", closing)
    await runtime.start()
    try:
        await until(lambda: events.count("sync") >= 3)
    finally:
        await runtime.stop()
    synced = events.count("sync")
    await asyncio.sleep(0.05)
    assert events.count("sync") == synced
    assert "close" in events and "sync" not in events[events.index("close"):]
    assert _versions_loaded() == []


def _local_store(tmp_path: Any, url: str = "") -> Any:
    """A real ``GitVersionStore`` over ``tmp_path/components.git`` whose first local main is ``minimal_tree()``."""
    root = tmp_path / "package"
    for relative, content in minimal_tree().items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return api().store.GitVersionStore(tmp_path / "components.git", url, seed=root)


def _local_host(monkeypatch: Any, tmp_path: Any, store: Any, copy: Any, url: str = "") -> SimpleNamespace:
    host = _host(monkeypatch, tmp_path, store)
    monkeypatch.setattr(config, "COMPONENT_REPO_URL", url)
    monkeypatch.setattr(config, "COMPONENTS_DIR", str(copy))
    return host


async def test_vg21_a_local_runtime_makes_the_working_copy_once(monkeypatch, tmp_path, isolated_git):
    """VG21 (a)(b): a local runtime clones its repository into ``COMPONENTS_DIR`` (branch main, origin the repository) when it is missing;
    a working copy that exists keeps its edits and its remote."""
    copy = tmp_path / "components"
    host = _local_host(monkeypatch, tmp_path, _local_store(tmp_path), copy)
    runtime = await host.app.build_runtime()
    try:
        main = runtime.repo.main.id
        assert git(copy, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
        assert git(copy, "log", "--format=%H").stdout.split() == [main]
        assert Path(git(copy, "remote", "get-url", "origin").stdout.strip()).resolve() == (tmp_path / "components.git").resolve()
        assert (copy / "tools" / "vl_dep.py").read_text(encoding="utf-8") == "VERSION = 1\n"
    finally:
        await runtime.repo.close()

    (copy / "tools" / "vl_dep.py").write_text("VERSION = 9\n", encoding="utf-8")
    elsewhere = str(tmp_path / "elsewhere.git")
    git(copy, "remote", "set-url", "origin", elsewhere)
    host.factory.return_value = _local_store(tmp_path)
    runtime = await host.app.build_runtime()
    try:
        assert runtime.repo.main.id == main
        assert (copy / "tools" / "vl_dep.py").read_text(encoding="utf-8") == "VERSION = 9\n"
        assert git(copy, "remote", "get-url", "origin").stdout.strip() == elsewhere
        assert git(copy, "log", "--format=%H").stdout.split() == [main]
    finally:
        await runtime.repo.close()
    assert _versions_loaded() == []


async def test_vg21_a_runtime_with_a_repository_url_makes_no_working_copy(monkeypatch, tmp_path, github):
    """VG21 (c): with a repository URL the runtime starts from GitHub's main and makes no working copy."""
    remote = github.commit(minimal_tree(), "first")
    copy = tmp_path / "components"
    host = _local_host(monkeypatch, tmp_path, _local_store(tmp_path, github.url), copy, github.url)
    runtime = await host.app.build_runtime()
    try:
        assert runtime.repo.main.id == remote
        assert not copy.exists()
    finally:
        await runtime.repo.close()


async def test_vg22_a_working_copy_that_cannot_be_made_only_warns(monkeypatch, tmp_path, isolated_git, caplog):
    """VG22: when the working copy cannot be made (its parent is a regular file), the runtime logs a warning naming the path and starts on its main."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n", encoding="utf-8")
    copy = blocker / "components"
    host = _local_host(monkeypatch, tmp_path, _local_store(tmp_path), copy)
    caplog.set_level(logging.WARNING)
    runtime = await host.app.build_runtime()
    try:
        assert runtime.repo.main.parent is None and runtime.repo.main.message == "Initial components"
        assert [record for record in caplog.records if record.levelno == logging.WARNING and str(copy) in record.getMessage()]
        assert not copy.exists()
    finally:
        await runtime.repo.close()
    assert _versions_loaded() == []
