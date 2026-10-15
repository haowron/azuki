"""版庫の fork と merge: VR01-VR08・VR53。

store は場面ごとの ``version_store``(固定の読取り結果と書込みの記録)で、
Repo が書いた版を後の読取りで使う場面は、write を確かめた後に ``seed`` で加え、
mirror の main の移動を確かめた後に ``read_main.return_value``、GitHub の main は ``fetch.return_value`` を変える。
版 id は store が決め(git の commit と同じく内容・親・message・秒単位の時刻から)、内容は id ではなく tree で比べる。
git 自体の規則(merge-tree・push の拒否)は test_git_repo.py が本物の git で確かめる。
"""

import asyncio
import inspect
import json
import re
import sys
import traceback
from datetime import UTC, datetime, timedelta

import pytest

from tests.async_helpers import WAIT, settle
from tests.fakes import ScriptedLLM, agent_call
from tests.versioning.helpers import api, commit_id, compose_source, host_services, loaded_state, make_commit, minimal_tree, namespace_entries, namespace_of, seed, version_store

#: 固定の clock が返す作成時刻(git の版は秒単位で持つ)。
NOW = datetime(2026, 10, 13, 9, 30, 15, 123456, tzinfo=UTC)
SECOND = timedelta(seconds=1)

# ---- 版 id と tree と記録の読取り


def _mod(value):
    return f"VALUE = {value!r}\n"


def _with(files, **values):
    """``tools/<名前>.py`` を ``VALUE = 値`` にした tree。None はそのファイルを削除する。"""
    changed = dict(files)
    for name, value in values.items():
        path = f"tools/{name}.py"
        if value is None:
            del changed[path]
        else:
            changed[path] = _mod(value).encode()
    return changed


def _probe_source(names):
    """入口 ``main``。各 ``tools/<名前>.py`` の VALUE(なければ None)と自分の module 名を JSON で返す。"""
    lookups = "".join(
        f"    try:\n"
        f"        from trackeverything.tools import {name} as found\n"
        f"    except ImportError:\n"
        f"        seen[{name!r}] = None\n"
        f"    else:\n"
        f"        seen[{name!r}] = found.VALUE\n"
        for name in names
    )
    return f'''
import json

from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.call import AgentResult


def seen_values():
    seen = {{}}
{lookups}    return seen


class Entry(BaseAgent):
    name = "main"
    description = "main entrance"

    async def _execute(self, call):
        return AgentResult(status="ok", response=json.dumps({{"seen": seen_values(), "module": __name__}}))
'''


def _probe_tree(names, **values):
    """``minimal_tree`` の入口 ``main`` を ``_probe_source(names)`` にし、``tools/<名前>.py`` に VALUE を置いた tree。"""
    return _with(minimal_tree(extra={"agent/main.py": _probe_source(names)}), **values)


def _bound(method, args, kwargs):
    """``VersionStore.<method>`` への引数を Protocol の順の tuple にする(self を除く)。"""
    signature = inspect.signature(getattr(api().store.VersionStore, method))
    return tuple(signature.bind(None, *args, **kwargs).arguments.values())[1:]


def _writes(store):
    """await された write の ``(tree, parent, message, created_at)``。"""
    return [(dict(files), parent, message, created_at) for files, parent, message, created_at in (_bound("write", call.args, call.kwargs) for call in store.write.await_args_list)]


def _kept(store):
    """await された keep の版 id。"""
    return [_bound("keep", call.args, call.kwargs)[0] for call in store.keep.await_args_list]


def _pushes(store):
    """await された push の版 id。"""
    return [_bound("push", call.args, call.kwargs)[0] for call in store.push.await_args_list]


def _moves(store):
    """await された move_main の ``(expected, new)``。"""
    return [_bound("move_main", call.args, call.kwargs) for call in store.move_main.await_args_list]


def _chain(error):
    found = []
    while error is not None and not any(item is error for item in found):
        found.append(error)
        error = error.__cause__ or error.__context__
    return found


def _in_chain(error, kind):
    """error 自身か、その原因の連鎖にある ``kind`` の例外。"""
    for item in _chain(error):
        if isinstance(item, kind):
            return item
    pytest.fail(f"{kind.__name__} is not in the chain of {error!r}")


async def _raised(awaitable):
    try:
        await awaitable
    except Exception as error:
        return error
    pytest.fail("例外が送出されない")


# ---- Repo の開始と run


def _tracking(services=None):
    """部品グラフを作った版の id を順に記録する build 関数と、その記録。"""
    prepare = api().compose.prepare_components(services if services is not None else host_services())
    built = []

    def build(version):
        built.append(version.id)
        return prepare(version)

    return build, built


async def _start(repos, tmp_path, store, files, **options):
    """mirror の main がある store で Repo を開始する(files は mirror の main の tree。ファイルは読まない)。"""
    repo = await repos(store, **options)
    await repo.start()
    return repo


async def _forked(repo, store, parent, files, message):
    """parent から fork し、書かれた版を store の読取りにも加える。"""
    commit = await repo.fork(parent, files, message)
    seed(store, (commit, files))
    return commit


def _agent(repo, name):
    """新しい run が名前で得る Agent の実例。run はすぐ解放する。"""
    run = repo.run()
    try:
        return run.agent(name)
    finally:
        repo.release(run)


async def _entrance(repo, name="main"):
    """新しい run で入口を実行し、``(実例, response)`` を返す。"""
    run = repo.run()
    try:
        agent = run.agent(name)
        assert agent is not None, name
        result = await agent.run(agent_call(agents=run))
    finally:
        repo.release(run)
    assert result.status == "ok", result.response
    return agent, result.response


async def _probe(repo):
    """新しい run の入口 ``main`` が見る VALUE と、入口の module 名。"""
    agent, response = await _entrance(repo)
    report = json.loads(response)
    return agent, report["seen"], report["module"]


def _entry_module(version_id, name="main"):
    return f"{namespace_of(version_id)}.trackeverything.agent.{name}"


# ---- fork


async def test_vr01_fork_always_stores_a_new_version_without_moving_main(repos, tmp_path):
    """VR01: fork は tree を v1 を親とする新しい版として書き(id は store が決める 40 桁)、保持して main を変えない。同じ tree・v1 と同じ tree でも別の版になり、id が既にあれば作成時刻を 1 秒進める。"""
    v1 = minimal_tree()
    v1c = make_commit(v1)
    store = version_store(main=v1c, versions=[(v1c, v1)])
    build, built = _tracking()
    repo = await _start(repos, tmp_path, store, v1, build=build, clock=lambda: NOW)
    assert built == [v1c.id]
    before = loaded_state(repos.view_root)
    agent, report = await _entrance(repo)
    assert report == f"1->1|{_entry_module(v1c.id)}"

    files = minimal_tree(
        extra={
            "tools/vl_dep.py": "VERSION = 2\n",
            "tools/x.txt": b"same\n",
            "tools/y.txt": b"same\n",
            "skills/s/logo.bin": b"\x89PNG\x00\xff",
        }
    )
    c = await repo.fork(v1c.id, files, "optimizer:test")
    assert re.fullmatch(r"[0-9a-f]{40}", c.id)
    assert c == store.written[c.id][0] and c.id == commit_id(files, v1c.id, "optimizer:test", NOW)
    assert (c.parent, c.message, c.created_at) == (v1c.id, "optimizer:test", NOW.replace(microsecond=0))
    assert _writes(store) == [(files, v1c.id, "optimizer:test", NOW)]
    assert _kept(store) == [c.id]
    assert built == [v1c.id, c.id]
    # fork は main を動かさず push もしない。検証に読み込んだ版は操作の終わりに解放される。
    store.move_main.assert_not_awaited()
    store.push.assert_not_awaited()
    assert repo.main == v1c and loaded_state(repos.view_root) == before
    assert await _entrance(repo) == (agent, report)

    seed(store, (c, files))
    assert await repo.tree(c.id) == files
    # 同じ tree・親・message・時刻の版は既にあるので、作成時刻を 1 秒進めた新しい版になる。
    again = await repo.fork(v1c.id, dict(reversed(files.items())), "optimizer:test")
    assert again.id == commit_id(files, v1c.id, "optimizer:test", NOW + SECOND) != c.id
    assert (again.parent, again.message, again.created_at) == (v1c.id, "optimizer:test", (NOW + SECOND).replace(microsecond=0))

    same = await repo.fork(v1c.id, v1, "same as v1")
    assert same.id == commit_id(v1, v1c.id, "same as v1", NOW) != v1c.id
    assert same.parent == v1c.id

    renamed = {**{path: content for path, content in files.items() if path != "tools/x.txt"}, "tools/z.txt": b"same\n"}
    one_byte = {**files, "tools/x.txt": b"samf\n"}
    empty_file = {**files, "tools/empty.txt": b""}
    variants = (renamed, one_byte, empty_file)
    forked = [await repo.fork(v1c.id, variant, "variant") for variant in variants]
    assert [commit.id for commit in forked] == [commit_id(variant, v1c.id, "variant", NOW) for variant in variants]
    assert len({commit.id for commit in forked}) == 3

    # 別の版が同じ id を既に持つ(mirror にある)。
    taken = {**files, "tools/taken.txt": b"taken\n"}
    busy = make_commit(taken, parent=v1c.id, message="taken", created_at=NOW)
    seed(store, (busy, taken))
    t = await repo.fork(v1c.id, taken, "taken")
    assert t.id == commit_id(taken, v1c.id, "taken", NOW + SECOND) != busy.id
    assert (t.parent, t.message, t.created_at) == (v1c.id, "taken", (NOW + SECOND).replace(microsecond=0))

    commits = [c, again, same, *forked, t]
    assert _kept(store) == [commit.id for commit in commits]
    assert [(files_, parent, message, at) for files_, parent, message, at in _writes(store)] == [
        (files, v1c.id, "optimizer:test", NOW),
        (files, v1c.id, "optimizer:test", NOW),
        (files, v1c.id, "optimizer:test", NOW + SECOND),
        (v1, v1c.id, "same as v1", NOW),
        (renamed, v1c.id, "variant", NOW),
        (one_byte, v1c.id, "variant", NOW),
        (empty_file, v1c.id, "variant", NOW),
        (taken, v1c.id, "taken", NOW),
        (taken, v1c.id, "taken", NOW + SECOND),
    ]
    assert len({v1c.id, busy.id, *(commit.id for commit in commits)}) == len(commits) + 2
    assert all(commit.parent == v1c.id for commit in commits)
    assert built == [v1c.id, *(commit.id for commit in commits)]
    store.move_main.assert_not_awaited()
    assert repo.main == v1c and loaded_state(repos.view_root) == before


def _unused_module_broke(error):
    assert "unused module broke" in str(_in_chain(error, RuntimeError))


def _division_on_line_two(error):
    found = _in_chain(error, ZeroDivisionError)
    frames = [(frame.f_code.co_filename, line) for frame, line in traceback.walk_tb(found.__traceback__)]
    assert any(name.endswith("tools/b.py") and line == 2 for name, line in frames), frames


def _syntax_in_bad(error):
    assert "tools/bad.py" in (_in_chain(error, SyntaxError).filename or "")


def _compose_missing(error):
    assert any(re.search(r"agent[./]compose", str(item)) for item in _chain(error)), error


def _literal_bypass(error):
    text = str(_in_chain(error, ImportError))
    assert "tools/vl10/bypass.py:2" in text and "trackeverything.tools.vl10.b" in text, text
    assert "resolve" not in text
    assert "trackeverything.tools.vl10.b" not in sys.modules


@pytest.mark.parametrize(
    ("extra", "check"),
    [
        pytest.param({"tools/unused.py": "raise RuntimeError('unused module broke')\n"}, _unused_module_broke, id="a-unused-module"),
        pytest.param({"tools/b.py": "import trackeverything.tools.vl_dep\nRESULT = 1 / 0\n"}, _division_on_line_two, id="b-module-error"),
        pytest.param({"tools/bad.py": "def f(:\n"}, _syntax_in_bad, id="c-syntax-error"),
        pytest.param({"agent/compose.py": None}, _compose_missing, id="d-no-compose"),
        pytest.param(
            {
                "tools/vl10/b.py": "NAME = 'b'\n",
                "tools/vl10/bypass.py": "import importlib\nM = importlib.import_module('trackeverything.tools.vl10.b')\n",
            },
            _literal_bypass,
            id="e-literal-import-module",
        ),
    ],
)
async def test_vr02_fork_that_fails_to_load_leaves_nothing(repos, tmp_path, extra, check):
    """VR02 (a)-(e): 読み込めない tree の fork は例外で、版を保持せず(keep しない)読み込んだものも残さず main も変えない。"""
    v1 = minimal_tree()
    v1c = make_commit(v1)
    store = version_store(main=v1c, versions=[(v1c, v1)])
    repo = await _start(repos, tmp_path, store, v1)
    agent, main_version = _agent(repo, "main"), repo.main_version
    before = loaded_state(repos.view_root)

    error = await _raised(repo.fork(v1c.id, minimal_tree(extra=extra), "broken"))
    check(error)
    store.keep.assert_not_awaited()
    store.move_main.assert_not_awaited()
    assert repo.main == v1c and repo.main_version is main_version
    assert loaded_state(repos.view_root) == before
    assert _agent(repo, "main") is agent


async def test_vr02_fork_whose_write_fails_leaves_nothing_and_can_be_retried(repos, tmp_path):
    """VR02 (f): 版の書込みに失敗した fork は store の例外を送出して何も残さず(読込みも keep もない)、同じ tree の再実行(clock を固定)は版になる。"""
    v1 = minimal_tree()
    v1c = make_commit(v1)
    store = version_store(main=v1c, versions=[(v1c, v1)])
    repo = await _start(repos, tmp_path, store, v1, clock=lambda: NOW)
    agent = _agent(repo, "main")
    before = loaded_state(repos.view_root)
    write = store.write.side_effect
    failures = [ConnectionError("store unavailable")]

    async def flaky(*args, **kwargs):
        if failures:
            raise failures.pop()
        return await write(*args, **kwargs)

    store.write.side_effect = flaky
    files = minimal_tree(extra={"tools/vl_dep.py": "VERSION = 2\n"})

    error = await _raised(repo.fork(v1c.id, files, "files"))
    assert "store unavailable" in str(_in_chain(error, ConnectionError))
    store.keep.assert_not_awaited()
    assert loaded_state(repos.view_root) == before
    assert repo.main == v1c and _agent(repo, "main") is agent

    retried = await repo.fork(v1c.id, files, "files")
    assert retried.id == commit_id(files, v1c.id, "files", NOW) and retried.parent == v1c.id
    assert _writes(store) == [(files, v1c.id, "files", NOW), (files, v1c.id, "files", NOW)]
    assert _kept(store) == [retried.id]
    store.move_main.assert_not_awaited()
    assert repo.main == v1c and loaded_state(repos.view_root) == before


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("core/host.py", id="outside-the-five-folders"),
        pytest.param("tools/../../evil.py", id="parent-segment"),
        pytest.param("tools/" + "../" * 8 + "evil.py", id="deep-parent-segments"),
        pytest.param("<absolute>", id="absolute-path"),
        pytest.param("tools//empty_part.py", id="empty-part"),
    ],
)
async def test_vr02_fork_rejects_paths_outside_the_five_folders(repos, tmp_path, key):
    """VR02 (g): 5 フォルダの外・".."・絶対パス・空の部分を持つ tree は、読み込む前に ValueError で拒否される。"""
    path = str(tmp_path / "evil.py") if key == "<absolute>" else key
    v1 = minimal_tree()
    v1c = make_commit(v1)
    store = version_store(main=v1c, versions=[(v1c, v1)])
    build, built = _tracking()
    repo = await _start(repos, tmp_path, store, v1, build=build)
    before = loaded_state(repos.view_root)

    error = await _raised(repo.fork(v1c.id, {**v1, path: b"VALUE = 'escaped'\n"}, "escape"))
    assert path in str(_in_chain(error, ValueError))
    assert built == [v1c.id]
    store.write.assert_not_awaited()
    store.move_main.assert_not_awaited()
    assert loaded_state(repos.view_root) == before
    assert list(tmp_path.rglob("evil.py")) == []
    assert repo.main == v1c


REGISTRY_PARTS = """
from trackeverything.core.abc.agent import BaseAgent


class Entry(BaseAgent):
    name = "entry"
    description = "entry"
    agents = {children}


class Helper(BaseAgent):
    name = "helper"
    description = "helper"
    llm = {helper_llm}


class Twin(BaseAgent):
    name = "helper"
    description = "twin"


class Nameless(BaseAgent):
    description = "nameless"
"""


def _registry(*, children=("helper",), helper_llm="low", agents=("Entry", "Helper"), entrances=("entry",), dependencies=None):
    """``agent/parts.py`` の Agent 類を ``build_agents`` で組む tree。既定は entry → helper(llm ``low``)。"""
    parts = REGISTRY_PARTS.format(children=repr(tuple(children)), helper_llm=repr(helper_llm))
    compose = compose_source([f"agent.parts.{name}" for name in agents], entrances)
    if dependencies is not None:
        compose = compose.replace("logger=services.logger)", f"logger=services.logger, dependencies={dependencies!r})")
        assert "dependencies=" in compose
    return minimal_tree((), {"agent/parts.py": parts}, compose=compose)


@pytest.mark.parametrize(
    ("change", "needles"),
    [
        pytest.param({"children": ("helper", "missing")}, ("entry", "missing"), id="unknown-child"),
        pytest.param({"helper_llm": "turbo"}, ("helper", "turbo"), id="unknown-llm"),
        pytest.param({"agents": ("Entry", "Helper", "Twin")}, ("helper", "Twin"), id="duplicate-name"),
        pytest.param({"agents": ("Entry", "Helper", "Nameless")}, ("Nameless",), id="nameless-class"),
        pytest.param({"dependencies": {"ghost": {}}}, ("ghost",), id="dependencies-for-unknown-agent"),
        pytest.param({"entrances": ("entry", "nowhere")}, ("nowhere",), id="entrance-without-agent"),
    ],
)
async def test_vr03_fork_rejects_a_tree_whose_composition_fails(repos, tmp_path, change, needles):
    """VR03: build_agents・ComponentGraph の構成に失敗する tree の fork は ValueError で、版を保持せず main も変えない。"""
    services = host_services(llms={"max": ScriptedLLM(), "low": ScriptedLLM()})
    v1 = _registry()
    v1c = make_commit(v1)
    store = version_store(main=v1c, versions=[(v1c, v1)])
    repo = await _start(repos, tmp_path, store, v1, services=services)
    graph = repo.main_version.graph
    assert set(graph.agents) == {"entry", "helper"} and graph.entrances == frozenset({"entry"})
    entry = _agent(repo, "entry")
    before = loaded_state(repos.view_root)

    error = await _raised(repo.fork(v1c.id, _registry(**change), "broken"))
    found = _in_chain(error, ValueError)
    assert all(needle in str(found) for needle in needles), found
    store.keep.assert_not_awaited()
    store.move_main.assert_not_awaited()
    assert repo.main == v1c and loaded_state(repos.view_root) == before
    assert _agent(repo, "entry") is entry


# ---- merge


async def test_vr04_merge_fast_forwards_main_to_a_candidate_forked_from_it(repos, tmp_path):
    """VR04: GitHub の main が v1 のまま、v1 から fork した m の merge は m を push して mirror の main を m へ進め、m を返す(版を作らない)。もう一度の merge は push せず main を返す。"""
    v1 = _probe_tree(("a", "b"), a=1, b=1)
    m = _with(v1, a=2, b=None)
    v1c = make_commit(v1)
    store = version_store(main=v1c, versions=[(v1c, v1)])
    build, built = _tracking()
    repo = await _start(repos, tmp_path, store, v1, build=build)
    mc = await _forked(repo, store, v1c.id, m, "candidate")
    written = _writes(store)
    assert written == [(m, v1c.id, "candidate", written[0][3])]

    assert await repo.merge(mc.id) == mc
    assert _pushes(store) == [mc.id]
    assert _moves(store) == [(v1c.id, mc.id)]
    assert _writes(store) == written
    store.merge_tree.assert_not_awaited()
    assert built == [v1c.id, mc.id, mc.id]
    assert repo.main == mc and repo.main_version.id == mc.id
    assert namespace_entries(v1c.id) == []
    agent, seen, module = await _probe(repo)
    assert seen == {"a": 2, "b": None} and module == _entry_module(mc.id)

    # GitHub と mirror の main は m。m は既に GitHub の main に含まれる。
    store.read_main.return_value = mc.id
    store.fetch.return_value = mc.id
    store.is_ancestor.return_value = True
    held = repo.main_version.modules()
    assert await repo.merge(mc.id) == mc
    assert built == [v1c.id, mc.id, mc.id]
    assert _writes(store) == written and _pushes(store) == [mc.id] and _moves(store) == [(v1c.id, mc.id)]
    assert repo.main == mc and _agent(repo, "main") is agent
    after = repo.main_version.modules()
    assert after.keys() == held.keys() and all(after[name] is held[name] for name in held)


async def test_vr05_merge_after_main_moved_rebases_onto_the_new_main(repos, tmp_path):
    """VR05: v1 から fork した k の merge は、m の merge で GitHub の main が動いた後なら、v1 を base に m と k を合わせた版(親 m、message に Rebased-From)を作り、検証して push する。"""
    v1 = _probe_tree(("a", "b"), a=1, b=1)
    v1c = make_commit(v1)
    store = version_store(main=v1c, versions=[(v1c, v1)])
    build, built = _tracking()
    repo = await _start(repos, tmp_path, store, v1, build=build)
    kc = await _forked(repo, store, v1c.id, _with(v1, b=3), "k")
    mc = await _forked(repo, store, v1c.id, _with(v1, a=2), "m")
    assert await repo.merge(mc.id) == mc
    store.read_main.return_value = mc.id
    store.fetch.return_value = mc.id
    combined = _with(v1, a=2, b=3)
    store.merge_tree.return_value = combined

    merged = await repo.merge(kc.id)
    assert [tuple(_bound("merge_tree", call.args, call.kwargs)) for call in store.merge_tree.await_args_list] == [(v1c.id, mc.id, kc.id)]
    assert merged not in (kc, mc) and merged.parent == mc.id
    assert merged.message == f"k\n\nRebased-From: {kc.id}"
    assert store.written[merged.id][1] == combined
    assert _kept(store)[-1] == merged.id
    assert _pushes(store) == [mc.id, merged.id]
    assert _moves(store) == [(v1c.id, mc.id), (mc.id, merged.id)]
    assert built[-1] == merged.id and namespace_entries(kc.id) == []
    assert repo.main == merged
    _, seen, module = await _probe(repo)
    assert seen == {"a": 2, "b": 3} and module == _entry_module(merged.id)


async def test_vr05_unknown_ids_raise_unknown_version(repos, tmp_path):
    """VR05: 未知の id と前方一致の id は merge・tree・fork の parent で UnknownVersion(LookupError)になり、何も変えない。"""
    modules = api()
    assert issubclass(modules.repo.UnknownVersion, LookupError)
    v1 = minimal_tree()
    v1c = make_commit(v1)
    store = version_store(main=v1c, versions=[(v1c, v1)])
    repo = await _start(repos, tmp_path, store, v1)
    before = loaded_state(repos.view_root)
    files = minimal_tree(extra={"tools/vl_dep.py": "VERSION = 2\n"})

    for unknown in ("0123456789ab" * 3 + "0123", v1c.id[:8]):
        with pytest.raises(modules.repo.UnknownVersion):
            await repo.merge(unknown)
        with pytest.raises(modules.repo.UnknownVersion):
            await repo.tree(unknown)
        with pytest.raises(modules.repo.UnknownVersion):
            await repo.fork(unknown, files, "orphan")
    store.write.assert_not_awaited()
    store.fetch.assert_not_awaited()
    store.push.assert_not_awaited()
    store.move_main.assert_not_awaited()
    assert repo.main == v1c and loaded_state(repos.view_root) == before


async def test_vr06_merge_of_a_candidate_the_host_can_no_longer_build_keeps_main(repos, tmp_path):
    """VR06: fork の後に Host が変わり(LLM low がなくなる)候補の部品グラフを作れないと、merge は読込みの例外を送出し、push も main の移動もせず候補を残さない。"""
    modules = api()
    v1 = _registry(helper_llm="max")
    v1c = make_commit(v1)
    store = version_store(main=v1c, versions=[(v1c, v1)])
    host = [modules.compose.prepare_components(host_services())]

    def build(version):
        return host[0](version)

    repo = await _start(repos, tmp_path, store, v1, build=build)
    kc = await _forked(repo, store, v1c.id, _registry(helper_llm="low"), "k")
    host[0] = modules.compose.prepare_components(host_services(llms={"max": ScriptedLLM(), "high": ScriptedLLM()}))
    entry = _agent(repo, "entry")
    before = loaded_state(repos.view_root)
    written = _writes(store)

    error = await _raised(repo.merge(kc.id))
    found = _in_chain(error, ValueError)
    assert "helper" in str(found) and "low" in str(found), found
    store.push.assert_not_awaited()
    store.move_main.assert_not_awaited()
    assert _writes(store) == written
    assert namespace_entries(kc.id) == []
    assert repo.main == v1c and loaded_state(repos.view_root) == before
    assert _agent(repo, "entry") is entry


async def test_vr07_merge_fails_when_github_main_moves_before_the_push(repos, tmp_path):
    """VR07: merge が GitHub の main(候補の親)を fetch した後、push の前に他の push が入ると(push が拒否される)、PushRejected で mirror の main は変わらない。"""
    modules = api()
    v1 = _probe_tree(("a", "b"), a=1, b=1)
    v1c = make_commit(v1)
    store = version_store(main=v1c, versions=[(v1c, v1)])
    build, built = _tracking()
    repo = await _start(repos, tmp_path, store, v1, build=build)
    kc = await _forked(repo, store, v1c.id, _with(v1, b=3), "k")
    agent = _agent(repo, "main")
    before = loaded_state(repos.view_root)
    store.push.return_value = False

    error = await _raised(repo.merge(kc.id))
    assert isinstance(error, modules.repo.PushRejected)
    assert kc.id in str(error)
    assert _pushes(store) == [kc.id]
    store.move_main.assert_not_awaited()
    # 読み込んで確かめた候補は、失敗した merge の終わりに解放される。
    assert built == [v1c.id, kc.id, kc.id]
    assert loaded_state(repos.view_root) == before and namespace_entries(kc.id) == []
    assert repo.main == v1c and _agent(repo, "main") is agent
    _, seen, _ = await _probe(repo)
    assert seen == {"a": 1, "b": 1}


async def test_vr07_merge_decides_by_the_github_main_not_the_loaded_main(repos, tmp_path):
    """VR07: 読込み済みの main(v1)が GitHub の main(v2)より古いとき、merge は fetch した GitHub の main で判断する。v2 から fork した候補はそのまま push し、v1 から fork した候補は GitHub の main の上へ載せ直す。"""
    v1 = _probe_tree(("a", "b"), a=1, b=1)
    v2 = _with(v1, b=2)
    v1c = make_commit(v1)
    v2c = make_commit(v2, parent=v1c.id, message="human")
    store = version_store(main=v1c, versions=[(v1c, v1)])
    repo = await _start(repos, tmp_path, store, v1)
    stale = await _forked(repo, store, v1c.id, _with(v1, a=3), "stale")
    seed(store, (v2c, v2))
    store.fetch.return_value = v2c.id
    cc = await _forked(repo, store, v2c.id, _with(v2, a=2), "c")
    assert repo.main == v1c

    assert await repo.merge(cc.id) == cc
    store.merge_tree.assert_not_awaited()
    assert _pushes(store) == [cc.id]
    assert _moves(store) == [(v1c.id, cc.id)]
    assert repo.main == cc
    _, seen, module = await _probe(repo)
    assert seen == {"a": 2, "b": 2} and module == _entry_module(cc.id)

    store.read_main.return_value = cc.id
    store.fetch.return_value = cc.id
    store.merge_tree.return_value = _with(v2, a=3)
    merged = await repo.merge(stale.id)
    assert [tuple(_bound("merge_tree", call.args, call.kwargs)) for call in store.merge_tree.await_args_list] == [(v1c.id, cc.id, stale.id)]
    assert merged.parent == cc.id and _pushes(store) == [cc.id, merged.id]
    assert _moves(store) == [(v1c.id, cc.id), (cc.id, merged.id)]
    _, seen, _ = await _probe(repo)
    assert seen == {"a": 3, "b": 2}


async def test_vr08_overlapping_merges_run_one_after_the_other(repos, tmp_path):
    """VR08: 一つのプロセスで v1 から fork した二つの候補の merge が重なると、merge は Repo の lock で直列になる。先の merge は候補 1 を push し、後の merge は候補 1 の後の GitHub の main で判断して、候補 2 を候補 1 の上へ載せ直す。"""
    v1 = _probe_tree(("a", "b"), a=1, b=1)
    v1c = make_commit(v1)
    store = version_store(main=v1c, versions=[(v1c, v1)])
    build, built = _tracking()
    repo = await _start(repos, tmp_path, store, v1, build=build)
    c1 = await _forked(repo, store, v1c.id, _with(v1, a=2), "c1")
    c2 = await _forked(repo, store, v1c.id, _with(v1, b=3), "c2")
    assert built == [v1c.id, c1.id, c2.id]
    github = {"main": v1c.id}
    mirror = {"main": v1c.id}
    paused, resume = asyncio.Event(), asyncio.Event()

    async def fetch():
        return github["main"]

    async def push(*args, **kwargs):
        # GitHub への fast-forward。最初の push は resume まで止まる。
        (version_id,) = _bound("push", args, kwargs)
        if not paused.is_set():
            paused.set()
            await resume.wait()
        github["main"] = version_id
        return True

    async def read_main():
        return mirror["main"]

    async def move_main(*args, **kwargs):
        expected, new = _bound("move_main", args, kwargs)
        if mirror["main"] != expected:
            return False
        mirror["main"] = new
        return True

    store.fetch.side_effect = fetch
    store.push.side_effect = push
    store.read_main.side_effect = read_main
    store.move_main.side_effect = move_main
    store.merge_tree.return_value = _with(v1, a=2, b=3)
    first = asyncio.create_task(repo.merge(c1.id))
    await asyncio.wait_for(paused.wait(), WAIT)
    second = asyncio.create_task(repo.merge(c2.id))
    await settle()
    assert store.fetch.await_count == 1
    resume.set()
    one, two = await asyncio.wait_for(asyncio.gather(first, second), WAIT)

    assert one == c1
    assert two.parent == c1.id and two.message == f"c2\n\nRebased-From: {c2.id}"
    assert [tuple(_bound("merge_tree", call.args, call.kwargs)) for call in store.merge_tree.await_args_list] == [(v1c.id, c1.id, c2.id)]
    assert _pushes(store) == [c1.id, two.id]
    assert github["main"] == mirror["main"] == two.id and repo.main == two
    assert namespace_entries(c2.id) == [] and namespace_entries(c1.id) == []
    _, seen, module = await _probe(repo)
    assert seen == {"a": 2, "b": 3} and module == _entry_module(two.id)


async def test_vr53_forking_back_to_an_older_tree_makes_a_new_version_that_merges(repos, tmp_path):
    """VR53: v1 から tools/review.py(agent/review.py の代役)を変えた v2 を fork・merge し、v2 から v1 と同じ tree を fork・merge すると、新しい版(親 v2)が main になり tree が v1 に戻る。

    版 id が内容だけから決まると、この fork が v1 を返し、merge が v1 の親を基準に判断して main が戻らなかった。
    """
    v1 = _probe_tree(("review",), review="v1")
    v2 = _with(v1, review="v2")
    v1c = make_commit(v1)
    store = version_store(main=v1c, versions=[(v1c, v1)])
    repo = await _start(repos, tmp_path, store, v1)
    v2c = await _forked(repo, store, v1c.id, v2, "review v2")
    assert await repo.merge(v2c.id) == v2c
    store.read_main.return_value = v2c.id
    store.fetch.return_value = v2c.id
    _, seen, _ = await _probe(repo)
    assert seen == {"review": "v2"}

    back = await repo.fork(v2c.id, v1, "review back to v1")
    assert back.id not in (v1c.id, v2c.id) and back.parent == v2c.id
    assert back.id == commit_id(v1, v2c.id, "review back to v1", back.created_at)
    assert _writes(store)[-1][:3] == (v1, v2c.id, "review back to v1")
    seed(store, (back, v1))
    assert await repo.merge(back.id) == back
    assert _pushes(store) == [v2c.id, back.id]
    assert _moves(store) == [(v1c.id, v2c.id), (v2c.id, back.id)]
    assert repo.main == back and await repo.tree(repo.main.id) == v1
    _, seen, module = await _probe(repo)
    assert seen == {"review": "v1"} and module == _entry_module(back.id)
