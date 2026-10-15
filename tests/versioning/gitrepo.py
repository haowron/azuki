"""Real git repositories for the git-backed version store cases (VG01-VG17, VR12).

``GitHub`` stands in for the component repository on GitHub: a bare repository on disk and a
person's clone of it. Nothing uses the network. The ``github`` fixture (``conftest.py``) isolates git
from the user's own configuration (signing, hooks, URL rewrites) before any git command runs.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

IDENTITY = {
    "GIT_AUTHOR_NAME": "Person",
    "GIT_AUTHOR_EMAIL": "person@example.invalid",
    "GIT_COMMITTER_NAME": "Person",
    "GIT_COMMITTER_EMAIL": "person@example.invalid",
}


def git(cwd: Path, *args: str, env: Mapping[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run git in ``cwd`` with a fixed identity; fail the case with git's stderr when ``check`` and git fails."""
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env={**os.environ, **IDENTITY, **(env or {})})
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}: {result.stderr}")
    return result


class GitHub:
    """A bare ``github.git`` (the remote) and ``person/``, a clone whose ``origin`` is that remote, on branch ``main``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.remote = root / "github.git"
        self.clone = root / "person"
        git(root, "init", "-q", "--bare", "--initial-branch=main", str(self.remote))
        git(root, "clone", "-q", str(self.remote), str(self.clone))
        git(self.clone, "checkout", "-q", "-B", "main")

    @property
    def url(self) -> str:
        return str(self.remote)

    def write(self, files: Mapping[str, str | bytes | None]) -> None:
        """Change the clone's working tree: a ``str``/``bytes`` value writes a file, ``None`` removes it."""
        for relative, content in files.items():
            path = self.clone / relative
            if content is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, encoding="utf-8")

    def commit(self, files: Mapping[str, str | bytes | None] = {}, message: str = "person", *, push: bool = True, at: int | None = None) -> str:
        """Write ``files``, commit everything in the clone (committer time ``at`` in Unix seconds when given) and push it to ``main``."""
        self.write(files)
        git(self.clone, "add", "-A")
        dated = {"GIT_COMMITTER_DATE": f"{at} +0000", "GIT_AUTHOR_DATE": f"{at} +0000"} if at is not None else None
        git(self.clone, "commit", "-q", "--allow-empty", "-m", message, env=dated)
        sha = self.head()
        if push:
            git(self.clone, "push", "-q", "origin", "HEAD:refs/heads/main")
        return sha

    def head(self) -> str:
        return git(self.clone, "rev-parse", "HEAD").stdout.strip()

    def main(self) -> str | None:
        """GitHub's main, None when it has none."""
        found = git(self.remote, "rev-parse", "-q", "--verify", "refs/heads/main", check=False)
        return found.stdout.strip() if found.returncode == 0 else None

    def force(self, sha: str) -> None:
        """Move GitHub's main to ``sha`` as a force push would."""
        git(self.remote, "update-ref", "refs/heads/main", sha)

    def offline(self) -> Path:
        """Make GitHub unreachable by moving the remote away; return where it went (``online`` puts it back)."""
        away = self.root / "github-away.git"
        self.remote.rename(away)
        return away

    def online(self) -> None:
        (self.root / "github-away.git").rename(self.remote)


def mirror_ref(mirror: Path, ref: str) -> str | None:
    """A ref of the service's mirror, None when it is missing."""
    found = git(mirror.parent, f"--git-dir={mirror}", "rev-parse", "-q", "--verify", ref, check=False)
    return found.stdout.strip() if found.returncode == 0 else None


def mirror_refs(mirror: Path) -> dict[str, str]:
    """Every ref of the mirror (name -> id)."""
    listed = git(mirror.parent, f"--git-dir={mirror}", "for-each-ref", "--format=%(refname) %(objectname)").stdout
    return dict(line.split(" ", 1) for line in listed.splitlines() if line)


def paths_of(repository: Path, version: str) -> set[str]:
    """Every path in a commit of a repository (bare or clone), including those outside the five folders."""
    listed = git(repository, "ls-tree", "-r", "--name-only", "--full-tree", version).stdout
    return set(listed.splitlines())
