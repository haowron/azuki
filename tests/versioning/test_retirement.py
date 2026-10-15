"""VR16・VR17: 版の解放(使用中の版と main だけを残し、解放は版が持つ参照だけを外す)。"""

import gc
import importlib
import linecache
import sys
import weakref
from pathlib import Path

import pytest

from tests.versioning.helpers import (
    ROOT,
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

STORE_METHODS = {"read_main", "move_main", "read_commit", "read_tree", "list_commits", "write", "keep", "fetch", "is_ancestor", "merge_tree", "push"}


async def _open(repos, tmp_path, files):
    """main が files の版である store で Repo を開始し、(repo, store, main の Commit) を返す。"""
    main = make_commit(files)
    store = version_store(main=main, versions=[(main, files)])
    repo = await repos(store)
    assert (await repo.start()).id == main.id
    return repo, store, main


async def _advance(repo, store, files):
    """files を mirror の main から fork して merge し、main が進んだことを確かめて GitHub と mirror の main もその版へ進める。"""
    commit = await repo.fork(store.read_main.return_value, files, "edit")
    seed(store, (commit, files))
    merged = await repo.merge(commit.id)
    assert merged.id == commit.id == repo.main.id, merged
    store.read_main.return_value = merged.id
    store.fetch.return_value = merged.id
    return commit


async def _answer(run):
    """run が引いた入口 main を実行し、ok の応答を返す。"""
    result = await run.agent("main").run(AgentCall(ctx=AgentContext(data={}, agents=run)))
    assert result.status == "ok", result
    return result.response


def _reply(version, value):
    """``entry_source`` の入口 main が版 version で依存の値 value を見たときの応答。"""
    return f"{value}->{value}|{namespace_of(version)}.trackeverything.agent.main"


def _attribute(version):
    """``_te_versions`` の root module に載る版の属性名(``v_<id>``)。"""
    return namespace_of(version).rpartition(".")[2]


def _on_root():
    """``_te_versions`` の root module に載っている版の属性名。"""
    return {name for name in vars(sys.modules[ROOT]) if not name.startswith("__")}


def _under(path, directory):
    resolved = Path(path).resolve()
    return resolved == directory.resolve() or resolved.is_relative_to(directory.resolve())


def _view_dir(view_root, path):
    """file view 上のパスを含む、view_root 直下の版の view。"""
    return view_root.resolve() / Path(path).resolve().relative_to(view_root.resolve()).parts[0]


def _dependency(value, extra=None):
    """``tools/vl_dep.py`` の VERSION が value の tree。"""
    return minimal_tree(extra={"tools/vl_dep.py": f"VERSION = {value}\n", **(extra or {})})


def _primed_view(version):
    """版の ``tools/vl_dep.py`` を linecache に載せ、その file view 上のパス(linecache のキー)を返す。"""
    dependency = version.module("trackeverything.tools.vl_dep")
    assert linecache.getlines(dependency.__file__, dependency.__dict__) == [f"VERSION = {dependency.VERSION}\n"]
    assert dependency.__file__ in linecache.cache and Path(dependency.__file__) == version.view_path("tools/vl_dep.py")
    return dependency.__file__


async def test_vr16_only_main_and_versions_in_use_stay_loaded(repos, tmp_path):
    """VR16: 使用中の版と main だけが読込みに残り、途中の版は merge の時点で解放され、merge を繰り返すと main だけが残る。"""
    repo, store, v1 = await _open(repos, tmp_path, _dependency(1))
    views = {v1.id: _primed_view(repo.main_version)}
    run1 = repo.run()
    assert run1.version("main").id == v1.id
    v2 = await _advance(repo, store, _dependency(2))
    views[v2.id] = _primed_view(repo.main_version)
    run2 = repo.run()
    assert run2.version("main").id == v2.id
    v3 = await _advance(repo, store, _dependency(3))
    views[v3.id] = _primed_view(repo.main_version)
    v4 = await _advance(repo, store, _dependency(4))
    views[v4.id] = _primed_view(repo.main_version)

    assert namespace_entries(v3) == [] and not Path(views[v3.id]).exists() and views[v3.id] not in linecache.cache
    assert all(namespace_entries(version) for version in (v1, v2, v4))
    assert _on_root() == {_attribute(v1), _attribute(v2), _attribute(v4)}
    assert await _answer(run1) == _reply(v1, 1) and await _answer(run2) == _reply(v2, 2)
    run3 = repo.run()
    assert await _answer(run3) == _reply(v4, 4)
    for run in (run1, run2, run3):
        repo.release(run)
    assert _on_root() == {_attribute(v4)}

    for number in range(20):
        commit = await _advance(repo, store, _dependency(4, {"tools/a.py": f"VALUE = {number}\n"}))
        views[commit.id] = _primed_view(repo.main_version)
    main = repo.main
    assert _on_root() == {_attribute(main)}
    state = loaded_state(repos.view_root)
    assert set(state["modules"]) == set(namespace_entries(main))
    main_view = _view_dir(repos.view_root, views[main.id])
    assert state["views"] == [main_view.name]
    assert state["lines"] and all(_under(line, main_view) for line in state["lines"])
    earlier = [path for version_id, path in views.items() if version_id != main.id]
    assert len(earlier) == 23
    assert not any(Path(path).exists() or path in linecache.cache for path in earlier)

    commits = {commit.id: commit for commit in await repo.log()}
    chain, cursor = [], main.id
    while cursor is not None:
        chain.append(cursor)
        cursor = commits[cursor].parent
    assert len(chain) == 24 and chain[-1] == v1.id and set(chain) == set(views)


def _owned(value, greeting):
    """Thing を持つ ``tools/a.py``、外へ出す Agentish を持つ ``tools/b.py``、Skill を持つ tree。

    Agentish は Thing と別の module に置く。外へ出た実例はその module の名前空間を生かし続けるため、
    同じ module では Thing の解放を確かめられない。
    """
    return minimal_tree(
        extra={
            "tools/a.py": f"VERSION = {value}\n\n\nclass Thing:\n    pass\n",
            "tools/b.py": f"GREETING = {greeting!r}\n\n\nclass Agentish:\n    def greet(self):\n        return GREETING\n",
            "agent/main.py": entry_source("main", "a"),
            "skills/alpha/SKILL.md": b"alpha",
        }
    )


async def test_vr17_unloading_drops_owned_references_and_keeps_escaped_objects(repos, tmp_path):
    """VR17: 解放は版が持つ module・linecache・file view・部品グラフを外し、表の行と外へ出た参照は壊さない。"""
    files = _owned(1, "hi")
    repo, store, v1 = await _open(repos, tmp_path, files)
    run1 = repo.run()
    entry = run1.agent("main")
    version = run1.version("main")
    module_name = f"{namespace_of(v1)}.trackeverything.tools.a"
    module = version.module("trackeverything.tools.a")
    assert sys.modules[module_name] is module
    refs = {"module": weakref.ref(module), "Thing": weakref.ref(module.Thing), "entry": weakref.ref(entry)}
    instance = version.module("trackeverything.tools.b").Agentish()
    class_ref = weakref.ref(type(instance))
    line_key = module.__file__
    assert linecache.getlines(line_key, module.__dict__) and line_key in linecache.cache
    skill = version.view_path("skills/alpha/SKILL.md")
    view = _view_dir(repos.view_root, skill)
    assert skill.read_bytes() == b"alpha" and _under(line_key, view)
    assert await _answer(run1) == _reply(v1, 1)

    v2 = await _advance(repo, store, _owned(2, "v2"))
    repo.release(run1)
    del run1, entry, version, module
    gc.collect()

    assert namespace_entries(v1) == [] and _attribute(v1) not in _on_root()
    assert not [key for key in list(linecache.cache) if isinstance(key, str) and _under(key, view)]
    assert not skill.exists() and not view.exists()
    assert {name: ref() for name, ref in refs.items()} == dict.fromkeys(refs)
    assert instance.greet() == "hi" and class_ref() is not None
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)
    assert "trackeverything.tools.a" not in sys.modules
    del instance
    gc.collect()
    assert class_ref() is None

    run2 = repo.run()
    assert run2.version("main").id == v2.id and namespace_entries(v2)
    assert await _answer(run2) == _reply(v2, 2)
    assert run2.version("main").module("trackeverything.tools.b").GREETING == "v2"
    assert run2.version("main").view_path("skills/alpha/SKILL.md").read_bytes() == b"alpha"
    repo.release(run2)
    assert {name for name, _, _ in store.mock_calls} <= STORE_METHODS
    assert await repo.tree(v1.id) == files
