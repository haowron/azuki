"""Optimizer の観点 tuning: TU01-TU18(docs/components/OPTIMIZER.md)。

``logs`` の読取りと LLM は代役にし、版庫は代役の store で開いた singleton(実際の Repo)を使う。fork の読込みと merge は
実際に通す。PostgreSQL と実 LLM は使わない。
"""

import itertools
import json
import random
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from tests.optimizer.conftest import live_store
from tests.versioning.helpers import api, host_services, make_commit, minimal_tree, seed
from trackeverything.core.abc.llm import Call, Calls, FinalAnswer, LLMClient, LLMMetadata, LLMUsage
from trackeverything.core.builtin.domain.execution_log import current_run_id
from trackeverything.optimizer import CheckContext, Observation
from trackeverything.optimizer.tuning import TuningCheck, detect, replay, watch
from trackeverything.optimizer.tuning.effort import declared_tier, inheritors, set_tier
from trackeverything.optimizer.tuning.records import AgentRow, Readers, Sample, StepRow

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
CURRENT = "run-current"
_ids = itertools.count(1)


def usage(output: int, reasoning: int = 0) -> LLMMetadata:
    return LLMMetadata("zai", "glm-5.3", usage=LLMUsage(input_tokens=100, output_tokens=output, total_tokens=output + 100,
                                                       reasoning_tokens=reasoning))


class Answering(LLMClient):
    """最後の user の本文から ``answer`` で応答を作る LLM(並行の再生でも順に依らない)。入力を記録する。"""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.requests: list[Any] = []

    async def respond(self, messages: Any, callables: Any) -> Any:
        self.requests.append((list(messages), callables))
        result = self.answer(next(message.content for message in reversed(messages) if message.role == "user"))
        if isinstance(result, Exception):
            raise result
        return result


def echo(output: int) -> Answering:
    """``text <i>`` に ``answer <i>`` と答え、出力 ``output`` token(うち推論は 10 を除いた分)を報告する。"""
    return Answering(lambda user: FinalAnswer(user.replace("text", "answer"), metadata=usage(output, max(output - 10, 0))))


def step(agent: str, execution: str, *, run: str | None = None, at: datetime | None = None, output: int = 1000, reasoning: int = 900,
         inputs: int = 2000, cached: int = 0, calls: Any = (), status: str = "ok", ms: int = 10_000, input_hash: str | None = None,
         output_hash: str = "out") -> StepRow:
    ident = next(_ids)
    return StepRow(id=ident, run_id=run or f"run-{execution}", execution_id=execution, agent=agent, started_at=at or NOW - timedelta(hours=1),
                   duration_ms=ms, status=status, model="zai/glm-5.3", input_tokens=inputs, cached_tokens=cached, output_tokens=output,
                   reasoning_tokens=reasoning, kind="calls" if calls else "final_answer", calls=tuple(calls),
                   input_hash=input_hash or f"in-{ident}", output_hash=output_hash)


def agent_row(component: str, execution: str, *, run: str | None = None, parent: str | None = None, at: datetime | None = None,
              ms: int = 60_000, status: str = "ok", label: str | None = None, request_hash: str | None = None,
              response_hash: str | None = None, identity: str | None = None, items: Any = ()) -> AgentRow:
    return AgentRow(execution_id=execution, run_id=run or f"run-{execution}", parent_execution_id=parent, component=component,
                    started_at=at or NOW - timedelta(hours=1), duration_ms=ms, status=status, label=label,
                    request_hash=request_hash or f"req-{execution}", response_hash=response_hash or f"res-{execution}",
                    identity=identity, items=tuple(items))


def sample(ident: int, index: int) -> Sample:
    request = {"messages": [{"role": "system", "content": "translate", "raw": None}, {"role": "user", "content": f"text {index}", "raw": None}],
               "callables": []}
    return Sample(id=ident, run_id=f"run-s{index}", request=request, result={"type": "final_answer", "text": f"answer {index}"})


def readers(agents: Any = (), steps: Any = (), samples: Any = (), requests: Any = None) -> Readers:
    by_id = {item.id: item for item in samples}
    return Readers(
        agents=AsyncMock(return_value=list(agents)),
        steps=AsyncMock(return_value=list(steps)),
        samples=AsyncMock(side_effect=lambda ids: [by_id[ident] for ident in ids if ident in by_id]),
        requests=AsyncMock(return_value=dict(requests or {})),
    )


async def check_run(read: Readers, llms: Any, *, merge: bool = True) -> list[Any]:
    ctx = CheckContext(observation=Observation(observed_at=NOW, main_version="", runs=()), llms=llms, merge=merge)
    token = current_run_id.set(CURRENT)
    try:
        return list(await TuningCheck(read).run(ctx))
    finally:
        current_run_id.reset(token)


def of_kind(findings: list[Any], kind: str) -> list[Any]:
    return [finding for finding in findings if finding.detail["kind"] == kind]


def translator_history(count: int = 24) -> tuple[list[AgentRow], list[StepRow], list[Sample]]:
    """translator の一応答の実行 ``count`` 回(推論が出力の 9 割)と、その再生の標本。"""
    steps = [step("translator", f"t{index}", input_hash=f"in{index}", ms=14_000) for index in range(count)]
    return ([agent_row("translator", f"t{index}") for index in range(count)], steps,
            [sample(item.id, index) for index, item in enumerate(steps)])


@pytest.fixture
async def main(singleton):
    """dispatch と translator(``llm`` の宣言なし = max)を持つ main の singleton の版庫。"""
    files = minimal_tree(("dispatch", "translator"))
    repo, store, commit = await singleton(files)
    return repo, store, commit, files


@pytest.fixture
async def tuned():
    """``await tuned(tier=…, later=…, shape=…)``: translator を max から ``tier``(既定 low)にした調整の commit を持つ版庫。

    ``shape``: ``main``(調整が main。``later`` があればその上に人の commit が main)、``merge``(人の commit を第一親、調整を第二親にした
    merge commit が main。Commit は第一親だけを持つので、調整は main の第一親の線の外)、``fork``(main は調整の前のままで、調整は
    fork のまま)。``(版庫, store, 調整の前の Commit, 調整の Commit, 前の tree, 調整の tree)`` を返す。
    """
    opened: list[Any] = []

    async def open_(*, tier: str = "low", created: datetime = NOW - timedelta(days=1), later: Any = None,
                    shape: str = "main") -> tuple[Any, ...]:
        modules = api()
        before = minimal_tree(("dispatch", "translator"))
        after = {**before, "agent/translator.py": set_tier(before["agent/translator.py"].decode(), "translator", tier).encode()}
        base = make_commit(before, created_at=created - timedelta(days=2))
        text = watch.message(f"optimizer:tuning effort translator max->{tier}", {
            "Tuning-Id": "effort/translator", "Tuning-Kind": "effort", "Tuning-Agent": "translator", "Tuning-From": "max",
            "Tuning-To": tier, "Tuning-Path": "agent/translator.py",
            "Tuning-Baseline": {"executions": 20, "ok": 1.0, "steps": 1.0, "tokens": 3000, "step_ms": 14000},
        })
        tuning = make_commit(after, parent=base.id, message=text, created_at=created)
        extra: list[Any] = [(base, before), (tuning, after)]
        head, head_files = tuning, after
        if shape == "main" and later is not None:
            head_files = {**after, **later}
            head = make_commit(head_files, parent=tuning.id, message="person", created_at=created + timedelta(hours=1))
        elif shape == "merge":
            person_files = {**before, "tools/person.py": b"VALUE = 2\n"}
            person = make_commit(person_files, parent=base.id, message="person", created_at=created + timedelta(minutes=30))
            head_files = {**after, "tools/person.py": b"VALUE = 2\n"}
            head = make_commit(head_files, parent=person.id, message="Merge branch 'main'", created_at=created + timedelta(hours=1))
            extra.append((person, person_files))
        elif shape == "fork":
            head, head_files = base, before
        store = live_store(head, head_files)
        seed(store, *extra)
        repo = modules.repo.open_repo(modules.compose.prepare_components(host_services()), store=store)
        opened.append(repo)
        await repo.start()
        return repo, store, base, tuning, before, after

    yield open_
    for repo in reversed(opened):
        with suppress(RuntimeError):
            await repo.close()


def executions_on(commit: Any, prefix: str, count: int, *, failures: int = 0, ms: int = 14_000,
                  at: datetime | None = None) -> tuple[list[AgentRow], list[StepRow]]:
    """版 ``commit`` での translator の実行 ``count`` 回(最初の ``failures`` 回は error)と、その一応答。"""
    rows = [agent_row("translator", f"{prefix}{index}", label=f"c.{commit.id[:8]}", status="error" if index < failures else "ok", at=at)
            for index in range(count)]
    return rows, [step("translator", row.execution_id, ms=ms, at=at) for row in rows]


# ---- 探査(Python だけ)


def test_tu01_effort_targets_reasoning_heavy_agents_with_small_answers():
    """TU01: max・high の宣言で、推論が出力の 7 割以上・見える出力の p90 が 1000 token 以下・ok の応答が 20 以上の Agent だけ。"""
    tiers = {"translator": "max", "compare": "max", "triage": "low", "general": "max", "project": "high", "document": "high",
             "optimizer": "max"}
    steps = [
        *(step("translator", f"t{index}") for index in range(20)),
        step("translator", "t-error", status="error"),
        *(step("compare", f"c{index}", output=3000, reasoning=900) for index in range(20)),
        *(step("triage", f"g{index}") for index in range(20)),
        *(step("general", f"q{index}") for index in range(19)),
        *(step("project", f"p{index}", reasoning=500) for index in range(20)),
        *(step("document", f"d{index}") for index in range(20)),
        *(step("optimizer", f"o{index}") for index in range(20)),
        *(step("unknown", f"u{index}") for index in range(20)),
    ]

    found = {item.agent: item for item in detect.effort(steps, tiers)}

    assert sorted(found) == ["document", "translator"]
    assert found["translator"].detail["candidates"] == ["low", "high"] and found["document"].detail["candidates"] == ["low"]
    assert found["translator"].detail["steps"] == 20
    assert found["translator"].detail["reasoning_share"] == 0.9 and found["translator"].detail["visible_p90"] == 100
    assert found["translator"].key == "effort/translator"


def test_tu02_repeated_inputs_requests_and_triggers_are_counted():
    """TU02: 同じ LLM 入力の 5 回以上の繰返し、同じ依頼(と要素)の繰返し(半数以上が同じ依頼なら固定の指示)、
    同じ対象への別の run の 10 分以内の再実行。"""
    same = [step("translator", f"t{index}", input_hash="fixed", output_hash=f"o{index % 4}") for index in range(6)]
    fewer = [step("translator", f"x{index}", input_hash="four") for index in range(4)]
    (repeated,) = detect.repeated_input([*same, *fewer])
    assert repeated.agent == "translator"
    assert repeated.detail == {"inputs": 1, "calls": 6, "top_calls": 6, "top_outputs": 4}
    assert repeated.tokens == 5 * 3000

    rows = [agent_row("triage", f"g{index}", request_hash="same" if index < 3 else None, response_hash="a" if index < 2 else "b",
                      items=("c1", "c2") if index < 4 else ("c3",)) for index in range(7)]
    (request,) = detect.repeated_request(rows, [step("triage", f"g{index}") for index in range(7)])
    assert request.detail == {"executions": 7, "repeated_requests": 3, "split_answers": 1, "items": 11, "repeated_items": 8}
    fixed = [agent_row("project", f"f{index}", request_hash="sync the source") for index in range(6)]
    assert detect.repeated_request(fixed, [step("project", f"f{index}") for index in range(6)]) == []

    start = NOW - timedelta(hours=3)
    project = [agent_row("project", f"p{index}", identity="pr-1", at=start + timedelta(minutes=3 * index)) for index in range(6)]
    project += [agent_row("project", "p-other", identity="pr-2", at=start),
                agent_row("project", "p-late", identity="pr-1", at=start + timedelta(hours=2)),
                agent_row("project", "p-same-run", run="run-p-late", identity="pr-1", at=start + timedelta(hours=2, minutes=1))]
    (trigger,) = detect.duplicate_triggers(project, [step("project", row.execution_id) for row in project])
    assert trigger.detail == {"executions": 9, "duplicates": 5}


def test_tu03_step_shapes_are_detected():
    """TU03: 単発呼出しの連鎖・大きな Tool 結果・効かない cache・軽い子 Agent・毎回読む Skill・依頼から決まる最初の呼出し。"""
    chain = [step("investigator", "i1", calls=[("run_command", {"command": f"c{index}"})]) for index in range(4)]
    chain += [step("investigator", "i1"), *(step("investigator", "i2", calls=[("run_command", {})]) for _ in range(2))]
    (found,) = detect.call_chains(chain)
    assert found.detail == {"target": "run_command", "chains": 1, "longest": 4, "steps": 4}

    big = [step("general", "g1", inputs=10_000, output=500, calls=[("github_api", {})]),
           step("general", "g1", inputs=40_000, output=500, calls=[("read", {})]),
           step("general", "g1", inputs=41_000, output=500), step("general", "g1", inputs=42_000, output=500)]
    (large,) = detect.large_outputs(big)
    assert large.detail == {"target": "github_api", "times": 1, "largest": 29_500, "carried_tokens": 88_500}

    cold = [step("project", f"p{index}", inputs=50_000, cached=5_000) for index in range(10)]
    warm = [step("compare", f"c{index}", inputs=50_000, cached=40_000) for index in range(10)]
    (cache,) = detect.cache_misses([*cold, *warm])
    assert cache.agent == "project" and cache.detail == {"executions": 10, "mean_input": 50_000, "cache_share": 0.1}

    parents = [agent_row("general", f"g{index}") for index in range(10)]
    children = [agent_row("codegen", f"k{index}", parent=f"g{index}") for index in range(10)]
    translators = [agent_row("translator", f"t{index}", parent=f"g{index}") for index in range(10)]
    child_steps = [*(step("codegen", f"k{index}", calls=[("execute_code", {})]) for index in range(10)),
                   *(step("codegen", f"k{index}") for index in range(10)), *(step("translator", f"t{index}") for index in range(10))]
    (child,) = detect.light_children([*parents, *children, *translators], child_steps)
    assert child.agent == "general"
    assert child.detail == {"target": "codegen", "executions": 10, "mean_steps": 2.0, "mean_calls": 1.0}

    reads = []
    for index in range(10):
        reads += [step("research", f"r{index}", calls=[("read_skill", {"name": "research-github"})]), step("research", f"r{index}")]
    (skill,) = detect.skills_always_read(reads)
    assert skill.detail == {"target": "research-github", "executions": 10, "reads": 10, "read_only_steps": 10}

    rows = [agent_row("project", f"p{index}") for index in range(10)]
    first = [step("project", f"p{index}", calls=[("project_read_doc", {"path": f"docs/pr-{index}.md"})]) for index in range(10)]
    targets = detect.prefetch_targets(rows, first)
    assert sorted(targets["project"]) == sorted(f"p{index}" for index in range(10))
    linked = {f"p{index}": json.dumps({"data": {"documents": [f"docs/pr-{index}.md"]}}) for index in range(9)} | {"p9": "{}"}
    (prefetch,) = detect.prefetch(first, linked, targets)
    assert prefetch.detail == {"judged": 10, "predictable": 9, "tools": {"project_read_doc": 9}}
    seven = {key: value for key, value in linked.items() if key not in ("p0", "p1")} | {"p0": "{}", "p1": "{}"}
    assert detect.prefetch(first, seven, targets) == []


# ---- effort(再生 → 変更 → fork → merge)


async def test_tu04_a_passing_effort_change_is_merged(main):
    """TU04: 現在の段と同じ判断で出力が 7 割以下の最初の段(low)へ ``llm`` の宣言だけを変えて merge する。trailer に判定と基準を残す。"""
    repo, _, commit, files = main
    agents, steps, samples = translator_history()
    steps.append(step("translator", "self", run=CURRENT))
    read = readers(agents, steps, samples)
    llms = {"max": echo(1000), "high": echo(600), "low": echo(100)}

    (finding,) = of_kind(await check_run(read, llms), "effort")

    assert finding.detail["outcome"] == "merged" and finding.detail["tier"] == "low"
    assert finding.detail["paths"] == ["agent/translator.py"] and finding.detail["base"] == commit.id
    read.agents.assert_awaited_once_with(since=NOW - timedelta(days=14))
    read.steps.assert_awaited_once_with(since=NOW - timedelta(days=14))
    assert list(read.samples.await_args.args[0]) == [item.id for item in reversed(steps[:-1])][:12]
    assert len(llms["max"].requests) == 12 and len(llms["low"].requests) == 12 and llms["high"].requests == []
    assert [message.role for message in llms["low"].requests[0][0]] == ["system", "user"]
    version = repo.main_version
    assert version.id == finding.detail["candidate"] != commit.id
    assert type(version.graph.agents["translator"]).llm == "low"
    assert {path for path in set(files) | set(version.tree) if files.get(path) != version.tree.get(path)} == {"agent/translator.py"}
    assert version.commit.message.startswith("optimizer:tuning effort translator max->low")
    trailers = watch.trailers(version.commit.message)
    assert (trailers["Tuning-Id"], trailers["Tuning-Kind"], trailers["Tuning-Agent"]) == ("effort/translator", "effort", "translator")
    assert json.loads(trailers["Tuning-Gate"])["tier"] == "low"
    assert json.loads(trailers["Tuning-Baseline"])["executions"] == 24


async def test_tu05_a_tier_that_disagrees_or_saves_nothing_is_rejected(main):
    """TU05: low は判断が変わり(呼出しに変わる)、high は出力が減らない。どちらも不合格で fork しない。"""
    repo, store, commit, _ = main
    agents, steps, samples = translator_history()
    llms = {"max": echo(1000), "low": Answering(lambda user: Calls((Call("read", {}),), metadata=usage(50))), "high": echo(1000)}

    (finding,) = of_kind(await check_run(readers(agents, steps, samples), llms), "effort")

    assert finding.detail["outcome"] == "rejected"
    assert [gate["tier"] for gate in finding.detail["gates"]] == ["low", "high"]
    assert "agreement 0%" in finding.detail["gates"][0]["reason"]
    assert "no saving" in finding.detail["gates"][1]["reason"]
    assert repo.main.id == commit.id
    store.write.assert_not_awaited()


async def test_tu06_without_merge_the_change_is_only_forked(main):
    """TU06: ``merge`` が偽なら候補を fork で残すだけ。再生の LLM の失敗は判定できない標本になり、有効な標本で判定する。"""
    repo, store, commit, _ = main
    agents, steps, samples = translator_history()

    def flaky(user: str) -> Any:
        index = int(user.split()[1])
        return RuntimeError("timeout") if index % 6 == 0 else FinalAnswer(user.replace("text", "answer"), metadata=usage(100, 90))

    llms = {"max": echo(1000), "low": Answering(flaky)}

    (finding,) = of_kind(await check_run(readers(agents, steps, samples), llms, merge=False), "effort")

    assert finding.detail["outcome"] == "forked"
    gate = finding.detail["gates"][0]
    assert (gate["candidate"]["samples"], gate["candidate"]["valid"], gate["candidate"]["agreement"]) == (12, 10, 1.0)
    assert repo.main.id == commit.id
    store.push.assert_not_awaited()
    assert finding.detail["candidate"] in store.written


def test_tu07_the_gate_compares_with_the_current_tier():
    """TU07: 一致率は現在の段の自己一致率から 10 ポイントまでの低下を許し、自己一致率は 80% 以上、有効な標本は 8 以上要る。
    節約は両方が応答した標本の出力 token で測り、失敗した再生は数えない。"""

    def runs(agreements: int, total: int, *, output: int, failed: int = 0) -> list[replay.Replay]:
        return ([replay.Replay(index, index < agreements, output, 1.0) for index in range(total)]
                + [replay.Replay(100 + index, None, 0, 0.01, responded=False) for index in range(failed)])

    assert replay.decide("low", runs(9, 10, output=1000), runs(8, 10, output=100)).passed
    assert not replay.decide("low", runs(9, 10, output=1000), runs(7, 10, output=100)).passed
    assert not replay.decide("low", runs(9, 10, output=1000), runs(7, 7, output=100)).passed
    assert not replay.decide("low", runs(9, 10, output=1000), runs(9, 10, output=800)).passed
    weak = replay.decide("low", runs(7, 10, output=1000), runs(7, 10, output=100))
    assert not weak.passed and "the replay cannot judge this agent" in weak.reason
    failures = replay.decide("low", runs(10, 12, output=1000), runs(8, 8, output=1000, failed=4))
    assert not failures.passed and "no saving" in failures.reason
    assert failures.candidate["output_tokens"] == 1000.0 and failures.candidate["measured"] == 8


# ---- 観察と取消し


async def test_tu08_a_tuning_that_lowers_the_success_rate_is_reverted(tuned):
    """TU08: 調整を含む版で成功率が下がれば、調整が変えたファイルを前に戻して merge する。取り消した Tuning-Id は冷却中で再提案しない。"""
    repo, _, base, tuning, before, _ = await tuned()
    old_rows, old_steps = executions_on(base, "b", 10, at=NOW - timedelta(days=2))
    new_rows, new_steps = executions_on(tuning, "a", 10, failures=5, ms=3_000)
    assert watch.trailers(tuning.message)["Tuning-To"] == "low"

    findings = await check_run(readers([*old_rows, *new_rows], [*old_steps, *new_steps]), {"max": echo(1000), "low": echo(100)})

    (watched,) = of_kind(findings, "watch")
    assert watched.detail["outcome"] == "reverted" and "success rate 50%" in watched.detail["reason"]
    assert watched.detail["before"]["ok"] == 1.0 and watched.detail["after"]["ok"] == 0.5
    head = repo.main_version
    assert declared_tier(head.tree["agent/translator.py"].decode(), "translator") == "max"
    assert {path for path in set(before) | set(head.tree) if before.get(path) != head.tree.get(path)} == {"agent/translator.py"}
    assert head.commit.parent == tuning.id
    assert head.commit.message.startswith("optimizer:tuning revert effort/translator")
    trailers = watch.trailers(head.commit.message)
    assert (trailers["Tuning-Revert"], trailers["Tuning-From"], trailers["Tuning-To"]) == (tuning.id, "low", "max")
    (effort,) = of_kind(findings, "effort")
    assert (effort.detail["outcome"], effort.detail["reason"]) == ("skipped", "reverted within the cooldown")


@pytest.mark.parametrize(
    ("count", "ms", "outcome"),
    [(4, 3_000, "watching"), (10, 3_000, "kept"), (10, 14_000, "reverted")],
    ids=["too-few", "faster", "no-gain"],
)
async def test_tu09_watching_keeping_and_reverting_without_gain(tuned, count, ms, outcome):
    """TU09: 調整を含む版の実行が 10 未満なら観察中、速くなれば維持、LLM 一応答の時間が下がらなければ取り消す。"""
    repo, _, base, tuning, _, _ = await tuned()
    old_rows, old_steps = executions_on(base, "b", 10, at=NOW - timedelta(days=2))
    new_rows, new_steps = executions_on(tuning, "a", count, ms=ms)

    (watched,) = of_kind(await check_run(readers([*old_rows, *new_rows], [*old_steps, *new_steps]), {}), "watch")

    assert watched.detail["outcome"] == outcome
    assert (repo.main.id == tuning.id) == (outcome != "reverted")


async def test_tu10_a_revert_keeps_other_changes_and_survives_merge_commits(tuned):
    """TU10: (a) 調整の後に人が同じファイルを変えていても、取消しは llm の宣言だけを戻し、人の変更は残る
    (b) 人の merge commit で調整が main の第一親の線の外にあっても、main で効いていれば観察して取り消す。"""
    before = minimal_tree(("dispatch", "translator"))["agent/translator.py"].decode()
    edited = set_tier(before, "translator", "low") + "\n# edited by a person\n"
    repo, _, base, tuning, _, _ = await tuned(later={"agent/translator.py": edited.encode()})
    old_rows, old_steps = executions_on(base, "b", 10, at=NOW - timedelta(days=2))
    new_rows, new_steps = executions_on(tuning, "a", 10, failures=5)

    (watched,) = of_kind(await check_run(readers([*old_rows, *new_rows], [*old_steps, *new_steps]), {}), "watch")

    assert watched.detail["outcome"] == "reverted"
    reverted = repo.main_version.tree["agent/translator.py"].decode()
    assert reverted == set_tier(edited, "translator", "max") and reverted.endswith("# edited by a person\n")
    await repo.close()

    repo, _, base, tuning, _, _ = await tuned(shape="merge")
    merged = repo.main.id
    old_rows, old_steps = executions_on(base, "c", 10, at=NOW - timedelta(days=2))
    new_rows, new_steps = executions_on(repo.main, "m", 10, failures=5)

    (watched,) = of_kind(await check_run(readers([*old_rows, *new_rows], [*old_steps, *new_steps]), {}), "watch")

    assert watched.detail["outcome"] == "reverted" and watched.detail["after"]["executions"] == 10
    assert repo.main_version.commit.parent == merged
    assert declared_tier(repo.main_version.tree["agent/translator.py"].decode(), "translator") == "max"
    assert repo.main_version.tree["tools/person.py"] == b"VALUE = 2\n"


async def test_tu14_a_watched_agent_gets_no_new_effort_change(tuned):
    """TU14: max から high にした調整を観察している間は、high から low への effort も出さない(LLM を呼ばない)。"""
    _, store, _, tuning, _, _ = await tuned(tier="high")
    rows, steps = executions_on(tuning, "w", 4)
    extra = [step("translator", f"x{index}") for index in range(20)]
    llms = {"max": Answering(lambda user: pytest.fail("no replay")), "low": Answering(lambda user: pytest.fail("no replay"))}

    findings = await check_run(readers(rows, [*steps, *extra]), llms)

    assert [finding.detail["outcome"] for finding in of_kind(findings, "watch")] == ["watching"]
    (effort,) = of_kind(findings, "effort")
    assert (effort.detail["outcome"], effort.detail["reason"]) == ("skipped", "an earlier tuning of this agent is being watched")
    store.write.assert_not_awaited()


# ---- 変更と比較の部品


def test_tu11_set_tier_edits_only_the_declaration():
    """TU11: 宣言があれば値だけを置き換え、なければ name の次の行に同じ形で加える。類がない・一行の文字列でなければ ValueError。"""
    annotated = ('from typing import ClassVar\n\n\nclass A:\n    name: ClassVar[str] = "a"\n    #: the LLM\n'
                 '    llm: ClassVar[str] = "max"  # 既定\n\n\nclass B:\n    name: ClassVar[str] = "b"\n    description = "x"\n')
    assert set_tier(annotated, "a", "low") == annotated.replace('= "max"  # 既定', '= "low"  # 既定')
    assert set_tier(annotated, "b", "high") == annotated.replace(
        '    name: ClassVar[str] = "b"\n', '    name: ClassVar[str] = "b"\n    llm: ClassVar[str] = "high"\n')
    assert set_tier('class C:\n    name = "c"\n', "c", "low") == 'class C:\n    name = "c"\n    llm = "low"\n'
    with pytest.raises(ValueError, match="no class"):
        set_tier('class C:\n    name = "c"\n', "missing", "low")
    with pytest.raises(ValueError, match="one-line string"):
        set_tier('class D:\n    name = "d"\n    llm = TIER\n', "d", "low")


async def test_tu12_answers_are_compared_by_decision_and_free_text_by_a_blind_judge():
    """TU12: 呼出しは Tool 名、JSON は短い値の骨格、同じ本文は一致。自由文は位置を入れ替える判定者に任せ、読めない判定は None。"""
    rng = random.Random(0)
    request = {"messages": [{"role": "system", "content": "summarize"}, {"role": "user", "content": "long input"}], "callables": []}

    def recorded(result: Any) -> Sample:
        return Sample(1, "r", request, result)

    unused = Answering(lambda user: pytest.fail("the judge is not needed"))
    calls = recorded({"type": "calls", "calls": [{"name": "read", "args": {}}, {"name": "grep", "args": {}}]})
    assert (await replay.compare(calls, Calls((Call("grep", {"q": 1}), Call("read", {}))), unused, rng=rng))[0] is True
    assert (await replay.compare(calls, Calls((Call("read", {}),)), unused, rng=rng))[0] is False
    assert (await replay.compare(calls, FinalAnswer("done"), unused, rng=rng))[0] is False
    category = recorded({"type": "final_answer", "text": json.dumps({"category": "ignore", "reason": "x" * 50})})
    assert (await replay.compare(category, FinalAnswer(json.dumps({"category": "ignore", "reason": "y" * 60})), unused, rng=rng))[0]
    assert not (await replay.compare(category, FinalAnswer(json.dumps({"category": "investigate", "reason": "y"})), unused, rng=rng))[0]

    judge = Answering(lambda user: FinalAnswer(json.dumps({"verdict": "a_better" if json.loads(user)["A"].startswith("GOOD") else "b_better"})))
    good, bad = recorded({"type": "final_answer", "text": "GOOD summary"}), recorded({"type": "final_answer", "text": "bad summary"})
    for seed_value in range(6):
        assert (await replay.compare(bad, FinalAnswer("GOOD text"), judge, rng=random.Random(seed_value)))[0] is True
        assert (await replay.compare(good, FinalAnswer("bad text"), judge, rng=random.Random(seed_value)))[0] is False
    assert {json.loads(messages[1].content)["A"][:4] for messages, _ in judge.requests} == {"GOOD", "bad "}
    assert (await replay.compare(good, FinalAnswer("GOOD summary"), unused, rng=rng))[0] is True
    broken = Answering(lambda user: FinalAnswer("not json"))
    assert (await replay.compare(bad, FinalAnswer("other"), broken, rng=rng))[0] is None


async def test_tu13_other_waste_is_reported_without_llm_or_fork(singleton):
    """TU13: effort 以外の探査は報告だけ(outcome ``reported``、見積もりの token の多い順)。main にない Agent(discord)は報告しない。
    LLM・fork を使わない。"""
    _, store, _ = await singleton(minimal_tree(("dispatch", "translator", "project")))
    same = [step("translator", f"t{index}", input_hash="fixed") for index in range(6)]
    cold = [step("project", f"p{index}", inputs=50_000, cached=5_000, reasoning=0) for index in range(10)]
    gone = [step("discord", f"d{index}", input_hash="old") for index in range(6)]
    rows = [agent_row("project", f"p{index}") for index in range(10)] + [agent_row("translator", f"t{index}") for index in range(6)]
    llms = {"max": Answering(lambda user: pytest.fail("no LLM call"))}

    findings = await check_run(readers(rows, [*same, *cold, *gone]), llms)

    assert [(finding.detail["kind"], finding.detail["agent"], finding.detail["outcome"]) for finding in findings] == [
        ("cache", "project", "reported"), ("repeated_input", "translator", "reported")]
    assert findings[0].detail["tokens"] == 450_000 and findings[0].evidence
    store.write.assert_not_awaited()


async def test_tu15_too_few_recorded_steps_are_rejected_without_replay(main):
    """TU15: 再生できる記録(入力の重複を除く)が 8 件未満なら、LLM を呼ばずに rejected。"""
    agents, steps, samples = translator_history()
    llms = {"max": Answering(lambda user: pytest.fail("no replay")), "low": Answering(lambda user: pytest.fail("no replay"))}

    (finding,) = of_kind(await check_run(readers(agents, steps, samples[-7:]), llms), "effort")

    assert finding.detail["outcome"] == "rejected" and finding.detail["reason"] == "only 7 recorded steps to replay (need 8)"


@pytest.mark.parametrize(
    ("shape", "outcome", "reason"),
    [("human-revert", "skipped", "reverted within the cooldown"), ("fork", "merged", None)],
    ids=["human-revert", "fork-only"],
)
async def test_tu16_a_tuning_undone_by_a_person_cools_down_but_a_fork_only_candidate_does_not(tuned, shape, outcome, reason):
    """TU16: 人が main で llm を戻した調整(main の第一親の線にあり、効いていない)は冷却中で再提案しない。
    fork のままの候補(merge なしの実行の残り)は冷却せず、再び判定して merge する。"""
    if shape == "human-revert":
        before = minimal_tree(("dispatch", "translator"))["agent/translator.py"].decode()
        await tuned(later={"agent/translator.py": set_tier(before, "translator", "max").encode()})
    else:
        await tuned(shape="fork")
    agents, steps, samples = translator_history()

    findings = await check_run(readers(agents, steps, samples), {"max": echo(1000), "low": echo(100)})

    assert of_kind(findings, "watch") == []
    (effort,) = of_kind(findings, "effort")
    assert effort.detail["outcome"] == outcome
    if reason is not None:
        assert effort.detail["reason"] == reason


async def test_tu17_a_failing_part_does_not_stop_the_others(main):
    """TU17: 標本の読取りが失敗しても effort はその finding(failed)になり、報告は続く。依頼の読取りの失敗は prefetch の failed になる。"""
    agents, steps, samples = translator_history()
    same = [step("translator", f"r{index}", input_hash="fixed", output=100, reasoning=90) for index in range(6)]
    read = readers(agents, [*steps, *same], samples)
    read.samples.side_effect = RuntimeError("database is gone")
    read.requests.side_effect = RuntimeError("database is gone")
    targets = [agent_row("translator", f"q{index}") for index in range(10)]
    first = [step("translator", f"q{index}", calls=[("read", {"path": "docs/x.md"})], output=100, reasoning=90) for index in range(10)]
    read.agents.return_value = [*agents, *targets]
    read.steps.return_value = [*steps, *same, *first]

    findings = await check_run(read, {"max": echo(1000), "low": echo(100)})

    (effort,) = of_kind(findings, "effort")
    assert effort.detail["outcome"] == "failed" and "database is gone" in effort.detail["reason"]
    assert [finding.detail["outcome"] for finding in of_kind(findings, "prefetch")] == ["failed"]
    assert [finding.detail["outcome"] for finding in of_kind(findings, "repeated_input")] == ["reported"]


def test_tu18_the_last_llm_declaration_wins_and_inheriting_classes_are_found():
    """TU18: llm を二度代入した類は最後の代入を書き換える。類を継いで llm を宣言しない Agent 類は inheritors が返す。"""
    twice = 'class A:\n    name = "a"\n    llm = "max"\n    llm = "high"\n'
    assert set_tier(twice, "a", "low") == 'class A:\n    name = "a"\n    llm = "max"\n    llm = "low"\n'
    assert declared_tier(twice, "a") == "high"
    tree = {
        "agent/a.py": b'class Base:\n    name = "base"\n',
        "agent/b.py": b'from agent import a\n\n\nclass Child(a.Base):\n    name = "child"\n\n\nclass Own(a.Base):\n    name = "own"\n    llm = "max"\n',
        "agent/c.py": b'class Helper(Base):\n    pass\n',
    }
    assert inheritors(tree, "Base") == ["agent/b.py:Child"]
    assert inheritors(tree, "Child") == []
