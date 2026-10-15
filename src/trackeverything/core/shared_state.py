"""版をまたいで一つであるべき状態の、部品を知らない共有置き場。

部品(Agent・Tool・Skill・Harness・Source)の lock や cache は、それを使う部品の
module に置いたまま、ここから名前で取得する。同じ名前は全ての版で同じ object を
返すため、旧版と新版の実行が同じ lock で排他し、同じ cache を読む。

置ける値は標準ライブラリか素のデータ(lock、set、素のデータの dict、ContextVar、
callback の入れ物)と Host が定義する :class:`TaskLock` に限る。版が所有するクラスの実例は旧版のコードを保持し続け、
``isinstance`` も版ごとに分かれるため拒否する(浅い検査)。値の形を変えるときは
別の名前を使う。版の退役はここを消さない(process の寿命まで保持する)。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, cast

#: 版ごとの module を載せる予約 namespace の接頭辞(loader と同じ値)。
_VERSIONED_PREFIX = "_te_versions."

_objects: dict[str, object] = {}
_keyed: dict[tuple[str, str], tuple[TaskLock, int]] = {}
_guard = threading.Lock()


class TaskLock:
    """版をまたいで共有する asyncio の排他 lock。再入しない。

    Tool の中で一回の操作だけを囲む。保持する task が同じ lock を要求すると、待ち続ける代わりに
    ``RuntimeError`` を送出する。内部の ``asyncio.Lock`` は空いていれば現在の event loop へ作り直す。
    """

    def __init__(self, label: str) -> None:
        self.label = label
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._owner: asyncio.Task[Any] | None = None

    def locked(self) -> bool:
        return self._lock.locked()

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError(f"{self.label}: a task-aware lock needs a running task")
        if self._owner is current:
            raise RuntimeError(f"{self.label} is not reentrant; the holding task requested it again")
        loop = asyncio.get_running_loop()
        if self._loop is not loop and not self._lock.locked():
            self._lock, self._loop = asyncio.Lock(), loop
        await self._lock.acquire()
        self._owner = current
        try:
            yield
        finally:
            self._owner = None
            self._lock.release()


def shared[T](name: str, factory: Callable[[], T]) -> T:
    """同じ名前なら全ての版に同じ object を返す。最初の呼出しだけが ``factory`` を使う。"""
    with _guard:
        if name not in _objects:
            value = factory()
            if type(value).__module__.startswith(_VERSIONED_PREFIX):
                raise TypeError(f"{name}: shared state must not be an instance of version-owned code")
            _objects[name] = value
        return cast(T, _objects[name])


def context_var(name: str, default: Any = None) -> ContextVar[Any]:
    """Host と部品が同じ名前で参照する ContextVar。"""
    return shared(name, lambda: ContextVar(name, default=default))


def task_lock(name: str) -> TaskLock:
    """名前で共有する :class:`TaskLock`。部品の排他(送信・保留中の作業など)に使う。"""
    return shared(name, lambda: TaskLock(name))


@asynccontextmanager
async def keyed_lock(namespace: str, key: str) -> AsyncIterator[None]:
    """(namespace, key) ごとの :class:`TaskLock` を全ての版で共有し、最後の利用者が去ると回収する。"""
    slot = (namespace, key)
    with _guard:
        lock, users = _keyed.get(slot, (TaskLock(f"keyed_lock({namespace!r}, {key!r})"), 0))
        _keyed[slot] = (lock, users + 1)
    try:
        async with lock.hold():
            yield
    finally:
        with _guard:
            lock, users = _keyed[slot]
            if users == 1:
                del _keyed[slot]
            else:
                _keyed[slot] = (lock, users - 1)
