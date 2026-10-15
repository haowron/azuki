"""Host が所有する基本 Tool。pi coding agent と同じ ``read``・``write``・``edit``・``bash`` の 4 つ。

``BaseAgent`` が全ての実例に内蔵し(プロセスで名前ごとに一つの共有インスタンス)、部品は同じ名前の Tool を持てない。
版の更新や optimizer の修復では変わらない。サービスの OS 利用者と全ての環境変数で動く。パスは絶対パス・
``~``・作業領域の root ``REPOS_DIR`` からの相対で、削除は ``bash`` で行う。ディスク処理は ``asyncio.to_thread`` で
event loop の外で行い、``bash`` は新しい session で実行して、期限切れと取消しでは process group を回収する。
失敗は ToolResult に変換し、失敗の追跡目標は ``path``・``command`` とする。
"""

from __future__ import annotations

import asyncio
import os
import signal
import tempfile
import time
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, ClassVar

import trackeverything.config as config
from trackeverything.core.abc.context import AgentContext
from trackeverything.core.abc.tool import Tool, ToolResult
from trackeverything.core.builtin.process_registry import spawn_exec, spawn_shell

#: ``read`` が既定で返す行数と、返す文字数の上限。
READ_LINES = 2000
READ_CHARS = 50_000
#: ``bash`` の出力で残す文字数(末尾)と、期限の既定値・上限(秒)。
OUTPUT_CHARS = 50_000
BASH_TIMEOUT = 120
BASH_TIMEOUT_MAX = 3600

_PATH: dict[str, Any] = {"type": "string", "description": "File path: absolute, '~'-based or relative to the workspace root."}


def service_path(value: str) -> Path:
    """絶対パス・``~``・``REPOS_DIR`` 相対のパスを絶対パスにする(``REPOS_DIR`` はなければ作る)。"""
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    root = Path(config.REPOS_DIR)
    root.mkdir(parents=True, exist_ok=True)
    return root / path


async def stop_process_group(process: Any, grace: float = 0.05) -> None:
    """子の process group を終わらせて(SIGTERM、``grace`` 秒後に SIGKILL)子を回収する。

    既にない group は ``ProcessLookupError``(macOS では ``PermissionError``)を返し、どちらも許容する。
    """
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGTERM)
    if process.returncode is None:
        with suppress(TimeoutError):
            async with asyncio.timeout(grace):
                await process.wait()
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()



# ---- 全ての Agent が共有する基礎の補助(部品の Tool もここから import する)

#: 1 回の Tool 出力に載せる本文の最大文字数(``text_window``)。
CHUNK_CHARS = 16384


def bounded_int(value: Any, name: str, *, default: int, low: int, high: int | None = None) -> int:
    """省略時は ``default``。整数で範囲内の値だけを受け付け、それ以外は ValueError。"""
    if value is None:
        return default
    if type(value) is not int or value < low or (high is not None and value > high):
        raise ValueError(f"'{name}' must be an integer from {low}" + (f" to {high}" if high is not None else ""))
    return value


def text_window(text: str, offset: Any = None) -> dict[str, Any]:
    """``offset`` から最大 ``CHUNK_CHARS`` 字を返す。続きがあれば ``next_offset`` を付ける。"""
    start = 0 if offset is None else offset
    if type(start) is not int or start < 0 or start > len(text):
        raise ValueError(f"'offset' must be an integer from 0 to {len(text)}")
    end = min(start + CHUNK_CHARS, len(text))
    return {"text": text[start:end], "offset": start, "next_offset": end if end < len(text) else None, "total_chars": len(text)}


async def run_captured(
    argv: Sequence[str],
    *,
    timeout: float,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[int, bytes, bytes]:
    """短い子 process を新しい session で実行し、(終了コード, stdout, stderr) を返す。

    期限切れは ``TimeoutError``(OSError の一種)。期限切れ・取消しでも process group を回収してから送出する。
    """
    process = await spawn_exec(
        *argv,
        cwd=cwd,
        env=None if env is None else dict(env),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await process.communicate()
    finally:
        await stop_process_group(process)
    return (process.returncode if process.returncode is not None else -1), stdout, stderr


@dataclass(frozen=True)
class ProcessResult:
    """``run_bounded`` の結果。``error`` は ``"timeout"``・``"output_limit"``、正常終了は None。"""

    stdout: bytes
    exit_code: int
    error: str | None = None
    stderr: bytes = b""


#: 実行中の出力ファイルの大きさを確かめる間隔(秒)。
_OUTPUT_CHECK_SECONDS = 0.02


async def _exit_or_output_limit(process: asyncio.subprocess.Process, stdout: IO[bytes], stderr: IO[bytes], output_limit: int) -> str | None:
    """終了を待ち、途中で出力が上限を超えたら ``"output_limit"`` を返す。"""
    exited = asyncio.ensure_future(process.wait())
    try:
        while True:
            if os.fstat(stdout.fileno()).st_size > output_limit or os.fstat(stderr.fileno()).st_size > output_limit:
                return "output_limit"
            done, _ = await asyncio.wait({exited}, timeout=_OUTPUT_CHECK_SECONDS)
            if done:
                return None
    finally:
        exited.cancel()


async def run_bounded(argv: list[str], cwd: Path, deadline: float, *, input_text: str = "", env: dict[str, str],
                      output_limit: int = 32 * 1024 * 1024) -> ProcessResult:
    """期限と出力上限つきで子 process を実行する。pipe の詰まりを避け、終了時には同じ process group の残存子も回収する。"""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return ProcessResult(b"", -1, "timeout")
    with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        stdin.write(input_text.encode("utf-8"))
        stdin.seek(0)
        process = await spawn_exec(*argv, cwd=cwd, env=env, stdin=stdin, stdout=stdout, stderr=stderr, start_new_session=True)
        failure = None
        try:
            async with asyncio.timeout(remaining):
                failure = await _exit_or_output_limit(process, stdout, stderr, output_limit)
        except TimeoutError:
            failure = "timeout"
        finally:
            # 成功した親が残した背景の子も、取消しが伝わる前にも回収する。
            await stop_process_group(process)
        stdout.seek(0)
        output = stdout.read(output_limit + 1)
        if len(output) > output_limit or os.fstat(stderr.fileno()).st_size > output_limit:
            failure = failure or "output_limit"
        stderr.seek(0)
        return ProcessResult(output[:output_limit], process.returncode if process.returncode is not None else -1, failure,
                             stderr.read(min(output_limit, 16384)))

def _text(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"not UTF-8 text: {path}") from exc


class _FileTool(Tool):
    """path の検証・loop 外での実行・失敗の変換を共有する。"""

    async def run(self, args: dict[str, Any], *, ctx: AgentContext | None = None) -> ToolResult:
        value = args.get("path")
        if not isinstance(value, str) or not value.strip():
            return ToolResult(ok=False, error="'path' must be a non-empty string")
        try:
            output = await asyncio.to_thread(self._apply, service_path(value), args)
        except (OSError, ValueError) as exc:
            return ToolResult(ok=False, error=f"{self.name} failed: {exc}")
        return ToolResult(ok=True, output=output)

    def error_key(self, args: dict[str, Any]) -> str | None:
        path = args.get("path")
        return path if isinstance(path, str) else None

    @abstractmethod
    def _apply(self, path: Path, args: dict[str, Any]) -> dict[str, Any]:
        """同期の file 操作。失敗は ``OSError``・``ValueError`` として送出する。"""


class ReadTool(_FileTool):
    name: ClassVar[str] = "read"
    description: ClassVar[str] = (
        f"Read a UTF-8 text file. Returns up to 'limit' lines (default {READ_LINES}, at most {READ_CHARS} characters) "
        "starting at line 'offset' (1-based, default 1), the total line count, and 'next_offset' when more lines remain."
    )
    arguments: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": _PATH,
            "offset": {"type": "integer", "minimum": 1, "description": "First line to return (1-based, default 1)."},
            "limit": {"type": "integer", "minimum": 1, "description": f"Lines to return (default {READ_LINES})."},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def _apply(self, path: Path, args: dict[str, Any]) -> dict[str, Any]:
        offset, limit = args.get("offset", 1), args.get("limit", READ_LINES)
        if not isinstance(offset, int) or offset < 1 or not isinstance(limit, int) or limit < 1:
            raise ValueError("'offset' and 'limit' must be positive integers")
        lines = _text(path).splitlines(keepends=True)
        chosen = lines[offset - 1:offset - 1 + limit]
        content = "".join(chosen)
        if len(content) > READ_CHARS:
            kept: list[str] = []
            size = 0
            for line in chosen:
                if size + len(line) > READ_CHARS and kept:
                    break
                kept.append(line[:READ_CHARS])
                size += len(line)
            chosen, content = kept, "".join(kept)
        end = offset - 1 + len(chosen)
        return {"path": str(path), "content": content, "total_lines": len(lines), "next_offset": end + 1 if end < len(lines) else None}


class WriteTool(_FileTool):
    name: ClassVar[str] = "write"
    description: ClassVar[str] = (
        "Create or overwrite a whole UTF-8 text file with 'content', creating missing parent directories. "
        "Use edit to change part of an existing file."
    )
    arguments: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": _PATH, "content": {"type": "string", "description": "The complete new file content."}},
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def _apply(self, path: Path, args: dict[str, Any]) -> dict[str, Any]:
        content = args.get("content")
        if not isinstance(content, str):
            raise ValueError("'content' must be a string")
        created = not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
        return {"path": str(path), "created": created, "chars": len(content)}


class EditTool(_FileTool):
    name: ClassVar[str] = "edit"
    description: ClassVar[str] = (
        "Replace 'old_text' with 'new_text' in a UTF-8 text file. 'old_text' must match exactly once; "
        "no match or several matches fail without changing the file."
    )
    arguments: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": _PATH,
            "old_text": {"type": "string", "description": "Exact current text, with enough context to be unique."},
            "new_text": {"type": "string", "description": "Replacement text."},
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    }

    def _apply(self, path: Path, args: dict[str, Any]) -> dict[str, Any]:
        old, new = args.get("old_text"), args.get("new_text")
        if not isinstance(old, str) or not old:
            raise ValueError("'old_text' must be a non-empty string")
        if not isinstance(new, str) or new == old:
            raise ValueError("'new_text' must be a string different from 'old_text'")
        text = _text(path)
        count = text.count(old)
        if count != 1:
            raise ValueError(f"'old_text' matches {count} times; it must match exactly once")
        path.write_bytes(text.replace(old, new, 1).encode("utf-8"))
        return {"path": str(path), "replaced": 1}


class BashTool(Tool):
    name: ClassVar[str] = "bash"
    description: ClassVar[str] = (
        "Run a bash command as the service's OS user with the full service environment, in the workspace root, and return "
        f"its combined stdout/stderr (the last {OUTPUT_CHARS} characters) and exit code. 'timeout' is in seconds "
        f"(default {BASH_TIMEOUT}, at most {BASH_TIMEOUT_MAX}); the command's process group is stopped when it expires. "
        "Reference credentials through environment variable names and never print their values. Do not detach "
        "processes (& or nohup)."
    )
    arguments: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Bash command line to execute."},
            "timeout": {"type": "integer", "minimum": 1, "maximum": BASH_TIMEOUT_MAX, "description": "Seconds before it is stopped."},
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any], *, ctx: AgentContext | None = None) -> ToolResult:
        command, timeout = args.get("command"), args.get("timeout", BASH_TIMEOUT)
        if not isinstance(command, str) or not command.strip():
            return ToolResult(ok=False, error="'command' must be a non-empty string")
        if not isinstance(timeout, int) or not 1 <= timeout <= BASH_TIMEOUT_MAX:
            return ToolResult(ok=False, error=f"'timeout' must be an integer from 1 to {BASH_TIMEOUT_MAX}")
        cwd = await asyncio.to_thread(service_path, ".")
        process = await spawn_shell(command, executable="/bin/bash", cwd=cwd, env=dict(os.environ), stdin=asyncio.subprocess.DEVNULL,
                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        try:
            async with asyncio.timeout(timeout):
                out, _ = await process.communicate()
        except TimeoutError:
            await stop_process_group(process)
            return ToolResult(ok=False, error=f"bash timed out after {timeout} seconds")
        except asyncio.CancelledError:
            await stop_process_group(process)
            raise
        await stop_process_group(process)
        output = out.decode("utf-8", "replace")[-OUTPUT_CHARS:]
        code = process.returncode
        if code != 0:
            return ToolResult(ok=False, output={"exit_code": code, "output": output}, error=f"exit code {code}")
        return ToolResult(ok=True, output={"exit_code": code, "output": output})

    def error_key(self, args: dict[str, Any]) -> str | None:
        command = args.get("command")
        return command if isinstance(command, str) else None


#: BaseAgent が内蔵する基本 Tool。
BASE_TOOLS: tuple[type[Tool], ...] = (ReadTool, WriteTool, EditTool, BashTool)
_SHARED: tuple[Tool, ...] = ()


def shared_tools() -> tuple[Tool, ...]:
    """基本 Tool の共有インスタンス。最初の呼出しで作り、以後はプロセスで同じものを返す。"""
    global _SHARED
    if not _SHARED:
        _SHARED = tuple(tool() for tool in BASE_TOOLS)
    return _SHARED
