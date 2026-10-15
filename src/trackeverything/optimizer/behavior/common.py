"""3 平台の取得が共有する値(出来事・一続きの出来事)・HTTP・本文の整形。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

#: 本人の本文と、契機になった他の人の本文の上限(字)。空白は一つに詰める。
SELF_TEXT_CHARS = 600
OTHER_TEXT_CHARS = 200
#: 本人を表す actor。
SELF = "self"
#: まとめた commit に残す題名の数。
_COMMIT_SUBJECTS = 5
#: 回数制限で待ち直す回数と、1 回の待ち秒数の範囲。
_RETRIES = 4
_MIN_WAIT = 1.0
_MAX_WAIT = 60.0


class FetchError(Exception):
    """平台の取得の失敗。資格情報を含めない。"""


@dataclass(frozen=True)
class Window:
    """取得する期間(UTC、両端を含む)。"""

    since: datetime
    until: datetime

    def contains(self, at: datetime) -> bool:
        return self.since <= at <= self.until


def clip(text: str, limit: int) -> str:
    """空白を一つに詰め、長ければ末尾を ``…`` で切る。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


@dataclass(frozen=True)
class Event:
    """一つの出来事。``actor`` は本人なら ``self``、他の人なら名前か login。``approx`` は時刻が推定であること。"""

    at: datetime
    actor: str
    kind: str
    text: str = ""
    url: str | None = None
    approx: bool = False

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"at": self.at.isoformat(timespec="seconds"), "actor": self.actor, "kind": self.kind}
        if self.text:
            data["text"] = clip(self.text, SELF_TEXT_CHARS if self.actor == SELF else OTHER_TEXT_CHARS)
        if self.url:
            data["url"] = self.url
        if self.actor == SELF and ai_marked(self.text):
            data["ai_marked"] = True
        if self.approx:
            data["approx_time"] = True
        return data


def episode_id(key: str) -> str:
    """Episode の鍵(``slack:<channel>:<日か thread>`` など)から、実行をまたいで変わらない短い id。"""
    return "e" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]


@dataclass
class Episode:
    """一続きの出来事(Issue・PR、thread、channel の一日)。本人の操作と、その契機になった他の人の出来事を持つ。"""

    key: str
    platform: str
    where: str
    subject: str
    url: str | None = None
    events: list[Event] = field(default_factory=list)

    def ordered(self) -> list[Event]:
        """重複を除いた時刻順。同じ時刻では他の人の出来事を先に置く。"""
        unique = {(event.at, event.actor, event.kind, event.url, event.text): event for event in self.events}
        return sorted(unique.values(), key=lambda event: (event.at, event.actor == SELF))

    def to_json(self) -> dict[str, Any]:
        """連続した本人の commit は一件にまとめ、件数・最後の時刻・先頭 5 件の題名を残す。"""
        data: dict[str, Any] = {"id": episode_id(self.key), "platform": self.platform, "where": self.where, "subject": self.subject}
        if self.url:
            data["url"] = self.url
        events: list[dict[str, Any]] = []
        subjects: list[str] = []
        for event in self.ordered():
            last = events[-1] if events else None
            if event.kind == "commit" and last is not None and last["kind"] == "commit" and last["actor"] == event.actor:
                last["count"] = last.get("count", 1) + 1
                last["until"] = event.at.isoformat(timespec="seconds")
                last.pop("url", None)
                if len(subjects) < _COMMIT_SUBJECTS:
                    subjects.append(event.text)
                    last["text"] = clip(" | ".join(subjects), SELF_TEXT_CHARS)
                if event.actor == SELF and ai_marked(event.text):
                    last["ai_marked"] = True
                continue
            events.append(event.to_json())
            subjects = [event.text] if event.kind == "commit" else []
        data["events"] = events
        return data


class Episodes:
    """鍵ごとに Episode をまとめる。最初に作った時の場所・題名を保つ。"""

    def __init__(self) -> None:
        self._episodes: dict[str, Episode] = {}

    def add(self, key: str, platform: str, where: str, subject: str, event: Event, *, url: str | None = None) -> None:
        self._episodes.setdefault(key, Episode(key, platform, where, subject, url)).events.append(event)

    def values(self) -> list[Episode]:
        return list(self._episodes.values())


@dataclass
class Fetched:
    """一つの平台の取得結果。``person`` は解決後の ID、``notes`` は取りこぼしなどの注記。"""

    person: str
    episodes: list[Episode]
    notes: list[str] = field(default_factory=list)


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def ai_marked(text: str) -> bool:
    """AI が書いた印(1 行目の 🤖、Claude の共著者行・生成表記)があるか。"""
    first = text.lstrip().split("\n", 1)[0]
    lowered = text.lower()
    return "🤖" in first or "co-authored-by: claude" in lowered or "generated with [claude code]" in lowered


def parse_time(value: Any) -> datetime | None:
    """ISO 8601(末尾 ``Z`` を含む)を UTC の datetime にする。読めなければ None。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _limited(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    # GitHub は primary・secondary の回数制限を 403 で返す。
    return response.status_code == 403 and (response.headers.get("x-ratelimit-remaining") == "0" or "rate limit" in response.text.lower())


def _wait_seconds(response: httpx.Response) -> float:
    wait: float | None = None
    try:
        if "retry-after" in response.headers:
            wait = float(response.headers["retry-after"])
        elif "x-ratelimit-reset" in response.headers:
            wait = float(response.headers["x-ratelimit-reset"]) - time.time()
        else:
            body = response.json()
            if isinstance(body, dict) and isinstance(body.get("retry_after"), int | float):
                wait = float(body["retry_after"])
    except ValueError:
        wait = None
    return min(max(wait if wait is not None else 2.0, _MIN_WAIT), _MAX_WAIT)


async def request_json(client: httpx.AsyncClient, method: str, url: str, *, headers: Mapping[str, str],
                       params: Mapping[str, Any] | None = None, json: Any = None) -> tuple[int, Any]:
    """回数制限の応答は指定の秒数だけ待って再試行し、最後の HTTP 状態と JSON 本文(読めなければ None)を返す。"""
    attempt = 0
    while True:
        response = await client.request(method, url, headers=dict(headers), params=dict(params) if params else None, json=json)
        if attempt >= _RETRIES or not _limited(response):
            break
        attempt += 1
        await asyncio.sleep(_wait_seconds(response))
    try:
        body = response.json()
    except ValueError:
        body = None
    return response.status_code, body
