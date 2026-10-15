"""Discord Gateway の受信。gateway frame の原文を session 付きで投入する。

SDK の client は Host の event loop 上の task(``client.start(token)``)として動く。
discord.py には MESSAGE_CREATE の原文を渡す event がないため、``enable_debug_events`` の
``on_socket_raw_receive`` で frame を解析する。剪定は送信者の身元(bot・webhook・本人以外)だけで、
内容による判断は dispatch Agent が行う。ボタンの応答(defer・followup)は SDK の ``on_interaction`` が
行い、同じ interaction ID の原文を投入する。本人の reaction の追加・取消し(MESSAGE_REACTION_ADD・REMOVE)も
``d`` を原形のまま投入し、絵文字と対象のメッセージの判断は dispatch Agent が行う。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

import discord

from trackeverything.core.abc.submission import EventRequest
from trackeverything.core.builtin.database import open_channel_store
from trackeverything.integration.connector import Connector, without_token
from trackeverything.queue.producer import Producer

log = logging.getLogger(__name__)

SessionResolver = Callable[[str, str], Awaitable[str]]
"""``(provider, channel_id)`` から channel 対応表上の session ID を返す解決口。"""

#: 原文を待つ interaction の上限。SDK の event と対応しなかった原文は古い順に捨てる。
_PENDING_LIMIT = 256
#: defer の後に原文を待つ秒数。
_RAW_WAIT = 5.0
#: 投入する reaction の frame。一括の取消し(REMOVE_ALL・REMOVE_EMOJI)は利用者を持たないため受けない。
_REACTION_EVENTS = ("MESSAGE_REACTION_ADD", "MESSAGE_REACTION_REMOVE")


def build_discord_gateway(producer: Producer) -> DiscordGateway | None:
    """受信 SDK を queue プロセスだけに構成する。"""
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        return None
    owner = os.environ.get("DISCORD_ALLOWED_USER_ID", "").strip()
    if not owner.isdecimal() or int(owner) <= 0:
        raise ValueError("DISCORD_ALLOWED_USER_ID must be a positive Discord user ID")
    general = os.environ.get("DISCORD_GENERAL_CHANNEL", "").strip() or os.environ.get("DISCORD_NOTIFY_CHANNEL", "").strip()
    return DiscordGateway(producer, token=token, owner_id=int(owner), resolve_session=open_channel_store().resolve_session, general_channel=general or None)


class _RawInteractions:
    """INTERACTION_CREATE の原文を interaction ID で SDK の Interaction に対応付ける。

    SDK は一つの frame について、原文の event task を作った後に同期処理のまま解析し、
    interaction の event task を作る。task は作成順に始まるため、通常は原文が先に届く。
    """

    def __init__(self) -> None:
        self._items: OrderedDict[str, asyncio.Future[dict[str, Any]]] = OrderedDict()

    def _slot(self, key: str) -> asyncio.Future[dict[str, Any]]:
        future = self._items.get(key)
        if future is None:
            future = self._items[key] = asyncio.get_running_loop().create_future()
            while len(self._items) > _PENDING_LIMIT:
                _, stale = self._items.popitem(last=False)
                stale.cancel()
        return future

    def put(self, data: dict[str, Any]) -> None:
        future = self._slot(str(data.get("id")))
        if not future.done():
            future.set_result(data)

    def discard(self, key: str) -> None:
        future = self._items.pop(key, None)
        if future is not None:
            future.cancel()

    async def take(self, key: str) -> dict[str, Any] | None:
        future = self._slot(key)
        try:
            return await asyncio.wait_for(asyncio.shield(future), _RAW_WAIT)
        except TimeoutError:
            return None
        except asyncio.CancelledError:
            # 上限で捨てた原文だけを欠落として扱い、呼出し元の取消しは伝える。
            task = asyncio.current_task()
            if future.cancelled() and (task is None or not task.cancelling()):
                return None
            raise
        finally:
            self._items.pop(key, None)


class DiscordGateway:
    """SDK が接続・heartbeat・再接続を所有し、Host が開始終了を所有する。"""

    def __init__(self, producer: Producer, *, token: str, owner_id: int, resolve_session: SessionResolver, general_channel: str | None = None) -> None:
        if not token or owner_id <= 0:
            raise ValueError("Discord token and a positive allowed user ID are required")
        self._producer, self._token, self._owner_id = producer, token, owner_id
        self._resolve_session, self._general = resolve_session, general_channel
        self._client = discord.Client(intents=discord.Intents(guilds=True, messages=True, message_content=True, reactions=True), enable_debug_events=True)
        for handler in (self.on_ready, self.on_socket_raw_receive, self.on_interaction):
            setattr(self._client, handler.__name__, handler)
        self._interactions = _RawInteractions()
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._error: str | None = None

    async def on_ready(self) -> None:
        log.info("Discord Gateway connected (bot=%s)", self._client.user)
        self._ready.set()

    async def on_socket_raw_receive(self, message: str | bytes) -> None:
        """dispatch frame の原文を解析する。原文の保持は await の前に終える。"""
        text = message.decode() if isinstance(message, bytes) else message
        if "MESSAGE_CREATE" not in text and "INTERACTION_CREATE" not in text and "MESSAGE_REACTION_" not in text:
            return
        try:
            frame = json.loads(text)
        except ValueError:
            return
        data = frame.get("d") if isinstance(frame, dict) and frame.get("op") == 0 else None
        if not isinstance(data, dict):
            return
        if frame.get("t") == "INTERACTION_CREATE":
            self._interactions.put(data)
        elif frame.get("t") == "MESSAGE_CREATE":
            await self._on_message(data)
        elif frame.get("t") in _REACTION_EVENTS:
            await self._on_reaction(str(frame["t"]), data)

    async def _on_message(self, data: dict[str, Any]) -> None:
        author = data.get("author")
        if not isinstance(author, dict) or author.get("bot") or data.get("webhook_id") is not None or str(author.get("id")) != str(self._owner_id):
            return
        try:
            session_id, parent_session_id = await self._sessions(str(data["channel_id"]))
            await self._producer.publish(EventRequest(provider="discord", external_id=str(data["id"]), payload=data,
                                                            session_id=session_id, parent_session_id=parent_session_id))
        except Exception:
            log.exception("Discord message was not durably accepted (message=%s)", data.get("id"))

    async def _on_reaction(self, event: str, data: dict[str, Any]) -> None:
        """本人の reaction だけを投入する。external_id は frame の種類・対象メッセージ・絵文字(custom は ID)。"""
        if str(data.get("user_id")) != str(self._owner_id):
            return
        emoji = data.get("emoji")
        key = (emoji.get("id") or emoji.get("name")) if isinstance(emoji, dict) else None
        try:
            session_id, parent_session_id = await self._sessions(str(data["channel_id"]))
            await self._producer.publish(EventRequest(provider="discord", external_id=f"{event}:{data['message_id']}:{key}"[:256], payload=data,
                                                            session_id=session_id, parent_session_id=parent_session_id))
        except Exception:
            log.exception("Discord reaction was not durably accepted (message=%s)", data.get("message_id"))

    async def _sessions(self, channel_id: str) -> tuple[str | None, str | None]:
        """general channel は session を付けない。thread は SDK の cache から親 channel を得る。"""
        if channel_id == self._general:
            return None, None
        session_id = await self._resolve_session("discord", channel_id)
        channel = self._client.get_channel(int(channel_id)) if channel_id.isdecimal() else None
        parent = getattr(channel, "parent_id", None)
        if parent is None or str(parent) == self._general:
            return session_id, None
        return session_id, await self._resolve_session("discord", str(parent))

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """本人以外の操作は拒否し、本人の操作は defer の後に原文を投入する。"""
        key = str(interaction.id)
        if interaction.type != discord.InteractionType.component:
            self._interactions.discard(key)
            return
        if interaction.user.bot or interaction.user.id != self._owner_id:
            self._interactions.discard(key)
            await interaction.response.send_message("🤖 只有请求修改的本人可以确认。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            data = await self._interactions.take(key)
            if data is None:
                raise LookupError("raw interaction payload is unavailable")
            session_id, parent_session_id = await self._sessions(str(data.get("channel_id")))
            await self._producer.publish(EventRequest(provider="discord", external_id=key, payload=without_token(data),
                                                            session_id=session_id, parent_session_id=parent_session_id))
        except Exception:
            log.exception("Discord confirmation could not be published (interaction=%s)", key)
            await interaction.followup.send("🤖 未能保存执行请求。请重试。", ephemeral=True)
            return
        await interaction.followup.send("🤖 已保存确认请求。结果会发到当前会话。", ephemeral=True)

    async def start(self) -> None:
        """SDK の接続を Host の loop の task として始め、ready(または接続の失敗)まで待つ。"""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._serve(), name="te-discord")
        try:
            await asyncio.wait_for(self._ready.wait(), 30)
        except TimeoutError:
            raise RuntimeError("Discord Gateway did not become ready within 30 seconds") from None
        if self._error:
            raise RuntimeError(f"Discord Gateway failed: {self._error}")

    async def _serve(self) -> None:
        try:
            async with self._client:
                await self._client.start(self._token)
        except Exception as exc:
            self._error = type(exc).__name__
            log.error("Discord Gateway stopped: %s", self._error)
        finally:
            self._ready.set()

    async def stop(self) -> None:
        if self._task is None:
            return
        await self._client.close()
        try:
            # 停止の期限で task 自体は取り消さない(SDK が自身の後始末を終えるまで待つ)。
            await asyncio.wait_for(asyncio.shield(self._task), 10)
        except TimeoutError:
            raise RuntimeError("Discord Gateway did not stop") from None


def discord_connector(gateway: DiscordGateway) -> Connector:
    """Discord Gateway の常駐接続。Webhook の route は持たない。"""
    return Connector("discord", listener=gateway)
