"""観点 behavior の survey の入力: 本人の行動を観測の範囲・時間帯・出来事の流れの要約(文字列)にする。

出来事は ``## [<id>] [<平台>] <場所> | <題名> <URL>`` の見出しと、時刻(JST)または前の行からの経過を先頭に置いた行で表す。
上限に収まるまで本文を短くし、なお超える分は末尾を落とす。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

#: 要約の上限(文字)。
DIGEST_CHARS = 100_000
#: 本人・他の人の本文の長さと URL の有無の段階。要約が上限に収まる最初の段階を使う。
_LEVELS = ((400, 160, True), (200, 80, True), (120, 50, False))
_JST = ZoneInfo("Asia/Tokyo")
#: URL を根拠として残す本人の出来事。
_CITED = {"message", "reply", "comment", "review", "review_comment"}


@dataclass(frozen=True)
class Digest:
    """``text`` は LLM に渡す要約、``episodes`` は要約に載った出来事(id → その出来事の部分の文字列、要約の順)。"""

    text: str
    episodes: Mapping[str, str]


def _jst(at: str) -> datetime:
    return datetime.fromisoformat(at).astimezone(_JST)


def _short(value: str | None, limit: int) -> str:
    """改行を詰めた一行。長ければ末尾を切る。"""
    flat = " ".join(str(value or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _elapsed(before: datetime, after: datetime) -> str:
    minutes = int((after - before).total_seconds() // 60)
    if minutes < 60:
        return f"+{minutes}分"
    return f"+{minutes // 60}時間{minutes % 60:02d}分" if minutes < 48 * 60 else f"+{minutes // (24 * 60)}日"


def _scope(activity: Mapping[str, Any]) -> list[str]:
    window = activity.get("window") or {}
    lines = [f"期間: {_jst(window['since']):%m-%d %H:%M} 〜 {_jst(window['until']):%m-%d %H:%M} (JST、{window.get('days')} 日)"] if window else []
    for platform, coverage in (activity.get("coverage") or {}).items():
        if coverage.get("status") == "ok":
            lines.append(f"- {platform}: 出来事 {coverage.get('episodes')} 件・本人の行動 {coverage.get('actions')} 件 {coverage.get('kinds')}。"
                         f"見える範囲: {coverage.get('scope')}" + (f"。注記: {'; '.join(coverage['notes'])}" if coverage.get("notes") else ""))
        else:
            lines.append(f"- {platform}: {coverage.get('status')}" + (f"({coverage.get('error')})" if coverage.get("error") else ""))
    return lines


def _rhythm(activity: Mapping[str, Any], episodes: Sequence[Mapping[str, Any]]) -> list[str]:
    """平台ごとの本人の行動の時間帯(JST の時)と曜日の件数。"""
    window = activity.get("window") or {}
    since, until = window.get("since", ""), window.get("until", "~")
    own = [(episode["platform"], _jst(event["at"])) for episode in episodes for event in episode["events"]
           if event["actor"] == "self" and since <= event["at"] <= until]
    lines: list[str] = []
    for platform in sorted({platform for platform, _ in own}):
        moments = [moment for name, moment in own if name == platform]
        hours, days = Counter(moment.hour for moment in moments), Counter(moment.strftime("%a") for moment in moments)
        lines.append(f"- {platform} 時: " + ", ".join(f"{hour:02d}時 {hours[hour]}" for hour in sorted(hours)))
        lines.append(f"- {platform} 曜日: " + ", ".join(f"{day} {count}" for day, count in days.most_common()))
    return lines


def _event_line(event: Mapping[str, Any], previous: datetime | None, own_chars: int, other_chars: int, cite: bool) -> str:
    moment = _jst(event["at"])
    stamp = f"{moment:%m-%d(%a) %H:%M}" if previous is None or previous.date() != moment.date() else _elapsed(previous, moment)
    own = event["actor"] == "self"
    kind = event["kind"] + (f" {event['count']}件" if event.get("count") else "")
    if event.get("until"):
        kind += f"(〜{_jst(event['until']):%H:%M})"
    line = f"- {stamp} {'本人' if own else event['actor']} {kind}"
    if event.get("text"):
        line += f": {_short(event['text'], own_chars if own else other_chars)}"
    if event.get("approx_time"):
        line += " (時刻は反応先のメッセージ)"
    if event.get("ai_marked"):
        line += " 🤖"
    if cite and own and event.get("url") and event["kind"] in _CITED:
        line += f" <{event['url']}>"
    return line


def _episode(episode: Mapping[str, Any], own_chars: int, other_chars: int, cite: bool) -> str:
    lines = [f"## [{episode['id']}] [{episode['platform']}] {episode['where']} | {_short(episode['subject'], 80)}"
             + (f" <{episode['url']}>" if episode.get("url") else "")]
    previous: datetime | None = None
    for event in episode["events"]:
        lines.append(_event_line(event, previous, own_chars, other_chars, cite))
        previous = _jst(event.get("until") or event["at"])
    return "\n".join(lines)


def build_digest(activity: Mapping[str, Any], episodes: Sequence[Mapping[str, Any]], *, limit: int = DIGEST_CHARS) -> Digest:
    """``collect_activity`` の結果のうち ``episodes`` を要約にする。上限に収まるまで本文を短くし、なお超えれば末尾を落とす。"""
    text = ""
    parts: dict[str, str] = {}
    for own_chars, other_chars, cite in _LEVELS:
        parts = {episode["id"]: _episode(episode, own_chars, other_chars, cite) for episode in episodes}
        sections = [
            ("観測の範囲", "\n".join(_scope(activity))),
            ("本人の行動の時間帯(JST)", "\n".join(_rhythm(activity, episodes))),
            ("出来事の流れ(時刻は JST。同じ日の 2 行目以降は前の行からの経過。🤖 は AI が書いた印)", "\n".join(parts.values())),
        ]
        text = "\n\n".join(f"# {title}\n" + (body or "(なし)") for title, body in sections)
        if len(text) <= limit:
            return Digest(text, parts)
    text = text[:limit] + f"\n\n(要約が {limit} 字を超えたため、以降を省いた)"
    return Digest(text, {key: part for key, part in parts.items() if f"## [{key}]" in text})
