"""外部接続の登録契約 — Webhook の検証と常駐接続を一つの単位で登録する。

``Connector`` は一つの外部サービスとの受信を表す。``webhook`` は ``POST /webhook/{name}`` の
検証関数(I/O を持たない同期関数)で、原形の ``EventRequest`` か protocol の応答を返す。
``listener`` は常駐接続(Socket Mode・Gateway)の開始・停止で、Host の event loop 上で await する。
queue プロセスの構成ルート(``api/app.py``)が具体の Connector を登録する。
共通の受付(token 検査・JSON 検証・status code・投入)は ``integration/webhook`` が持ち、
provider が迂回できない。イベントの内容による判断は受信側に置かない。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from trackeverything.core.abc.submission import EventRequest


class InvalidWebhookError(ValueError):
    """形式の検証に失敗した配信(HTTP 422)。"""


@dataclass(frozen=True)
class WebhookIntake:
    """検証の結果。``request`` を投入する。None なら ``response`` を同期で返し、投入しない。"""

    request: EventRequest | None
    response: dict[str, Any] | None = None


WebhookParser = Callable[[Mapping[str, str], dict[str, Any]], WebhookIntake]
"""``(header, JSON object の body)`` を検証する。失敗は :class:`InvalidWebhookError`。"""


class Listener(Protocol):
    """常駐接続。Host が受付開始後に登録順で開始し、終了時に逆順で停止する。"""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


_NAME = re.compile(r"[a-z0-9_-]+")


def without_token(payload: dict[str, Any]) -> dict[str, Any]:
    """原文の最上位にある ``token``(interaction token・verification token)だけを除く。"""
    return {key: value for key, value in payload.items() if key != "token"}


@dataclass(frozen=True)
class Connector:
    """一つの外部サービスとの受信。facet(webhook・listener)を一つ以上持つ。"""

    name: str
    webhook: WebhookParser | None = None
    listener: Listener | None = None

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise ValueError(f"connector name must match [a-z0-9_-]+: {self.name!r}")
        if self.webhook is None and self.listener is None:
            raise ValueError(f"connector {self.name!r} needs a webhook or a listener")
