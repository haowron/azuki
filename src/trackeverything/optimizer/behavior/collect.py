"""3 平台の取得を並行に実行し、本人の一続きの出来事(Episode)の時刻順の一覧と平台ごとの取得状況にまとめる。

本人は各平台の資格情報の持ち主(``me``)。Slack は ``SLACK_TOKEN``、GitHub は ``GITHUB_TOKEN`` か ``GH_TOKEN``、Discord は
``DISCORD_BOT_TOKEN`` と ``DISCORD_ALLOWED_USER_ID`` を読む。読取りだけで、何も書かない。
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from trackeverything.optimizer.behavior.common import SELF, Episode, Fetched, FetchError, Window
from trackeverything.optimizer.behavior.discord import fetch_discord
from trackeverything.optimizer.behavior.github import fetch_github
from trackeverything.optimizer.behavior.slack import fetch_slack

PLATFORMS = ("slack", "github", "discord")
MAX_DAYS = 31
_TIMEOUT = 30.0
_ME = "me"

_FETCHERS: dict[str, Callable[[httpx.AsyncClient, str, Window], Awaitable[Fetched]]] = {
    "slack": fetch_slack, "discord": fetch_discord,
}
#: 各平台で見える範囲。分析の側で「記録がない」と「見えない」を区別するために返す。
SCOPES = {
    "slack": ("messages and emoji reactions the SLACK_TOKEN user can see: public channels, and private channels and DMs that user "
              "is a member of. Triggers: a reply's thread parent and the preceding message by someone else; a channel post's "
              "preceding message within 2 hours. A reaction's time is unknown, so it takes the reacted message's time (approx_time)"),
    "github": ("repositories GITHUB_TOKEN can read: every action by the person (opening, comments, reviews including approvals, "
               "line comments, merges, closes, labels, assignments, review requests, PR commits, ...) on issues and PRs the person "
               "is involved in or reviewed, with other people's opening, comments, reviews, review requests, merges, closes, reopens "
               "and ready-for-review as triggers; closes by a closing keyword are attributed to 'GitHub (自動)', not the merger; "
               "commits on default branches by repository and day. Cross-references, automatic branch deletion after merge, actions "
               "on other issues/PRs, branches outside PRs, stars and forks are not included"),
    "discord": ("guild channels the bot can read, by channel and day; direct messages are not included. Triggers: the replied "
                "message, or the preceding message by someone else (bots included) within 2 hours"),
}


async def _fetch(platform: str, client: httpx.AsyncClient, window: Window, exclude_personal_repos: bool) -> dict[str, Any] | Fetched:
    try:
        if platform == "github":
            return await fetch_github(client, _ME, window, exclude_personal_repos=exclude_personal_repos)
        return await _FETCHERS[platform](client, _ME, window)
    except (FetchError, ValueError) as exc:
        return {"error": str(exc)}
    except httpx.HTTPError as exc:
        return {"error": f"{type(exc).__name__}: request to {platform} failed"}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


async def collect_activity(*, days: int, exclude_personal_repos: bool, now: datetime | None = None) -> dict[str, Any]:
    """直近 ``days`` 日の本人の発言と操作を、一続きの出来事(Episode)ごとに時刻順で集める。

    戻り値は ``{"window": {...}, "episodes": [...], "coverage": {平台: {...}}}``。Episode は本人が期間内に何かした
    Issue・PR、thread、channel の一日で、本人の出来事(actor ``self``)とその契機・結果になった他の人の出来事を持ち、
    本人が期間内に最初に何かした順に並ぶ。連続した本人の commit は一件にまとめる。``exclude_personal_repos`` が真なら
    GitHub の本人が所有する repository を除く。平台の失敗は ``coverage`` に記録し、他の平台は返す。
    """
    if type(days) is not int or not 1 <= days <= MAX_DAYS:
        raise ValueError(f"'days' must be an integer from 1 to {MAX_DAYS}")
    until = (now or datetime.now(UTC)).astimezone(UTC)
    window = Window(since=until - timedelta(days=days), until=until)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        results = await asyncio.gather(*(_fetch(platform, client, window, exclude_personal_repos) for platform in PLATFORMS))
    episodes: list[Episode] = []
    coverage: dict[str, dict[str, Any]] = {}
    for platform, result in zip(PLATFORMS, results, strict=True):
        if not isinstance(result, Fetched):
            coverage[platform] = {"status": "failed", "person": _ME, "scope": SCOPES[platform], "error": result["error"]}
            continue
        own = [episode for episode in result.episodes if any(event.actor == SELF and window.contains(event.at) for event in episode.events)]
        actions = [event for episode in own for event in episode.events if event.actor == SELF and window.contains(event.at)]
        episodes.extend(own)
        coverage[platform] = {"status": "ok", "person": result.person, "episodes": len(own), "actions": len(actions),
                              "kinds": dict(Counter(event.kind for event in actions)), "scope": SCOPES[platform], "notes": result.notes}
    # 本人が期間内に最初に何かした時刻の順に並べる(作成などの起点は期間の外にあり得る)。
    episodes.sort(key=lambda episode: min(event.at for event in episode.events if event.actor == SELF and window.contains(event.at)))
    return {
        "window": {"since": window.since.isoformat(timespec="seconds"), "until": window.until.isoformat(timespec="seconds"), "days": days},
        "episodes": [episode.to_json() for episode in episodes],
        "coverage": coverage,
    }
