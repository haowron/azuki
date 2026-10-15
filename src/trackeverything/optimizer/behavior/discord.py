"""Discord: bot が参加する guild ごとに本人の投稿を検索し、channel の一日ごとの出来事にして契機のメッセージを添える。

bot が読める channel だけを対象にし、DM は含まない。投稿は Search Guild Messages を ``author_id`` で検索する。初回の検索は
guild の索引作成中に 202(code 110000)を返すため、``retry_after`` だけ待って再試行する。契機は、返信なら返信先、
それ以外は 2 時間以内の直前の他の人(bot を含む)のメッセージ。
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from trackeverything.optimizer.behavior.common import (
    SELF,
    Episodes,
    Event,
    Fetched,
    FetchError,
    Window,
    as_dict,
    as_list,
    parse_time,
    request_json,
)

DISCORD_API = "https://discord.com/api/v10"
_EPOCH_MS = 1420070400000
_PAGE = 25
#: 検索の offset の上限。
_MAX_OFFSET = 9975
_INDEX_TRIES = 10
_INDEX_NOT_READY = 110000
_REPLY = 19
#: 投稿の契機とみなす直前のメッセージの間隔。
_TRIGGER_GAP = timedelta(hours=2)
_JST = ZoneInfo("Asia/Tokyo")


def snowflake(at: datetime) -> int:
    """その時刻の最小の snowflake。"""
    return (int(at.timestamp() * 1000) - _EPOCH_MS) << 22


class _Discord:
    def __init__(self, client: httpx.AsyncClient, token: str) -> None:
        self._client = client
        self._headers = {"Authorization": f"Bot {token}"}
        self._channels: dict[str, str] = {}
        self._messages: dict[tuple[str, str], dict[str, Any] | None] = {}

    async def get(self, path: str, params: dict[str, Any] | None = None, *, missing_ok: bool = False) -> Any:
        status, body = await request_json(self._client, "GET", f"{DISCORD_API}{path}", headers=self._headers, params=params)
        if missing_ok and status in (403, 404):
            return None
        if status >= 400:
            message = body.get("message") if isinstance(body, dict) else None
            raise FetchError(f"GET {path} failed: HTTP {status}" + (f": {message}" if message else ""))
        return body

    async def search(self, guild: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        """索引の作成中(202)は待って再試行する。"""
        for _ in range(_INDEX_TRIES):
            status, body = await request_json(self._client, "GET", f"{DISCORD_API}/guilds/{guild['id']}/messages/search",
                                              headers=self._headers, params=params)
            if status == 202 and isinstance(body, dict) and body.get("code") == _INDEX_NOT_READY:
                retry = body.get("retry_after")
                await asyncio.sleep(max(float(retry) if isinstance(retry, int | float) else 2.0, 2.0))
                continue
            if status >= 400 or not isinstance(body, dict):
                message = body.get("message") if isinstance(body, dict) else None
                raise FetchError(f"search in guild {guild.get('name')!r} failed: HTTP {status}" + (f": {message}" if message else ""))
            return body
        raise FetchError(f"search index of guild {guild.get('name')!r} is not available yet; try again later")

    async def channel(self, guild_id: str, channel_id: str) -> str:
        if not self._channels:
            for channel in await self.get(f"/guilds/{guild_id}/channels") or []:
                self._channels[str(channel.get("id"))] = str(channel.get("name") or "")
        if channel_id not in self._channels:
            # thread は guild の channel 一覧に含まれない。
            body = await self.get(f"/channels/{channel_id}", missing_ok=True)
            self._channels[channel_id] = str(body.get("name") or channel_id) if isinstance(body, dict) else channel_id
        return self._channels[channel_id]

    async def trigger(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """返信なら返信先、それ以外は channel の直前のメッセージ。"""
        inline = message.get("referenced_message")
        if isinstance(inline, dict):
            return inline
        reference = as_dict(message.get("message_reference"))
        channel_id = str(reference.get("channel_id") or message.get("channel_id"))
        if reference.get("message_id"):
            key = (channel_id, str(reference["message_id"]))
            if key not in self._messages:
                self._messages[key] = await self.get(f"/channels/{key[0]}/messages/{key[1]}", missing_ok=True)
            return self._messages[key]
        key = (channel_id, f"before:{message['id']}")
        if key not in self._messages:
            found = await self.get(f"/channels/{channel_id}/messages", {"before": message["id"], "limit": 1}, missing_ok=True)
            self._messages[key] = found[0] if isinstance(found, list) and found and isinstance(found[0], dict) else None
        return self._messages[key]


def _text(message: dict[str, Any]) -> str:
    lines = [str(message.get("content") or "")]
    lines.extend(f"[attachment] {item.get('filename')}" for item in message.get("attachments") or [] if isinstance(item, dict))
    # bot の通知は本文が embed にあることが多い。
    lines.extend(str(embed.get("description") or embed.get("title") or "") for embed in message.get("embeds") or [] if isinstance(embed, dict))
    return "\n".join(line for line in lines if line)


async def _messages(api: _Discord, guild: dict[str, Any], user: str, window: Window, notes: list[str]) -> dict[str, dict[str, Any]]:
    params = {"author_id": user, "min_id": str(snowflake(window.since)), "max_id": str(snowflake(window.until) + (1 << 22) - 1),
              "limit": _PAGE}
    messages: dict[str, dict[str, Any]] = {}
    offset = 0
    while True:
        body = await api.search(guild, {**params, "offset": offset})
        raw_total = body.get("total_results")
        total = raw_total if isinstance(raw_total, int) else 0
        groups = as_list(body.get("messages"))
        for group in groups:
            hits = [item for item in as_list(group) if isinstance(item, dict)]
            hit = next((item for item in hits if item.get("hit")), hits[0] if hits else None)
            if hit and isinstance(hit.get("id"), str):
                messages[hit["id"]] = hit
        if body.get("doing_deep_historical_index"):
            notes.append(f"discord {guild.get('name')!r}: Discord is still indexing older messages; results may be incomplete")
        # 検索は limit より少なく返すことがあるため、件数ではなく offset で進める。
        offset += _PAGE
        if not groups or offset >= total:
            return messages
        if offset > _MAX_OFFSET:
            notes.append(f"discord {guild.get('name')!r}: {total} messages, only the first {_MAX_OFFSET + _PAGE} are searchable")
            return messages


async def _guild(api: _Discord, guild: dict[str, Any], user: str, window: Window, episodes: Episodes, notes: list[str]) -> None:
    guild_id = str(guild["id"])
    for message in sorted((await _messages(api, guild, user, window, notes)).values(), key=lambda item: str(item.get("timestamp"))):
        at = parse_time(message.get("timestamp"))
        if at is None or not window.contains(at):
            continue
        channel_id = str(message.get("channel_id"))
        where = f"{guild.get('name')} #{await api.channel(guild_id, channel_id)}"
        day = at.astimezone(_JST).strftime("%Y-%m-%d")
        key, subject = f"discord:{channel_id}:{day}", f"{where} {day}"
        reply = message.get("type") == _REPLY
        episodes.add(key, "discord", where, subject, Event(at, SELF, "reply" if reply else "message", _text(message),
                                                           f"https://discord.com/channels/{guild_id}/{channel_id}/{message['id']}"))
        trigger = await api.trigger(message)
        author = as_dict((trigger or {}).get("author"))
        trigger_at = parse_time((trigger or {}).get("timestamp"))
        if trigger is None or trigger_at is None or str(author.get("id")) == user or (not reply and at - trigger_at > _TRIGGER_GAP):
            continue
        actor = str(author.get("global_name") or author.get("username") or "unknown") + (" (bot)" if author.get("bot") else "")
        episodes.add(key, "discord", where, subject, Event(trigger_at, actor, "message", _text(trigger)))


async def fetch_discord(client: httpx.AsyncClient, person: str, window: Window) -> Fetched:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise FetchError("DISCORD_BOT_TOKEN is required")
    user = os.environ.get("DISCORD_ALLOWED_USER_ID", "").strip() if person == "me" else person
    if not user.isdigit():
        raise FetchError("DISCORD_ALLOWED_USER_ID must be a Discord user ID to use 'me'")
    notes: list[str] = []
    episodes = Episodes()
    guilds = await _Discord(client, token).get("/users/@me/guilds")
    for guild in guilds if isinstance(guilds, list) else []:
        if isinstance(guild, dict) and guild.get("id"):
            # channel 名と契機の cache は guild ごとに分ける。
            await _guild(_Discord(client, token), guild, user, window, episodes, notes)
    return Fetched(person=user, episodes=episodes.values(), notes=notes)
