"""The check CLI (``python -m trackeverything.core.versioning check``): VG16.

The entry function runs in-process against a person's real git clone (``gitrepo.GitHub``). The schema
read and the Host-service factories the runtime also uses are mocked at their definitions and at every
module that bound them. The CLI never opens the service's mirror, the queue or a Runtime.
"""

import importlib
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import trackeverything.config as config
from tests.fakes import ScriptedLLM
from tests.versioning.helpers import EMPTY_STATE, ROOT, compose_source, loaded_state, minimal_tree
from trackeverything.core.builtin import database
from trackeverything.core.builtin.execution_log import PostgresExecutionLogger
from trackeverything.core.builtin.runtime import Runtime
from trackeverything.core.builtin.session_store import PostgresSessionStore
from trackeverything.core.orm import connection
from trackeverything.llm import compose as llm_compose
from trackeverything.queue.producer import Producer

DSN = "postgresql://vg16@127.0.0.1:5432/vg16"
HELPER = '''
from trackeverything.core.abc.agent import BaseAgent


class Helper(BaseAgent):
    name = "helper"
    description = "declares an LLM name the Host does not provide"
    llm = "turbo"
'''


def _replace(monkeypatch, original, replacement):
    """Replace ``original`` in every loaded ``trackeverything`` module that defines or imported it."""
    patched = 0
    for name, module in list(sys.modules.items()):
        if module is None or not (name == "trackeverything" or name.startswith("trackeverything.")):
            continue
        for attribute, value in list(vars(module).items()):
            if value is original:
                monkeypatch.setattr(module, attribute, replacement)
                patched += 1
    assert patched, f"{original!r} is not bound in any module"


def _version_modules():
    return sorted(name for name in list(sys.modules) if name.startswith(f"{ROOT}."))


def _cli(tmp_path, monkeypatch):
    """Wire the CLI to mocked Host-service factories; return what it touches."""
    cli = importlib.import_module("trackeverything.core.versioning.__main__")
    importlib.import_module("trackeverything.core.builtin.app")
    monkeypatch.setattr(config, "DATABASE_URL", DSN)
    mirror = tmp_path / "components.git"
    monkeypatch.setattr(config, "COMPONENT_REPO_DIR", str(mirror))
    revision = AsyncMock(return_value=connection.schema_head())
    monkeypatch.setattr(connection, "_current_revision", revision)
    llms = {name: ScriptedLLM() for name in ("max", "high", "low")}
    factories = SimpleNamespace(
        logger=Mock(return_value=AsyncMock(spec=PostgresExecutionLogger)),
        llms=Mock(return_value=llms),
        sessions=Mock(return_value=AsyncMock(spec=PostgresSessionStore)),
    )
    for original, replacement in (
        (database.open_execution_logger, factories.logger),
        (llm_compose.build_llms, factories.llms),
        (database.open_session_store, factories.sessions),
    ):
        _replace(monkeypatch, original, replacement)
    runtime_init = Mock(side_effect=AssertionError("the check CLI built a Runtime"))
    monkeypatch.setattr(Runtime, "__init__", runtime_init)
    from_env = Mock(side_effect=AssertionError("the check CLI opened the queue producer"))
    monkeypatch.setattr(Producer, "from_env", from_env)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    return SimpleNamespace(main=cli.main, llms=llms, revision=revision, runtime_init=runtime_init, from_env=from_env,
                           scratch=scratch, mirror=mirror, modules_before=_version_modules())


def _invoke(env, capsys, *argv):
    """Run the CLI; check it left no version loaded, no file view and no mirror behind; return (code, stdout, stderr)."""
    code = env.main(list(argv))
    out, err = capsys.readouterr()
    assert loaded_state(env.scratch) == {**EMPTY_STATE, "modules": env.modules_before}
    assert _version_modules() == env.modules_before
    assert not env.mirror.exists()
    assert not env.runtime_init.called and not env.from_env.called
    return code, out, err


def test_vg16_check_passes_a_commit_the_host_can_load(github, tmp_path, monkeypatch, capsys):
    """VG16 (a): a loadable commit prints ``ok <SHA>`` and exits 0, after the schema check and with the runtime's Host services."""
    env = _cli(tmp_path, monkeypatch)
    sha = github.commit(minimal_tree(extra={"tools/a.py": "A = 1\n"}), "loadable")
    code, out, err = _invoke(env, capsys, "check", "--repo", str(github.clone), sha)
    assert code == 0, err
    assert out.splitlines() == [f"ok {sha}"]
    env.revision.assert_awaited_once_with(DSN)
    assert all(llm.requests == [] for llm in env.llms.values())


@pytest.mark.parametrize(
    ("files", "pattern"),
    [
        pytest.param(
            minimal_tree(extra={"tools/a.py": "A = 2\n", "tools/b.py": "import trackeverything.tools.a\nRESULT = 1 / 0\n"}),
            r"tools/b\.py\W+(line\s+)?2\b.*ZeroDivisionError",
            id="b-module-error",
        ),
        pytest.param(
            minimal_tree(extra={"agent/helper.py": HELPER}, compose=compose_source(["agent.main.Entry", "agent.helper.Helper"], ["main"])),
            r"ValueError.*turbo",
            id="c-unknown-llm",
        ),
    ],
)
def test_vg16_check_reports_what_fails_to_load(github, tmp_path, monkeypatch, capsys, files, pattern):
    """VG16 (b)(c): a module that raises while loading, or an Agent asking for an LLM the Host lacks, fails with the path and line or the error on stderr and exit 1."""
    env = _cli(tmp_path, monkeypatch)
    sha = github.commit(files, "broken")
    code, out, err = _invoke(env, capsys, "check", "--repo", str(github.clone), sha)
    assert code == 1
    assert re.search(pattern, err), err
    assert "ok" not in out.split()


def test_vg16_check_reports_an_unknown_revision(github, tmp_path, monkeypatch, capsys):
    """VG16 (d): a revision the repository does not have exits 1 with one line naming it."""
    env = _cli(tmp_path, monkeypatch)
    github.commit(minimal_tree(), "a")
    code, out, err = _invoke(env, capsys, "check", "--repo", str(github.clone), "0" * 40)
    assert code == 1 and out == ""
    assert len(err.strip().splitlines()) == 1 and "0" * 40 in err


def test_vg16_check_reports_a_setup_failure_on_one_line(github, tmp_path, monkeypatch, capsys):
    """VG16 (e): a database below the schema head fails the check with one stderr line and loads nothing."""
    env = _cli(tmp_path, monkeypatch)
    env.revision.return_value = "0000older000"
    sha = github.commit(minimal_tree(), "a")
    code, out, err = _invoke(env, capsys, "check", "--repo", str(github.clone), sha)
    assert code == 1 and out == ""
    assert len(err.strip().splitlines()) == 1 and "Traceback" not in err


def test_vg16_check_defaults_to_head_of_the_current_directory(github, tmp_path, monkeypatch, capsys):
    """VG16 (f): without ``--repo`` and a revision, the CLI checks HEAD of the repository in the current directory."""
    env = _cli(tmp_path, monkeypatch)
    sha = github.commit(minimal_tree(), "a")
    monkeypatch.chdir(github.clone / "tools")
    code, out, err = _invoke(env, capsys, "check")
    assert code == 0, err
    assert out.splitlines() == [f"ok {sha}"]
    assert Path.cwd() == github.clone / "tools"
