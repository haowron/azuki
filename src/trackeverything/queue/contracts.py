"""投入の受理結果と、Redis から読んだ 1 件のメッセージ。"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict


class QueueUnavailableError(RuntimeError):
    """Redis への投入を確認できない。呼出元は受理を表示しない。"""


class PublishReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)
    #: ``XADD`` が返した stream id。メッセージの唯一の ID。
    message_id: str
    #: stream id の時刻(秒)。
    accepted_at: float


@dataclass(frozen=True, slots=True)
class StreamEntry:
    message_id: str
    #: field ``data`` の実行依頼 JSON。読めない場合もそのまま持つ。
    data: str
    #: これまでの配送回数。初回の読取りが 1。
    deliveries: int = 1
