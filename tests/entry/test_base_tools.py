"""BT01-BT20: Host が所有し BaseAgent が内蔵する基本 Tool(read・write・edit・bash)と、全ての Agent が共有する基礎の補助。

file 操作は一時領域の実ファイル、bash は実際の ``/bin/bash`` で確かめる(他の process の起動は引き続き拒否する)。
"""

import ast
import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Any, ClassVar

import pytest

import trackeverything.config as config
from tests.fakes import ScriptedLLM
from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.tool import Tool, ToolResult
from trackeverything.core.builtin import base_tools
from trackeverything.core.builtin.base_tools import (
    CHUNK_CHARS,
    OUTPUT_CHARS,
    READ_CHARS,
    READ_LINES,
    BashTool,
    EditTool,
    ReadTool,
    WriteTool,
    bounded_int,
    run_bounded,
    run_captured,
    text_window,
)

#: 本物の shell 起動。root conftest は process の起動を拒否するため、bash だけを通す。
_REAL_SHELL = asyncio.create_subprocess_shell
_REAL_EXEC = asyncio.create_subprocess_exec
_REAL_POPEN = subprocess.Popen


@pytest.fixture
def real_bash(monkeypatch):
    async def bash_only(cmd: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        if kwargs.get("executable") != "/bin/bash":
            pytest.fail("基本 Tool の bash 以外の shell 起動")
        return await _REAL_SHELL(cmd, **kwargs)

    async def exec_bash_only(program: Any, *args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        if program != "/bin/bash":
            pytest.fail("bash 以外の process 起動")
        return await _REAL_EXEC(program, *args, **kwargs)

    def popen_bash_only(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        argv = args[0] if args else kwargs.get("args")
        first = argv[0] if isinstance(argv, list | tuple) else None
        if kwargs.get("executable") != "/bin/bash" and first != "/bin/bash":
            pytest.fail("基本 Tool の bash 以外の process 起動")
        return _REAL_POPEN(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_shell", bash_only)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_bash_only)
    monkeypatch.setattr(subprocess, "Popen", popen_bash_only)


class Probe(BaseAgent):
    name: ClassVar[str] = "probe"


class OtherProbe(BaseAgent):
    name: ClassVar[str] = "other_probe"


def tools_of(agent: BaseAgent) -> dict[str, Tool]:
    return {tool.name: tool for tool in agent.list_tools()}


def workspace() -> Path:
    root = Path(config.REPOS_DIR)
    root.mkdir(parents=True, exist_ok=True)
    return root


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_gone(pid: int, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


# ---- BaseAgent への内蔵


def test_bt01_every_base_agent_has_the_shared_base_tools():
    first, second = tools_of(Probe(ScriptedLLM())), tools_of(OtherProbe(ScriptedLLM()))
    assert {"read", "write", "edit", "bash"} <= set(first)
    for name in ("read", "write", "edit", "bash"):
        assert first[name] is second[name]


def test_bt02_a_tool_with_a_base_tool_name_fails_composition():
    class Clash(Tool):
        name: ClassVar[str] = "read"
        description: ClassVar[str] = "memory read"
        arguments: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

        async def run(self, args: dict[str, Any], *, ctx: Any = None) -> ToolResult:
            return ToolResult(ok=True)

    class Clashing(BaseAgent):
        name: ClassVar[str] = "clashing"
        tools: ClassVar[tuple[type[Tool], ...]] = (Clash,)

    with pytest.raises(ValueError, match="tool already registered: read"):
        Clashing(ScriptedLLM())
    with pytest.raises(ValueError, match="tool already registered: bash"):
        Probe(ScriptedLLM()).register(BashTool())


# ---- read


async def test_bt03_read_returns_the_requested_lines_and_the_next_offset():
    path = workspace() / "lines.txt"
    path.write_text("".join(f"line {n}\n" for n in range(1, 11)), encoding="utf-8")
    middle = await ReadTool().run({"path": str(path), "offset": 3, "limit": 4})
    assert middle.ok
    assert middle.output["content"] == "line 3\nline 4\nline 5\nline 6\n"
    assert middle.output["total_lines"] == 10
    assert middle.output["next_offset"] == 7
    tail = await ReadTool().run({"path": str(path), "offset": 9})
    assert tail.output["content"] == "line 9\nline 10\n"
    assert tail.output["next_offset"] is None


async def test_bt04_read_caps_a_large_file_by_lines_and_characters():
    many = workspace() / "many.txt"
    many.write_text("x\n" * (READ_LINES + 500), encoding="utf-8")
    by_lines = await ReadTool().run({"path": "many.txt"})
    assert by_lines.output["content"].count("\n") == READ_LINES
    assert by_lines.output["next_offset"] == READ_LINES + 1

    wide = workspace() / "wide.txt"
    line = "y" * 99 + "\n"
    wide.write_text(line * 1000, encoding="utf-8")
    by_chars = await ReadTool().run({"path": "wide.txt"})
    content = by_chars.output["content"]
    assert len(content) <= READ_CHARS
    assert by_chars.output["next_offset"] == content.count("\n") + 1


async def test_bt05_read_failures_become_tool_results():
    binary = workspace() / "binary.bin"
    binary.write_bytes(b"\xff\xfe\x00bad")
    for args in ({"path": str(binary)}, {"path": str(workspace() / "missing.txt")}, {"path": ""}):
        result = await ReadTool().run(args)
        assert not result.ok and result.error


async def test_bt06_paths_resolve_like_the_bash_working_directory(real_bash, monkeypatch, tmp_path):
    relative = await WriteTool().run({"path": "nested/rel.txt", "content": "r"})
    assert Path(relative.output["path"]) == workspace() / "nested" / "rel.txt"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    tilde = await WriteTool().run({"path": "~/t.txt", "content": "t"})
    assert Path(tilde.output["path"]) == home / "t.txt"
    absolute = tmp_path / "abs.txt"
    assert Path((await WriteTool().run({"path": str(absolute), "content": "a"})).output["path"]) == absolute
    where = await BashTool().run({"command": "pwd -P"})
    assert where.ok
    assert where.output["output"].strip() == str(workspace().resolve())


# ---- write


async def test_bt07_write_creates_the_file_and_its_parents_without_newline_conversion():
    result = await WriteTool().run({"path": "a/b/c.txt", "content": "one\r\ntwo"})
    assert result.ok and result.output["created"] is True
    assert (workspace() / "a" / "b" / "c.txt").read_bytes() == b"one\r\ntwo"


async def test_bt08_write_replaces_an_existing_file():
    path = workspace() / "exists.txt"
    path.write_text("old content", encoding="utf-8")
    result = await WriteTool().run({"path": str(path), "content": "new"})
    assert result.ok and result.output["created"] is False
    assert path.read_text(encoding="utf-8") == "new"


# ---- edit


async def test_bt09_edit_replaces_a_unique_match():
    path = workspace() / "edit.txt"
    path.write_text("alpha beta gamma", encoding="utf-8")
    result = await EditTool().run({"path": str(path), "old_text": "beta", "new_text": "BETA"})
    assert result.ok
    assert path.read_text(encoding="utf-8") == "alpha BETA gamma"


async def test_bt10_edit_without_exactly_one_match_changes_nothing():
    path = workspace() / "same.txt"
    path.write_text("dup dup", encoding="utf-8")
    for old in ("missing", "dup"):
        result = await EditTool().run({"path": str(path), "old_text": old, "new_text": "x"})
        assert not result.ok and "match" in (result.error or "")
    assert path.read_text(encoding="utf-8") == "dup dup"


async def test_bt11_edit_rejects_empty_or_unchanged_text():
    path = workspace() / "valid.txt"
    path.write_text("keep", encoding="utf-8")
    for args in ({"old_text": "", "new_text": "x"}, {"old_text": "keep", "new_text": "keep"}):
        result = await EditTool().run({"path": str(path), **args})
        assert not result.ok
    assert path.read_text(encoding="utf-8") == "keep"


# ---- bash


async def test_bt12_bash_returns_combined_output_and_the_exit_code(real_bash, monkeypatch):
    monkeypatch.setenv("TE_BASE_TOOL_PROBE", "inherited")
    ok = await BashTool().run({"command": "echo out $TE_BASE_TOOL_PROBE; echo err 1>&2"})
    assert ok.ok and ok.output["exit_code"] == 0
    assert "out inherited" in ok.output["output"] and "err" in ok.output["output"]
    failed = await BashTool().run({"command": "echo partial; exit 3"})
    assert not failed.ok
    assert failed.output["exit_code"] == 3 and "partial" in failed.output["output"]


async def test_bt13_bash_timeout_stops_the_whole_process_group(real_bash):
    pidfile = workspace() / "child.pid"
    result = await BashTool().run({"command": f"sleep 30 & echo $! > {pidfile}; wait", "timeout": 1})
    assert not result.ok and "timed out" in (result.error or "")
    assert wait_gone(int(pidfile.read_text().strip()))


async def test_bt14_bash_keeps_only_the_tail_of_large_output(real_bash):
    result = await BashTool().run({"command": f"head -c {OUTPUT_CHARS + 10000} /dev/zero | tr '\\0' 'a'; echo END"})
    assert result.ok
    output = result.output["output"]
    assert len(output) == OUTPUT_CHARS and output.endswith("END\n")


async def test_bt15_cancelling_bash_stops_the_process_group_and_propagates(real_bash):
    pidfile = workspace() / "cancel.pid"
    task = asyncio.create_task(BashTool().run({"command": f"sleep 30 & echo $! > {pidfile}; wait", "timeout": 60}))
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert wait_gone(int(pidfile.read_text().strip()))


# ---- 全ての Agent が共有する基礎の補助


async def test_bt17_run_captured_returns_streams_and_reaps_on_timeout(real_bash):
    code, out, err = await run_captured(["/bin/bash", "-c", "echo out; echo err 1>&2; exit 4"], timeout=10)
    assert (code, out, err) == (4, b"out\n", b"err\n")
    pidfile = workspace() / "captured.pid"
    with pytest.raises(TimeoutError):
        await run_captured(["/bin/bash", "-c", f"sleep 30 & echo $! > {pidfile}; wait"], timeout=1)
    assert wait_gone(int(pidfile.read_text().strip()))


async def test_bt18_run_bounded_reports_the_output_limit(real_bash):
    result = await run_bounded(["/bin/bash", "-c", "head -c 5000 /dev/zero"], workspace(), time.monotonic() + 10,
                               env=dict(os.environ), output_limit=1000)
    assert result.error == "output_limit"
    assert len(result.stdout) == 1000


async def test_bt19_run_bounded_stops_the_group_at_the_deadline(real_bash):
    pidfile = workspace() / "bounded.pid"
    result = await run_bounded(["/bin/bash", "-c", f"sleep 30 & echo $! > {pidfile}; wait"], workspace(), time.monotonic() + 1,
                               env=dict(os.environ))
    assert result.error == "timeout"
    assert wait_gone(int(pidfile.read_text().strip()))
    expired = await run_bounded(["/bin/bash", "-c", "true"], workspace(), time.monotonic() - 1, env=dict(os.environ))
    assert expired.error == "timeout" and expired.exit_code == -1


def test_bt20_text_window_and_bounded_int():
    text = "z" * (CHUNK_CHARS + 10)
    first = text_window(text)
    assert len(first["text"]) == CHUNK_CHARS and first["next_offset"] == CHUNK_CHARS and first["total_chars"] == len(text)
    rest = text_window(text, CHUNK_CHARS)
    assert rest["text"] == "z" * 10 and rest["next_offset"] is None
    with pytest.raises(ValueError):
        text_window(text, len(text) + 1)
    assert bounded_int(None, "n", default=5, low=1) == 5
    assert bounded_int(3, "n", default=5, low=1, high=10) == 3
    for bad in (0, 11, True, "3"):
        with pytest.raises(ValueError):
            bounded_int(bad, "n", default=5, low=1, high=10)


# ---- Host の境界


def test_bt16_host_base_tools_import_no_component_modules():
    tree = ast.parse(Path(base_tools.__file__).read_text(encoding="utf-8"))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    components = ("agent", "tools", "skills", "harnesses", "sources")
    assert not [name for name in imported if name.split(".")[:2] in (["trackeverything", part] for part in components)]
