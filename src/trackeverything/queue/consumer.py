"""runtime process の読取り元。Redis から 1 件ずつ取得し、ACK する。loop と同時実行の上限は Runtime が持つ。"""

from __future__ import annotations

from pydantic import ValidationError

import trackeverything.config as config
from trackeverything.core.abc.submission import EventRequest
from trackeverything.queue.redis_stream import RedisStream


class QueueConsumer:
    """取り直しを先に、なければ新着を 1 件読む。配送回数の超過と読めないメッセージ(旧形式を含む)は dead へ移す。"""

    def __init__(self, stream: RedisStream) -> None:
        self.stream = stream

    async def prepare(self) -> None:
        await self.stream.ensure_group()

    async def next(self) -> tuple[str, EventRequest] | None:
        """実行する 1 件(stream id と実行依頼)。読むものがない、または dead へ移した場合は None。"""
        entry = await self.stream.reclaim()
        if entry is None:
            entry = await self.stream.read()
        if entry is None:
            return None
        if entry.deliveries > config.QUEUE_MAX_DELIVERIES:
            await self.stream.dead(entry, f"delivered {entry.deliveries} times")
            return None
        try:
            request = EventRequest.model_validate_json(entry.data)
        except (ValidationError, ValueError) as exc:
            await self.stream.dead(entry, f"invalid message: {exc}")
            return None
        return entry.message_id, request

    async def ack(self, message_id: str) -> None:
        await self.stream.ack(message_id)
