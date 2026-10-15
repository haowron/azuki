"""transport に依存しない実行依頼。queue が運ぶ依頼はこの一種類だけで、run ID は実行側だけが発行する。"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class EventRequest(BaseModel):
    """queue の唯一の実行依頼。受信側・CLI・自動開発・保守 sweep が投入し、解釈は dispatch Agent が行う。

    種類は ``provider`` と payload が表す。``internal`` は外部サービス以外の投入で、payload に
    ``agent_name`` があれば名前指定の実行、なければ自動開発の開始である。``kind`` は旧形式
    (``agent.run``・``source.run``)のメッセージを読めないものとして判別するために残す。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["event.handle"] = "event.handle"
    provider: Literal["github", "discord", "slack", "internal"]
    external_id: str = Field(min_length=1, max_length=256)
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    session_id: str | None = None
    parent_session_id: str | None = None


#: 実行中のメッセージの queue ID(stream id)。Runtime が run の間だけ置く。
REQUEST_ID: ContextVar[str | None] = ContextVar("logical_request_id", default=None)
