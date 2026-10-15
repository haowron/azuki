"""受信側・CLI・自動開発・保守 sweep が共有する投入口。実行依頼を JSON にして ``XADD`` する。実行基盤を起動しない。"""

from __future__ import annotations

import os
from typing import Self

from trackeverything.core.abc.submission import EventRequest
from trackeverything.queue.contracts import PublishReceipt
from trackeverything.queue.redis_stream import RedisStream


class Producer:
    def __init__(self, stream: RedisStream) -> None:
        self.stream = stream

    @classmethod
    def from_env(cls) -> Self:
        url = os.environ.get("QUEUE_REDIS_URL", "redis://127.0.0.1:6379/0")
        return cls(RedisStream(url, namespace=os.environ.get("QUEUE_NAMESPACE", "trackeverything")))

    async def __aenter__(self) -> Self:
        try:
            await self.stream.ping()
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def publish(self, request: EventRequest) -> PublishReceipt:
        """実行依頼を投入する。重複排除はしない。"""
        return await self.stream.publish(request.model_dump_json())

    async def close(self) -> None:
        await self.stream.close()
