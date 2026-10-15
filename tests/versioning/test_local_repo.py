"""The local component repository (no ``COMPONENT_REPO_URL``): VG19, VG20 and VG23-VG28.

The service's repository ``tmp_path/components.git`` is the only git hub. Its first main is a commit of the
package's five folders (``seed``, here a temporary package root). A person works in a clone of it
(``components/``, made by ``store.ensure_working_copy``) and pushes to it; the optimizer merges into it without
pushing anywhere. VG23 and VG24 open the repository with the hooks of a stand-in Host checkout
(``hooks.stand_in_host``) whose launcher records the check and fails a commit holding ``tools/BROKEN``.
"""

import logging
import os
from pathlib import Path

import pytest

from tests.versioning.gitrepo import git, mirror_ref, paths_of
from tests.versioning.helpers import EMPTY_STATE, api, loaded_state, minimal_tree
from tests.versioning.hooks import calls, stand_in_host
from tests.versioning.probe import BROKEN, chain, changed, seen, values


def _package(root, files):
    """Write ``files`` (package-relative path -> bytes) under a stand-in package root."""
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


def _store(tmp_path, seed=None, *, hooks=None):
    """The local ``GitVersionStore`` over ``tmp_path/components.git`` whose first main is ``seed`` (default: ``values()``)."""
    root = seed if seed is not None else _package(tmp_path / "package", values())
    return api().store.GitVersionStore(tmp_path / "components.git", "", hooks=hooks, seed=root)


def _hub(tmp_path):
    return tmp_path / "components.git"


def _config(repository, key):
    found = git(repository.parent, f"--git-dir={repository}", "config", "--get", key, check=False)
    return found.stdout.strip() if found.returncode == 0 else None


def _commit(copy, files, message):
    """The person writes ``files`` in the working copy and commits them; return the commit id."""
    for relative, content in files.items():
        path = copy / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(copy, "add", "-A")
    git(copy, "commit", "-q", "-m", message)
    return git(copy, "rev-parse", "HEAD").stdout.strip()


def _push(copy, *refspec):
    return git(copy, "push", "origin", *refspec, check=False)


async def _working_copy(tmp_path):
    copy = tmp_path / "components"
    assert await api().store.ensure_working_copy(_hub(tmp_path), copy) is True
    return copy


# ---- the first main


async def test_vg19_first_local_start_commits_the_package_components_as_the_first_main(isolated_git, tmp_path, repos):
    """VG19: with no repository and no URL, start creates the bare repository and makes a root commit ``Initial components``
    of the package's five folders (regular files only) its main; a restart uses that main and writes nothing."""
    files = minimal_tree(extra={"skills/s/SKILL.md": "# s\n"})
    extra = {"core/host.py": b"HOST = 1\n", "tools/__pycache__/x.cpython-314.pyc": b"\x00", "agent/.hidden": b"x\n"}
    root = _package(tmp_path / "package", {**files, **extra})
    os.symlink("../core/host.py", root / "tools" / "linked.py")
    hub = _hub(tmp_path)
    repo = await repos(_store(tmp_path, root))

    main = await repo.start()
    assert main.parent is None and main.message == "Initial components"
    assert mirror_ref(hub, "refs/heads/main") == main.id
    assert git(hub.parent, f"--git-dir={hub}", "rev-parse", "--is-bare-repository").stdout.strip() == "true"
    assert await repo.tree(main.id) == files
    assert paths_of(hub, main.id) == set(files)
    assert (await repo.refresh()).id == main.id and (await repo.sync()).id == main.id
    assert [commit.id for commit in await repo.log()] == [main.id]
    await repo.close()

    (root / "tools" / "vl_dep.py").write_text("VERSION = 2\n", encoding="utf-8")
    again = await repos(_store(tmp_path, root))
    assert (await again.start()).id == main.id
    assert [commit.id for commit in await again.log()] == [main.id]
    assert again.main_version.module("trackeverything.tools.vl_dep").VERSION == 1


async def test_vg20_a_package_that_does_not_load_aborts_the_first_local_start(isolated_git, tmp_path, repos):
    """VG20: when the package's five folders do not load, the first local start raises; the repository gets no main and nothing stays loaded."""
    repo = await repos(_store(tmp_path, _package(tmp_path / "package", values(**BROKEN))))
    with pytest.raises(Exception) as raised:
        await repo.start()
    assert any(isinstance(item, ZeroDivisionError) for item in chain(raised.value))
    assert mirror_ref(_hub(tmp_path), "refs/heads/main") is None
    assert loaded_state(repos.view_root) == EMPTY_STATE


# ---- a person's push


async def test_vg23_a_push_to_the_local_main_is_checked_and_new_runs_use_it(isolated_git, tmp_path, repos):
    """VG23: the repository's ``core.hooksPath`` is the Host's hooks; a loadable commit pushed from the working copy passes the Host's check,
    becomes main, and the next refresh runs it. A store opened by a Host elsewhere points the hooks there."""
    host = tmp_path / "host"
    hooks = stand_in_host(host)
    hub = _hub(tmp_path)
    repo = await repos(_store(tmp_path, hooks=hooks))
    a = (await repo.start()).id
    assert Path(_config(hub, "core.hooksPath")).resolve() == hooks.resolve()
    copy = await _working_copy(tmp_path)

    c = _commit(copy, changed(a=2), "person changes a")
    pushed = _push(copy, "HEAD:refs/heads/main")
    assert pushed.returncode == 0, pushed.stderr
    assert mirror_ref(hub, "refs/heads/main") == c != a
    [check] = calls(host)
    *command, repository, sha = check.split(" ")
    assert command == ["python", "-m", "trackeverything.core.versioning", "check", "--repo"]
    assert Path(repository).resolve() == hub.resolve() and sha == c
    assert (await repo.refresh()).id == c
    now = await seen(repo)
    assert now["a"] == 2 and c in now["module"]
    await repo.close()

    moved = stand_in_host(tmp_path / "moved")
    again = await repos(_store(tmp_path, hooks=moved))
    assert (await again.start()).id == c
    assert Path(_config(hub, "core.hooksPath")).resolve() == moved.resolve()


async def test_vg24_the_local_repository_refuses_a_main_that_does_not_load_and_its_deletion(isolated_git, tmp_path, repos):
    """VG24: a pushed main that does not load is refused with the check's error and main and the runs stay; deleting main is refused;
    a push to another branch is taken without a check."""
    host = tmp_path / "host"
    hub = _hub(tmp_path)
    repo = await repos(_store(tmp_path, hooks=stand_in_host(host)))
    a = (await repo.start()).id
    copy = await _working_copy(tmp_path)

    broken = _commit(copy, {"tools/BROKEN": "x\n"}, "broken")
    stopped = _push(copy, "HEAD:refs/heads/main")
    assert stopped.returncode != 0
    assert "tools/BROKEN:1" in stopped.stderr
    assert mirror_ref(hub, "refs/heads/main") == a
    assert (await repo.refresh()).id == a and (await seen(repo))["a"] == 1

    checked = len(calls(host))
    side = _push(copy, "HEAD:refs/heads/side")
    assert side.returncode == 0, side.stderr
    assert mirror_ref(hub, "refs/heads/side") == broken
    deleted = _push(copy, ":refs/heads/main")
    assert deleted.returncode != 0
    assert "main" in deleted.stderr
    assert mirror_ref(hub, "refs/heads/main") == a
    assert len(calls(host)) == checked


async def test_vg25_a_local_sync_fetches_nothing_and_adopts_a_pushed_main(isolated_git, tmp_path, repos, caplog):
    """VG25: with no URL, sync reaches no remote and logs no warning; a main the person pushed is adopted by the next sync."""
    hub = _hub(tmp_path)
    repo = await repos(_store(tmp_path))
    a = (await repo.start()).id
    caplog.set_level(logging.WARNING)

    assert (await repo.sync()).id == a
    copy = await _working_copy(tmp_path)
    c = _commit(copy, changed(b=4), "person changes b")
    pushed = _push(copy, "HEAD:refs/heads/main")
    assert pushed.returncode == 0, pushed.stderr
    assert (await repo.sync()).id == c
    assert (await seen(repo))["b"] == 4
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert git(hub.parent, f"--git-dir={hub}", "remote").stdout.split() == []
    assert mirror_ref(hub, "refs/remotes/origin/main") is None


# ---- the optimizer's merge


async def test_vg26_a_local_merge_moves_main_to_a_candidate_forked_from_it(isolated_git, tmp_path, repos):
    """VG26: a candidate forked from main becomes main without any push; the person gets it with the next pull; a second merge changes nothing."""
    hub = _hub(tmp_path)
    repo = await repos(_store(tmp_path))
    a = (await repo.start()).id
    copy = await _working_copy(tmp_path)
    b = await repo.fork(a, values(a=2), "optimizer:errors main")

    assert await repo.merge(b.id) == b
    assert mirror_ref(hub, "refs/heads/main") == b.id == repo.main.id
    assert mirror_ref(hub, "refs/remotes/origin/main") is None
    assert (await seen(repo))["a"] == 2
    git(copy, "pull", "-q", "--ff-only", "origin", "main")
    assert git(copy, "rev-parse", "HEAD").stdout.strip() == b.id

    assert await repo.merge(b.id) == b
    assert mirror_ref(hub, "refs/heads/main") == b.id


async def test_vg27_a_local_merge_rebases_onto_a_main_the_person_moved(isolated_git, tmp_path, repos):
    """VG27 (a): after the person pushed C (``tools/b.py``), merging the optimizer's B (``tools/a.py``, forked from A) makes B' on top of C
    with both changes and a ``Rebased-From`` trailer naming B; the person pulls B'."""
    hub = _hub(tmp_path)
    repo = await repos(_store(tmp_path))
    a = (await repo.start()).id
    copy = await _working_copy(tmp_path)
    b = await repo.fork(a, values(a=2), "optimizer:errors main")
    c = _commit(copy, changed(b=3), "person changes b")
    assert _push(copy, "HEAD:refs/heads/main").returncode == 0

    merged = await repo.merge(b.id)
    assert merged.id not in (a, b.id, c) and merged.parent == c
    assert merged.message == f"optimizer:errors main\n\nRebased-From: {b.id}"
    assert await repo.tree(merged.id) == values(a=2, b=3)
    assert mirror_ref(hub, "refs/heads/main") == merged.id == repo.main.id
    now = await seen(repo)
    assert (now["a"], now["b"]) == (2, 3)
    git(copy, "pull", "-q", "--ff-only", "origin", "main")
    assert git(copy, "rev-parse", "HEAD").stdout.strip() == merged.id


async def test_vg27_a_local_merge_stops_on_a_conflict(isolated_git, tmp_path, repos):
    """VG27 (b): when the optimizer's B and the person's C change the same line, merge raises MergeConflict naming the path and main stays C."""
    hub = _hub(tmp_path)
    repo = await repos(_store(tmp_path))
    a = (await repo.start()).id
    copy = await _working_copy(tmp_path)
    b = await repo.fork(a, values(a=2), "optimizer")
    c = _commit(copy, changed(a=3), "person changes a")
    assert _push(copy, "HEAD:refs/heads/main").returncode == 0

    with pytest.raises(api().repo.MergeConflict) as raised:
        await repo.merge(b.id)
    assert "tools/a.py" in raised.value.paths
    assert mirror_ref(hub, "refs/heads/main") == c
    assert repo.main.id == a


async def test_vg28_a_local_merge_fails_when_a_push_lands_before_its_update(isolated_git, tmp_path, repos, monkeypatch):
    """VG28: a push that lands after the merge read main makes the merge fail with PushRejected; main keeps the person's push and runs stay on A."""
    hub = _hub(tmp_path)
    store = _store(tmp_path)
    repo = await repos(store)
    a = (await repo.start()).id
    copy = await _working_copy(tmp_path)
    b = await repo.fork(a, values(a=2), "optimizer")
    fetch = store.fetch
    raced = []

    async def racing():
        read = await fetch()
        raced.append(_commit(copy, changed(b=7), "person pushes first"))
        assert _push(copy, "HEAD:refs/heads/main").returncode == 0
        return read

    monkeypatch.setattr(store, "fetch", racing)
    with pytest.raises(api().repo.PushRejected):
        await repo.merge(b.id)
    assert mirror_ref(hub, "refs/heads/main") == raced[0]
    assert repo.main.id == a
    assert (await seen(repo))["a"] == 1
