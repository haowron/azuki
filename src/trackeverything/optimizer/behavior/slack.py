"""Slack: 本人の投稿と絵文字の反応を、thread・channel の一日ごとの出来事にし、契機になったメッセージを添える。

``SLACK_TOKEN``(user token)の本人が見られる範囲だけを読む。``search.messages`` の ``from:`` は一つの検索で新しい順に
100 件までしか返さないため、超えた期間は日付で二分して検索し直す。契機は、thread の返信なら thread の親と直前の他の人の
メッセージ、channel への投稿なら 2 時間以内の直前の他の人のメッセージ。``reactions.list`` は反応した時刻を返さないため、
反応の時刻は反応先のメッセージの時刻とする(``approx_time``)。
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
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
    clip,
    request_json,
)

SLACK_API = "https://slack.com/api"
#: 一つの検索で取得できる件数の上限(2 page 目以降は ``max_total_pages`` で返らない)。
_SEARCH_CAP = 100
_CONCURRENCY = 4
#: ``reactions.list`` を読む page 数の上限(1 page 100 件)。
_REACTION_PAGES = 10
#: channel への投稿の契機とみなす直前のメッセージの間隔。
_TRIGGER_GAP = timedelta(hours=2)
_JST = ZoneInfo("Asia/Tokyo")


async def _call(client: httpx.AsyncClient, token: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    status, body = await request_json(client, "GET", f"{SLACK_API}/{method}", headers={"Authorization": f"Bearer {token}"}, params=params)
    if not isinstance(body, dict):
        raise FetchError(f"{method} returned HTTP {status} without a JSON object")
    if not body.get("ok"):
        raise FetchError(f"{method} failed: {body.get('error') or f'HTTP {status}'}")
    return body


async def _search(client: httpx.AsyncClient, token: str, user: str, start: date, end: date,
                  found: dict[tuple[str, str], dict[str, Any]], notes: list[str]) -> None:
    """``start``〜``end``(両端を含む日付)の投稿を ``found`` へ加える。100 件を超えれば二分する。"""
    query = f"from:<@{user}> after:{(start - timedelta(days=1)).isoformat()} before:{(end + timedelta(days=1)).isoformat()}"
    body = await _call(client, token, "search.messages", {"query": query, "count": _SEARCH_CAP, "sort": "timestamp", "sort_dir": "desc"})
    messages = body.get("messages")
    if not isinstance(messages, dict) or not isinstance(messages.get("matches"), list):
        raise FetchError("search.messages returned no matches array")
    matches = [match for match in messages["matches"] if isinstance(match, dict)]
    total = messages.get("total")
    total = total if isinstance(total, int) else len(matches)
    if total > len(matches) and start < end:
        middle = start + (end - start) // 2
        await _search(client, token, user, start, middle, found, notes)
        await _search(client, token, user, middle + timedelta(days=1), end, found, notes)
        return
    if total > len(matches):
        notes.append(f"slack {start.isoformat()}: {total} messages in one day, only the newest {len(matches)} are searchable")
    for match in matches:
        channel = match.get("channel")
        if isinstance(channel, dict) and isinstance(match.get("ts"), str):
            found[(str(channel.get("id")), match["ts"])] = match


def _moment(ts: str) -> datetime:
    return datetime.fromtimestamp(float(ts), UTC)


def _thread_ts(permalink: Any) -> str | None:
    return parse_qs(urlsplit(permalink).query).get("thread_ts", [None])[0] if isinstance(permalink, str) else None


def _day(at: datetime) -> str:
    return at.astimezone(_JST).strftime("%Y-%m-%d")


class _Lookups:
    """利用者名・channel の表示名・thread・channel の直前のメッセージを一度だけ引く。引けなかった数を数える。"""

    def __init__(self, client: httpx.AsyncClient, token: str, user: str) -> None:
        self._client, self._token, self.user = client, token, user
        self._limit = asyncio.Semaphore(_CONCURRENCY)
        # 同じ鍵の並行な問合せを一回にまとめる。
        self._locks: dict[tuple[str, ...], asyncio.Lock] = {}
        self._names: dict[str, str] = {}
        self._channels: dict[str, str] = {}
        self._threads: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._previous: dict[tuple[str, str], dict[str, Any] | None] = {}
        self.unreadable = 0

    async def _get(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        try:
            async with self._limit:
                return await _call(self._client, self._token, method, params)
        except FetchError:
            self.unreadable += 1
            return None

    async def name(self, user: str) -> str:
        async with self._locks.setdefault(("user", user), asyncio.Lock()):
            if user not in self._names:
                profile = as_dict((await self._get("users.info", {"user": user}) or {}).get("user"))
                name = profile.get("real_name") or profile.get("name")
                self._names[user] = name if isinstance(name, str) and name else user
        return self._names[user]

    async def actor(self, message: dict[str, Any]) -> str:
        user = message.get("user")
        if user == self.user:
            return SELF
        return await self.name(user) if isinstance(user, str) and user else str(message.get("username") or "bot")

    async def channel(self, channel_id: str) -> str:
        async with self._locks.setdefault(("channel", channel_id), asyncio.Lock()):
            if channel_id not in self._channels:
                info = as_dict((await self._get("conversations.info", {"channel": channel_id}) or {}).get("channel"))
                self._channels[channel_id] = await _where(info or {"id": channel_id}, self)
        return self._channels[channel_id]

    async def thread(self, channel: str, thread_ts: str) -> list[dict[str, Any]]:
        key = (channel, thread_ts)
        async with self._locks.setdefault(("thread", *key), asyncio.Lock()):
            if key not in self._threads:
                body = await self._get("conversations.replies", {"channel": channel, "ts": thread_ts, "limit": 200})
                self._threads[key] = [message for message in as_list((body or {}).get("messages")) if isinstance(message, dict)]
        return self._threads[key]

    async def previous(self, channel: str, ts: str) -> dict[str, Any] | None:
        """channel で ``ts`` の直前のメッセージ。"""
        key = (channel, ts)
        async with self._locks.setdefault(("previous", *key), asyncio.Lock()):
            if key not in self._previous:
                body = await self._get("conversations.history", {"channel": channel, "latest": ts, "limit": 1, "inclusive": "false"})
                found = as_list((body or {}).get("messages"))
                self._previous[key] = found[0] if found and isinstance(found[0], dict) else None
        return self._previous[key]


async def _where(channel: dict[str, Any], lookups: _Lookups) -> str:
    name = str(channel.get("name") or channel.get("id") or "")
    if channel.get("is_im"):
        # 検索結果の DM は name に、conversations.info の DM は user に相手の ID を持つ。
        other = str(channel.get("user") or name)
        return f"DM with {await lookups.name(other)}" if other[:1] in ("U", "W") else "DM"
    if channel.get("is_mpim"):
        return f"group DM {name}"
    return f"#{name}"


class _Collector:
    """本人の投稿・反応と、その契機になった他の人のメッセージを Episode に加える。"""

    def __init__(self, lookups: _Lookups, window: Window) -> None:
        self._lookups, self._window = lookups, window
        self.episodes = Episodes()

    async def _trigger(self, key: str, where: str, subject: str, message: dict[str, Any]) -> datetime | None:
        """他の人のメッセージを契機として加え、その時刻を返す。本人のメッセージは加えない。"""
        actor = await self._lookups.actor(message)
        ts = message.get("ts")
        if actor == SELF or not isinstance(ts, str):
            return None
        self.episodes.add(key, "slack", where, subject, Event(_moment(ts), actor, "message", str(message.get("text") or "")))
        return _moment(ts)

    async def message(self, match: dict[str, Any]) -> None:
        ts, channel = match["ts"], as_dict(match.get("channel"))
        at = _moment(ts)
        if not self._window.contains(at):
            return
        channel_id, where = str(channel.get("id")), await _where(channel, self._lookups)
        permalink = match.get("permalink") if isinstance(match.get("permalink"), str) else None
        thread_ts = _thread_ts(permalink)
        text = str(match.get("text") or "")
        if thread_ts is None:
            key, subject = f"slack:{channel_id}:{_day(at)}", f"{where} {_day(at)}"
            self.episodes.add(key, "slack", where, subject, Event(at, SELF, "message", text, permalink))
            previous = await self._lookups.previous(channel_id, ts)
            previous_ts = previous.get("ts") if previous else None
            if previous and isinstance(previous_ts, str) and at - _moment(previous_ts) <= _TRIGGER_GAP:
                await self._trigger(key, where, subject, previous)
            return
        thread = await self._lookups.thread(channel_id, thread_ts)
        parent = thread[0] if thread else {}
        key, subject = f"slack:{channel_id}:{thread_ts}", f"thread: {clip(str(parent.get('text') or text), 60)}"
        reply = thread_ts != ts
        self.episodes.add(key, "slack", where, subject, Event(at, SELF, "reply" if reply else "message", text, permalink))
        if not reply:
            return
        # 契機: thread の親と、この返信の直前の他の人のメッセージ。
        before = [item for item in thread if isinstance(item.get("ts"), str) and float(item["ts"]) < float(ts)]
        latest = next((item for item in reversed(before) if item.get("user") != self._lookups.user), None)
        for candidate in (parent, latest):
            if candidate:
                await self._trigger(key, where, subject, candidate)

    async def reaction(self, entry: dict[str, Any], names: list[str]) -> None:
        message = as_dict(entry.get("message"))
        ts, channel_id = str(message["ts"]), str(entry.get("channel"))
        at = _moment(ts)
        where = await self._lookups.channel(channel_id)
        thread_ts = message.get("thread_ts")
        if isinstance(thread_ts, str):
            key, subject = f"slack:{channel_id}:{thread_ts}", f"thread: {clip(str(message.get('text') or ''), 60)}"
        else:
            key, subject = f"slack:{channel_id}:{_day(at)}", f"{where} {_day(at)}"
        await self._trigger(key, where, subject, message)
        permalink = message.get("permalink") if isinstance(message.get("permalink"), str) else None
        self.episodes.add(key, "slack", where, subject,
                          Event(at, SELF, "reaction", " ".join(f":{name}:" for name in names), permalink, approx=True))


async def _reactions(client: httpx.AsyncClient, token: str, user: str, window: Window, collector: _Collector, notes: list[str]) -> None:
    """本人が絵文字で反応した、期間内のメッセージ。token に ``reactions:read`` がなければ注記して何もしない。"""
    cursor: str | None = None
    for _ in range(_REACTION_PAGES):
        params: dict[str, Any] = {"user": user, "full": "true", "limit": 100}
        if cursor:
            params["cursor"] = cursor
        try:
            body = await _call(client, token, "reactions.list", params)
        except FetchError as exc:
            if "missing_scope" not in str(exc):
                raise
            notes.append("slack reactions skipped: SLACK_TOKEN lacks the reactions:read scope")
            return
        recent = False
        tasks = []
        for raw in as_list(body.get("items")):
            entry = as_dict(raw)
            message = as_dict(entry.get("message"))
            ts = message.get("ts")
            if entry.get("type") != "message" or not isinstance(ts, str) or _moment(ts) < window.since:
                continue
            recent = True
            names = [str(reaction.get("name")) for reaction in map(as_dict, as_list(message.get("reactions"))) if user in as_list(reaction.get("users"))]
            if names and window.contains(_moment(ts)):
                tasks.append(collector.reaction(entry, names))
        await asyncio.gather(*tasks)
        cursor = as_dict(body.get("response_metadata")).get("next_cursor") or None
        # 反応の新しい順に並ぶため、期間内のメッセージへの反応が一件もない page で止める。
        if not cursor or not recent:
            return
    notes.append(f"slack reactions: only the newest {_REACTION_PAGES * 100} reactions are read")


async def fetch_slack(client: httpx.AsyncClient, person: str, window: Window) -> Fetched:
    token = os.environ.get("SLACK_TOKEN", "").strip()
    if not token:
        raise FetchError("SLACK_TOKEN is required")
    user = str((await _call(client, token, "auth.test", {})).get("user_id") or "") if person == "me" else person
    if not user:
        raise FetchError("auth.test returned no user_id")
    notes: list[str] = []
    found: dict[tuple[str, str], dict[str, Any]] = {}
    # 検索の日付は利用者の時間帯で解釈されるため前後に一日広げ、ts で期間を絞る。
    await _search(client, token, user, (window.since - timedelta(days=1)).date(), (window.until + timedelta(days=1)).date(), found, notes)
    lookups = _Lookups(client, token, user)
    collector = _Collector(lookups, window)
    await asyncio.gather(*(collector.message(match) for match in found.values()))
    # 投稿の後に反応を加え、同じ thread の Episode の題名を投稿の側にそろえる。
    await _reactions(client, token, user, window, collector, notes)
    if lookups.unreadable:
        notes.append(f"slack: {lookups.unreadable} lookups (names, threads or preceding messages) could not be read")
    return Fetched(person=user, episodes=collector.episodes.values(), notes=notes)
