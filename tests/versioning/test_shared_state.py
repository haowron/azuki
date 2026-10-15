"""Shared state stays with its component across versions: VR41, VR42, VL54.

The store is mocked per scene (``version_store``). A version another step must read is seeded
into the mock first, and ``read_main`` and ``fetch`` follow each accepted merge.
"""

import asyncio
import inspect
import threading
import uuid
from unittest.mock import Mock

import pytest

from tests.async_helpers import settle
from tests.fakes import finish_task, in_task
from tests.versioning.helpers import api, loaded_state, make_commit, minimal_tree, namespace_entries, seed, version_store, with_marker

WAIT = 5


def _arguments(store, method):
    """The last awaited call of a store method, bound to ``VersionStore``'s parameter names."""
    call = getattr(store, method).await_args
    assert call is not None, f"{method} was not awaited"
    signature = inspect.signature(getattr(api().store.VersionStore, method))
    return signature.bind(None, *call.args, **call.kwargs).arguments


async def _open(repos, tmp_path, files, *, build=None):
    """A started Repo whose mirror main is ``files``."""
    seeded = make_commit(files)
    store = version_store(main=seeded, versions=[(seeded, files)])
    repo = await repos(store, build=build)
    assert (await repo.start()).id == seeded.id
    return repo, store


async def _merge(repo, store, commit, files):
    """Merge a child of main that is already in the mirror, and make GitHub's and the mirror's main follow it."""
    seed(store, (commit, files))
    ours = repo.main.id
    merged = await repo.merge(commit.id)
    assert merged.id == commit.id
    swap = _arguments(store, "move_main")
    assert (swap["expected"], swap["new"]) == (ours, commit.id)
    store.read_main.return_value = commit.id
    store.fetch.return_value = commit.id
    version = repo.main_version
    assert version.id == commit.id
    return version


async def _exclusive(first, second):
    """``second`` waits while another task holds ``first``, and enters once it is released."""
    held, release, entered = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def hold():
        async with first:
            held.set()
            await release.wait()

    async def wait():
        async with second:
            entered.set()

    holding = in_task(hold)
    await asyncio.wait_for(held.wait(), WAIT)
    waiting = in_task(wait)
    await settle()
    assert not entered.is_set(), "the second holder must wait for the first"
    release.set()
    await finish_task(holding)
    await finish_task(waiting)
    assert entered.is_set()


def _vr41_tree(prefix):
    """A version whose ``tools/vr41.py`` takes a task lock, a set and a keyed lock from shared state under ``prefix``."""
    return minimal_tree(
        extra={
            "tools/vr41.py": (
                "from trackeverything.core.shared_state import keyed_lock, shared, task_lock\n\n"
                f"PENDING = task_lock({prefix + '.pending'!r})\n"
                f"PUSHING = shared({prefix + '.pushing'!r}, set)\n\n\n"
                "def issue_lock(key):\n"
                f"    return keyed_lock({prefix + '.issue'!r}, key)\n"
            )
        }
    )


async def test_vr41_locks_and_shared_values_span_versions(repos, tmp_path):
    """VR41: a keyed lock and a task lock exclude across versions, and a shared value is one object in every version, also in a version merged after the first is unloaded."""
    prefix = f"tests.vr41.{uuid.uuid4().hex}"
    files = _vr41_tree(prefix)
    repo, store = await _open(repos, tmp_path, files)
    v1 = repo.main_version
    run = repo.run()
    assert run.version("main") is v1
    second = with_marker(files, "vr41-two")
    v2 = await _merge(repo, store, make_commit(second, parent=v1.id, message="vr41 two"), second)
    one, two = v1.module("trackeverything.tools.vr41"), v2.module("trackeverything.tools.vr41")
    assert one is not two

    key = f"issue-{uuid.uuid4().hex}"
    await _exclusive(one.issue_lock(key), two.issue_lock(key))
    await _exclusive(one.PENDING.hold(), two.PENDING.hold())
    assert one.PUSHING is two.PUSHING

    repo.release(run)
    assert namespace_entries(v1) == [] and namespace_entries(v2)
    third = with_marker(second, "vr41-three")
    v3 = await _merge(repo, store, make_commit(third, parent=v2.id, message="vr41 three"), third)
    assert namespace_entries(v2) == [] and namespace_entries(v3)
    later = v3.module("trackeverything.tools.vr41")
    assert later.PENDING is one.PENDING and later.PUSHING is one.PUSHING


def _vl49_tree(name, label):
    return minimal_tree(
        extra={
            "tools/vl49.py": (
                "import threading\n"
                "from trackeverything.core.shared_state import shared\n"
                f"LOCK = shared({name!r}, threading.Lock)\n\n\n"
                "def use():\n"
                "    with LOCK:\n"
                f"        return {label!r}\n"
            )
        }
    )


async def test_vr42_shared_state_survives_versions_and_version_owned_values_fail_fork(repos, tmp_path):
    """VR42: shared() state survives versions, and a version-owned value in shared state fails the fork."""
    from trackeverything.core import shared_state

    name = f"tests.vr42.{uuid.uuid4().hex}"
    repo, store = await _open(repos, tmp_path, _vl49_tree(name, "v1"))
    v1 = repo.main_version
    run = repo.run()
    assert run.version("main") is v1

    second = _vl49_tree(name, "v2")
    c2 = await repo.fork(v1.id, second, "vl49 v2")
    assert (c2.parent, c2.message) == (v1.id, "vl49 v2")
    written = _arguments(store, "write")
    assert dict(written["tree"]) == second and store.written[c2.id][0] == c2
    assert _arguments(store, "keep")["version_id"] == c2.id
    v2 = await _merge(repo, store, c2, second)

    old, new = v1.module("trackeverything.tools.vl49"), v2.module("trackeverything.tools.vl49")
    assert old is not new and old.LOCK is new.LOCK
    assert old.use() == "v1" and new.use() == "v2"
    factory = Mock(side_effect=threading.Lock)
    counted = f"{name}.counted"
    assert shared_state.shared(counted, factory) is shared_state.shared(counted, factory)
    factory.assert_called_once()

    repo.release(run)
    assert namespace_entries(v1) == [] and namespace_entries(v2)
    assert shared_state.shared(name, threading.Lock) is new.LOCK

    before = loaded_state(repos.view_root)
    kept, moves = store.keep.await_count, store.move_main.await_count
    bad = {
        **second,
        "tools/vl49bad.py": (
            "from trackeverything.core.shared_state import shared\n\n\n"
            "class Box:\n    pass\n\n\n"
            f"BOX = shared({name + '.bad'!r}, Box)\n"
        ).encode(),
    }
    with pytest.raises(TypeError, match="version-owned"):
        await repo.fork(v2.id, bad, "vl49 bad")
    assert (store.keep.await_count, store.move_main.await_count) == (kept, moves), "nothing is kept"
    assert repo.main.id == v2.id and repo.main_version is v2
    assert loaded_state(repos.view_root) == before
    assert shared_state.shared(f"{name}.bad", dict) == {}, "the rejected value was not kept"


async def test_vl54_task_lock_is_not_reentrant():
    """VL54: a TaskLock refuses a second hold by the task that holds it, stays held until that task leaves, and then serves another task."""
    from trackeverything.core.shared_state import TaskLock

    lock = TaskLock(f"tests.vl54.{uuid.uuid4().hex}")

    async def nested():
        async with lock.hold():
            with pytest.raises(RuntimeError, match="not reentrant"):
                async with asyncio.timeout(1), lock.hold():
                    pass
            assert lock.locked()

    await finish_task(in_task(nested))
    assert not lock.locked()
    entered = asyncio.Event()

    async def other():
        async with lock.hold():
            entered.set()

    await finish_task(in_task(other))
    assert entered.is_set()
