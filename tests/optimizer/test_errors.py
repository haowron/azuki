"""Optimizer の観点 errors: OP11-OP22・OP40・OP42・OP44(docs/components/OPTIMIZER.md)。

誤りの読取りと LLM は代役にし、版庫は代役の store で開いた singleton(実際の Repo)を使う。
fork の読込みと merge は実際に通す。PostgreSQL と実 LLM は使わない。
計画した module は用例の中で import し、実装前は各用例が red になる。
"""

import asyncio
import importlib
import itertools
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from tests.fakes import ScriptedLLM
from tests.versioning.helpers import api, entry_source, host_services, make_commit, minimal_tree, seed
from trackeverything.core.abc.llm import Call, Calls, FinalAnswer, LLMClient
from trackeverything.core.builtin.domain.execution_log import current_run_id
from trackeverything.optimizer import CheckContext, Observation

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
CURRENT = "run-current"
OLD = "c.00000000"
NAMES = ("dispatch", "review", "triage", "general")
TOOLS = {"list_files", "read_file", "write_file"}
_executions = itertools.count()


def errors() -> Any:
    return importlib.import_module("trackeverything.optimizer.errors")


def row(component: str, run: str, label: str | None, *, status: str = "error", execution: str | None = None,
        parent: str | None = None, cause: str = "KeyError: 'diff'") -> Any:
    """``logs`` の agent 行。``label`` は ``code_version`` の ``+c.<…>``(None なら版の外の行)。"""
    return errors().ErrorRow(
        run_id=run,
        execution_id=execution or f"x{next(_executions)}",
        parent_execution_id=parent,
        component=component,
        started_at=(NOW - timedelta(hours=1)).isoformat(),
        status=status,
        error=[{"stage": "execution", "cause": cause}],
        code_version=f"hostsha+{label}" if label is not None else "hostsha",
    )


def final(verdict: str, reason: str = "because") -> FinalAnswer:
    return FinalAnswer(json.dumps({"verdict": verdict, "reason": reason}))


def calls(*items: tuple[str, dict[str, Any]]) -> Calls:
    return Calls(tuple(Call(name, args) for name, args in items))


def write(path: str, content: str) -> tuple[str, dict[str, Any]]:
    return ("write_file", {"path": path, "content": content})


async def check_run(rows: list[Any], llm: LLMClient, *, read: Any = None) -> tuple[list[Any], Any]:
    """観点 errors を一度実行する。現在の run は ``CURRENT``、観測時刻は ``NOW``。(findings, 読取り)。"""
    read = read if read is not None else AsyncMock(return_value=list(rows))
    check = errors().ErrorsCheck(read=read)
    ctx = CheckContext(observation=Observation(observed_at=NOW, main_version="", runs=()), llms={"max": llm})
    token = current_run_id.set(CURRENT)
    try:
        findings = list(await check.run(ctx))
    finally:
        current_run_id.reset(token)
    return findings, read


def by_agent(findings: list[Any]) -> dict[str, Any]:
    return {finding.detail["agent"]: finding for finding in findings}


def saved(store: Any) -> list[tuple[Any, dict[str, bytes]]]:
    """fork で保持(keep)した版の ``(Commit, tree)``。"""
    kept = [call.args[0] for call in store.keep.await_args_list]
    return [(store.written[version_id][0], dict(store.written[version_id][1])) for version_id in kept]


def user_message(request: tuple[list[Any], list[dict[str, Any]]]) -> str:
    messages, _ = request
    return next(message.content for message in messages if message.role == "user")


def tool_results(request: tuple[list[Any], list[dict[str, Any]]], count: int) -> list[dict[str, Any]]:
    messages, _ = request
    tail = messages[-count:]
    assert [message.role for message in tail] == ["tool"] * count
    return [json.loads(message.content) for message in tail]


@pytest.fixture
async def main(singleton):
    """dispatch・review・triage・general を持つ main の singleton の版庫。(版庫, store, main の Commit, main の tree)。"""
    files = minimal_tree(NAMES)
    repo, store, commit = await singleton(files)
    return repo, store, commit, files


async def test_op11_the_deepest_failed_agent_of_each_run_is_the_cause(main):
    """OP11: 子の失敗で失敗した親・cancelled・optimizer 自身・現在の run は対象外。読取りは 24 時間前を下限に一度。"""
    repo, store, _, _ = main
    label = repo.main_version.label
    rows = [
        row("dispatch", "r1", label, execution="d1"),
        row("review", "r1", label, parent="d1"),
        row("triage", "r2", label, status="max_steps"),
        row("general", "r2", label, status="cancelled"),
        row("optimizer", "r3", label),
        row("review", CURRENT, label),
    ]
    llm = ScriptedLLM(final("not_agent"), final("not_agent"))

    findings, read = await check_run(rows, llm)

    read.assert_awaited_once_with(since=NOW - timedelta(hours=24))
    assert [finding.detail["agent"] for finding in findings] == ["review", "triage"]
    assert [finding.detail["outcome"] for finding in findings] == ["not_agent", "not_agent"]
    assert findings[0].evidence == ("r1",) and findings[1].evidence == ("r2",)
    store.write.assert_not_awaited()


async def test_op12_only_errors_on_the_main_version_reach_the_llm(main):
    """OP12: 観測時の main の版の行だけを LLM の入力に使う。"""
    repo, _, _, _ = main
    rows = [row("review", "r-new", repo.main_version.label), row("review", "r-old", OLD)]
    llm = ScriptedLLM(final("not_agent"))

    findings, _ = await check_run(rows, llm)

    (finding,) = findings
    assert finding.evidence == ("r-new",)
    text = user_message(llm.requests[0])
    assert "r-new" in text and "r-old" not in text


async def test_op13_errors_only_on_older_versions_are_stale(main):
    """OP13: 古い版の誤りだけの Agent は skipped(stale)で、LLM を呼ばない。"""
    _, store, _, _ = main
    llm = ScriptedLLM()

    findings, _ = await check_run([row("review", "r1", OLD), row("review", "r2", OLD)], llm)

    (finding,) = findings
    assert (finding.detail["agent"], finding.detail["outcome"], finding.detail["reason"]) == ("review", "skipped", "stale")
    assert llm.requests == []
    store.write.assert_not_awaited()


@pytest.mark.parametrize(
    ("component", "versioned", "reason"),
    [("review", False, "no component version"), ("ghost", True, "not in main")],
    ids=["no-label", "not-in-main"],
)
async def test_op14_errors_without_a_resolvable_agent_are_skipped(main, component, versioned, reason):
    """OP14: 版の label のない行だけの Agent・main にない Agent は理由付きで skipped、LLM を呼ばない。"""
    repo, _, _, _ = main
    llm = ScriptedLLM()

    findings, _ = await check_run([row(component, "r1", repo.main_version.label if versioned else None)], llm)

    (finding,) = findings
    assert (finding.detail["agent"], finding.detail["outcome"], finding.detail["reason"]) == (component, "skipped", reason)
    assert llm.requests == []


async def test_op15_a_fixed_agent_is_forked_from_main_and_merged(main):
    """OP15: 直した tree を main から fork し、merge で main を進めて merged を報告する。"""
    repo, store, commit, files = main
    llm = ScriptedLLM(
        calls(("read_file", {"path": "agent/review.py"})),
        calls(write("tools/vl_dep.py", "VERSION = 2\n")),
        final("fixed", "dep was stale"),
    )

    findings, _ = await check_run([row("review", "r1", repo.main_version.label)], llm)

    ((candidate, tree),) = saved(store)
    assert candidate.parent == commit.id
    assert candidate.message == "optimizer:errors review runs=r1"
    assert tree == {**files, "tools/vl_dep.py": b"VERSION = 2\n"}
    assert store.fetch.return_value == store.read_main.return_value == candidate.id
    assert api().repo.current_repo().main.id == candidate.id
    (finding,) = findings
    assert finding.summary.startswith("review: merged")
    assert finding.evidence == ("r1",)
    assert finding.detail == {"agent": "review", "outcome": "merged", "reason": "dep was stale",
                              "base": commit.id, "candidate": candidate.id, "paths": ["tools/vl_dep.py"]}
    assert finding.to_json()["detail"] == finding.detail

    messages, callables = llm.requests[0]
    assert messages[0].role == "system"
    assert {item["name"] for item in callables} == TOOLS
    text = user_message(llm.requests[0])
    assert "review" in text and "KeyError: 'diff'" in text and "agent/review.py" in text
    (read_result,) = tool_results(llm.requests[1], 1)
    assert read_result["ok"] is True and "class Entry" in read_result["output"]
    (write_result,) = tool_results(llm.requests[2], 1)
    assert write_result["ok"] is True


@pytest.mark.parametrize(
    ("script", "outcome"),
    [
        ((final("not_agent"),), "not_agent"),
        ((calls(write("tools/vl_dep.py", "VERSION = 2\n")), final("unfixable")), "unfixable"),
        ((final("fixed"),), "unfixable"),
        ((FinalAnswer("原因は外部の障害です。 " + final("not_agent").text),), "not_agent"),
    ],
    ids=["not-agent", "writes-but-unfixable", "fixed-without-writes", "text-before-the-json"],
)
async def test_op16_no_fork_without_a_fixed_verdict_and_writes(main, script, outcome):
    """OP16: fixed でない書込みは捨て、書込みのない fixed は unfixable。どれも fork せず main は変わらない。最終回答の JSON の前の文は読み飛ばす。"""
    repo, store, commit, _ = main
    llm = ScriptedLLM(*script)

    findings, _ = await check_run([row("review", "r1", repo.main_version.label)], llm)

    (finding,) = findings
    assert finding.detail["outcome"] == outcome
    store.write.assert_not_awaited()
    store.move_main.assert_not_awaited()
    assert api().repo.current_repo().main.id == commit.id


async def test_op17_writes_are_limited_to_agent_tools_and_skills(main):
    """OP17: agent/・tools/・skills/ の外への書込みは失敗の結果を返して tree を変えない。"""
    repo, store, _, files = main
    rejected = ["harnesses/x.py", "sources/x.py", "../x.py", "core/x.py", "agent/../x.py"]
    accepted = {"agent/x.py": "X = 1\n", "tools/x.py": "X = 1\n", "skills/a/SKILL.md": "# a\n"}
    llm = ScriptedLLM(
        calls(*(write(path, "X = 1\n") for path in rejected), *(write(path, content) for path, content in accepted.items())),
        final("fixed"),
    )

    findings, _ = await check_run([row("review", "r1", repo.main_version.label)], llm)

    results = tool_results(llm.requests[1], len(rejected) + len(accepted))
    assert [result["ok"] for result in results] == [False] * len(rejected) + [True] * len(accepted)
    assert all(result["error"] for result in results[: len(rejected)])
    ((_, tree),) = saved(store)
    assert tree == {**files, **{path: content.encode() for path, content in accepted.items()}}
    assert findings[0].detail["outcome"] == "merged"
    assert findings[0].detail["paths"] == sorted(accepted)


async def test_op18_a_fork_failure_is_returned_to_the_llm_once(main):
    """OP18 (a): 読み込めない tree の fork の失敗は LLM へ返り、直した 2 回目を merge する。"""
    repo, store, commit, _ = main
    fixed = entry_source("review") + "\n# fixed\n"
    llm = ScriptedLLM(
        calls(write("agent/review.py", "def broken(:\n")),
        final("fixed"),
        calls(write("agent/review.py", fixed)),
        final("fixed"),
    )

    findings, _ = await check_run([row("review", "r1", repo.main_version.label)], llm)

    messages, _ = llm.requests[2]
    assert messages[-1].role == "user"
    assert "SyntaxError" in messages[-1].content and "agent/review.py" in messages[-1].content
    ((candidate, tree),) = saved(store)
    assert candidate.parent == commit.id and tree["agent/review.py"] == fixed.encode()
    assert findings[0].detail["outcome"] == "merged"


async def test_op18_two_fork_failures_leave_main_unchanged(main):
    """OP18 (b): fork が 2 回とも失敗すれば fork_failed で、3 回目を求めず main は変わらない。"""
    repo, store, commit, _ = main
    llm = ScriptedLLM(
        calls(write("agent/review.py", "def broken(:\n")),
        final("fixed"),
        calls(write("agent/review.py", "def still_broken(:\n")),
        final("fixed"),
    )

    findings, _ = await check_run([row("review", "r1", repo.main_version.label)], llm)

    assert findings[0].detail["outcome"] == "fork_failed"
    assert len(llm.requests) == 4
    store.keep.assert_not_awaited()
    store.push.assert_not_awaited()
    store.move_main.assert_not_awaited()
    assert api().repo.current_repo().main.id == commit.id


def _moves_github_after_the_fork(store: Any, other: Any) -> None:
    """GitHub の main は、fork が版を書いた直後に他の push で ``other`` へ動く。"""
    writing = store.write.side_effect

    async def write_then_move(*args: Any) -> Any:
        commit = await writing(*args)
        store.fetch.return_value = other.id
        return commit

    store.write.side_effect = write_then_move


async def test_op19_a_merge_after_github_main_moved_is_rebased_and_merged(main):
    """OP19 (a): fork の後に他の push で GitHub の main が動いたら、merge は候補をその上へ載せ直して push し、merged と元の候補を報告する。"""
    repo, store, commit, files = main
    other_files = {**files, "tools/other.txt": b"other"}
    other = make_commit(other_files, parent=commit.id, message="person")
    seed(store, (other, other_files))
    _moves_github_after_the_fork(store, other)
    combined = {**other_files, "tools/vl_dep.py": b"VERSION = 2\n"}
    store.merge_tree.return_value = combined
    llm = ScriptedLLM(calls(write("tools/vl_dep.py", "VERSION = 2\n")), final("fixed", "dep was stale"))

    findings, _ = await check_run([row("review", "r1", repo.main_version.label)], llm)

    (candidate, _), (rebased, tree) = saved(store)
    assert candidate.parent == commit.id and rebased.parent == other.id and tree == combined
    assert findings[0].detail == {"agent": "review", "outcome": "merged", "reason": "dep was stale", "base": commit.id,
                                  "candidate": rebased.id, "rebased_from": candidate.id, "paths": ["tools/vl_dep.py"]}
    assert store.fetch.return_value == store.read_main.return_value == rebased.id
    assert api().repo.current_repo().main.id == rebased.id


async def test_op19_a_merge_that_cannot_be_rebased_fails(main):
    """OP19 (b): 載せ直しが衝突すれば failed(``merge: MergeConflict: …``)で、GitHub は他の版のまま、mirror の main は変わらない。"""
    repo, store, commit, files = main
    other_files = {**files, "tools/vl_dep.py": b"VERSION = 9\n"}
    other = make_commit(other_files, parent=commit.id, message="person")
    seed(store, (other, other_files))
    _moves_github_after_the_fork(store, other)
    store.merge_tree.side_effect = api().repo.MergeConflict(["tools/vl_dep.py"])
    llm = ScriptedLLM(calls(write("tools/vl_dep.py", "VERSION = 2\n")), final("fixed"))

    findings, _ = await check_run([row("review", "r1", repo.main_version.label)], llm)

    ((candidate, _),) = saved(store)
    detail = findings[0].detail
    assert detail["outcome"] == "failed" and detail["reason"].startswith("merge: MergeConflict") and "tools/vl_dep.py" in detail["reason"]
    assert detail["candidate"] == candidate.id and "rebased_from" not in detail
    assert store.fetch.return_value == other.id
    assert store.read_main.return_value == commit.id == api().repo.current_repo().main.id
    store.push.assert_not_awaited()
    store.move_main.assert_not_awaited()


async def test_op20_agents_are_fixed_in_order_of_error_count_on_the_advancing_main(main):
    """OP20 (a): 誤りの多い順に処理し、後の Agent は先の merge で進んだ main から fork する。"""
    repo, store, commit, files = main
    label = repo.main_version.label
    rows = [row("triage", "r4", label), *(row("review", run, label) for run in ("r1", "r2", "r3"))]
    triage = entry_source("triage") + "\n# fixed\n"
    llm = ScriptedLLM(
        calls(write("tools/vl_dep.py", "VERSION = 2\n")),
        final("fixed"),
        calls(write("agent/triage.py", triage)),
        final("fixed"),
    )

    findings, _ = await check_run(rows, llm)

    assert [(finding.detail["agent"], finding.detail["outcome"]) for finding in findings] == [("review", "merged"), ("triage", "merged")]
    assert "review" in user_message(llm.requests[0]) and "triage" in user_message(llm.requests[2])
    (first, first_tree), (second, second_tree) = saved(store)
    assert first.parent == commit.id and second.parent == first.id
    assert second_tree == {**first_tree, "agent/triage.py": triage.encode()}
    assert first_tree == {**files, "tools/vl_dep.py": b"VERSION = 2\n"}
    assert api().repo.current_repo().main.id == second.id


async def test_op20_agents_over_the_limit_are_skipped(singleton):
    """OP20 (b): 対象の Agent が 6 つなら、誤りの最も少ない 1 つが skipped(over limit)。"""
    names = tuple(f"a{index}" for index in range(1, 7))
    repo, _, _ = await singleton(minimal_tree(names))
    label = repo.main_version.label
    rows = [row(name, f"{name}-{count}", label) for index, name in enumerate(names) for count in range(6 - index)]
    llm = ScriptedLLM(*(final("not_agent") for _ in range(5)))

    findings, _ = await check_run(rows, llm)

    outcomes = {name: (finding.detail["outcome"], finding.detail["reason"]) for name, finding in by_agent(findings).items()}
    assert outcomes["a6"] == ("skipped", "over limit")
    assert all(outcomes[name][0] == "not_agent" for name in names[:5])
    assert all(f"{name}-0" in user_message(llm.requests[index]) for index, name in enumerate(names[:5]))
    assert len(llm.requests) == errors().MAX_AGENTS == 5


@pytest.mark.parametrize(
    ("first", "reason"),
    [
        ((RuntimeError("llm down"),), "RuntimeError: llm down"),
        (tuple(calls(("list_files", {"prefix": ""})) for _ in range(30)), "30"),
        ((FinalAnswer("done"),), "JSON"),
    ],
    ids=["llm-error", "turn-limit", "not-json"],
)
async def test_op21_a_failed_conversation_does_not_stop_the_next_agent(main, first, reason):
    """OP21 (a)-(c): LLM の例外・応答の上限・JSON でない最終回答は failed で fork せず、次の Agent を処理する。"""
    repo, store, _, _ = main
    label = repo.main_version.label
    assert errors().MAX_TURNS == 30
    rows = [row("review", "r1", label), row("review", "r2", label), row("triage", "r3", label)]
    llm = ScriptedLLM(*first, final("not_agent"))

    findings, _ = await check_run(rows, llm)

    review, triage = findings
    assert (review.detail["agent"], review.detail["outcome"]) == ("review", "failed")
    assert reason in review.detail["reason"]
    assert (triage.detail["agent"], triage.detail["outcome"]) == ("triage", "not_agent")
    store.write.assert_not_awaited()


async def test_op21_cancellation_during_the_conversation_propagates(main):
    """OP21 (d): LLM の応答中の取消しは伝わる。"""
    repo, _, _, _ = main
    started = asyncio.Event()

    class Blocking(LLMClient):
        async def respond(self, messages, callables):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(check_run([row("review", "r1", repo.main_version.label)], Blocking()))
    await asyncio.wait_for(started.wait(), 1)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_op22_no_errors_means_no_findings(main):
    """OP22: 24 時間に誤りがなければ finding は空で、LLM・fork を呼ばない。errors は最初に登録した観点。"""
    _, store, _, _ = main
    llm = ScriptedLLM()

    findings, read = await check_run([], llm)

    assert findings == []
    read.assert_awaited_once()
    assert llm.requests == []
    store.write.assert_not_awaited()
    from trackeverything.optimizer import CHECKS

    assert CHECKS[0].name == "errors"
    assert isinstance(CHECKS[0], errors().ErrorsCheck)


async def test_op40_without_merge_a_fixed_repair_is_only_forked(main):
    """OP40: ``merge`` が偽なら fixed の修復は fork で候補を残すだけで、main は変わらない(outcome ``forked``)。"""
    repo, store, commit, _ = main
    rows = [row("review", "r1", repo.main_version.label)]
    llm = ScriptedLLM(calls(write("tools/fixed.py", "VALUE = 1\n")), final("fixed", "added the missing helper"))
    ctx = CheckContext(observation=Observation(observed_at=NOW, main_version="", runs=()), llms={"max": llm}, merge=False)
    token = current_run_id.set(CURRENT)
    try:
        (finding,) = list(await errors().ErrorsCheck(read=AsyncMock(return_value=rows)).run(ctx))
    finally:
        current_run_id.reset(token)

    assert finding.detail["outcome"] == "forked"
    assert finding.detail["candidate"] is not None and finding.detail["paths"] == ["tools/fixed.py"]
    assert repo.main.id == commit.id
    store.push.assert_not_awaited()


# ---- agent/SYSTEM.md

SYSTEM = "agent/SYSTEM.md"


async def test_op42_the_repair_can_read_but_not_write_the_system_preamble(singleton):
    """OP42: read_file は agent/SYSTEM.md を読めるが、write_file は失敗の結果で tree を変えない。書込みのない fixed は unfixable で fork しない。"""
    files = minimal_tree(NAMES, extra={SYSTEM: "RULES\n"})
    repo, store, commit = await singleton(files)
    llm = ScriptedLLM(calls(("read_file", {"path": SYSTEM}), write(SYSTEM, "OTHER RULES\n")), final("fixed"))

    findings, _ = await check_run([row("review", "r1", repo.main_version.label)], llm)

    read, written = tool_results(llm.requests[1], 2)
    assert read["ok"] is True and read["output"] == "RULES\n"
    assert written["ok"] is False and SYSTEM in written["error"]
    assert findings[0].detail["outcome"] == "unfixable"
    store.write.assert_not_awaited()
    assert api().repo.current_repo().main.id == commit.id


async def test_op44_a_rebased_repair_keeps_the_persons_system_preamble(github, tmp_path, monkeypatch):
    """OP44: fork と merge の間に人が agent/SYSTEM.md を push すれば、修復はその main の上へ載せ直され、人の SYSTEM.md と修復の両方が main に残る。"""
    modules = api()
    github.commit(minimal_tree(NAMES, extra={SYSTEM: "RULES_A\n"}), "a")
    store = modules.store.GitVersionStore(tmp_path / "components.git", github.url)
    repo = modules.repo.open_repo(modules.compose.prepare_components(host_services()), store=store)
    try:
        await repo.start()
        writing = store.write
        pushed: list[str] = []

        async def write_then_person_pushes(*args: Any, **kwargs: Any) -> Any:
            # 人は fork の書込みの直後に一度だけ push する(載せ直しの書込みでは push しない)。
            commit = await writing(*args, **kwargs)
            if not pushed:
                pushed.append(github.commit({SYSTEM: "RULES_PERSON\n"}, "person edits SYSTEM.md"))
            return commit

        monkeypatch.setattr(store, "write", write_then_person_pushes)
        llm = ScriptedLLM(calls(write("tools/vl_dep.py", "VERSION = 2\n")), final("fixed", "dep was stale"))

        findings, _ = await check_run([row("review", "r1", repo.main_version.label)], llm)

        (finding,) = findings
        assert finding.detail["outcome"] == "merged" and finding.detail["rebased_from"], finding.detail
        merged = await repo.tree(finding.detail["candidate"])
        assert merged[SYSTEM] == b"RULES_PERSON\n" and merged["tools/vl_dep.py"] == b"VERSION = 2\n"
        assert repo.main.parent == pushed[0]
        assert github.main() == finding.detail["candidate"] == repo.main.id
    finally:
        await repo.close()
