"""Version-local loading through the version repository: VR37-VR39.

The store is mocked per scene (``version_store``). A row the Repo saved is seeded back
into the mock before the next step reads it, and ``read_main`` follows each accepted ``move_main``.
"""

import asyncio
import inspect
import json
import sys
from datetime import UTC, datetime

import pytest

from tests.async_helpers import settle
from tests.fakes import finish_task, in_task
from tests.versioning.helpers import api, commit_id, compose_source, loaded_state, make_commit, minimal_tree, namespace_entries, namespace_of, seed, version_store, with_marker
from trackeverything.core.abc.call import AgentCall
from trackeverything.core.abc.context import AgentContext


def _arguments(store, method):
    """The last awaited call of a store method, bound to ``VersionStore``'s parameter names."""
    call = getattr(store, method).await_args
    assert call is not None, f"{method} was not awaited"
    signature = inspect.signature(getattr(api().store.VersionStore, method))
    return signature.bind(None, *call.args, **call.kwargs).arguments


async def _open(repos, tmp_path, files, **options):
    """A started Repo whose mirror main is ``files``."""
    seeded = make_commit(files)
    store = version_store(main=seeded, versions=[(seeded, files)])
    repo = await repos(store, **options)
    assert (await repo.start()).id == seeded.id
    return repo, store, seeded


async def _fork(repo, store, files, message):
    """Fork ``files`` from main, check the written and kept version, and seed it as the mirror now holds it."""
    parent = repo.main.id
    commit = await repo.fork(parent, files, message)
    assert (commit.parent, commit.message) == (parent, message)
    written = _arguments(store, "write")
    assert dict(written["tree"]) == files and (written["parent"], written["message"]) == (parent, message)
    assert _arguments(store, "keep")["version_id"] == commit.id
    seed(store, (commit, files))
    return commit


async def _merge(repo, store, commit):
    """Merge a child of main, check the push and the compare-and-set on main, and make GitHub's and the mirror's main follow it."""
    ours = repo.main.id
    merged = await repo.merge(commit.id)
    assert merged.id == commit.id
    assert _arguments(store, "push")["version_id"] == commit.id
    swap = _arguments(store, "move_main")
    assert (swap["expected"], swap["new"]) == (ours, commit.id)
    store.read_main.return_value = commit.id
    store.fetch.return_value = commit.id
    version = repo.main_version
    assert repo.main.id == version.id == commit.id
    return version


ENTRY = '''
import asyncio

from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.call import AgentResult
from trackeverything.tools import a


def _probe(load):
    try:
        return load()
    except ImportError as error:
        return type(error).__name__


def _removed():
    import trackeverything.tools.removed as module
    return module.GONE


def _added():
    import trackeverything.tools.added as module
    return [module.NEW, module.__name__]


class Entry(BaseAgent):
    name = "entry"
    description = "entry"

    async def _execute(self, call):
        data = call.ctx.data
        await data["start"].wait()
        reads = []
        for _ in range(300):
            value = a.read()
            reads.append(value)
            data["trace"].append(value)
            await asyncio.sleep(0)
        return AgentResult(status="ok", response={"reads": reads, "removed": _probe(_removed), "added": _probe(_added)})
'''


def _counter_tree(counter, *, removed=False, added=False):
    """tools.a -> tools.b -> tools.helper (COUNTER), optional tools.removed / tools.added, and the ``entry`` entrance."""
    extra = {
        "tools/a.py": "from trackeverything.tools.b import value\n\n\ndef read():\n    return value()\n",
        "tools/b.py": "from trackeverything.tools import helper\n\n\ndef value():\n    return helper.COUNTER\n",
        "tools/helper.py": f"COUNTER = {counter}\n",
        "tools/removed.py": "GONE = 1\n" if removed else None,
        "tools/added.py": "NEW = 2\n" if added else None,
        "agent/entry.py": ENTRY,
    }
    return minimal_tree((), extra, compose=compose_source(["agent.entry.Entry"], ["entry"]))


async def test_vr37_each_run_resolves_modules_inside_its_own_version(repos, tmp_path):
    """VR37: transitive dependencies, additions and removals resolve inside the version each run pinned."""
    repo, store, _ = await _open(repos, tmp_path, _counter_tree(1, removed=True))
    v1 = repo.main_version
    run1 = repo.run()
    assert run1.version("entry") is v1
    entry1 = run1.agent("entry")
    assert type(entry1).__module__ == f"{v1.namespace}.trackeverything.agent.entry"

    v2 = await _merge(repo, store, await _fork(repo, store, _counter_tree(2, added=True), "counter 2"))
    run2 = repo.run()
    assert run2.version("entry") is v2 and run1.version("entry") is v1
    entry2 = run2.agent("entry")
    assert entry2 is not entry1 and run1.agent("entry") is entry1
    assert type(entry2).__module__ == f"{v2.namespace}.trackeverything.agent.entry"

    start, trace = asyncio.Event(), []
    tasks = [
        in_task(agent.run, AgentCall(AgentContext(data={"start": start, "trace": trace}, agents=run)))
        for agent, run in ((entry1, run1), (entry2, run2))
    ]
    await settle()
    assert trace == [], "both runs wait for the same start event"
    start.set()
    old, new = [await finish_task(task) for task in tasks]
    assert old.status == "ok", old.response
    assert new.status == "ok", new.response
    assert old.response["reads"] == [1] * 300 and new.response["reads"] == [2] * 300
    assert set(trace[:300]) == {1, 2}, "the two runs interleave"
    assert old.response["removed"] == 1 and old.response["added"] == "ModuleNotFoundError"
    assert new.response["removed"] == "ModuleNotFoundError"
    assert new.response["added"] == [2, f"{v2.namespace}.trackeverything.tools.added"]
    canonical = {f"trackeverything.tools.{name}" for name in ("a", "b", "helper", "removed", "added")}
    assert not canonical & set(sys.modules), "nothing falls back to disk or to the canonical package"

    repo.release(run1)
    assert namespace_entries(v1) == [] and namespace_entries(v2)
    assert run2.agent("entry") is entry2
    again = await entry2.run(AgentCall(AgentContext(data={"start": start, "trace": []}, agents=run2)))
    assert again.status == "ok", again.response
    assert again.response["reads"] == [2] * 300 and again.response["removed"] == "ModuleNotFoundError"
    repo.release(run2)
    assert namespace_entries(v2), "main stays loaded without users"


USER = '''
import trackeverything.core.abc.tool
import trackeverything.tools.vl09.leaf
import trackeverything.tools.vl09.other as other_alias
from trackeverything.tools.vl09.leaf import LEAF
from trackeverything.tools.vl09.leaf import LEAF as LEAF_ALIAS
from trackeverything.tools.vl09 import REEXPORT, other
from . import leaf as relative_leaf
from .other import OTHER
import json, trackeverything.tools.vl09.other
import trackeverything.core.abc.agent

TEXT = "trackeverything.tools.vl09.leaf"


def deferred():
    from trackeverything.tools.vl09.leaf import LEAF as inner
    import trackeverything.tools.vl09.other
    return inner, trackeverything.tools.vl09.other
'''

VL09 = {
    "tools/vl09/__init__.py": "from trackeverything.tools.vl09.leaf import LEAF as REEXPORT\n",
    "tools/vl09/leaf.py": "LEAF = 'leaf'\n",
    "tools/vl09/other.py": "OTHER = 'other'\n",
    "tools/vl09/user.py": USER,
}


#: The fixed ``clock()`` of the VR38 Repo, so a fork's version id is known before the fork.
STAMP = datetime(2026, 10, 13, 12, tzinfo=UTC)


def _vl09_tree(parent, digit):
    """A vl09 tree with a marker resource whose fork from ``parent`` at ``STAMP`` gets a digit-leading (or letter-leading) version id."""
    base = minimal_tree(extra=VL09)
    for index in range(64):
        files = with_marker(base, f"vl09-{index}")
        if commit_id(files, parent, "vl09", STAMP)[0].isdigit() == digit:
            return files
    pytest.fail(f"no marker gave a {'digit' if digit else 'letter'}-leading version id")


async def test_vr38_every_import_form_binds_inside_the_version(repos, tmp_path):
    """VR38: every Python import form binds to the version's modules or to the canonical shared modules."""
    repo, store, _ = await _open(repos, tmp_path, minimal_tree(), clock=lambda: STAMP)
    for digit in (True, False):
        commit = await _fork(repo, store, _vl09_tree(repo.main.id, digit), "vl09")
        assert commit.id[0].isdigit() == digit, commit.id
        version = await _merge(repo, store, commit)
        assert version.namespace == namespace_of(commit.id)
        package = f"{version.namespace}.trackeverything"
        user = version.module("trackeverything.tools.vl09.user")
        leaf = version.module("trackeverything.tools.vl09.leaf")
        other = version.module("trackeverything.tools.vl09.other")
        assert sys.modules[f"{package}.tools.vl09.user"] is user
        assert leaf.__name__ == f"{package}.tools.vl09.leaf"
        assert user.trackeverything.tools.vl09.leaf is leaf
        assert user.trackeverything.core.abc.tool is sys.modules["trackeverything.core.abc.tool"]
        assert user.trackeverything.core.abc.agent is sys.modules["trackeverything.core.abc.agent"]
        assert user.other_alias is other and user.other is other and user.relative_leaf is leaf
        assert user.LEAF == user.LEAF_ALIAS == user.REEXPORT == "leaf" and user.OTHER == "other"
        assert user.json is json and user.TEXT == "trackeverything.tools.vl09.leaf"
        inner, deferred_other = user.deferred()
        assert inner == "leaf" and deferred_other is other
        assert "_te_loaded" not in vars(user)
        assert (await repo.tree(commit.id))["tools/vl09/user.py"] == USER.encode()
        assert "trackeverything.tools.vl09" not in sys.modules


VL11 = {
    "tools/vl11/a.py": "import trackeverything.tools.vl11.b as b\nA = 'a'\n\n\ndef call():\n    return b.B + A\n",
    "tools/vl11/b.py": "from trackeverything.tools.vl11 import a\nB = 'b'\n\n\ndef back():\n    return a.A\n",
    "tools/vl11/registry.py": (
        "import sys\nREG = []\n\n\ndef register(fn):\n    REG.append((fn.__module__, sys.modules[fn.__module__].__name__))\n    return fn\n"
    ),
    "tools/vl11/c.py": "from trackeverything.tools.vl11.registry import register\n\n\n@register\ndef handler():\n    return 'h'\n",
}

INVALID_CYCLE = {
    "tools/vl11x/p.py": "from trackeverything.tools.vl11x.q import Q\nP = 'p'\n",
    "tools/vl11x/q.py": "from trackeverything.tools.vl11x.p import P\nQ = 'q'\n",
}


async def test_vr39_package_init_and_cycles_behave_as_python_and_an_invalid_cycle_fails_fork(repos, tmp_path):
    """VR39: package initialization and import cycles work as in Python, and an invalid cycle fails the fork."""
    repo, store, _ = await _open(repos, tmp_path, minimal_tree())
    valid = minimal_tree(extra=VL11)
    c1 = await _fork(repo, store, valid, "vl11")
    version = await _merge(repo, store, c1)
    qualified = f"{version.namespace}.trackeverything.tools.vl11.c"
    version.module("trackeverything.tools.vl11.c")
    assert version.module("trackeverything.tools.vl11.a").call() == "ba"
    assert version.module("trackeverything.tools.vl11.b").back() == "a"
    assert [(qualified, qualified)] == version.module("trackeverything.tools.vl11.registry").REG

    before = loaded_state(repos.view_root)
    kept, moves = store.keep.await_count, store.move_main.await_count
    with pytest.raises(ImportError) as caught:
        await repo.fork(c1.id, minimal_tree(extra={**VL11, **INVALID_CYCLE}), "vl11x")
    assert not isinstance(caught.value, ModuleNotFoundError), caught.value
    assert "tools.vl11x" in str(caught.value)
    assert (store.keep.await_count, store.move_main.await_count) == (kept, moves), "nothing is kept"
    assert repo.main.id == c1.id and repo.main_version is version
    assert loaded_state(repos.view_root) == before
    assert version.module("trackeverything.tools.vl11.a").call() == "ba"

    run = repo.run()
    assert run.version("main") is version
    assert run.agent("main") is version.graph.agents["main"]
    assert type(run.agent("main")).__module__ == f"{version.namespace}.trackeverything.agent.main"
    repo.release(run)
    assert loaded_state(repos.view_root) == before, "main stays loaded after its last run is released"
