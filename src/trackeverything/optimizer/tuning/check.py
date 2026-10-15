"""観点 tuning: 実行の費用と時間を調べ、LLM の段(effort)を下げられる Agent を再生で確かめて変え、merge した調整を観察する。

一回の実行は 1) 観察(調整が効いている版と効いていない版の比較、悪化なら取消し)、2) effort(探査 → 再生の判定 →
``llm`` の宣言の変更 → fork → merge)、3) 報告だけの探査、の順に進む。状態は版の履歴の trailer に置き、表を持たない。
一つの部分の失敗はその finding にし、次の部分へ進む(取消しは伝える)。仕様は docs/components/OPTIMIZER.md。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from trackeverything.core.builtin.domain.execution_log import current_run_id
from trackeverything.core.versioning.repo import Repo, current_repo, describe_failure
from trackeverything.optimizer.model import Finding
from trackeverything.optimizer.tuning import detect, replay
from trackeverything.optimizer.tuning.detect import Opportunity
from trackeverything.optimizer.tuning.effort import class_name, declared_tier, inheritors, set_tier
from trackeverything.optimizer.tuning.records import AgentRow, Readers, StepRow
from trackeverything.optimizer.tuning.watch import (
    PREFIX,
    REVERT_PREFIX,
    WATCH,
    History,
    Tuning,
    blocked,
    message,
    metrics,
    split,
    verdict,
)
from trackeverything.optimizer.worktree import class_file

if TYPE_CHECKING:
    from trackeverything.optimizer.checks import CheckContext

log = logging.getLogger(__name__)

#: 探査で読む期間(観測時刻から遡る)。
WINDOW = timedelta(days=14)
#: 一回の実行で再生まで進める effort の候補の数。
MAX_EFFORT = 3
#: 報告する探査の結果の数(見積もりの token の多い順)。
MAX_REPORTS = 20
#: 自由文の比較に使う Host の LLM。
JUDGE = "max"


def _finding(kind: str, agent: str, outcome: str, reason: str, *, evidence: Sequence[str] = (), **detail: Any) -> Finding:
    log.info("optimizer tuning: %s %s %s (%s)", kind, agent, outcome, reason)
    return Finding(summary=f"{kind} {agent}: {outcome} — {reason}", evidence=tuple(evidence),
                   detail={"kind": kind, "agent": agent, "outcome": outcome, "reason": reason, **detail})


def _reported(item: Opportunity) -> Finding:
    return _finding(item.kind, item.agent, "reported", item.summary, evidence=item.evidence,
                    tokens=item.tokens, seconds=round(item.seconds, 1), **dict(item.detail))


def _error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


class _Tiers:
    """版ごとの Agent の宣言した ``llm``。tree は版ごとに一度だけ読む。"""

    def __init__(self, repo: Repo) -> None:
        self._repo = repo
        self._trees: dict[str, Mapping[str, bytes]] = {}

    async def of(self, version_id: str, path: str, agent: str) -> str | None:
        if version_id not in self._trees:
            self._trees[version_id] = await self._repo.tree(version_id)
        data = self._trees[version_id].get(path)
        if data is None:
            return None
        try:
            return declared_tier(data.decode("utf-8"), agent)
        except (SyntaxError, UnicodeDecodeError, ValueError):
            return None

    async def effective(self, tuning: Tuning, version_id: str) -> bool:
        """``version_id`` で調整が効いているか(Agent の類が ``Tuning-To`` を宣言している)。"""
        return await self.of(version_id, tuning.path, tuning.agent) == tuning.target


class TuningCheck:
    """観点 ``tuning``。``readers`` は ``logs`` の読取り(既定は PostgreSQL)。"""

    name = "tuning"

    def __init__(self, readers: Readers | None = None) -> None:
        self._read = readers or Readers()

    async def run(self, ctx: CheckContext) -> Sequence[Finding]:
        now = ctx.observation.observed_at
        current = current_run_id.get()
        agents: list[AgentRow] = [row for row in await self._read.agents(since=now - WINDOW) if row.run_id != current]
        steps: list[StepRow] = [step for step in await self._read.steps(since=now - WINDOW) if step.run_id != current]
        executions = detect.by_execution(steps)
        repo = current_repo()
        await repo.refresh()
        findings = await self._watch(ctx, repo, agents, executions, now)
        await repo.refresh()
        main = repo.main_version
        history = History(await repo.log(), main.id)
        tiers = _Tiers(repo)
        effective = {item.commit.id: await self._effective(tiers, item, main.id)
                     for item in history.tunings() if not item.reverted and item.complete}
        watching, cooling = blocked(history, effective, now)
        declared = {name: type(agent).llm for name, agent in main.graph.agents.items()}
        effort = sorted(detect.effort(steps, declared), key=lambda item: (-item.seconds, item.agent))
        for item in effort[:MAX_EFFORT]:
            if item.agent in watching:
                findings.append(_finding("effort", item.agent, "skipped", "an earlier tuning of this agent is being watched",
                                         evidence=item.evidence))
            elif item.key in cooling:
                findings.append(_finding("effort", item.agent, "skipped", "reverted within the cooldown", evidence=item.evidence))
            else:
                findings.append(await self._effort(ctx, repo, item, agents, steps, executions))
        findings += [_finding("effort", item.agent, "skipped", "over limit", evidence=item.evidence) for item in effort[MAX_EFFORT:]]
        targets = detect.prefetch_targets(agents, steps)
        try:
            requests = await self._read.requests([execution for ids in targets.values() for execution in ids])
        except Exception as exc:
            log.warning("optimizer tuning: reading the requests for prefetch failed", exc_info=True)
            findings.append(_finding("prefetch", "*", "failed", f"requests: {_error(exc)}"))
            requests, targets = {}, {}
        findings += [_reported(item) for item in detect.report_only(agents, steps, requests, targets, declared)[:MAX_REPORTS]]
        return findings

    @staticmethod
    async def _effective(tiers: _Tiers, tuning: Tuning, version_id: str) -> bool:
        try:
            return await tiers.effective(tuning, version_id)
        except Exception:
            log.warning("optimizer tuning: reading %s for %s failed", version_id, tuning.id, exc_info=True)
            return False

    # ---- 観察

    async def _watch(self, ctx: CheckContext, repo: Repo, agents: Sequence[AgentRow], executions: Mapping[str, Sequence[StepRow]],
                     now: datetime) -> list[Finding]:
        """``WATCH`` 以内の、main で効いている調整を、効いている版と効いていない版の実行で比べ、悪化した調整を取り消す。"""
        history = History(await repo.log(), repo.main_version.id)
        tiers = _Tiers(repo)
        findings = []
        for tuning in history.tunings():
            if tuning.reverted or not tuning.complete or tuning.commit.created_at < now - WATCH:
                continue
            try:
                if not await tiers.effective(tuning, history.main):
                    continue
                rows = []
                for row in agents:
                    version = history.resolve(row.label) if row.component == tuning.agent else None
                    if version is not None:
                        rows.append((row, await tiers.effective(tuning, version)))
                before, after = split(tuning, rows)
                outcome, reason = verdict(tuning, metrics(before, executions), metrics(after, executions))
                numbers = {"tuning": tuning.id, "version": tuning.commit.id, "before": metrics(before, executions).to_json(),
                           "after": metrics(after, executions).to_json()}
                if outcome != "revert":
                    findings.append(_finding("watch", tuning.agent, outcome, reason, evidence=detect.recent_runs(after), **numbers))
                else:
                    findings.append(await self._revert(ctx, repo, tuning, reason, detect.recent_runs(after), numbers))
            except Exception as exc:
                log.warning("optimizer tuning: watching %s failed", tuning.id, exc_info=True)
                findings.append(_finding("watch", tuning.agent, "failed", _error(exc), tuning=tuning.id, version=tuning.commit.id))
        return findings

    async def _revert(self, ctx: CheckContext, repo: Repo, tuning: Tuning, reason: str, evidence: Sequence[str],
                      numbers: Mapping[str, Any]) -> Finding:
        """main の ``Tuning-Path`` で Agent の ``llm`` を ``Tuning-From`` に戻す(同じファイルの他の変更は残る)。"""
        await repo.refresh()
        base = repo.main_version
        data = base.tree.get(tuning.path)
        try:
            if data is None:
                raise ValueError(f"{tuning.path} is not in main")
            source = set_tier(data.decode("utf-8"), tuning.agent, tuning.source)
        except (ValueError, UnicodeDecodeError, SyntaxError) as exc:
            return _finding("watch", tuning.agent, "failed", f"{reason}; revert: {_error(exc)}", evidence=evidence, **numbers)
        text = message(f"{REVERT_PREFIX}{tuning.id}", {
            "Tuning-Id": tuning.id, "Tuning-Kind": tuning.kind, "Tuning-Agent": tuning.agent, "Tuning-From": tuning.target,
            "Tuning-To": tuning.source, "Tuning-Path": tuning.path, "Tuning-Revert": tuning.commit.id, "Tuning-Reason": reason,
        })
        return await self._land(ctx, repo, "watch", tuning.agent, base.id, {**base.tree, tuning.path: source.encode("utf-8")}, text,
                                reason, evidence, {**numbers, "paths": [tuning.path]}, done="reverted")

    # ---- effort

    async def _effort(self, ctx: CheckContext, repo: Repo, item: Opportunity, agents: Sequence[AgentRow], steps: Sequence[StepRow],
                      executions: Mapping[str, Sequence[StepRow]]) -> Finding:
        tier = str(item.detail["tier"])
        tiers = [name for name in item.detail["candidates"] if name in ctx.llms]
        if tier not in ctx.llms or JUDGE not in ctx.llms or not tiers:
            return _finding("effort", item.agent, "skipped", "the LLM clients for the replay are not available", evidence=item.evidence)
        detected = dict(item.detail)
        try:
            samples = await self._read.samples(replay.pick([step for step in steps if step.agent == item.agent]))
            if len(samples) < replay.MIN_VALID:
                return _finding("effort", item.agent, "rejected", f"only {len(samples)} recorded steps to replay (need {replay.MIN_VALID})",
                                evidence=item.evidence, detected=detected, gates=[])
            baseline = await replay.replay(samples, ctx.llms[tier], ctx.llms[JUDGE])
            gates = []
            for name in tiers:
                gate = replay.decide(name, baseline, await replay.replay(samples, ctx.llms[name], ctx.llms[JUDGE]))
                gates.append(gate)
                if gate.passed:
                    break
        except Exception as exc:
            log.warning("optimizer tuning: the replay failed for %s", item.agent, exc_info=True)
            return _finding("effort", item.agent, "failed", f"replay: {_error(exc)}", evidence=item.evidence, detected=detected)
        detail = {"detected": detected, "gates": [gate.to_json() for gate in gates]}
        passed = next((gate for gate in gates if gate.passed), None)
        if passed is None:
            return _finding("effort", item.agent, "rejected", "; ".join(f"{gate.tier}: {gate.reason}" for gate in gates),
                            evidence=item.evidence, **detail)
        await repo.refresh()
        base = repo.main_version
        instance = base.graph.agents.get(item.agent)
        path = class_file(base, type(instance)) if instance is not None else None
        if path is None:
            return _finding("effort", item.agent, "failed", "the agent's module is not in main", evidence=item.evidence, **detail)
        try:
            original = base.tree[path].decode("utf-8")
            source = set_tier(original, item.agent, passed.tier)
            shared = inheritors(base.tree, class_name(original, item.agent) or "")
        except (ValueError, UnicodeDecodeError, SyntaxError) as exc:
            return _finding("effort", item.agent, "failed", _error(exc), evidence=item.evidence, **detail)
        if shared:
            return _finding("effort", item.agent, "rejected", f"agent classes that inherit its llm would change too: {', '.join(shared)}",
                            evidence=item.evidence, **detail)
        own = [row for row in agents if row.component == item.agent]
        text = message(f"{PREFIX}effort {item.agent} {tier}->{passed.tier}", {
            "Tuning-Id": item.key, "Tuning-Kind": "effort", "Tuning-Agent": item.agent, "Tuning-From": tier, "Tuning-To": passed.tier,
            "Tuning-Path": path, "Tuning-Gate": passed.to_json(), "Tuning-Baseline": metrics(own, executions).to_json(),
        })
        return await self._land(ctx, repo, "effort", item.agent, base.id, {**base.tree, path: source.encode("utf-8")}, text,
                                f"{tier} -> {passed.tier}: {passed.reason}", item.evidence,
                                {**detail, "tier": passed.tier, "paths": [path]}, done="merged")

    # ---- fork と merge

    async def _land(self, ctx: CheckContext, repo: Repo, kind: str, agent: str, base: str, tree: Mapping[str, bytes], text: str,
                    reason: str, evidence: Sequence[str], detail: Mapping[str, Any], *, done: str) -> Finding:
        """tree を main から fork し、``ctx.merge`` なら merge する。"""
        try:
            candidate = await repo.fork(base, tree, text)
        except Exception as exc:
            return _finding(kind, agent, "fork_failed", describe_failure(exc), evidence=evidence, base=base, **detail)
        if not ctx.merge:
            return _finding(kind, agent, "forked", reason, evidence=evidence, base=base, candidate=candidate.id, **detail)
        try:
            merged = await repo.merge(candidate.id)
        except Exception as exc:
            return _finding(kind, agent, "failed", f"merge: {_error(exc)}", evidence=evidence, base=base, candidate=candidate.id, **detail)
        extra = {"rebased_from": candidate.id} if merged.id != candidate.id else {}
        return _finding(kind, agent, done, reason, evidence=evidence, base=base, candidate=merged.id, **extra, **detail)
