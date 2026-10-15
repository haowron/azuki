"""実行中のメッセージを親とする投入口。部品の Tool が新しい実行依頼(自動開発の開始・Sentry の次の 1 件・PR の退去)を投入する。

runtime の構成(``core/builtin/app.py``)が自分の Producer を ``install`` で登録し、部品は ``publish_child`` を import して呼ぶ。
依頼は ``provider="internal"``・``external_id="<実行中のメッセージ id>:<key>"`` の ``EventRequest`` で、戻り値は受付(完了ではない)。
"""

from __future__ import annotations

from typing import Protocol

from pydantic import JsonValue

from trackeverything.core.abc.submission import REQUEST_ID, EventRequest
from trackeverything.queue.contracts import PublishReceipt


class _Publisher(Protocol):
    async def publish(self, request: EventRequest) -> PublishReceipt: ...


_producer: _Publisher | None = None


def install(producer: _Publisher | None) -> None:
    """このプロセスの投入口を登録する(None で外す)。"""
    global _producer
    _producer = producer


async def publish_child(payload: dict[str, JsonValue], key: str, *, session_id: str | None = None) -> PublishReceipt:
    """実行中のメッセージを親として ``payload`` の internal の依頼を投入する。実行の外・未登録・不正な依頼は例外で、何も投入しない。"""
    parent = REQUEST_ID.get()
    if parent is None:
        raise RuntimeError("a child request needs a running queue message as its parent")
    if _producer is None:
        raise RuntimeError("the queue producer is not installed in this process")
    request = EventRequest(provider="internal", external_id=f"{parent}:{key}", session_id=session_id, payload=payload)
    return await _producer.publish(request)
