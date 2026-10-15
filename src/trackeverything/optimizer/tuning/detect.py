"""観点 tuning の探査: ``logs`` の行から調整の候補(``Opportunity``)を Python だけで見つける。I/O も LLM も使わない。

各探査は一つの種類の無駄を、根拠の run_id と見積もり(省ける token と LLM の時間)付きで返す。
``effort`` だけが変更まで進み、他は報告する(docs/components/OPTIMIZER.md)。
"""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from itertools import pairwise
from typing import Any

from trackeverything.optimizer.tuning.records import AgentRow, StepRow

#: effort: 対象にする宣言(下げられる先の順)。
LOWER = {"max": ("low", "high"), "high": ("low",)}
#: effort: 判定に要る ok の LLM 応答の数・推論の割合の下限・推論以外の出力の p90 の上限(token)。
EFFORT_MIN_STEPS = 20
EFFORT_REASONING_SHARE = 0.7
EFFORT_VISIBLE_P90 = 1000
#: 同じ入力・依頼の繰返しとみなす回数。
REPEAT_MIN = 5
REQUEST_REPEAT_MIN = 3
#: 先読み: 最初の応答の呼出しの引数が依頼に現れる割合と、判定する実行の数。
PREFETCH_SHARE = 0.8
PREFETCH_MIN = 10
PREFETCH_SAMPLE = 30
#: 単発の呼出しが続く応答の数。
CHAIN_MIN = 3
#: 一回の呼出しで増えた入力(token)と、その後に運ばれた応答の数。
LARGE_OUTPUT_TOKENS = 20_000
LARGE_OUTPUT_CARRIED = 2
#: cache: 最初の応答の平均入力(token)の下限と、cache の割合の上限。
CACHE_MIN_INPUT = 5_000
CACHE_SHARE = 0.5
CACHE_MIN = 10
#: 子 Agent: 平均の応答数の上限と判定に要る実行の数。
CHILD_MAX_STEPS = 2.0
CHILD_MIN = 10
#: Skill: 読まれる実行の割合と、判定に要る実行の数。
SKILL_SHARE = 0.8
SKILL_MIN = 10
#: 起動: 同じ対象の前の実行から、この時間内に始まった実行(実行中の重なりを含む)を重複とみなす。
TRIGGER_GAP = timedelta(minutes=10)
TRIGGER_MIN = 5
#: 根拠に載せる run_id の上限。
EVIDENCE = 10
_OWN = "optimizer"


@dataclass(frozen=True)
class Opportunity:
    """調整の候補一つ。``tokens`` は省ける見込みの token、``seconds`` は LLM の時間。``detail`` は種類ごとの数値。"""

    kind: str
    agent: str
    summary: str
    evidence: tuple[str, ...] = ()
    tokens: int = 0
    seconds: float = 0.0
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """``Tuning-Id`` などに使う一意の名前。"""
        target = self.detail.get("target")
        return f"{self.kind}/{self.agent}" + (f"/{target}" if target else "")


def recent_runs(items: Iterable[Any]) -> tuple[str, ...]:
    """新しい順の run_id(重複なし、``EVIDENCE`` 件まで)。"""
    ordered = sorted(items, key=lambda item: item.started_at, reverse=True)
    return tuple(dict.fromkeys(item.run_id for item in ordered))[:EVIDENCE]


def _tokens(steps: Iterable[StepRow]) -> int:
    return sum(step.input_tokens + step.output_tokens for step in steps)


def _seconds(steps: Iterable[StepRow]) -> float:
    return sum(step.duration_ms for step in steps) / 1000


def _p90(values: Sequence[int]) -> float:
    if len(values) < 2:
        return float(values[0]) if values else 0.0
    return statistics.quantiles(values, n=10)[-1]


def by_execution(steps: Iterable[StepRow]) -> dict[str, list[StepRow]]:
    """実行 id → その LLM 応答(記録順)。"""
    grouped: dict[str, list[StepRow]] = defaultdict(list)
    for step in sorted(steps, key=lambda item: item.id):
        grouped[step.execution_id].append(step)
    return dict(grouped)


def effort(steps: Sequence[StepRow], tiers: Mapping[str, str]) -> list[Opportunity]:
    """推論が出力の大半を占め、見える出力が小さい応答ばかりの Agent。``tiers`` は main の Agent 名 → 宣言した LLM の名前。"""
    found: list[Opportunity] = []
    grouped: dict[str, list[StepRow]] = defaultdict(list)
    for step in steps:
        if step.status == "ok":
            grouped[step.agent].append(step)
    for agent, items in grouped.items():
        tier = tiers.get(agent)
        if agent == _OWN or tier not in LOWER or len(items) < EFFORT_MIN_STEPS:
            continue
        output = sum(step.output_tokens for step in items)
        reasoning = sum(step.reasoning_tokens for step in items)
        share = reasoning / output if output else 0.0
        visible = _p90([max(step.output_tokens - step.reasoning_tokens, 0) for step in items])
        if share < EFFORT_REASONING_SHARE or visible > EFFORT_VISIBLE_P90:
            continue
        p50 = statistics.median(step.duration_ms for step in items)
        found.append(Opportunity(
            kind="effort", agent=agent, evidence=recent_runs(items), tokens=reasoning, seconds=_seconds(items),
            summary=f"{agent} は {tier} で {len(items)} 応答、出力の {share:.0%} が推論、見える出力の p90 は {visible:.0f} token",
            detail={"tier": tier, "candidates": list(LOWER[tier]), "steps": len(items), "reasoning_share": round(share, 3),
                    "visible_p90": round(visible), "p50_ms": round(p50)},
        ))
    return found


def repeated_input(steps: Sequence[StepRow]) -> list[Opportunity]:
    """全く同じ LLM 入力が ``REPEAT_MIN`` 回以上ある Agent(常数か cache で置き換えられる)。"""
    groups: dict[tuple[str, str], list[StepRow]] = defaultdict(list)
    for step in steps:
        if step.status == "ok" and step.agent != _OWN:
            groups[(step.agent, step.input_hash)].append(step)
    per_agent: dict[str, list[list[StepRow]]] = defaultdict(list)
    for (agent, _), items in groups.items():
        if len(items) >= REPEAT_MIN:
            per_agent[agent].append(items)
    found = []
    for agent, repeated in per_agent.items():
        extra = [step for items in repeated for step in items[1:]]
        top = max(repeated, key=len)
        found.append(Opportunity(
            kind="repeated_input", agent=agent, evidence=recent_runs(step for items in repeated for step in items),
            tokens=_tokens(extra), seconds=_seconds(extra),
            summary=(f"{agent} は同じ入力を {len(repeated)} 種で計 {sum(map(len, repeated))} 回受けた"
                     f"(最多 {len(top)} 回、回答は {len({step.output_hash for step in top})} 通り)"),
            detail={"inputs": len(repeated), "calls": sum(map(len, repeated)), "top_calls": len(top),
                    "top_outputs": len({step.output_hash for step in top})},
        ))
    return found


def repeated_request(agents: Sequence[AgentRow], steps: Sequence[StepRow]) -> list[Opportunity]:
    """同じ依頼(または同じ依頼の要素)を何度も LLM で処理した Agent。回答が割れた依頼の数も数える。

    実行の半数以上で同じ依頼は Agent の固定の指示とみなし、数えない。
    """
    executions = by_execution(steps)
    using = [row for row in agents if row.component != _OWN and row.execution_id in executions]
    found = []
    for agent in sorted({row.component for row in using}):
        rows = [row for row in using if row.component == agent]
        requests: dict[str, list[AgentRow]] = defaultdict(list)
        for row in rows:
            if row.request_hash:
                requests[row.request_hash].append(row)
        # 実行の半数以上が同じ依頼なら、それは Agent の固定の指示(内容は data にある)で、繰返しではない。
        repeated = [items for items in requests.values() if REQUEST_REPEAT_MIN <= len(items) < len(rows) / 2]
        items = Counter(item for row in rows for item in row.items)
        total_items = sum(items.values())
        repeated_items = sum(count - 1 for count in items.values() if count > 1)
        if not repeated and not (total_items and repeated_items >= REQUEST_REPEAT_MIN):
            continue
        extra_rows = [row for group in repeated for row in group[1:]]
        extra_steps = [step for row in extra_rows for step in executions[row.execution_id]]
        split = sum(1 for group in repeated if len({row.response_hash for row in group}) > 1)
        found.append(Opportunity(
            kind="repeated_request", agent=agent, evidence=recent_runs(row for group in repeated for row in group) or recent_runs(rows),
            tokens=_tokens(extra_steps), seconds=_seconds(extra_steps),
            summary=(f"{agent} は同じ依頼を {sum(map(len, repeated))} 回(回答が割れた依頼 {split})、"
                     f"依頼の要素は {total_items} 件中 {repeated_items} 件が繰返し"),
            detail={"executions": len(rows), "repeated_requests": sum(map(len, repeated)), "split_answers": split,
                    "items": total_items, "repeated_items": repeated_items},
        ))
    return found


def _literals(value: Any) -> list[str]:
    """引数に含まれる 3 文字以上の文字列と数の JSON 表現。依頼の本文での出現を調べる(短い値はどこにでも現れるので見ない)。"""
    if isinstance(value, dict):
        return [text for item in value.values() for text in _literals(item)]
    if isinstance(value, list):
        return [text for item in value for text in _literals(item)]
    if isinstance(value, bool) or value is None:
        return []
    text = json.dumps(value) if isinstance(value, int | float) else json.dumps(value, ensure_ascii=False)[1:-1] if isinstance(value, str) else ""
    return [text] if len(text) >= 3 else []


def prefetch_targets(agents: Sequence[AgentRow], steps: Sequence[StepRow]) -> dict[str, list[str]]:
    """先読みを判定する Agent → 最初の応答が呼出しだった実行 id(新しい順、``PREFETCH_SAMPLE`` 件まで)。"""
    rows = {row.execution_id: row for row in agents}
    first: dict[str, list[StepRow]] = defaultdict(list)
    for execution, items in by_execution(steps).items():
        if execution in rows and items[0].calls and items[0].agent != _OWN:
            first[items[0].agent].append(items[0])
    return {agent: [step.execution_id for step in sorted(items, key=lambda step: step.started_at, reverse=True)[:PREFETCH_SAMPLE]]
            for agent, items in first.items() if len(items) >= PREFETCH_MIN}


def prefetch(steps: Sequence[StepRow], requests: Mapping[str, str], targets: Mapping[str, Sequence[str]]) -> list[Opportunity]:
    """最初の応答の呼出しの引数が全て Agent の入力(agent 行の ``request_json``: request と data)に現れる実行が多い Agent。

    LLM は request と data を最初の発言で受け取るので、これらの呼出しは Python で先に取得できる。
    """
    executions = by_execution(steps)
    found = []
    for agent, ids in targets.items():
        judged = [(executions[execution][0], requests[execution]) for execution in ids if execution in requests]
        hits = [step for step, request in judged if all(literal in request for _, args in step.calls for literal in _literals(args))]
        if len(judged) < PREFETCH_MIN or len(hits) / len(judged) < PREFETCH_SHARE:
            continue
        tools = Counter(name for step in hits for name, _ in step.calls)
        found.append(Opportunity(
            kind="prefetch", agent=agent, evidence=recent_runs(hits), tokens=_tokens(hits), seconds=_seconds(hits),
            summary=(f"{agent} の最初の応答は {len(judged)} 実行中 {len(hits)} 回、依頼に書かれた引数で "
                     f"{', '.join(name for name, _ in tools.most_common(3))} を呼ぶだけ"),
            detail={"judged": len(judged), "predictable": len(hits), "tools": dict(tools.most_common(5))},
        ))
    return found


def call_chains(steps: Sequence[StepRow]) -> list[Opportunity]:
    """同じ Tool を一回ずつ呼ぶ応答が ``CHAIN_MIN`` 回以上続く実行(並べて呼ぶか、一つの Tool にまとめられる)。"""
    chains: dict[tuple[str, str], list[list[StepRow]]] = defaultdict(list)
    for items in by_execution(steps).values():
        run: list[StepRow] = []
        for step in [*items, None]:
            name = step.calls[0][0] if step is not None and len(step.calls) == 1 else None
            if run and (name is None or name != run[0].calls[0][0]):
                if len(run) >= CHAIN_MIN and run[0].agent != _OWN:
                    chains[(run[0].agent, run[0].calls[0][0])].append(run)
                run = []
            if name is not None and step is not None:
                run.append(step)
    found = []
    for (agent, tool), runs in chains.items():
        extra = [step for run in runs for step in run[1:]]
        found.append(Opportunity(
            kind="call_chain", agent=agent, evidence=recent_runs(step for run in runs for step in run),
            tokens=_tokens(extra), seconds=_seconds(extra),
            summary=f"{agent} は {tool} を一回ずつ呼ぶ応答を {len(runs)} 回連ねた(最長 {max(map(len, runs))} 応答)",
            detail={"target": tool, "chains": len(runs), "longest": max(map(len, runs)), "steps": sum(map(len, runs))},
        ))
    return found


def large_outputs(steps: Sequence[StepRow]) -> list[Opportunity]:
    """一回の呼出しで入力が ``LARGE_OUTPUT_TOKENS`` 以上増え、その後も ``LARGE_OUTPUT_CARRIED`` 応答以上運ばれたもの。"""
    found_by: dict[tuple[str, str], list[tuple[StepRow, int, int]]] = defaultdict(list)
    for items in by_execution(steps).values():
        for index, (step, following) in enumerate(pairwise(items)):
            added = following.input_tokens - step.input_tokens - step.output_tokens
            carried = len(items) - index - 1
            if added >= LARGE_OUTPUT_TOKENS and carried >= LARGE_OUTPUT_CARRIED and step.calls and step.agent != _OWN:
                tools = "+".join(sorted({name for name, _ in step.calls}))
                found_by[(step.agent, tools)].append((step, added, carried))
    found = []
    for (agent, tools), hits in found_by.items():
        carried_tokens = sum(added * carried for _, added, carried in hits)
        found.append(Opportunity(
            kind="large_output", agent=agent, evidence=recent_runs(step for step, _, _ in hits), tokens=carried_tokens,
            summary=(f"{agent} の {tools} の結果が {len(hits)} 回、一度に {max(added for _, added, _ in hits):,} token まで増え、"
                     f"後の応答で計 {carried_tokens:,} token 運ばれた"),
            detail={"target": tools, "times": len(hits), "largest": max(added for _, added, _ in hits), "carried_tokens": carried_tokens},
        ))
    return found


def cache_misses(steps: Sequence[StepRow]) -> list[Opportunity]:
    """最初の応答の入力が大きいのに cache がほとんど効かない Agent(変わらない内容を前に置けば効く)。"""
    first: dict[str, list[StepRow]] = defaultdict(list)
    for items in by_execution(steps).values():
        if items[0].agent != _OWN and items[0].status == "ok":
            first[items[0].agent].append(items[0])
    found = []
    for agent, items in first.items():
        inputs = sum(step.input_tokens for step in items)
        cached = sum(step.cached_tokens for step in items)
        if len(items) < CACHE_MIN or inputs / len(items) < CACHE_MIN_INPUT or cached / inputs >= CACHE_SHARE:
            continue
        found.append(Opportunity(
            kind="cache", agent=agent, evidence=recent_runs(items), tokens=inputs - cached,
            summary=f"{agent} の最初の応答は平均 {inputs // len(items):,} token の入力で cache は {cached / inputs:.0%}",
            detail={"executions": len(items), "mean_input": inputs // len(items), "cache_share": round(cached / inputs, 3)},
        ))
    return found


def light_children(agents: Sequence[AgentRow], steps: Sequence[StepRow]) -> list[Opportunity]:
    """Tool を一つ二つ呼ぶだけの子 Agent(親の Tool か Skill にすれば往復が減る)。Tool のない子(隔離・変換)は除く。"""
    executions = by_execution(steps)
    rows = {row.execution_id: row for row in agents}
    edges: dict[tuple[str, str], list[AgentRow]] = defaultdict(list)
    for row in agents:
        caller = rows.get(row.parent_execution_id or "")
        if caller is not None and row.execution_id in executions and _OWN not in (caller.component, row.component):
            edges[(caller.component, row.component)].append(row)
    found = []
    for (parent, child), items in edges.items():
        child_steps = [executions[row.execution_id] for row in items]
        mean_steps = sum(map(len, child_steps)) / len(items)
        mean_calls = sum(len(step.calls) for group in child_steps for step in group) / len(items)
        if len(items) < CHILD_MIN or mean_steps > CHILD_MAX_STEPS or mean_calls < 1:
            continue
        flat = [step for group in child_steps for step in group]
        found.append(Opportunity(
            kind="light_child", agent=parent, evidence=recent_runs(items), tokens=_tokens(flat), seconds=_seconds(flat),
            summary=f"{parent} の子 {child} は {len(items)} 回とも平均 {mean_steps:.1f} 応答・{mean_calls:.1f} 呼出しで終わる",
            detail={"target": child, "executions": len(items), "mean_steps": round(mean_steps, 2), "mean_calls": round(mean_calls, 2)},
        ))
    return found


def skills_always_read(steps: Sequence[StepRow]) -> list[Opportunity]:
    """ほぼ毎回 ``read_skill`` で読まれる Skill(system に置けば一往復減る)。"""
    executions = by_execution(steps)
    per_agent: dict[str, list[list[StepRow]]] = defaultdict(list)
    for items in executions.values():
        if items[0].agent != _OWN:
            per_agent[items[0].agent].append(items)
    found = []
    for agent, runs in per_agent.items():
        reads: dict[str, list[StepRow]] = defaultdict(list)
        for items in runs:
            for skill in {str(args.get("name")) for step in items for name, args in step.calls if name == "read_skill" and isinstance(args, dict)}:
                reads[skill].append(next(step for step in items if any(name == "read_skill" for name, _ in step.calls)))
        for skill, hits in reads.items():
            if len(runs) < SKILL_MIN or len(hits) / len(runs) < SKILL_SHARE:
                continue
            only = [step for step in hits if all(name == "read_skill" for name, _ in step.calls)]
            found.append(Opportunity(
                kind="skill_inline", agent=agent, evidence=recent_runs(hits), tokens=_tokens(only), seconds=_seconds(only),
                summary=f"{agent} は {len(runs)} 実行中 {len(hits)} 回 Skill {skill} を読む({len(only)} 回はそのための応答)",
                detail={"target": skill, "executions": len(runs), "reads": len(hits), "read_only_steps": len(only)},
            ))
    return found


def duplicate_triggers(agents: Sequence[AgentRow], steps: Sequence[StepRow]) -> list[Opportunity]:
    """同じ Source の対象に対し、別の run の前の実行の最中か ``TRIGGER_GAP`` 以内に始まった実行(防抖・串行化で減らせる)。

    data は run の中で共有されるため、同じ run の実行同士は比べない。
    """
    executions = by_execution(steps)
    groups: dict[tuple[str, str], list[AgentRow]] = defaultdict(list)
    for row in agents:
        if row.identity and row.component != _OWN and row.execution_id in executions:
            groups[(row.component, row.identity)].append(row)
    per_agent: dict[str, list[AgentRow]] = defaultdict(list)
    totals: Counter[str] = Counter()
    for (agent, _), rows in groups.items():
        rows.sort(key=lambda row: row.started_at)
        totals[agent] += len(rows)
        for previous, row in pairwise(rows):
            if previous.run_id == row.run_id:
                continue
            busy_until = previous.started_at + timedelta(milliseconds=previous.duration_ms)
            if row.started_at <= max(busy_until, previous.started_at + TRIGGER_GAP):
                per_agent[agent].append(row)
    found = []
    for agent, rows in per_agent.items():
        if len(rows) < TRIGGER_MIN:
            continue
        flat = [step for row in rows for step in executions[row.execution_id]]
        found.append(Opportunity(
            kind="duplicate_trigger", agent=agent, evidence=recent_runs(rows), tokens=_tokens(flat), seconds=_seconds(flat),
            summary=(f"{agent} の {totals[agent]} 実行中 {len(rows)} 回は、同じ対象の前の実行の最中か "
                     f"{int(TRIGGER_GAP.total_seconds() // 60)} 分以内に始まった"),
            detail={"executions": totals[agent], "duplicates": len(rows)},
        ))
    return found


def report_only(agents: Sequence[AgentRow], steps: Sequence[StepRow], requests: Mapping[str, str],
                targets: Mapping[str, Sequence[str]], known: Iterable[str]) -> list[Opportunity]:
    """変更まで進めない探査を全て実行し、``known``(main の Agent 名)の Agent のものを見積もりの token の多い順に返す。"""
    found = [
        *repeated_input(steps),
        *repeated_request(agents, steps),
        *prefetch(steps, requests, targets),
        *call_chains(steps),
        *large_outputs(steps),
        *cache_misses(steps),
        *light_children(agents, steps),
        *skills_always_read(steps),
        *duplicate_triggers(agents, steps),
    ]
    names = set(known)
    return sorted((item for item in found if item.agent in names), key=lambda item: (-item.tokens, -item.seconds, item.kind, item.agent))
