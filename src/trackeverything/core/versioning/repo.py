"""版庫: 版の取得・main・fork・merge と、run ごとの Agent の取得口。

版は git の commit(5 フォルダの tree)で、id は commit の SHA。保存は ``VersionStore``(git の mirror)が持ち、
このプロセスは main と run が使う版だけを読み込む。読込みは版ごとの予約 namespace(``_te_versions.v_<id>``)へ
行い、新旧の版の module を並存させる。main でも run の利用中でもない版は、module・部品グラフ・file view を解放する。

共有の main(GitHub 方式は GitHub の main、ローカル方式は mirror の main)は ``sync()`` が読み、現在の Host で読み込めたときだけ採用する。
fork は版から新しい版を作り、merge は候補を共有の最新の main の上へ載せ直して進め(GitHub 方式は push)、成功したときだけ main を進める。
ローカル方式で main がなければ、``start()`` が package の 5 フォルダを最初の main にする。

Runtime は run の開始ごとに ``refresh()`` で mirror の main を読み、``run()`` の取得口を ``AgentContext.agents`` に載せる。
取得口は名前ごとに最初に引いたときの main を記録し、その run の終わり(``release``)まで同じ版を返す。

一つのプロセスの版庫は ``open_repo`` で作る singleton で、runtime・optimizer は ``current_repo()`` で同じものを使う。
"""

from __future__ import annotations

import asyncio
import importlib
import linecache
import logging
import re
import shutil
import stat
import sys
import tempfile
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import TYPE_CHECKING, TypeGuard
from uuid import uuid4

import trackeverything
import trackeverything.config as config
from trackeverything.core.versioning import loader
from trackeverything.core.versioning.snapshot import PACKAGE_NAME, check_paths, index_modules
from trackeverything.core.versioning.store import (
    Commit,
    GitError,
    GitVersionStore,
    MergeConflict,
    VersionStore,
)

__all__ = [
    "Commit", "GitError", "MergeConflict", "PushRejected", "Repo", "RunVersions", "UnknownVersion", "Version",
    "current_repo", "describe_failure", "open_repo",
]

if TYPE_CHECKING:
    from trackeverything.core.abc.agent import BaseAgent
    from trackeverything.core.builtin.compose import ComponentGraph

log = logging.getLogger(__name__)

#: file view 上の版のファイル(``<view root>/v_<id>/trackeverything/<相対パス>``)。
_VIEW_FILE = re.compile(r"[/\\]v_[0-9a-f]+[/\\]" + PACKAGE_NAME + r"[/\\](.+)$")


#: package の root(``src/trackeverything``)。ローカル方式の最初の main はその 5 フォルダ(既定の部品)。
PACKAGE_ROOT = Path(trackeverything.__file__).resolve().parent
#: Host の checkout の部品の hooks(``pre-push``・``pre-receive``)。mirror の ``core.hooksPath`` にする。
HOOKS_DIR = PACKAGE_ROOT.parents[1] / "scripts" / "component-hooks"
#: ローカル方式の最初の main の message。
INITIAL_MESSAGE = "Initial components"


class UnknownVersion(LookupError):  # noqa: N818 - 契約の名前
    """保存されていない版 id。"""


class PushRejected(Exception):  # noqa: N818 - 契約の名前
    """merge が共有の main を進められなかった(読んだ後に他の push が入った)。採用済みの main は変えていない。"""


class Version:
    """読込み済みの版。module・file view・部品グラフを所有する。"""

    def __init__(self, commit: Commit, tree: Mapping[str, bytes], view_root: Path) -> None:
        self.commit = commit
        self.id = commit.id
        self.label = f"c.{commit.id[:8]}"
        self.name = f"v_{commit.id}"
        self.namespace = f"{loader.ROOT}.{self.name}"
        self.tree: Mapping[str, bytes] = MappingProxyType(dict(tree))
        index = index_modules(tree)
        self.sources: Mapping[str, str] = MappingProxyType(dict(index.sources))
        self.packages = index.packages
        self.virtual_packages = index.virtual
        self.view_dir = view_root / self.name
        self.users = 0
        self._graph: ComponentGraph | None = None

    @property
    def graph(self) -> ComponentGraph:
        if self._graph is None:
            raise RuntimeError(f"component version {self.id} has no component graph")
        return self._graph

    @contextmanager
    def in_use(self) -> Iterator[Version]:
        """block の間、版を利用中にする(lock を取らない ``release()`` の解放から守る)。"""
        self.users += 1
        try:
            yield self
        finally:
            self.users -= 1

    def module(self, logical: str) -> ModuleType:
        """この版の module。読込み済みならそれを、未読込みなら tree から読み込む。"""
        qualified = f"{self.namespace}.{logical}"
        return sys.modules.get(qualified) or importlib.import_module(qualified)

    def modules(self) -> dict[str, ModuleType]:
        """読込み済みの module(logical 名 → module)。"""
        prefix = self.namespace + "."
        return {name.removeprefix(prefix): module for name, module in list(sys.modules.items())
                if name.startswith(prefix) and module is not None}

    def view_path(self, relative: str) -> Path:
        """版の不変の file view 上のパス(``skills/alpha/SKILL.md`` など)。"""
        return self.view_dir / PACKAGE_NAME / relative

    def __repr__(self) -> str:
        return f"<Version {self.id} {self.commit.message}>"


class RunVersions:
    """一つの run の Agent の取得口。名前ごとに最初に引いた main をその run の終わりまで使う。"""

    def __init__(self, repo: Repo) -> None:
        self._repo = repo
        self._pins: dict[str, Version] = {}
        self._used: list[Version] = []
        self.released = False

    def version(self, name: str) -> Version:
        """名前の版。最初に引いたときの main を記録し、版ごとに一度だけ利用者に加わる。"""
        if self.released:
            raise RuntimeError("the run's component versions are released")
        pinned = self._pins.get(name)
        if pinned is None:
            pinned = self._repo.main_version
            self._pins[name] = pinned
            if all(used is not pinned for used in self._used):
                pinned.users += 1
                self._used.append(pinned)
        return pinned

    def agent(self, name: str) -> BaseAgent | None:
        return self.version(name).graph.agents.get(name)

    def end(self) -> None:
        """使った版の利用を外す。二度目は何もしない。"""
        if self.released:
            return
        self.released = True
        for version in self._used:
            version.users -= 1


def describe_failure(error: BaseException) -> str:
    """版の読込み・構築の失敗を、版のパスと行・例外の一行にする。ソース行は含めない。"""

    def short(filename: str) -> str:
        match = _VIEW_FILE.search(filename)
        return match.group(1).replace("\\", "/") if match else filename

    if isinstance(error, SyntaxError):
        where = f"{short(error.filename or '?')}:{error.lineno}"
    else:
        # frame を直接たどる。traceback の整形は linecache を登録し、解放した版の参照を作り直すため使わない。
        frames = [(frame.f_code.co_filename, line) for frame, line in traceback.walk_tb(error.__traceback__)]
        ours = [item for item in frames if _VIEW_FILE.search(item[0])]
        last = (ours or frames)[-1] if frames else None
        where = f"{short(last[0])}:{last[1]}" if last is not None else "unknown"
    return f"{where}: {type(error).__name__}: {error}"


class Repo:
    """版の取得・main・sync・fork・merge と、run ごとの取得口を所有する。``clock`` は新しい版の作成時刻(UTC)。"""

    def __init__(self, store: VersionStore, build: Callable[[Version], ComponentGraph], *, view_root: Path | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._store = store
        self._build = build
        self._clock = clock or partial(datetime.now, UTC)
        self._owns_view_root = view_root is None
        self._view_root = Path(view_root) if view_root is not None else Path(tempfile.mkdtemp(prefix="te-versions-"))
        self._lock = asyncio.Lock()
        self._loaded: dict[str, Version] = {}
        self._commits: dict[str, Commit] = {}
        self._main: Version | None = None
        self._failed: set[str] = set()
        self._closed = False
        self._cleanup_pending: list[Path] = []
        #: 呼出し元が取り消された後も worker thread で続く読込み(版 id → task)。
        self._abandoned: dict[str, asyncio.Task[Version]] = {}
        loader.install()

    @property
    def main(self) -> Commit:
        return self.main_version.commit

    @property
    def main_version(self) -> Version:
        if self._main is None:
            state = "is closed" if self._closed else "has no loaded main"
            raise RuntimeError(f"the component version repository {state}")
        return self._main

    # ---- 起動と最新の main

    async def start(self) -> Commit:
        """mirror の main を読み込む。main がなければ最初の main を採用する(``_adopt_first_main``)。失敗は例外(起動の中止)。"""
        async with self._exclusive():
            main_id = await self._store.read_main() or await self._adopt_first_main()
            self._switch(await self._ensure_loaded(main_id))
            log.info("component version %s is main (%s)", self.main.id, self.main.message)
            return self.main

    async def refresh(self) -> Commit:
        """mirror の main が読込み済みの main と違えば読み込んで main にする。失敗は記録し、読込み済みの main を使い続ける。

        main が変わっていなければ lock を取らない。
        """
        current = self.main
        if not self._is_new(await self._read_or_keep(self._store.read_main, "could not read the component main")):
            return current
        async with self._exclusive():
            stored = await self._read_or_keep(self._store.read_main, "could not read the component main")
            version = await self._load_candidate(stored) if self._is_new(stored) else None
            if version is not None:
                self._switch(version)
                log.info("component version %s is main (%s)", version.id, version.commit.message)
            return self.main

    async def sync(self) -> Commit:
        """共有の main を読み(GitHub 方式は取得する)、採用済みと違えば読み込んで検証し、成功したら mirror の main にして採用する。

        取得の失敗は warning、読み込めない版はその版について一度だけ error で記録し、採用済みの main を使い続ける。
        共有の main が採用済みの履歴の外(force push)でも、読み込めれば採用する。返り値は main の Commit。
        """
        async with self._exclusive():
            remote = await self._read_or_keep(self._store.fetch, "could not fetch the component repository")
            version = await self._load_candidate(remote) if self._is_new(remote) else None
            if version is None:
                return self.main
            if await self._advance_main(version):
                log.info("component version %s is main (%s)", version.id, version.commit.message)
            else:
                log.warning("the component main moved while %s was adopted; keeping %s", version.id, self.main.id)
            return self.main

    # ---- fork・merge

    async def fork(self, parent: str, tree: Mapping[str, bytes], message: str) -> Commit:
        """``parent`` を親として tree を書き、読み込んで検証し、保持する。main は変えない。一つの版から何度でも fork できる。"""
        async with self._exclusive():
            if not isinstance(parent, str):
                raise UnknownVersion(repr(parent))
            return await self._new_commit(tree, message, parent=parent)

    async def merge(self, version_id: str) -> Commit:
        """候補で共有の main を進め(GitHub 方式は push、ローカル方式は mirror の main を fast-forward)、成功したら採用して返す。

        共有の main が候補の親でなければ、候補をその main の上へ載せ直した版(message に ``Rebased-From``)を作って検証する。
        候補が既に共有の main に含まれていれば何もせず main を返す。衝突は ``MergeConflict``、
        進められなければ ``PushRejected``、読込みと git の失敗はその例外で、採用済みの main は変えない。
        """
        async with self._exclusive():
            candidate = await self._require_commit(version_id)
            remote = await self._store.fetch()
            if remote is None:
                raise RuntimeError("the component repository has no main")
            if await self._store.is_ancestor(candidate.id, remote):
                log.info("component version %s is already in the main %s", candidate.id, remote)
                return self.main
            target = await self._merge_target(candidate, remote)
            with target.in_use():
                if not await self._store.push(target.id):
                    raise PushRejected(f"component version {target.id} was not merged: the component main moved after {remote}")
                if not await self._advance_main(target):
                    log.warning("the mirror main moved while %s was merged; sync adopts it next", target.id)
            log.info("component version %s is main (%s)", target.id, target.commit.message)
            return target.commit

    async def validate(self, tree: Mapping[str, bytes]) -> None:
        """tree を読み込んで部品グラフを作り、保存せずに解放する。読み込めなければ例外(check CLI)。"""
        files = {key: bytes(value) for key, value in tree.items()}
        check_paths(files)
        commit = Commit(id=uuid4().hex, parent=None, message="check", created_at=self._clock())
        async with self._exclusive():
            self._dispose(await self._load_in_thread(commit, files))

    async def log(self) -> list[Commit]:
        """保存された全ての版(新しい順)。"""
        self._check_open()
        return await self._store.list_commits()

    async def tree(self, version_id: str) -> dict[str, bytes]:
        self._check_open()
        return dict(await self._tree_of(version_id))

    # ---- run の取得口

    def run(self) -> RunVersions:
        self._check_open()
        return RunVersions(self)

    def release(self, run: RunVersions) -> None:
        """run が使った版の利用を外し、main でなく利用者のない版を解放する。二度目は何もしない。"""
        run.end()
        if not self._closed:
            self._collect()

    async def close(self) -> None:
        """取り消された読込みが終わるのを待ち、全ての版を解放する。以後の操作は ``RuntimeError``。singleton ならそれを外す。"""
        global _current
        async with self._lock:
            if self._closed:
                return
            while self._abandoned:
                await asyncio.wait(tuple(self._abandoned.values()))
            self._closed = True
            if _current is self:
                _current = None
            for version in list(self._loaded.values()):
                self._dispose(version)
            self._loaded.clear()
            self._main = None
            if self._owns_view_root:
                shutil.rmtree(self._view_root, ignore_errors=True)

    # ---- 内部

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("the component version repository is closed")

    @asynccontextmanager
    async def _exclusive(self) -> AsyncIterator[None]:
        async with self._lock:
            self._check_open()
            try:
                yield
            finally:
                self._collect()

    def _is_new(self, version_id: str | None) -> TypeGuard[str]:
        return version_id is not None and version_id != self.main.id and version_id not in self._failed

    async def _read_or_keep(self, read: Callable[[], Awaitable[str | None]], failure: str) -> str | None:
        # 読めなければ今の main のまま、次の呼出しで再試行する。
        try:
            return await read()
        except Exception:
            log.warning("%s; keeping %s", failure, self.main.id, exc_info=True)
            return None

    async def _load_candidate(self, version_id: str) -> Version | None:
        # 版がない・読み込めない版は二度と試さない。読取りの失敗は一時的なものとして次に再試行する。
        loaded = self._loaded.get(version_id)
        if loaded is not None:
            return loaded
        try:
            commit, files = await self._read_version(version_id)
        except UnknownVersion:
            self._failed.add(version_id)
            log.error("component version %s is not stored; keeping %s", version_id, self.main.id)
            return None
        except Exception:
            log.warning("could not read component version %s; keeping %s", version_id, self.main.id, exc_info=True)
            return None
        try:
            version = await self._load_in_thread(commit, files)
        except Exception as error:
            self._failed.add(version_id)
            # traceback は整形しない(linecache が解放した版の行を持ち直すため)。
            log.error("component version %s could not be loaded; keeping %s: %s", version_id, self.main.id, describe_failure(error))
            return None
        self._loaded[version_id] = version
        return version

    async def _adopt_first_main(self) -> str:
        # mirror に main がない: 共有の main、それもなければ store の最初の tree(ローカル方式の既定の部品)を main にする。
        # 他のプロセスが先に main を作っていればそれを使う。
        first = await self._store.fetch()
        if first is None:
            initial = await self._store.initial_tree()
            if initial is None:
                raise RuntimeError("the component repository has no main")
            first = (await self._new_commit(initial, INITIAL_MESSAGE, parent=None)).id
        await self._ensure_loaded(first)
        if await self._store.move_main(None, first):
            return first
        adopted = await self._store.read_main()
        if adopted is None:
            raise RuntimeError("the component main disappeared while it was adopted")
        return adopted

    async def _merge_target(self, candidate: Commit, remote: str) -> Version:
        # main が候補の親なら候補のまま、そうでなければ候補を main の上へ載せ直した版。
        if remote == candidate.parent:
            return await self._ensure_loaded(candidate.id)
        if candidate.parent is None:
            raise RuntimeError(f"component version {candidate.id} has no parent to merge from")
        tree = await self._store.merge_tree(candidate.parent, remote, candidate.id)
        rebased = await self._new_commit(tree, f"{candidate.message}\n\nRebased-From: {candidate.id}", parent=remote)
        return self._loaded[rebased.id]

    async def _require_commit(self, version_id: str) -> Commit:
        found = self._commits.get(version_id) or await self._store.read_commit(version_id)
        if found is None:
            raise UnknownVersion(version_id)
        self._commits[version_id] = found
        return found

    async def _tree_of(self, version_id: str) -> Mapping[str, bytes]:
        loaded = self._loaded.get(version_id)
        if loaded is not None:
            return loaded.tree
        files = await self._store.read_tree(version_id)
        if files is None:
            raise UnknownVersion(version_id)
        return files

    async def _new_commit(self, tree: Mapping[str, bytes], message: str, *, parent: str | None) -> Commit:
        """tree を新しい版として書き、読み込んで検証してから保持する(同じ版が既にあれば作成時刻を 1 秒進める)。"""
        files = {key: bytes(value) for key, value in tree.items()}
        check_paths(files)
        if parent is not None:
            await self._require_commit(parent)
        created_at = self._clock()
        while (commit := await self._store.write(files, parent, message, created_at)) is None:
            created_at += timedelta(seconds=1)
        version = await self._load_in_thread(commit, files)
        try:
            await self._store.keep(commit.id)
        except BaseException:
            self._dispose(version)
            raise
        self._loaded[commit.id] = version
        self._commits[commit.id] = commit
        return commit

    async def _advance_main(self, version: Version) -> bool:
        # 読取りから移動までずっと版を利用中にする。待つ間に lock を取らない release() が解放しないように。
        with version.in_use():
            moved = await self._store.move_main(await self._store.read_main(), version.id)
        if moved and self._main is not None:
            self._switch(version)
        return moved

    async def _read_version(self, version_id: str) -> tuple[Commit, Mapping[str, bytes]]:
        return await self._require_commit(version_id), await self._tree_of(version_id)

    async def _ensure_loaded(self, version_id: str) -> Version:
        loaded = self._loaded.get(version_id)
        if loaded is not None:
            return loaded
        commit, files = await self._read_version(version_id)
        version = await self._load_in_thread(commit, files)
        self._loaded[version_id] = version
        return version

    def _switch(self, version: Version) -> None:
        self._main = version
        self._failed.discard(version.id)

    def _collect(self) -> None:
        for version in list(self._loaded.values()):
            if version is not self._main and version.users <= 0:
                del self._loaded[version.id]
                self._dispose(version)

    async def _load_in_thread(self, commit: Commit, files: Mapping[str, bytes]) -> Version:
        """``_load`` を worker thread で行う。取り消しはすぐ伝え、thread は終わるまで続けて、読み込めた版をその時点で解放する。

        同じ版の取り消された読込みが残っていれば、その解放を待ってから読み込む(namespace と file view は版 id で決まるため)。
        """
        while (abandoned := self._abandoned.get(commit.id)) is not None:
            await asyncio.wait((abandoned,))
        loading = asyncio.create_task(asyncio.to_thread(self._load, commit, files))
        try:
            return await asyncio.shield(loading)
        except asyncio.CancelledError:
            self._abandoned[commit.id] = loading
            loading.add_done_callback(lambda _: self._release_abandoned(commit.id))
            raise

    def _release_abandoned(self, version_id: str) -> None:
        """取り消された読込みの終わり。読み込めた版を解放する(失敗した読込みは ``_load`` が外している)。"""
        loading = self._abandoned.pop(version_id)
        if not loading.cancelled() and loading.exception() is None:
            self._dispose(loading.result())

    def _load(self, commit: Commit, files: Mapping[str, bytes]) -> Version:
        # worker thread で動く。失敗したら読み込んだものを全て外して送出する。
        version = Version(commit, files, self._view_root)
        loader.register(version)
        try:
            for relative, data in version.tree.items():
                path = version.view_path(relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            for logical in sorted(version.sources):
                version.module(logical)
            graph = self._build(version)
            for agent in graph.agents.values():
                agent.version_label = version.label
            version._graph = graph
        except BaseException:
            self._dispose(version)
            raise
        return version

    def _dispose(self, version: Version) -> None:
        """版が所有する参照を外す。共有の object と管理外に残った参照は壊さない。"""
        loader.unregister(version)
        prefix = version.namespace
        for name in [name for name in list(sys.modules) if name == prefix or name.startswith(prefix + ".")]:
            sys.modules.pop(name, None)
        root = sys.modules.get(loader.ROOT)
        if root is not None:
            root.__dict__.pop(version.name, None)
        view = str(version.view_dir)
        for key in [key for key in list(linecache.cache) if isinstance(key, str) and key.startswith(view)]:
            linecache.cache.pop(key, None)
        version._graph = None
        self._remove_view(version.view_dir)
        for pending in list(self._cleanup_pending):
            self._cleanup_pending.remove(pending)
            self._remove_view(pending)
        log.debug("component version %s unloaded", version.id)

    def _remove_view(self, view_dir: Path) -> None:
        if not view_dir.exists():
            return
        try:
            shutil.rmtree(view_dir)
        except OSError:
            log.exception("could not remove the file view %s; retrying at the next unload", view_dir)
            self._cleanup_pending.append(view_dir)


#: このプロセスの版庫(``open_repo`` で開いたもの)。
_current: Repo | None = None


def open_repo(build: Callable[[Version], ComponentGraph], *, store: VersionStore | None = None) -> Repo:
    """このプロセスの版庫を作って singleton にする。``store`` を省くと ``COMPONENT_REPO_DIR`` の mirror の ``GitVersionStore``
    (``COMPONENT_REPO_URL`` があれば GitHub 方式、なければローカル方式。hooks は ``HOOKS_DIR``、最初の main の種は ``PACKAGE_ROOT``)。

    一つのプロセスに版庫は一つにする。二つの版庫は同じ版の module の namespace(``_te_versions.v_<id>``)を共有し、
    片方の解放がもう片方の module を外すためである。開いた版庫があれば ``RuntimeError``。
    main は読み込まない(読込みは ``start``)。``close()`` で singleton が外れる。
    """
    global _current
    if _current is not None:
        raise RuntimeError("the component version repository is already open in this process")
    if store is None:
        store = GitVersionStore(config.COMPONENT_REPO_DIR, config.COMPONENT_REPO_URL, ssh_key=config.COMPONENT_REPO_SSH_KEY,
                                hooks=HOOKS_DIR, seed=PACKAGE_ROOT)
    repo = Repo(store, build)
    _current = repo
    return repo


def current_repo() -> Repo:
    """このプロセスの版庫。``open_repo`` で開いていなければ ``RuntimeError``。"""
    if _current is None:
        raise RuntimeError("the component version repository is not open in this process")
    return _current
