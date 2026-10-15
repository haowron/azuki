"""Redis Streams の投入・読取り・取り直し・ACK・dead。redis-py の基本 command だけを使う。

ACK はメッセージを stream から消す(``XACK`` と ``XDEL``)。配送回数が上限を超えたもの、
読めないものは dead stream へ移す。各操作は command を順に送り、途中の失敗は扱わない。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError

import trackeverything.config as config
from trackeverything.queue.contracts import PublishReceipt, QueueUnavailableError, StreamEntry


class RedisStream:
    #: consumer group と consumer の名前。runtime プロセスは namespace ごとに一つだけ動く。
    group = consumer = "runtime"

    def __init__(self, url: str, *, namespace: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", namespace):
            raise ValueError("invalid queue namespace")
        self.namespace = namespace
        self.client = Redis.from_url(url, decode_responses=True, socket_connect_timeout=5, socket_timeout=5)
        # redis-py の共用 command 宣言は sync/async union。生の非同期境界で一度だけ型を限定する。
        self._command = cast("Callable[..., Awaitable[Any]]", self.client.execute_command)
        root = f"te:{{{namespace}}}"
        self.key, self.dead_key, self.catalog_key = f"{root}:queue", f"{root}:dead", f"{root}:catalog"
        self._claim_cursor = "0-0"

    async def ping(self) -> None:
        try:
            await self._command("PING")
        except RedisError as exc:
            raise QueueUnavailableError("Redis is not reachable") from exc

    async def publish(self, data: str) -> PublishReceipt:
        try:
            message_id = str(await self.client.xadd(self.key, {"data": data}))
        except RedisError as exc:
            raise QueueUnavailableError("queue write could not be confirmed") from exc
        return PublishReceipt(message_id=message_id, accepted_at=int(message_id.split("-", 1)[0]) / 1000)

    async def ensure_group(self) -> None:
        try:
            await self.client.xgroup_create(self.key, self.group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def read(self, *, block_ms: int = 1000) -> StreamEntry | None:
        """新着を 1 件読む。``block_ms`` だけ待っても新着がなければ None。"""
        response = await self.client.xreadgroup(self.group, self.consumer, {self.key: ">"}, count=1, block=block_ms)
        for _, entries in response or []:
            for message_id, fields in entries:
                return StreamEntry(str(message_id), str((fields or {}).get("data", "")))
        return None

    async def reclaim(self, *, min_idle_ms: int = config.QUEUE_CLAIM_IDLE * 1000) -> StreamEntry | None:
        """``min_idle_ms`` 以上 ACK されていない pending を 1 件取り直し、配送回数を添える。"""
        cursor, entries, *_ = await self.client.xautoclaim(self.key, self.group, self.consumer, min_idle_ms, start_id=self._claim_cursor, count=1)
        self._claim_cursor = str(cursor)
        if not entries:
            return None
        message_id, fields = entries[0]
        pending = await self.client.xpending_range(self.key, self.group, min=message_id, max=message_id, count=1)
        deliveries = int(pending[0]["times_delivered"]) if pending else 1
        return StreamEntry(str(message_id), str((fields or {}).get("data", "")), deliveries)

    async def ack(self, message_id: str) -> None:
        """処理済みにする。ACK したメッセージは stream に残さない。"""
        await self.client.xack(self.key, self.group, message_id)
        await self.client.xdel(self.key, message_id)

    async def dead(self, entry: StreamEntry, reason: str) -> None:
        await self.client.xadd(self.dead_key, {"id": entry.message_id, "data": entry.data, "reason": reason[:2000]})
        await self.ack(entry.message_id)

    async def save_catalog(self, snapshot: dict[str, Any]) -> None:
        await self._command("HSET", self.catalog_key, "snapshot", json.dumps(snapshot), "heartbeat", str(time.time()))

    async def heartbeat(self) -> None:
        await self._command("HSET", self.catalog_key, "heartbeat", str(time.time()))

    async def catalog(self) -> dict[str, Any]:
        fields = await self._command("HGETALL", self.catalog_key)
        snapshot: dict[str, Any] = {"agents": [], "tools": [], "harnesses": [], "sources": [], "entrances": [], "as_of": None, "code_version": None}
        snapshot.update(json.loads(fields.get("snapshot", "{}")))
        snapshot["online"] = time.time() - float(fields.get("heartbeat", "0")) < 60
        return snapshot

    async def close(self) -> None:
        await self.client.aclose()
