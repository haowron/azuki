import asyncio
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from tests.versioning.gitrepo import GitHub
from tests.versioning.helpers import EMPTY_STATE, api, host_services, loaded_state


class Repos:
    """``await repos(store)`` opens ``Repo(store, prepare_components(host_services()), view_root=repos.view_root)``.

    ``build`` replaces the whole build function and ``services`` the HostServices. ``clock`` (a
    callable returning a UTC datetime) is passed as ``Repo(..., clock=clock)`` only when given;
    otherwise the Repo keeps its default clock. Every Repo is closed at teardown.
    """

    def __init__(self, view_root: Path) -> None:
        self.view_root = view_root
        self.created: list[Any] = []

    async def __call__(self, store: Any, *, build: Any = None, services: Any = None, clock: Any = None) -> Any:
        modules = api()
        prepare = build if build is not None else modules.compose.prepare_components(services if services is not None else host_services())
        options: dict[str, Any] = {"view_root": self.view_root}
        if clock is not None:
            options["clock"] = clock
        repo = modules.repo.Repo(store, prepare, **options)
        self.created.append(repo)
        return repo


@pytest.fixture
async def repos(tmp_path):
    """Open Repos over mocked stores; close them all and check that no version stays loaded."""
    factory = Repos(tmp_path / "views")
    yield factory
    for repo in reversed(factory.created):
        # A case may have closed its Repo already; a repeated close may be refused.
        with suppress(RuntimeError):
            await repo.close()
    assert loaded_state(factory.view_root) == EMPTY_STATE


#: The real process starters. The root conftest forbids processes; the git cases allow git alone.
_REAL_POPEN = subprocess.Popen
_REAL_EXEC = asyncio.create_subprocess_exec


def _program(args: Any) -> str:
    first = args[0] if isinstance(args, list | tuple) else args
    return Path(str(first)).name


@pytest.fixture
def isolated_git(tmp_path, monkeypatch):
    """Allow git (and only git) to run, reading no user or system configuration (no signing, hooks or URL rewrites)."""

    def popen(args, *rest, **options):
        if _program(args) != "git":
            pytest.fail(f"only git may start here, not {args!r}")
        return _REAL_POPEN(args, *rest, **options)

    async def create_subprocess_exec(program, *args, **options):
        if _program([program]) != "git":
            pytest.fail(f"only git may start here, not {program!r}")
        return await _REAL_EXEC(program, *args, **options)

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)
    config = tmp_path / "gitconfig"
    config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def github(tmp_path, isolated_git):
    """An empty stand-in for the component repository on GitHub, with a person's clone (``gitrepo.GitHub``)."""
    root = tmp_path / "git"
    root.mkdir()
    return GitHub(root)


@pytest.fixture
def git_store(tmp_path, github):
    """``GitVersionStore`` over the service's mirror ``tmp_path/components.git`` (not created yet) of ``github``."""
    return api().store.GitVersionStore(tmp_path / "components.git", github.url)
