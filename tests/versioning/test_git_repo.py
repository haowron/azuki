"""The version repository over real git: start (VG04, VG05), sync (VG06-VG09) and merge (VG10-VG15).

GitHub is a bare repository on disk (``gitrepo.GitHub``) and the person commits in its clone.
The Repo keeps the mirror in ``tmp_path/components.git``. Versions are ``minimal_tree`` trees whose
entrance ``main`` reports the ``VALUE`` of ``tools/a.py`` and ``tools/b.py``.
"""

import logging

import pytest

from tests.versioning.gitrepo import git, mirror_ref, mirror_refs
from tests.versioning.helpers import EMPTY_STATE, api, host_services, loaded_state, namespace_entries
from tests.versioning.hooks import calls, stand_in_host
from tests.versioning.probe import BROKEN, chain
from tests.versioning.probe import changed as _changed
from tests.versioning.probe import seen as _seen
from tests.versioning.probe import values as _values


def _tracking():
    """A build function that records the id of every version it composes."""
    prepare = api().compose.prepare_components(host_services())
    built = []

    def build(version):
        built.append(version.id)
        return prepare(version)

    return build, built


def _store(tmp_path, github, url=None):
    return api().store.GitVersionStore(tmp_path / "components.git", url if url is not None else github.url)


def _spy(monkeypatch, store, name):
    """Record the arguments of each call of a store method, still calling it."""
    calls = []
    original = getattr(store, name)

    async def spy(*args, **kwargs):
        calls.append(args)
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, name, spy)
    return calls


# ---- start


async def test_vg04_first_start_clones_github_and_adopts_a_main_it_can_load(github, tmp_path, repos):
    """VG04 (a): with no mirror, start clones GitHub, loads GitHub's main and adopts it as the mirror's main."""
    a = github.commit(_values(), "a")
    repo = await repos(_store(tmp_path, github))
    main = await repo.start()
    assert main.id == a == repo.main.id
    assert mirror_ref(tmp_path / "components.git", "refs/heads/main") == a
    assert (await _seen(repo))["a"] == 1


@pytest.mark.parametrize("remote", ["empty", "unloadable"])
async def test_vg04_first_start_aborts_without_a_loadable_github_main(github, tmp_path, repos, remote):
    """VG04 (b)(c): GitHub without a main, or with a main that does not load, aborts startup; the mirror gets no main and nothing stays loaded."""
    if remote == "unloadable":
        github.commit(_values(**BROKEN), "broken")
    repo = await repos(_store(tmp_path, github))
    with pytest.raises(Exception) as raised:
        await repo.start()
    if remote == "unloadable":
        assert "ZeroDivisionError" in repr(raised.value) or any(isinstance(item, ZeroDivisionError) for item in chain(raised.value))
    else:
        assert "main" in str(raised.value)
    assert mirror_ref(tmp_path / "components.git", "refs/heads/main") is None
    assert loaded_state(repos.view_root) == EMPTY_STATE


async def test_vg05_start_uses_the_mirror_main_without_reaching_github(github, tmp_path, repos):
    """VG05: when the mirror has a main, start loads it without the network: an unreachable GitHub URL does not matter."""
    a = github.commit(_values(), "a")
    first = await repos(_store(tmp_path, github))
    await first.start()
    await first.close()

    offline = await repos(_store(tmp_path, github, url=str(tmp_path / "nowhere.git")))
    assert (await offline.start()).id == a
    assert (await _seen(offline))["a"] == 1


# ---- sync


async def test_vg06_sync_adopts_a_new_github_main_for_new_runs(github, tmp_path, repos):
    """VG06: sync adopts GitHub's new main C; new runs use C while a running run finishes on B, which is then released. An unchanged GitHub main loads nothing."""
    b = github.commit(_values(), "b")
    build, built = _tracking()
    repo = await repos(_store(tmp_path, github), build=build)
    await repo.start()
    running = repo.run()
    held = running.agent("main")
    c = github.commit(_changed(a=2), "c")

    assert (await repo.sync()).id == c
    assert mirror_ref(tmp_path / "components.git", "refs/heads/main") == c
    seen = await _seen(repo)
    assert seen["a"] == 2 and c in seen["module"]
    assert running.agent("main") is held and namespace_entries(b)
    repo.release(running)
    assert namespace_entries(b) == []

    loads = list(built)
    assert (await repo.sync()).id == c
    assert built == loads


async def test_vg07_sync_rejects_a_github_main_it_cannot_load_and_reports_it_once(github, tmp_path, repos, caplog):
    """VG07: GitHub's main C that does not load is not adopted; the error about C is logged once over two syncs and runs stay on B. A later loadable D is adopted."""
    b = github.commit(_values(), "b")
    repo = await repos(_store(tmp_path, github))
    await repo.start()
    before = loaded_state(repos.view_root)
    c = github.commit(BROKEN, "c")
    caplog.set_level(logging.ERROR)

    for _ in range(2):
        assert (await repo.sync()).id == b
        assert mirror_ref(tmp_path / "components.git", "refs/heads/main") == b
        assert namespace_entries(c) == [] and loaded_state(repos.view_root) == before
    assert len([record for record in caplog.records if record.levelno >= logging.ERROR and c[:8] in record.getMessage()]) == 1
    assert (await _seen(repo))["a"] == 1

    d = github.commit({"tools/vl_dep.py": "VERSION = 1\n", "tools/boom.py": None, **_changed(a=4)}, "d")
    assert (await repo.sync()).id == d
    assert (await _seen(repo))["a"] == 4


async def test_vg08_a_failed_fetch_keeps_the_main_and_the_next_sync_retries(github, tmp_path, repos, caplog):
    """VG08: a sync that cannot reach GitHub logs a warning and keeps B; the next sync after GitHub is back adopts its main."""
    b = github.commit(_values(), "b")
    repo = await repos(_store(tmp_path, github))
    await repo.start()
    c = github.commit(_changed(a=3), "c")
    github.offline()
    caplog.set_level(logging.WARNING)

    assert (await repo.sync()).id == b
    assert [record.levelno for record in caplog.records if record.levelno >= logging.WARNING] == [logging.WARNING]
    github.online()
    assert (await repo.sync()).id == c
    assert (await _seen(repo))["a"] == 3


async def test_vg09_sync_adopts_a_force_pushed_main_it_can_load(github, tmp_path, repos):
    """VG09: GitHub is the source of truth: a main moved back to an older commit, or to a commit off the adopted history, is adopted once it loads."""
    a = github.commit(_values(), "a")
    repo = await repos(_store(tmp_path, github))
    await repo.start()
    b = github.commit(_changed(a=2), "b")
    assert (await repo.sync()).id == b

    github.force(a)
    assert (await repo.sync()).id == a
    assert (await _seen(repo))["a"] == 1

    git(github.clone, "reset", "-q", "--hard", a)
    d = github.commit(_changed(b=5), "d", push=False)
    git(github.clone, "push", "-q", "--force", "origin", "HEAD:refs/heads/main")
    assert (await repo.sync()).id == d
    seen = await _seen(repo)
    assert (seen["a"], seen["b"]) == (1, 5)


# ---- merge


async def test_vg10_merge_pushes_a_candidate_forked_from_github_main(github, tmp_path, repos, monkeypatch):
    """VG10: a candidate forked from GitHub's main is pushed as it is; GitHub's and the mirror's main become the candidate. A second merge pushes nothing and returns the main.

    The mirror has the Host's hooks as ``core.hooksPath``; the merge's own push does not run them (the merge has loaded the candidate itself).
    """
    a = github.commit(_values(), "a")
    host = tmp_path / "host"
    store = api().store.GitVersionStore(tmp_path / "components.git", github.url, hooks=stand_in_host(host))
    pushes = _spy(monkeypatch, store, "push")
    repo = await repos(store)
    await repo.start()
    b = await repo.fork(a, _values(a=2), "optimizer:errors main")

    assert await repo.merge(b.id) == b
    assert github.main() == b.id
    assert mirror_ref(tmp_path / "components.git", "refs/heads/main") == b.id
    assert mirror_ref(tmp_path / "components.git", f"refs/te/forks/{b.id}") == b.id
    assert pushes == [(b.id,)]
    assert (await _seen(repo))["a"] == 2

    assert await repo.merge(b.id) == b
    assert pushes == [(b.id,)] and github.main() == b.id
    assert calls(host) == []


async def test_vg11_merge_rebases_a_candidate_onto_a_main_the_person_moved(github, tmp_path, repos):
    """VG11: after the person pushed C (``tools/b.py``), merging the optimizer's B (``tools/a.py``, forked from A) pushes B' whose parent is C, which holds both changes and names B in a ``Rebased-From`` trailer."""
    a = github.commit(_values(), "a")
    repo = await repos(_store(tmp_path, github))
    await repo.start()
    b = await repo.fork(a, _values(a=2), "optimizer:errors main")
    c = github.commit(_changed(b=3), "person changes b")

    merged = await repo.merge(b.id)
    assert merged.id not in (a, b.id, c) and merged.parent == c
    assert merged.message == f"optimizer:errors main\n\nRebased-From: {b.id}"
    assert await repo.tree(merged.id) == _values(a=2, b=3)
    assert github.main() == merged.id == mirror_ref(tmp_path / "components.git", "refs/heads/main")
    assert mirror_ref(tmp_path / "components.git", f"refs/te/forks/{merged.id}") == merged.id
    seen = await _seen(repo)
    assert (seen["a"], seen["b"]) == (2, 3) and merged.id in seen["module"]
    # The person receives B' with the next pull.
    git(github.clone, "pull", "-q", "--ff-only", "origin", "main")
    assert github.head() == merged.id


async def test_vg12_merge_stops_on_a_conflict(github, tmp_path, repos):
    """VG12: when the optimizer's B and the person's C change the same line, merge raises MergeConflict naming the path; GitHub stays C, the mirror's main stays A and nothing is pushed."""
    modules = api()
    a = github.commit(_values(), "a")
    repo = await repos(_store(tmp_path, github))
    await repo.start()
    b = await repo.fork(a, _values(a=2), "optimizer")
    c = github.commit(_changed(a=3), "person")
    refs = mirror_refs(tmp_path / "components.git")

    with pytest.raises(modules.repo.MergeConflict) as raised:
        await repo.merge(b.id)
    assert "tools/a.py" in str(raised.value) and "tools/a.py" in raised.value.paths
    assert github.main() == c
    assert repo.main.id == a == mirror_ref(tmp_path / "components.git", "refs/heads/main")
    assert {name: value for name, value in mirror_refs(tmp_path / "components.git").items() if name.startswith("refs/te/")} == {
        name: value for name, value in refs.items() if name.startswith("refs/te/")
    }


async def test_vg13_merge_stops_when_the_rebased_version_does_not_load(github, tmp_path, repos):
    """VG13: when the person's C removes what the optimizer's B imports, the rebased B' fails to load with its path and line; nothing is pushed and the mirror's main stays A."""
    lib = {"tools/lib.py": "def value():\n    return 1\n"}
    a = github.commit(_values(**lib), "a")
    repo = await repos(_store(tmp_path, github))
    await repo.start()
    with_user = _values(**lib, **{"tools/user.py": "from trackeverything.tools.lib import value\n\nUSED = value()\n"})
    b = await repo.fork(a, with_user, "optimizer")
    c = github.commit({"tools/lib.py": "def amount():\n    return 1\n"}, "person renames value")

    with pytest.raises(ImportError) as raised:
        await repo.merge(b.id)
    assert "tools/user.py" in api().repo.describe_failure(raised.value)
    assert github.main() == c
    assert repo.main.id == a == mirror_ref(tmp_path / "components.git", "refs/heads/main")


async def test_vg14_merge_fails_when_another_push_lands_before_its_own(github, tmp_path, repos, monkeypatch):
    """VG14: a push that lands on GitHub after the merge fetched makes the merge's push rejected: PushRejected, the mirror's main stays A and GitHub keeps the other push."""
    modules = api()
    a = github.commit(_values(), "a")
    store = _store(tmp_path, github)
    repo = await repos(store)
    await repo.start()
    b = await repo.fork(a, _values(a=2), "optimizer")
    push = store.push
    raced = []

    async def racing(version_id):
        raced.append(github.commit(_changed(b=7), "person pushes first"))
        return await push(version_id)

    monkeypatch.setattr(store, "push", racing)
    with pytest.raises(modules.repo.PushRejected):
        await repo.merge(b.id)
    assert github.main() == raced[0]
    assert repo.main.id == a == mirror_ref(tmp_path / "components.git", "refs/heads/main")
    assert (await _seen(repo))["a"] == 1


async def test_vg15_merge_fails_when_github_is_unreachable(github, tmp_path, repos):
    """VG15: a merge that cannot reach GitHub raises the store's GitError; the mirror's main stays A and the fork stays kept."""
    a = github.commit(_values(), "a")
    repo = await repos(_store(tmp_path, github))
    await repo.start()
    b = await repo.fork(a, _values(a=2), "optimizer")
    github.offline()

    with pytest.raises(api().store.GitError):
        await repo.merge(b.id)
    github.online()
    assert github.main() == a
    assert repo.main.id == a == mirror_ref(tmp_path / "components.git", "refs/heads/main")
    assert mirror_ref(tmp_path / "components.git", f"refs/te/forks/{b.id}") == b.id
