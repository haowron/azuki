"""実行中のコードの版 — git SHA と、実行に影響する未 commit 変更の有無。

実行ログと session の実行記録が同じ版を参照し、各行が「どの版で起きたか」を
判断できるようにする。解決は process 内で一度だけ行い、失敗しても例外を出さない
(観測のために起動や記録を止めない)。

解決はパッケージ位置から求めた repo root の git による(失敗すれば ``unknown``)。
cwd は使わない(Agent が別 directory から呼び出すことがあるため)。

git の呼出しは asyncio の subprocess で、起動時に :func:`load_code_version` が一度だけ
await する。以後の :func:`current_code_version` は保持した値を同期で返す(未解決の間は ``unknown``)。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from trackeverything.core.builtin.domain.execution_log import current_component_version

log = logging.getLogger(__name__)

# このファイルは <root>/src/trackeverything/core/builtin/ にある。
REPO_ROOT = Path(__file__).resolve().parents[4]
# 実行に影響するファイルだけを dirty の判定に使う(docs/ と data/ は含めない)。
WATCHED_PATHS = ("src", "alembic", "pyproject.toml", "uv.lock", "scripts")
UNKNOWN = "unknown"
_GIT_TIMEOUT = 5.0


@dataclass(frozen=True, slots=True)
class CodeVersion:
    """版の表示値(``<sha12>`` / ``<sha12>+dirty.<hash8>`` / ``unknown``)と元の情報。"""

    value: str
    git_sha: str | None
    dirty: bool


async def resolve_code_version(root: Path | None = None) -> CodeVersion:
    """版を解決する。``root`` はテスト用の差し替え口。"""
    return await _from_git(REPO_ROOT if root is None else root)


async def _git(root: Path, *args: str) -> str | None:
    """git の stdout。失敗・時間切れ・git の不在は None(例外を出さない)。"""
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(root),
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except OSError:
        return None
    try:
        async with asyncio.timeout(_GIT_TIMEOUT):
            stdout, _ = await process.communicate()
    except TimeoutError:
        await _kill(process)
        return None
    except asyncio.CancelledError:
        await _kill(process)
        raise
    return stdout.decode("utf-8", errors="replace") if process.returncode == 0 else None


async def _kill(process: asyncio.subprocess.Process) -> None:
    """時間切れ・取消しの git を終了させ、終了を待つ(zombie を残さない)。"""
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    await process.wait()


async def _from_git(root: Path) -> CodeVersion:
    unknown = CodeVersion(UNKNOWN, None, False)
    top = await _git(root, "rev-parse", "--show-toplevel")
    # root を含む別 repo(例: site-packages の上位にある checkout)の版を使わない。
    if top is None or Path(top.strip()).resolve() != root.resolve():
        return unknown
    head = await _git(root, "rev-parse", "HEAD")
    if not head or not head.strip():
        return unknown
    sha = head.strip()
    status = await _git(root, "status", "--porcelain", "--untracked-files=all", "--", *WATCHED_PATHS)
    if status is None:
        log.warning("git status failed under %s; recording %s as clean", root, sha[:12])
        return CodeVersion(sha[:12], sha, False)
    if not status.strip():
        return CodeVersion(sha[:12], sha, False)
    return CodeVersion(f"{sha[:12]}+dirty.{await _dirty_digest(root, status)}", sha, True)


async def _dirty_digest(root: Path, status: str) -> str:
    """未 commit 内容の hash 8 桁。内容が違えば版も違う(同じ HEAD の別 build を区別する)。"""
    digest = hashlib.sha1()
    digest.update((await _git(root, "diff", "HEAD", "--", *WATCHED_PATHS) or "").encode())
    for line in sorted(status.splitlines()):
        if not line.startswith("?? "):
            continue
        relative = line[3:]
        digest.update(relative.encode())
        path = root / relative
        if path.is_file():
            with contextlib.suppress(OSError):
                digest.update(path.read_bytes())
    return digest.hexdigest()[:8]


_lock = threading.Lock()
_current: CodeVersion | None = None


async def load_code_version() -> CodeVersion:
    """この process の版を一度だけ解決して保持する(起動時に await する)。"""
    global _current
    with _lock:
        if _current is not None:
            return _current
    try:
        version = await resolve_code_version()
    except Exception:
        log.exception("code version resolution failed; recording %s", UNKNOWN)
        version = CodeVersion(UNKNOWN, None, False)
    with _lock:
        if _current is None:
            _current = version
        return _current


def current_code_version_info() -> CodeVersion:
    """この process の版。git による解決は :func:`load_code_version` が行い、ここは待たない(未解決の間は ``unknown``)。"""
    with _lock:
        return _current if _current is not None else CodeVersion(UNKNOWN, None, False)


@contextmanager
def component_scope(label: str | None) -> Iterator[None]:
    """区間の間、記録に付ける版へ部品の版(``c.<版 id の先頭 8 桁>``)を加える。None なら変えない。"""
    if label is None:
        yield
        return
    token = current_component_version.set(label)
    try:
        yield
    finally:
        current_component_version.reset(token)


def current_code_version() -> str:
    """ログと実行記録に付ける版の表示値。Agent の実行中は ``<Host の版>+c.<その Agent の版 id の先頭 8 桁>``。"""
    host = current_code_version_info().value
    label = current_component_version.get()
    return f"{host}+{label}" if label else host


def override_code_version(value: str | None) -> None:
    """テスト用に版を固定する。``None`` で解除し、次の ``load_code_version`` で再解決する。"""
    global _current
    with _lock:
        _current = None if value is None else CodeVersion(value, None, False)
