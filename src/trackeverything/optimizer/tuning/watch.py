"""merge した調整の観察と取消しの値と判定。状態は表に持たず、版の履歴(commit の message の trailer)から読む。

調整の commit は ``optimizer:tuning <…>`` で始まり、``Tuning-Id``・``Tuning-Kind``・``Tuning-Agent``・``Tuning-From``・``Tuning-To``・
``Tuning-Path``・``Tuning-Baseline`` を持つ。取消しの commit は ``optimizer:tuning revert <…>`` で始まり、``Tuning-Revert`` を持つ。
調整が版で効いているかは祖先ではなく内容で決める: 版の ``Tuning-Path`` で Agent の類が ``Tuning-To`` を宣言していれば効いている。
これで人の merge commit(第二親の側に入った調整)や人の取消しでも判定が変わらない。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from trackeverything.core.versioning.store import Commit
from trackeverything.optimizer.tuning.records import AgentRow, StepRow

PREFIX = "optimizer:tuning "
REVERT_PREFIX = "optimizer:tuning revert "
#: merge した調整を観察する期間。この間、同じ Agent に新しい調整を出さない。
WATCH = timedelta(days=7)
#: 取り消した調整を再び出さない期間。
COOLDOWN = timedelta(days=30)
#: 判定に要る、調整が効いている版での実行の数。
WATCH_MIN = 10
#: 成功率がこれ以上下がれば取り消す。
OK_DROP = 0.05
#: effort: LLM 一応答の平均時間がこの割合を下回らなければ、効果なしとして取り消す。
EFFORT_GAIN = 0.95


@dataclass(frozen=True)
class Tuning:
    """版の履歴の調整(または取消し)の commit 一つ。"""

    commit: Commit
    trailers: Mapping[str, str]

    def _get(self, key: str) -> str:
        return self.trailers.get(key, "")

    @property
    def id(self) -> str:
        return self._get("Tuning-Id")

    @property
    def agent(self) -> str:
        return self._get("Tuning-Agent")

    @property
    def kind(self) -> str:
        return self._get("Tuning-Kind")

    @property
    def source(self) -> str:
        """変更前の値(effort なら元の LLM の名前)。"""
        return self._get("Tuning-From")

    @property
    def target(self) -> str:
        """変更後の値(effort なら新しい LLM の名前)。"""
        return self._get("Tuning-To")

    @property
    def path(self) -> str:
        return self._get("Tuning-Path")

    @property
    def reverted(self) -> str | None:
        """取消しの commit なら取り消した版 id。"""
        return self.trailers.get("Tuning-Revert")

    @property
    def baseline(self) -> dict[str, Any]:
        try:
            value = json.loads(self._get("Tuning-Baseline") or "{}")
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    @property
    def complete(self) -> bool:
        """観察と取消しに要る trailer が揃っているか。"""
        return all((self.id, self.agent, self.kind, self.source, self.target, self.path))


def trailers(message: str) -> dict[str, str]:
    """message の ``Key: value`` の行(``Tuning-`` で始まるものだけ)。"""
    found: dict[str, str] = {}
    for line in message.splitlines():
        key, sep, value = line.partition(": ")
        if sep and key.startswith("Tuning-"):
            found[key] = value.strip()
    return found


def message(title: str, values: Mapping[str, Any]) -> str:
    """調整の commit の message。値が文字列でなければ JSON にする。"""
    lines = [f"{key}: {value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)}"
             for key, value in values.items()]
    return f"{title}\n\n" + "\n".join(lines) + "\n"


class History:
    """版の履歴(``repo.log()``: main と保持した fork から辿れる全ての版)と main。"""

    def __init__(self, commits: Sequence[Commit], main: str) -> None:
        self.commits = {commit.id: commit for commit in commits}
        self.main = main
        prefixes: dict[str, str | None] = {}
        for ident in self.commits:
            prefixes[ident[:8]] = None if ident[:8] in prefixes else ident
        self._prefixes = prefixes
        line: set[str] = set()
        current: str | None = main
        while current is not None and current in self.commits and current not in line:
            line.add(current)
            current = self.commits[current].parent
        self._line = frozenset(line)

    def on_main_line(self, version_id: str) -> bool:
        """main から第一親を辿って届く版か。"""
        return version_id in self._line

    def resolve(self, label: str | None) -> str | None:
        """``c.<先頭 8 桁>`` の版 id。知らない・曖昧なら None。"""
        if not label or not label.startswith("c.") or len(label) != 10:
            return None
        return self._prefixes.get(label[2:])

    def tunings(self) -> list[Tuning]:
        """調整と取消しの commit(新しい順)。fork のままの候補も含む。"""
        found = [Tuning(commit, trailers(commit.message)) for commit in self.commits.values() if commit.message.startswith(PREFIX)]
        return sorted(found, key=lambda item: item.commit.created_at, reverse=True)


@dataclass(frozen=True)
class Metrics:
    """一群の実行の指標。``ok`` は cancelled を除いた成功の割合、``step_ms`` は LLM 一応答の平均時間。"""

    executions: int
    ok: float
    steps: float
    tokens: float
    step_ms: float

    def to_json(self) -> dict[str, Any]:
        return {"executions": self.executions, "ok": round(self.ok, 3), "steps": round(self.steps, 2),
                "tokens": round(self.tokens), "step_ms": round(self.step_ms)}


def metrics(rows: Iterable[AgentRow], steps: Mapping[str, Sequence[StepRow]]) -> Metrics:
    counted = [row for row in rows if row.status != "cancelled"]
    if not counted:
        return Metrics(0, 0.0, 0.0, 0.0, 0.0)
    flat = [step for row in counted for step in steps.get(row.execution_id, ())]
    return Metrics(
        executions=len(counted),
        ok=sum(1 for row in counted if row.status == "ok") / len(counted),
        steps=len(flat) / len(counted),
        tokens=sum(step.input_tokens + step.output_tokens for step in flat) / len(counted),
        step_ms=sum(step.duration_ms for step in flat) / len(flat) if flat else 0.0,
    )


def split(tuning: Tuning, rows: Iterable[tuple[AgentRow, bool]]) -> tuple[list[AgentRow], list[AgentRow]]:
    """``(実行, その版で調整が効いているか)`` を、効いていない版の調整の前 ``WATCH`` 以内の実行と、効いている版の調整の後の実行に分ける。"""
    before: list[AgentRow] = []
    after: list[AgentRow] = []
    for row, effective in rows:
        if effective and row.started_at >= tuning.commit.created_at:
            after.append(row)
        elif not effective and tuning.commit.created_at - WATCH <= row.started_at < tuning.commit.created_at:
            before.append(row)
    return before, after


def verdict(tuning: Tuning, before: Metrics, after: Metrics) -> tuple[str, str]:
    """``(watching | kept | revert, 理由)``。比べる前の指標は、実行が足りなければ merge 時の ``Tuning-Baseline``。"""
    if after.executions < WATCH_MIN:
        return "watching", f"{after.executions} executions on versions with the tuning (need {WATCH_MIN})"
    base = before.to_json() if before.executions >= WATCH_MIN else tuning.baseline
    if not base:
        return "kept", "no baseline to compare"
    if after.ok < float(base.get("ok", 0.0)) - OK_DROP:
        return "revert", f"success rate {after.ok:.0%} fell from {float(base['ok']):.0%}"
    if tuning.kind == "effort" and base.get("step_ms") and after.step_ms > float(base["step_ms"]) * EFFORT_GAIN:
        return "revert", f"LLM step time {after.step_ms:.0f} ms did not fall from {float(base['step_ms']):.0f} ms"
    return "kept", f"success {after.ok:.0%} (before {float(base.get('ok', 0.0)):.0%}), step {after.step_ms:.0f} ms"


def blocked(history: History, effective: Mapping[str, bool], now: datetime) -> tuple[set[str], set[str]]:
    """``(観察中の Agent, 冷却中の Tuning-Id)``。``effective`` は調整の版 id → main で効いているか。

    観察中: ``WATCH`` 以内の調整で main で効いているもの。冷却中: ``COOLDOWN`` 以内に取り消された調整(optimizer の取消し、
    または main の第一親の線にあるのに main で効いていない調整 = 人が戻した調整)。fork のままの候補は冷却しない。
    """
    watching: set[str] = set()
    cooling: set[str] = set()
    for item in history.tunings():
        recent = item.commit.created_at >= now - COOLDOWN
        if item.reverted:
            if recent:
                cooling.add(item.id)
            continue
        if effective.get(item.commit.id):
            if item.commit.created_at >= now - WATCH:
                watching.add(item.agent)
        elif recent and history.on_main_line(item.commit.id):
            cooling.add(item.id)
    return watching, cooling
