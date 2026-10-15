"""The component repository's pre-push check (``scripts/component-hooks/pre-push``): VG17.

A real ``git push`` runs from the person's clone with ``core.hooksPath`` set to the hooks directory of a
stand-in Host checkout (``hooks.stand_in_host``). The hook finds that Host from its own location, so no
``TE_HOME`` is set; the Host's launcher records the check and fails a commit containing ``tools/BROKEN``.
"""

import os
import subprocess

from tests.versioning.gitrepo import IDENTITY, git
from tests.versioning.helpers import minimal_tree
from tests.versioning.hooks import calls, stand_in_host


def _wire(github, tmp_path):
    """Point the clone's ``core.hooksPath`` at a stand-in Host's hooks; return that Host's root."""
    home = tmp_path / "te-home"
    git(github.clone, "config", "core.hooksPath", str(stand_in_host(home)))
    return home


def _push(github, *refspec):
    environment = {key: value for key, value in os.environ.items() if key != "TE_HOME"}
    return subprocess.run(["git", "push", "origin", *refspec], cwd=github.clone, capture_output=True, text=True,
                          env={**environment, **IDENTITY})


def test_vg17_pre_push_checks_main_and_stops_a_broken_push(github, tmp_path):
    """VG17: a push to main runs the check through the launcher of the Host that holds the hook and goes through when it passes;
    a broken main stops the push with the check's error and GitHub keeps its main; other branches and deletions are not checked."""
    first = github.commit(minimal_tree(), "first")
    home = _wire(github, tmp_path)

    good = github.commit({"tools/vl_dep.py": "VERSION = 2\n"}, "good", push=False)
    pushed = _push(github, "HEAD:refs/heads/main")
    assert pushed.returncode == 0, pushed.stderr
    assert github.main() == good
    assert calls(home) == [f"python -m trackeverything.core.versioning check --repo {github.clone} {good}"]

    broken = github.commit({"tools/BROKEN": "x\n"}, "broken", push=False)
    stopped = _push(github, "HEAD:refs/heads/main")
    assert stopped.returncode != 0
    assert "tools/BROKEN:1" in stopped.stderr
    assert github.main() == good
    assert calls(home)[-1].endswith(broken)

    checked = len(calls(home))
    side = _push(github, "HEAD:refs/heads/side")
    assert side.returncode == 0, side.stderr
    removed = _push(github, ":refs/heads/side")
    assert removed.returncode == 0, removed.stderr
    assert len(calls(home)) == checked
    assert github.main() == good != first
