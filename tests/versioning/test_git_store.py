"""The git version store over real git: VG01-VG03, VR12.

GitHub is a bare repository on disk (``gitrepo.GitHub``) and the store keeps its mirror in
``tmp_path/components.git``. Commits that the person makes carry fixed committer times so that
their order and ``created_at`` are known.
"""

import os
import re
from datetime import UTC, datetime, timedelta

import pytest

from tests.versioning.gitrepo import git, mirror_ref, mirror_refs, paths_of
from tests.versioning.helpers import minimal_tree, namespace_entries

T0 = 1_791_878_400  # 2026-10-14T00:00:00Z
ROOT_FILES = {"README.md": "# components\n", ".githooks/pre-push": "#!/bin/sh\nexit 0\n"}


def _at(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


async def test_vg01_reading_takes_regular_files_of_the_five_folders_first_parents_and_reachable_versions(github, git_store, tmp_path):
    """VG01: a version's tree is the regular files under the five folders (no root files, symlinks or caches); a merge commit's parent is its first parent; ``list_commits`` returns the versions reachable from main and the forks, newest first."""
    files = minimal_tree(extra={"skills/s/run.sh": b"#!/bin/sh\necho run\n", "skills/s/SKILL.md": "# s\n"})
    first = github.commit({**files, **ROOT_FILES}, "first", at=T0)
    (github.clone / "skills/s/run.sh").chmod(0o755)
    os.symlink("../README.md", github.clone / "tools/readme_link.md")
    github.write({"tools/__pycache__/vl_dep.cpython-314.pyc": b"\x00cache", "tools/.hidden": "hidden\n"})
    git(github.clone, "add", "-A", "--force")
    git(github.clone, "commit", "-q", "-m", "links and caches", env={"GIT_COMMITTER_DATE": f"{T0 + 10} +0000"})
    second = github.head()
    git(github.clone, "checkout", "-q", "-b", "side", first)
    side = github.commit({"tools/side.py": "SIDE = 1\n"}, "side", push=False, at=T0 + 20)
    git(github.clone, "checkout", "-q", "main")
    git(github.clone, "merge", "-q", "--no-ff", "-m", "merge side", side, env={"GIT_COMMITTER_DATE": f"{T0 + 30} +0000"})
    git(github.clone, "push", "-q", "origin", "HEAD:refs/heads/main")
    merged = github.head()

    assert await git_store.read_main() is None
    assert await git_store.fetch() == merged
    assert mirror_ref(tmp_path / "components.git", "refs/remotes/origin/main") == merged
    expected = {**files, "tools/side.py": b"SIDE = 1\n"}
    assert await git_store.read_tree(merged) == expected
    assert {"README.md", ".githooks/pre-push", "tools/readme_link.md"} <= paths_of(github.clone, merged)

    commit = await git_store.read_commit(merged)
    assert commit.id == merged and commit.parent == second and commit.message == "merge side"
    assert commit.created_at == _at(T0 + 30)
    assert (await git_store.read_commit(first)).parent is None
    assert (await git_store.read_commit(first)).created_at == _at(T0)
    for unknown in ("0" * 40, merged[:12], "main", "not hex"):
        assert await git_store.read_commit(unknown) is None
        assert await git_store.read_tree(unknown) is None

    # Nothing is reachable before the mirror has a main.
    assert await git_store.list_commits() == []
    assert await git_store.move_main(None, second)
    fork = await git_store.write(expected, second, "fork", _at(T0 + 40))
    loose = await git_store.write({**expected, "tools/loose.py": b"LOOSE = 1\n"}, second, "loose", _at(T0 + 50))
    await git_store.keep(fork.id)
    listed = await git_store.list_commits()
    assert [item.id for item in listed] == [fork.id, second, first]
    assert loose.id not in {item.id for item in listed}
    assert [item.created_at for item in listed] == [_at(T0 + 40), _at(T0 + 10), _at(T0)]


async def test_vg02_move_main_compares_before_it_swaps(github, git_store, tmp_path):
    """VG02: ``move_main`` changes the mirror's main only from the expected id (None means no main yet)."""
    a = github.commit(minimal_tree(), "a", at=T0)
    b = github.commit({"tools/vl_dep.py": "VERSION = 2\n"}, "b", at=T0 + 1)
    c = github.commit({"tools/vl_dep.py": "VERSION = 3\n"}, "c", at=T0 + 2)
    await git_store.fetch()

    assert await git_store.move_main(None, a) is True
    assert await git_store.move_main(None, b) is False
    assert await git_store.read_main() == a
    assert await git_store.move_main(a, b) is True
    assert await git_store.move_main(a, c) is False
    assert await git_store.read_main() == b == mirror_ref(tmp_path / "components.git", "refs/heads/main")
    # Moving the mirror never moves GitHub.
    assert github.main() == c


async def test_vg03_write_returns_the_git_id_creates_no_ref_and_keeps_the_root_files(github, git_store, tmp_path, repos):
    """VG03: ``write`` returns the 40-hex id git gives the commit, creates no ref and keeps the parent's files outside the five folders; the same commit again is reported as already stored, and a fork in the same second moves its time one second on."""
    mirror = tmp_path / "components.git"
    a = github.commit({**minimal_tree(), **ROOT_FILES}, "a", at=T0)
    await git_store.fetch()
    assert await git_store.move_main(None, a)
    refs = mirror_refs(mirror)
    files = minimal_tree(extra={"tools/vl_dep.py": "VERSION = 2\n"})
    when = _at(T0 + 100) + timedelta(microseconds=999)

    written = await git_store.write(files, a, "two", when)
    assert re.fullmatch(r"[0-9a-f]{40}", written.id)
    assert git(mirror.parent, f"--git-dir={mirror}", "rev-parse", f"{written.id}^{{commit}}").stdout.strip() == written.id
    assert (written.parent, written.message, written.created_at) == (a, "two", _at(T0 + 100))
    assert mirror_refs(mirror) == refs
    assert await git_store.read_tree(written.id) == files
    assert {"README.md", ".githooks/pre-push"} <= paths_of(mirror, written.id)
    assert await git_store.write(files, a, "two", when) is None

    # The Repo moves a fork's time one second on while its id is already stored.
    repo = await repos(git_store, clock=lambda: _at(T0 + 200))
    await repo.start()
    first = await repo.fork(a, files, "same")
    again = await repo.fork(a, files, "same")
    assert first.id != again.id
    assert (first.created_at, again.created_at) == (_at(T0 + 200), _at(T0 + 201))
    assert {mirror_ref(mirror, f"refs/te/forks/{first.id}"), mirror_ref(mirror, f"refs/te/forks/{again.id}")} == {first.id, again.id}
    assert github.main() == a


async def test_vr12_reading_skips_links_serves_big_resources_and_rejects_a_non_utf8_module(github, git_store, tmp_path, repos):
    """VR12: a symlink in the version tree is not part of the version, a 2 MiB resource is read from the version's file view, and a fork with a non-UTF-8 module fails with its path and keeps nothing."""
    big = bytes(range(256)) * 8192
    github.write({**minimal_tree(), "skills/big/data.bin": big})
    os.symlink("../../etc/passwd", github.clone / "tools/outside")
    os.symlink("vl_dep.py", github.clone / "tools/inside.py")
    a = github.commit({}, "a", at=T0)
    repo = await repos(git_store)
    await repo.start()

    version = repo.main_version
    assert "tools/outside" not in version.tree and "tools/inside.py" not in version.tree
    assert not version.view_path("tools/outside").exists() and not version.view_path("tools/inside.py").exists()
    assert version.view_path("skills/big/data.bin").read_bytes() == big
    refs = mirror_refs(tmp_path / "components.git")

    latin = minimal_tree(extra={"tools/latin.py": b"VALUE = '\xe9'\n"})
    with pytest.raises(ValueError, match=r"tools/latin\.py"):
        await repo.fork(a, latin, "latin")
    assert mirror_refs(tmp_path / "components.git") == refs
    assert repo.main.id == a and namespace_entries(a)
