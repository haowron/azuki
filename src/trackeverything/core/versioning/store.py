"""版の保存: runtime が読む bare の mirror と、共有の main。

版は git の commit で、版 id は commit の 40 桁の SHA。版の tree は commit の tree のうち 5 フォルダ配下の通常ファイル
(symlink・submodule・``__pycache__``・``.`` で始まる名前を除く)。mirror の ref は次のとおり。

- ``refs/heads/main``: 採用した main(現在の Host で読み込めることを確かめた版)。runtime はこれだけを読む。
- ``refs/remotes/origin/main``: 最後に fetch した GitHub の main(未検証。GitHub 方式だけ)。
- ``refs/te/forks/<id>``: optimizer の候補。版を消さないために保持し、push しない。

共有の main の在りかは方式で決まる。GitHub 方式(URL あり)は部品のリポジトリの main で、``fetch`` と ``push`` が
network を使う。ローカル方式(URL なし)は mirror の main そのもので、人は作業コピー(``ensure_working_copy``)から
mirror へ push し、mirror の pre-receive hook(``hooks``)が検証する。最初の main は ``seed``(package の 5 フォルダ)から作る。

git は CLI を ``asyncio.create_subprocess_exec`` で呼ぶ。
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from trackeverything.core.versioning.snapshot import OWNED, owned, package_tree

MAIN_REF = "refs/heads/main"
REMOTE_MAIN_REF = "refs/remotes/origin/main"
FORK_PREFIX = "refs/te/forks/"
_ZERO = "0" * 40
_SHA = re.compile(r"[0-9a-f]{40}")
_AUTHOR = "TrackEverything <trackeverything@localhost>"
#: エラー文に URL の資格情報(``https://user:token@host``)を出さない。
_CREDENTIALS = re.compile(r"(//)[^/@\s]+@")
#: 版の tree に入れる通常ファイルの mode。
_FILE_MODES = ("100644", "100755")
#: 版を読む ``git log`` の引数。一件は ``<id>\0<親>\0<committer の時刻>\0<message>\x1e``。
_LOG = ("log", "--no-show-signature", "--format=%H%x00%P%x00%ct%x00%B%x1e")


@dataclass(frozen=True, slots=True)
class Commit:
    """保存された版。``id`` は git の commit の SHA、``parent`` は第一親、``created_at`` は committer の時刻(秒単位)。"""

    id: str
    parent: str | None
    message: str
    created_at: datetime


class GitError(RuntimeError):
    """git の失敗(届かない remote・壊れた repository など)。文は git の stderr で、URL の資格情報は伏せる。"""


class MergeConflict(Exception):  # noqa: N818 - 契約の名前
    """二つの版の合流が同じ箇所の変更で衝突した。``paths`` は衝突したパス。"""

    def __init__(self, paths: Sequence[str]) -> None:
        self.paths = tuple(paths)
        super().__init__("merge conflict in " + ", ".join(self.paths))


class VersionStore(Protocol):
    """版の保存先。読み書きは全て coroutine。"""

    async def read_main(self) -> str | None:
        """mirror の採用済みの main。"""
        ...

    async def move_main(self, expected: str | None, new: str) -> bool:
        """mirror の main が ``expected`` のときだけ ``new`` にする。変えたら True。GitHub は変えない。"""
        ...

    async def read_commit(self, version_id: str) -> Commit | None: ...

    async def read_tree(self, version_id: str) -> dict[str, bytes] | None: ...

    async def list_commits(self) -> list[Commit]:
        """main と保持した fork から辿れる版を新しい順に返す。"""
        ...

    async def write(self, tree: Mapping[str, bytes], parent: str | None, message: str, created_at: datetime) -> Commit | None:
        """版を書く(ref は作らない)。5 フォルダ以外は親の内容を引き継ぐ。同じ版が既にあれば None。"""
        ...

    async def keep(self, version_id: str) -> None:
        """書いた版を fork として保持する。"""
        ...

    async def fetch(self) -> str | None:
        """共有の main の id(GitHub 方式は GitHub の main を取得する。ローカル方式は mirror の main)。main がなければ None。"""
        ...

    async def is_ancestor(self, ancestor: str, descendant: str) -> bool: ...

    async def merge_tree(self, base: str, ours: str, theirs: str) -> dict[str, bytes]:
        """``base`` を基に ``ours`` と ``theirs`` を合わせた 5 フォルダの tree。衝突は ``MergeConflict``。"""
        ...

    async def push(self, version_id: str) -> bool:
        """版で共有の main を進める(force しない)。fast-forward でなく拒否されたら False。"""
        ...

    async def initial_tree(self) -> dict[str, bytes] | None:
        """共有の main がないときに最初の main にする tree。作らない store は None。"""
        ...


class GitVersionStore:
    """``VersionStore`` の git 実装。``directory`` の repository(bare の mirror か clone)を読み書きする。

    ``url`` があれば GitHub 方式(remote ``origin`` = ``url``)、なければローカル方式(最初の main の種は ``seed``)。
    ``directory`` がなければ、GitHub 方式か ``seed`` のあるローカル方式だけが bare で作る(check CLI の store は作らない)。
    ``hooks`` があれば開くたびに ``core.hooksPath`` をその directory にする。``ssh_key`` は fetch・push の deploy key。
    """

    def __init__(self, directory: str | Path, url: str = "", *, ssh_key: str = "", hooks: str | Path | None = None,
                 seed: str | Path | None = None) -> None:
        self._directory = Path(directory).expanduser()
        self._upstream = _GitHubUpstream(url) if url else _LocalUpstream(Path(seed) if seed is not None else None)
        # mirror を作れるのは GitHub 方式か、最初の main の種があるローカル方式だけ(check CLI の store は既存のものを読む)。
        self._creatable = bool(url) or seed is not None
        self._ssh_key = ssh_key
        self._hooks = Path(hooks).expanduser().resolve() if hooks is not None else None
        self._ready = False
        self._opening = asyncio.Lock()

    # ---- 読取り

    async def read_main(self) -> str | None:
        return await self._resolve(MAIN_REF)

    async def resolve(self, revision: str) -> str | None:
        """revision の commit の id。なければ None。"""
        return await self._resolve(f"{revision}^{{commit}}")

    async def read_commit(self, version_id: str) -> Commit | None:
        if not _SHA.fullmatch(version_id):
            return None
        code, out, _ = await self._git(*_LOG, "-1", version_id, "--", check=False)
        commits = _parse_commits(out) if code == 0 else []
        return commits[0] if commits else None

    async def read_tree(self, version_id: str) -> dict[str, bytes] | None:
        if await self.read_commit(version_id) is None:
            return None
        return await self._files(version_id)

    async def list_commits(self) -> list[Commit]:
        _, out, _ = await self._git("for-each-ref", "--format=%(objectname)", MAIN_REF, FORK_PREFIX)
        tips = sorted(set(out.decode().split()))
        if not tips:
            return []
        _, out, _ = await self._git(*_LOG, *tips, "--")
        return sorted(_parse_commits(out), key=lambda commit: commit.created_at, reverse=True)

    async def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        code, _, err = await self._git("merge-base", "--is-ancestor", ancestor, descendant, check=False)
        if code in (0, 1):
            return code == 0
        raise GitError(f"git merge-base: {_redact(err)}")

    # ---- 書込み

    async def move_main(self, expected: str | None, new: str) -> bool:
        code, _, err = await self._git("update-ref", MAIN_REF, new, expected or _ZERO, check=False)
        if code == 0:
            return True
        if await self.read_main() != expected:
            return False
        raise GitError(f"git update-ref: {_redact(err)}")

    async def write(self, tree: Mapping[str, bytes], parent: str | None, message: str, created_at: datetime) -> Commit | None:
        stamp = int(created_at.timestamp())
        with tempfile.TemporaryDirectory(prefix="te-write-") as scratch:
            entries = [*await self._inherited_entries(parent), *await self._blob_entries(Path(scratch), tree)]
            tree_id = await self._write_tree(Path(scratch) / "index", entries)
        text = _commit_text(tree_id, parent, message, stamp).encode()
        _, out, _ = await self._git("hash-object", "-t", "commit", "--stdin", stdin=text)
        version_id = out.decode().strip()
        exists, _, _ = await self._git("cat-file", "-e", version_id, check=False)
        if exists == 0:
            return None
        await self._git("hash-object", "-t", "commit", "-w", "--stdin", stdin=text)
        return Commit(id=version_id, parent=parent, message=message, created_at=datetime.fromtimestamp(stamp, UTC))

    async def keep(self, version_id: str) -> None:
        await self._git("update-ref", f"{FORK_PREFIX}{version_id}", version_id)

    # ---- 共有の main(方式ごとの処理は ``_Upstream``)

    async def fetch(self) -> str | None:
        return await self._upstream.fetch(self)

    async def merge_tree(self, base: str, ours: str, theirs: str) -> dict[str, bytes]:
        code, out, err = await self._git("merge-tree", "--write-tree", "--name-only", "--no-messages", f"--merge-base={base}", ours, theirs,
                                         check=False)
        lines = out.decode("utf-8", "replace").splitlines()
        if code == 1:
            raise MergeConflict(sorted({line for line in lines[1:] if line}))
        if code != 0 or not lines:
            raise GitError(f"git merge-tree: {_redact(err)}")
        return await self._files(lines[0])

    async def push(self, version_id: str) -> bool:
        return await self._upstream.push(self, version_id)

    async def initial_tree(self) -> dict[str, bytes] | None:
        return await self._upstream.initial_tree()

    # ---- 内部

    async def _resolve(self, revision: str) -> str | None:
        code, out, _ = await self._git("rev-parse", "-q", "--verify", revision, check=False)
        return out.decode().strip() if code == 0 else None

    async def _files(self, treeish: str) -> dict[str, bytes]:
        _, out, _ = await self._git("ls-tree", "-r", "-z", "--full-tree", treeish)
        wanted: list[tuple[str, str]] = []
        for line in _tree_lines(out):
            mode, kind, blob, path = _tree_entry(line)
            if kind == "blob" and mode in _FILE_MODES and owned(path):
                wanted.append((path, blob))
        contents = await self._read_blobs([blob for _, blob in wanted])
        return {path: contents[blob] for path, blob in wanted}

    async def _read_blobs(self, blobs: list[str]) -> dict[str, bytes]:
        if not blobs:
            return {}
        _, out, _ = await self._git("cat-file", "--batch", stdin="".join(f"{blob}\n" for blob in blobs).encode())
        contents: dict[str, bytes] = {}
        position = 0
        for blob in blobs:
            end = out.index(b"\n", position)
            size = int(out[position:end].split()[2])
            contents[blob] = out[end + 1:end + 1 + size]
            position = end + 1 + size + 1
        return contents

    async def _inherited_entries(self, parent: str | None) -> list[str]:
        # 版は 5 フォルダの外(README.md など)を親から引き継ぐ。
        if parent is None:
            return []
        _, out, _ = await self._git("ls-tree", "-r", "-z", "--full-tree", parent)
        return [line for line in _tree_lines(out) if _tree_entry(line)[3].split("/", 1)[0] not in OWNED]

    async def _blob_entries(self, scratch: Path, tree: Mapping[str, bytes]) -> list[str]:
        paths = sorted(tree)
        files = await asyncio.to_thread(_spill, scratch, [tree[path] for path in paths])
        listing = "".join(f"{file}\n" for file in files).encode()
        _, out, _ = await self._git("hash-object", "-w", "--no-filters", "--stdin-paths", stdin=listing)
        return [f"100644 blob {blob}\t{path}" for blob, path in zip(out.decode().split(), paths, strict=True)]

    async def _write_tree(self, index: Path, entries: list[str]) -> str:
        env = {"GIT_INDEX_FILE": str(index)}
        info = "".join(f"{entry}\0" for entry in entries).encode("utf-8", "surrogateescape")
        await self._git("update-index", "--add", "-z", "--index-info", stdin=info, env=env)
        _, out, _ = await self._git("write-tree", env=env)
        return out.decode().strip()

    async def _open(self) -> None:
        if self._ready:
            return
        async with self._opening:
            if self._ready:
                return
            if not self._directory.exists():
                if not self._creatable:
                    raise GitError(f"no component repository at {self._directory}")
                self._directory.parent.mkdir(parents=True, exist_ok=True)
                await self._run("init", "-q", "--bare", "--initial-branch=main", str(self._directory), cwd=self._directory.parent)
            await self._upstream.configure(self)
            if self._hooks is not None:
                await self._run("config", "core.hooksPath", str(self._hooks))
            self._ready = True

    async def _git(self, *args: str, stdin: bytes | None = None, env: Mapping[str, str] | None = None,
                   check: bool = True) -> tuple[int, bytes, bytes]:
        await self._open()
        return await self._run(*args, stdin=stdin, env=env, check=check)

    async def _run(self, *args: str, stdin: bytes | None = None, env: Mapping[str, str] | None = None, check: bool = True,
                   cwd: Path | None = None) -> tuple[int, bytes, bytes]:
        # _open を待たない(_open 自身と上流の設定が使う)。
        directory = None if cwd is not None else self._directory
        return await _git_command(args, directory=directory, cwd=cwd, stdin=stdin, env=env, check=check, ssh_key=self._ssh_key)


# ---- 共有の main の方式(Strategy)


@dataclass(frozen=True, slots=True)
class _GitHubUpstream:
    """GitHub 方式: 共有の main は部品のリポジトリ(remote ``origin``)の main。fetch と push だけが network を使う。"""

    url: str

    async def configure(self, store: GitVersionStore) -> None:
        # fetch と push は refspec を明示するので、remote には URL だけを置く。
        await store._run("config", "remote.origin.url", self.url)

    async def fetch(self, store: GitVersionStore) -> str | None:
        code, _, err = await store._git("fetch", "-q", "--no-tags", "origin", f"+{MAIN_REF}:{REMOTE_MAIN_REF}", check=False)
        if code == 0:
            return await store._resolve(REMOTE_MAIN_REF)
        if b"couldn't find remote ref" in err:
            return None
        raise GitError(f"git fetch: {_redact(err)}")

    async def push(self, store: GitVersionStore, version_id: str) -> bool:
        # 版は merge が読み込んで検証済み。mirror の core.hooksPath(Host の hooks)の pre-push は人の clone 用なので走らせない。
        refspec = f"{version_id}:{MAIN_REF}"
        code, out, err = await store._git("push", "--porcelain", "--no-verify", "origin", refspec, check=False)
        if code == 0:
            await store._git("update-ref", REMOTE_MAIN_REF, version_id)
            return True
        if any(line.startswith("!") for line in out.decode("utf-8", "replace").splitlines()):
            return False
        raise GitError(f"git push: {_redact(err)}")

    async def initial_tree(self) -> dict[str, bytes] | None:
        return None


@dataclass(frozen=True, slots=True)
class _LocalUpstream:
    """ローカル方式: mirror の main が共有の main。push は fast-forward だけ。最初の main は ``seed`` の package の 5 フォルダ。"""

    seed: Path | None

    async def configure(self, store: GitVersionStore) -> None:
        return None

    async def fetch(self, store: GitVersionStore) -> str | None:
        return await store.read_main()

    async def push(self, store: GitVersionStore, version_id: str) -> bool:
        current = await store.read_main()
        if current is not None and not await store.is_ancestor(current, version_id):
            return False
        return await store.move_main(current, version_id)

    async def initial_tree(self) -> dict[str, bytes] | None:
        return None if self.seed is None else await asyncio.to_thread(package_tree, self.seed)


# ---- ローカル方式の作業コピー


async def ensure_working_copy(mirror: str | Path, path: str | Path) -> bool:
    """ローカル方式の人の作業コピーを ``path`` に作る(mirror の clone。``origin`` は mirror の絶対パス、branch は main)。

    ``path`` が既にあれば中身も remote も触らず False。作ったら True。失敗は ``GitError``。
    """
    target = Path(path).expanduser()
    if os.path.lexists(target):
        return False
    await _git_command(("clone", "-q", "--branch", "main", str(Path(mirror).expanduser().resolve()), str(target.absolute())))
    return True


# ---- git の実行と出力の解析


async def _git_command(args: Sequence[str], *, directory: Path | None = None, cwd: Path | None = None, stdin: bytes | None = None,
                       env: Mapping[str, str] | None = None, check: bool = True, ssh_key: str = "") -> tuple[int, bytes, bytes]:
    """git を子 process で実行する。``check`` なら失敗を ``GitError`` にする。取り消されたら子を止める。"""
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", **(env or {})}
    if ssh_key:
        environment["GIT_SSH_COMMAND"] = f"ssh -i {shlex.quote(ssh_key)} -o IdentitiesOnly=yes -o BatchMode=yes"
    argv = ["git", *(["-C", str(directory)] if directory is not None else []), *args]
    process = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=environment, cwd=cwd,
    )
    try:
        out, err = await process.communicate(stdin)
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    code = process.returncode if process.returncode is not None else -1
    if check and code != 0:
        raise GitError(f"git {args[0]}: {_redact(err)}")
    return code, out, err


def _redact(err: bytes) -> str:
    return _CREDENTIALS.sub(r"\1***@", err.decode("utf-8", "replace").strip())


def _parse_commits(out: bytes) -> list[Commit]:
    commits = []
    for record in out.decode("utf-8", "replace").split("\x1e"):
        record = record.lstrip("\n")
        if not record:
            continue
        version_id, parents, stamp, message = record.split("\x00", 3)
        commits.append(Commit(id=version_id, parent=parents.split()[0] if parents.strip() else None,
                              message=message.rstrip("\n"), created_at=datetime.fromtimestamp(int(stamp), UTC)))
    return commits


def _commit_text(tree_id: str, parent: str | None, message: str, stamp: int) -> str:
    # 作者・committer を固定し、同じ tree・親・message・秒が同じ commit になるようにする。
    return "\n".join([
        f"tree {tree_id}",
        *([f"parent {parent}"] if parent is not None else []),
        f"author {_AUTHOR} {stamp} +0000",
        f"committer {_AUTHOR} {stamp} +0000",
        "",
        message,
    ]) + "\n"


def _tree_lines(out: bytes) -> list[str]:
    return [line for line in out.decode("utf-8", "surrogateescape").split("\0") if line]


def _tree_entry(line: str) -> tuple[str, str, str, str]:
    """(mode, 種類, object id, パス)。"""
    meta, path = line.split("\t", 1)
    mode, kind, blob = meta.split(" ")
    return mode, kind, blob, path


def _spill(scratch: Path, contents: list[bytes]) -> list[str]:
    # hash-object --stdin-paths に渡す一時ファイル。thread で動く。
    paths = [scratch / f"blob-{number}" for number in range(len(contents))]
    for path, content in zip(paths, contents, strict=True):
        path.write_bytes(content)
    return [str(path) for path in paths]
