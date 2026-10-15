"""VR14・VR15・VR18: run ごとの版の固定(RunVersions)と、merge による main の切替え。"""

import asyncio
import inspect
import sys
from contextlib import suppress

import pytest

from tests.async_helpers import WAIT
from tests.versioning.helpers import (
    ROOT,
    api,
    compose_source,
    entry_source,
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


async def _open(repos, tmp_path, files):
    """main が files の版である store で Repo を開始し、(repo, store, main の Commit) を返す。"""
    main = make_commit(files)
    store = version_store(main=main, versions=[(main, files)])
    repo = await repos(store)
    assert (await repo.start()).id == main.id
    return repo, store, main


async def _fork(repo, store, parent, files):
    """files を parent(mirror の main)から fork し、書かれた版を store の読取りにも反映する。"""
    commit = await repo.fork(parent.id, files, "edit")
    seed(store, (commit, files))
    return commit


async def _merge(repo, store, commit):
    """merge で main が候補へ進んだことを確かめ、GitHub と mirror の main を結果へ進める(push と move_main の成功後に返す値)。"""
    merged = await repo.merge(commit.id)
    assert merged.id == commit.id, merged
    store.read_main.return_value = merged.id
    store.fetch.return_value = merged.id
    return merged


def _last_move(store):
    """最後に await された move_main の ``(expected, new)``(位置・キーワードのどちらで渡しても同じ)。"""
    call = store.move_main.await_args
    assert call is not None, "move_main was not awaited"
    signature = inspect.signature(api().store.VersionStore.move_main)
    return tuple(signature.bind(None, *call.args, **call.kwargs).arguments.values())[1:]


def _ctx(run, **data):
    return AgentContext(data=dict(data), agents=run)


async def _answer(run, **data):
    """run が引いた入口 main を実行し、ok の応答を返す。"""
    result = await run.agent("main").run(AgentCall(ctx=_ctx(run, **data)))
    assert result.status == "ok", result
    return result.response


def _reply(version, value):
    """``entry_source`` の入口 main が版 version で依存の値 value を見たときの応答。"""
    return f"{value}->{value}|{namespace_of(version)}.trackeverything.agent.main"


def _on_root(version):
    """``_te_versions`` の root module に版の属性が残っているか。"""
    return hasattr(sys.modules[ROOT], namespace_of(version).rpartition(".")[2])


def _family(marker):
    """入口 main と、入口でない kid(``MARKER = marker``)を持つ tree。"""
    kid = f"""
from trackeverything.core.abc.agent import BaseAgent

MARKER = {marker}


class Kid(BaseAgent):
    name = "kid"
    description = "kid {marker}"
"""
    return minimal_tree(extra={"agent/kid.py": kid}, compose=compose_source(["agent.main.Entry", "agent.kid.Kid"], ["main"]))


def _counter(value):
    """``tools/a.py`` の VERSION が value で、入口 main がそれを報告する tree。"""
    return minimal_tree(extra={"tools/a.py": f"VERSION = {value}\n", "agent/main.py": entry_source("main", "a")})


async def test_vr14_a_run_pins_each_name_to_the_main_it_first_saw(repos, tmp_path):
    """VR14: 取得口は同期で読込みをせず、run の中では名前ごとに最初に決まった版を run の終わりまで使う。"""
    modules = api()
    plain = (modules.repo.Repo.run, modules.repo.Repo.release, modules.repo.RunVersions.version, modules.repo.RunVersions.agent)
    assert not any(inspect.iscoroutinefunction(function) for function in plain)
    repo, store, v1 = await _open(repos, tmp_path, _family(1))

    loaded, calls = set(sys.modules), list(store.mock_calls)
    run1 = repo.run()
    e1 = run1.agent("main")
    assert run1.agent("main") is e1 and run1.agent("nope") is None
    assert set(sys.modules) == loaded and store.mock_calls == calls
    assert type(e1).__module__ == f"{namespace_of(v1)}.trackeverything.agent.main"
    first = run1.version("main")
    assert first.id == v1.id and first.users == 1

    v2 = await _fork(repo, store, v1, _family(2))
    assert (await _merge(repo, store, v2)).id == v2.id == repo.main.id
    assert run1.agent("main") is e1 and run1.version("main") is first
    kid = run1.agent("kid")
    assert run1.agent("kid") is kid and run1.version("kid").id == v2.id
    assert type(kid).__module__ == f"{namespace_of(v2)}.trackeverything.agent.kid" and sys.modules[type(kid).__module__].MARKER == 2

    run2 = repo.run()
    e2 = run2.agent("main")
    assert e2 is not e1 and type(e2).__module__ == f"{namespace_of(v2)}.trackeverything.agent.main"
    assert run2.agent("kid") is kid and repo.main_version.users == 2

    repo.release(run1)
    assert namespace_entries(v1) == [] and not _on_root(v1) and repo.main_version.users == 1
    repo.release(run1)
    assert repo.main_version.users == 1
    with pytest.raises(RuntimeError):
        run1.agent("main")
    with pytest.raises(RuntimeError):
        run1.version("kid")
    assert await _answer(run2) == _reply(v2, 1)
    repo.release(run2)
    assert namespace_entries(v2) and repo.main.id == v2.id and repo.main_version.users == 0


async def test_vr15_merge_switches_main_without_waiting_and_unloads_an_unused_old_main(repos, tmp_path):
    """VR15: merge は実行中の run を待たずに main を切り替え、利用者のない旧 main を解放し、表の行は残す。"""
    repo, store, v1 = await _open(repos, tmp_path, _counter(1))
    first = repo.run()
    assert await _answer(first) == _reply(v1, 1)
    v1_view = first.version("main").view_path("tools/a.py")
    assert v1_view.is_file()
    repo.release(first)
    assert namespace_entries(v1)

    c = await _fork(repo, store, v1, _counter(2))
    assert store.keep.await_count == 1
    assert (await _merge(repo, store, c)).id == c.id == repo.main.id
    assert store.keep.await_count == 1 and store.write.await_count == 1
    assert _last_move(store) == (v1.id, c.id)
    assert namespace_entries(v1) == [] and not _on_root(v1) and not v1_view.exists()
    assert await repo.tree(v1.id) == _counter(1)
    log = {commit.id: commit for commit in await repo.log()}
    assert {v1.id, c.id} <= set(log) and log[c.id].parent == v1.id

    run1 = repo.run()
    old = run1.agent("main")
    assert run1.version("main").id == c.id
    c_view = run1.version("main").view_path("tools/a.py")
    entered, gate = asyncio.Event(), asyncio.Event()

    async def hold():
        entered.set()
        await gate.wait()

    running = asyncio.create_task(old.run(AgentCall(ctx=_ctx(run1, hook=hold))))
    await asyncio.wait_for(entered.wait(), WAIT)
    d = await _fork(repo, store, c, _counter(3))
    assert (await asyncio.wait_for(_merge(repo, store, d), WAIT)).id == d.id == repo.main.id
    assert _last_move(store) == (c.id, d.id)
    assert not running.done()

    run2 = repo.run()
    assert type(run2.agent("main")).__module__ == f"{namespace_of(d)}.trackeverything.agent.main"
    assert await _answer(run2) == _reply(d, 3)
    assert run1.agent("main") is old and namespace_entries(c) and c_view.is_file()
    gate.set()
    result = await asyncio.wait_for(running, WAIT)
    assert (result.status, result.response) == ("ok", _reply(c, 2))

    repo.release(run1)
    assert namespace_entries(c) == [] and not _on_root(c) and not c_view.exists()
    assert namespace_entries(d) and await repo.tree(c.id) == _counter(2)
    repo.release(run2)
    assert namespace_entries(d) and repo.main.id == d.id


class _Pauses:
    """store の各 await を止め、テストが ``go`` を設定するまで待たせる。"""

    def __init__(self, store):
        self.waiting: asyncio.Queue[tuple[str, asyncio.Event]] = asyncio.Queue()
        for name in ("read_main", "move_main", "read_commit", "read_tree", "list_commits", "write", "keep", "fetch", "is_ancestor", "push"):
            method = getattr(store, name)
            method.side_effect = self._pause(name, method, method.side_effect)

    def _pause(self, name, method, effect):
        async def paused(*args, **kwargs):
            go = asyncio.Event()
            self.waiting.put_nowait((name, go))
            await go.wait()
            return await effect(*args, **kwargs) if effect is not None else method.return_value

        return paused


async def test_vr18_runs_resolved_between_merge_steps_never_use_an_unloaded_version(repos, tmp_path):
    """VR18: merge の await の合間に取得・解放しても、各 run はその時点の main を得て、解放済みの版を使わない。"""
    repo, store, v1 = await _open(repos, tmp_path, minimal_tree())
    run0 = repo.run()
    assert run0.version("main").id == v1.id
    v2 = await _fork(repo, store, v1, minimal_tree(extra={"tools/vl_dep.py": "VERSION = 2\n"}))
    pauses = _Pauses(store)
    merging = asyncio.create_task(_merge(repo, store, v2))
    picks = []  # (run, 入口の実例, 取得した時点の main の id)
    while True:
        waiting = asyncio.create_task(pauses.waiting.get())
        done, _ = await asyncio.wait({merging, waiting}, timeout=WAIT, return_when=asyncio.FIRST_COMPLETED)
        assert done, "merge が store の await で止まったまま進まない"
        if waiting not in done:
            waiting.cancel()
            with suppress(asyncio.CancelledError):
                await waiting
            break
        _, go = waiting.result()
        # 別の run の終了(release)は lock を取らずに解放を走らせる。move_main の待ちを含む各 await で起こす。
        repo.release(repo.run())
        run, current = repo.run(), repo.main.id
        assert run.version("main").id == current and namespace_entries(current)
        picks.append((run, run.agent("main"), current))
        if len(picks) == 1:
            repo.release(run0)
            assert namespace_entries(v1)
        go.set()
    assert (await merging).id == v2.id == repo.main.id
    assert picks and picks[0][2] == v1.id, "merge は push の前に main を切り替えない"
    assert bool(namespace_entries(v1)) == any(pinned == v1.id for _, _, pinned in picks)
    late = repo.run()
    assert late.version("main").id == v2.id
    picks.append((late, late.agent("main"), v2.id))

    for run, entry, pinned in picks:
        present: list[bool] = []

        def hook(pinned=pinned, present=present):
            present.append(namespace_of(pinned) in sys.modules)

        result = await entry.run(AgentCall(ctx=_ctx(run, hook=hook)))
        value = 1 if pinned == v1.id else 2
        assert (result.status, result.response, present) == ("ok", _reply(pinned, value), [True])

    for index, (run, _, _) in enumerate(picks):
        repo.release(run)
        assert bool(namespace_entries(v1)) == any(pinned == v1.id for _, _, pinned in picks[index + 1 :])
        assert namespace_entries(v2)
    assert set(loaded_state(repos.view_root)["modules"]) == set(namespace_entries(v2))
    assert repo.main.id == v2.id
