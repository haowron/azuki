"""Optimizer の観点 additions: AD01-AD22(docs/components/OPTIMIZER.md)。

作業の行の読取り・LLM・環境変数は代役にし、版庫は代役の store で開いた singleton(実際の Repo)を使う。
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
from tests.versioning.helpers import api, compose_source, entry_source, make_commit, minimal_tree, seed
from trackeverything.core.abc.llm import Call, Calls, FinalAnswer, LLMClient
from trackeverything.core.builtin.domain.execution_log import current_run_id
from trackeverything.optimizer import CheckContext, Observation

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
START = NOW - timedelta(days=6)
CURRENT = "run-current"
TOOLS = {"list_files", "read_file", "grep", "write_file", "delete_file"}
SECRET = "xoxb-secret-value-123"
ENVIRON = {"SLACK_TOKEN": SECRET, "HOME": "/Users/someone"}
_minutes = itertools.count()
_ids = itertools.count()

SLACK_IMPORT = "from trackeverything.tools.slack import SlackApiTool"
POST_IMPORT = "from trackeverything.tools.slack_post import SlackPostTool"

SLACK = '''
from trackeverything.core.abc.tool import Tool, ToolResult


class SlackApiTool(Tool):
    name = "slack_api"
    description = "Call the Slack Web API."
    arguments = {"type": "object", "properties": {"method": {"type": "string"}}, "required": ["method"]}

    async def run(self, args, *, ctx=None):
        return ToolResult(ok=True, output={"method": args.get("method")})
'''
SLACK_POST = SLACK.replace("SlackApiTool", "SlackPostTool").replace('"slack_api"', '"slack_post"').replace(
    "Call the Slack Web API.", "Post a message to a Slack channel."
)
DEVDB_AGENT = '''
from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.call import AgentResult


class DevdbAgent(BaseAgent):
    name = "devdb"
    description = "Investigate the dev database."

    async def _execute(self, call):
        return AgentResult(status="ok", response="checked")
'''
SKILL = "---\nname: devdb-query\ndescription: dev DB を調べる手順。\n---\n\n# devdb-query\n\n1. venv を作り pymysql を入れる。\n"
AGENTS = ["agent.dispatch.Entry", "agent.review.Entry", "agent.general.GeneralAgent"]


def additions() -> Any:
    return importlib.import_module("trackeverything.optimizer.additions")


def general_source(*, imports: tuple[str, ...] = (SLACK_IMPORT,), tools: tuple[str, ...] = ("SlackApiTool",),
                   skills: tuple[str, ...] = ("evidence-summary",), agents: tuple[str, ...] = ("review",),
                   prompt: str = "Use the tools.", helper: bool = False) -> str:
    """``agent/general.py`` の ``GeneralAgent``。``helper`` は論理の変更(method の追加)。"""
    declared = ", ".join(tools) + ("," if len(tools) == 1 else "")
    lines = [
        "from trackeverything.core.abc.agent import BaseAgent",
        "from trackeverything.core.abc.call import AgentResult",
        *imports,
        "",
        "",
        "class GeneralAgent(BaseAgent):",
        '    name = "general"',
        '    description = "General requests"',
        f"    tools = ({declared})",
        f"    skills = {tuple(skills)!r}",
        f"    agents = {tuple(agents)!r}",
        "",
        "    def get_prompt(self):",
        f"        return {prompt!r}",
        "",
        "    async def _execute(self, call):",
        "        return AgentResult(status='ok', response='done')",
        "",
    ]
    if helper:
        lines += ["    def helper(self):", "        return 1", ""]
    return "\n".join(lines)


def base_tree() -> dict[str, bytes]:
    """dispatch・review(Entry)と general(slack_api・evidence-summary・子 review)、3 つの inventory を持つ版。"""
    return minimal_tree(
        ("dispatch", "review"),
        extra={
            "agent/general.py": general_source(),
            "agent/compose.py": compose_source(AGENTS, ("dispatch",)),
            "tools/slack.py": SLACK,
            "skills/evidence-summary/SKILL.md": "---\nname: evidence-summary\ndescription: Organize facts and sources.\n---\n\n# Evidence summary\n",
            "agent/inventory.md": "# Agents\n",
            "tools/inventory.md": "# Tools\n",
            "skills/inventory.md": "# Skills\n",
        },
    )


# ---- 作業の行


def _at() -> str:
    return (START + timedelta(minutes=next(_minutes))).isoformat()


def general(run: str, execution: str, request: str = "post the summary to slack", *, data: Any = None, status: str = "ok") -> Any:
    """``general`` の agent 行。``request_json`` は ``{"request": …, "data": …}``。"""
    return additions().ActivityRow(
        run_id=run, execution_id=execution, parent_execution_id=f"d-{run}", event_type="agent", component="general",
        started_at=_at(), status=status, request={"request": request, "data": data if data is not None else {"source": "discord"}},
        parent="dispatch",
    )


def tool(run: str, name: str, args: dict[str, Any], *, parent_execution: str, parent: str = "general", status: str = "ok") -> Any:
    """Tool の行。``parent`` は親の agent 行の component。"""
    return additions().ActivityRow(
        run_id=run, execution_id=f"t{next(_ids)}", parent_execution_id=parent_execution, event_type="tool", component=name,
        started_at=_at(), status=status, request=args, parent=parent,
    )


def child(run: str, name: str, request: str, *, execution: str, parent_execution: str, status: str = "ok") -> Any:
    """general の子の agent 行。"""
    return additions().ActivityRow(
        run_id=run, execution_id=execution, parent_execution_id=parent_execution, event_type="agent", component=name,
        started_at=_at(), status=status, request={"request": request, "data": {}}, parent="general",
    )


def activity(*runs: str, command: str = "curl -s https://slack.com/api/chat.postMessage") -> list[Any]:
    """run ごとに general の行と shell の呼出し一つ。"""
    rows: list[Any] = []
    for run in runs:
        rows += [general(run, f"g-{run}", f"post to slack {run}"), tool(run, "shell", {"command": command}, parent_execution=f"g-{run}")]
    return rows


RUNS = ("r1", "r2", "r3")


# ---- LLM の応答


def survey(*candidates: dict[str, Any]) -> FinalAnswer:
    return FinalAnswer(json.dumps({"candidates": list(candidates)}))


def candidate(name: str = "devdb-query", *, kind: str = "skill", owners: tuple[str, ...] = ("general",),
              evidence: tuple[str, ...] = RUNS, purpose: str = "query the dev DB", reason: str = "repeated") -> dict[str, Any]:
    return {"kind": kind, "name": name, "owners": list(owners), "purpose": purpose, "evidence": list(evidence), "reason": reason}


def final(verdict: str, reason: str = "because") -> FinalAnswer:
    return FinalAnswer(json.dumps({"verdict": verdict, "reason": reason}))


def calls(*items: tuple[str, dict[str, Any]]) -> Calls:
    return Calls(tuple(Call(name, args) for name, args in items))


def write(path: str, content: str) -> tuple[str, dict[str, Any]]:
    return ("write_file", {"path": path, "content": content})


def delete(path: str) -> tuple[str, dict[str, Any]]:
    return ("delete_file", {"path": path})


def skill_writes(*, skill: str = SKILL, general: str | None = None) -> list[tuple[str, dict[str, Any]]]:
    """新しい skill ``devdb-query`` の追加: SKILL.md・general の宣言・inventory。"""
    return [
        write("skills/devdb-query/SKILL.md", skill),
        write("agent/general.py", general if general is not None else general_source(skills=("evidence-summary", "devdb-query"))),
        write("skills/inventory.md", "# Skills\n\n- devdb-query\n"),
    ]


GENERAL_WITH_POST = general_source(imports=(SLACK_IMPORT, POST_IMPORT), tools=("SlackApiTool", "SlackPostTool"))


# ---- 実行と観察


async def check_run(rows: list[Any], llm: LLMClient, *, environ: dict[str, str] | None = None, merge: bool = True) -> tuple[list[Any], Any]:
    """観点 additions を一度実行する。現在の run は ``CURRENT``、観測時刻は ``NOW``。(findings, 読取り)。"""
    read = AsyncMock(return_value=list(rows))
    check = additions().AdditionsCheck(read=read, environ=environ if environ is not None else {})
    ctx = CheckContext(observation=Observation(observed_at=NOW, main_version="", runs=()), llms={"max": llm}, merge=merge)
    token = current_run_id.set(CURRENT)
    try:
        findings = list(await check.run(ctx))
    finally:
        current_run_id.reset(token)
    return findings, read


def user_json(request: tuple[list[Any], list[dict[str, Any]]]) -> Any:
    messages, _ = request
    return json.loads(next(message.content for message in messages if message.role == "user"))


def last_message(request: tuple[list[Any], list[dict[str, Any]]]) -> Any:
    messages, _ = request
    return messages[-1]


def tool_results(request: tuple[list[Any], list[dict[str, Any]]], count: int) -> list[dict[str, Any]]:
    messages, _ = request
    tail = messages[-count:]
    assert [message.role for message in tail] == ["tool"] * count
    return [json.loads(message.content) for message in tail]


def saved(store: Any) -> list[tuple[Any, dict[str, bytes]]]:
    """fork で保持(keep)した版の ``(Commit, tree)``。"""
    kept = [call.args[0] for call in store.keep.await_args_list]
    return [(store.written[version_id][0], dict(store.written[version_id][1])) for version_id in kept]


def outcomes(findings: list[Any]) -> list[tuple[str, str, str]]:
    return [(finding.detail["name"], finding.detail["outcome"], finding.detail["reason"]) for finding in findings]


def current_main() -> Any:
    return api().repo.current_repo()


@pytest.fixture
async def main(singleton):
    """``base_tree()`` を main とする singleton の版庫。(版庫, store, main の Commit, main の tree)。"""
    files = base_tree()
    repo, store, commit = await singleton(files)
    return repo, store, commit, files


# ---- 観測


async def test_ad01_the_digest_reads_general_runs_and_shell_commands(main):
    """AD01: general の行・その子の呼出し・他の Agent の shell/bash を run ごとにまとめ、data・llm の行・孫・現在の run・optimizer を除く。"""
    g1 = general("r1", "g1", data={"payload": "DATA-SHOULD-NOT-APPEAR"})
    shell = tool("r1", "shell", {"command": "curl -s https://slack.com/api/chat.postMessage"}, parent_execution="g1")
    slack = tool("r1", "slack_api", {"method": "chat.postMessage"}, parent_execution="g1", status="error")
    codegen = child("r1", "codegen", "write a script", execution="c1", parent_execution="g1")
    grandchild = tool("r1", "read_file", {"path": "GRANDCHILD"}, parent_execution="c1", parent="codegen")
    llm_row = additions().ActivityRow(run_id="r1", execution_id="l1", parent_execution_id="g1", event_type="llm", component="zai/glm",
                                      started_at=_at(), status="ok", request={"messages": "LLM-MESSAGES"}, parent="general")
    bash = tool("r2", "bash", {"command": "uv run pytest -q"}, parent_execution="i1", parent="implementation")
    rows = [g1, llm_row, shell, slack, codegen, grandchild, bash, general(CURRENT, "g-current", "CURRENT-RUN"),
            tool("r3", "shell", {"command": "OPTIMIZER-INTERNAL"}, parent_execution="o1", parent="optimizer")]
    llm = ScriptedLLM(survey())

    findings, read = await check_run(rows, llm)

    assert findings == []
    read.assert_awaited_once_with(since=NOW - timedelta(days=7))
    data = user_json(llm.requests[0])
    assert (data["window_days"], data["min_runs"]) == (7, 3)
    assert data["runs"] == [
        {"run_id": "r1", "started_at": g1.started_at,
         "general": [{"request": "post the summary to slack", "status": "ok", "steps": [
             {"tool": "shell", "command": "curl -s https://slack.com/api/chat.postMessage", "status": "ok"},
             {"tool": "slack_api", "args": '{"method": "chat.postMessage"}', "status": "error"},
             {"agent": "codegen", "request": "write a script", "status": "ok"},
         ]}],
         "commands": []},
        {"run_id": "r2", "started_at": bash.started_at, "general": [],
         "commands": [{"agent": "implementation", "tool": "bash", "command": "uv run pytest -q", "status": "ok"}]},
    ]
    text = json.dumps(data, ensure_ascii=False)
    assert all(marker not in text for marker in ("DATA-SHOULD-NOT-APPEAR", "LLM-MESSAGES", "GRANDCHILD", "CURRENT-RUN", "OPTIMIZER-INTERNAL"))
    catalog = data["catalog"]
    entry = next(agent for agent in catalog["agents"] if agent["name"] == "general")
    assert entry["module"] == "agent/general.py" and "slack_api" in entry["tools"]
    assert (entry["skills"], entry["agents"]) == (["evidence-summary"], ["review"])
    assert {"name": "slack_api", "description": "Call the Slack Web API.", "module": "tools/slack.py", "used_by": ["general"]} in catalog["tools"]
    assert {"name": "evidence-summary", "description": "Organize facts and sources.", "used_by": ["general"]} in catalog["skills"]
    messages, callables = llm.requests[0]
    assert messages[0].role == "system" and callables == []


async def test_ad02_long_text_is_clipped(main):
    """AD02 (a): command は 800、request は 500、他の Tool の引数は 300 文字に切り、末尾に … を付ける。"""
    args = {"text": "z" * 1000}
    rows = [general("r1", "g1", "y" * 2000), tool("r1", "shell", {"command": "x" * 2000}, parent_execution="g1"),
            tool("r1", "slack_api", args, parent_execution="g1")]
    llm = ScriptedLLM(survey())

    await check_run(rows, llm)

    (run,) = user_json(llm.requests[0])["runs"]
    (execution,) = run["general"]
    assert execution["request"] == "y" * 500 + "…"
    shell, slack = execution["steps"]
    assert shell["command"] == "x" * 800 + "…"
    assert slack["args"] == json.dumps(args, ensure_ascii=False)[:300] + "…"


async def test_ad02_the_oldest_runs_are_dropped_beyond_the_digest_limit(main, monkeypatch):
    """AD02 (b): run の JSON が DIGEST_CHARS を超えれば古い run から落とす。"""
    monkeypatch.setattr(additions(), "DIGEST_CHARS", 2000)
    llm = ScriptedLLM(survey())

    await check_run(activity("r1", "r2", "r3", "r4", "r5", command="x" * 700), llm)

    runs = user_json(llm.requests[0])["runs"]
    assert [run["run_id"] for run in runs] == ["r4", "r5"]
    assert len(json.dumps(runs, ensure_ascii=False)) <= 2000


async def test_ad03_runs_used_by_earlier_additions_are_consumed(main):
    """AD03: 窓の中の additions の版(main・fork・載せ直し)が runs= に挙げた run は摘要に入れない。"""
    _, store, commit, files = main

    def version(message: str, days: int) -> tuple[Any, dict[str, bytes]]:
        return make_commit(files, parent=commit.id, message=message, created_at=NOW - timedelta(days=days)), files

    seed(
        store,
        version("optimizer:additions tool slack_post runs=rA,rB", 1),
        version("optimizer:additions skill kept-fork runs=rC", 2),
        version("optimizer:additions skill rebased runs=rD\n\nRebased-From: 0123456789abcdef", 3),
        version("optimizer:additions tool old runs=rE", 8),
        version("person: retry runs=rF", 1),
    )
    llm = ScriptedLLM(survey())

    await check_run(activity("rA", "rB", "rC", "rD", "rE", "rF", "rG"), llm)

    assert [run["run_id"] for run in user_json(llm.requests[0])["runs"]] == ["rE", "rF", "rG"]


async def test_ad04_nothing_to_read_means_no_llm_call(main):
    """AD04 (a): 行がなければ finding は空で LLM を呼ばない。"""
    _, store, _, _ = main
    llm = ScriptedLLM()

    findings, read = await check_run([], llm)

    assert findings == []
    read.assert_awaited_once()
    assert llm.requests == []
    store.write.assert_not_awaited()


async def test_ad04_only_consumed_runs_means_no_llm_call(main):
    """AD04 (b): 消費済みの run の行だけなら finding は空で LLM を呼ばない。"""
    _, store, commit, files = main
    used = make_commit(files, parent=commit.id, message="optimizer:additions tool slack_post runs=rA,rB", created_at=NOW - timedelta(days=1))
    seed(store, (used, files))
    llm = ScriptedLLM()

    findings, _ = await check_run(activity("rA", "rB"), llm)

    assert findings == []
    assert llm.requests == []


# ---- survey の検証


async def test_ad05_a_candidate_needs_three_distinct_runs_in_the_digest(main):
    """AD05: 摘要にある異なる run が 3 つ未満の証拠は too few runs。"""
    llm = ScriptedLLM(
        survey(candidate("devdb-query"), candidate("devdb-check", evidence=("r1", "r1", "r2")),
               candidate("devdb-scan", evidence=("r1", "r2", "ghost"))),
        final("keep"),
    )

    findings, _ = await check_run(activity(*RUNS), llm)

    assert outcomes(findings) == [("devdb-query", "kept", "because"), ("devdb-check", "skipped", "too few runs"),
                                  ("devdb-scan", "skipped", "too few runs")]
    assert [finding.evidence for finding in findings] == [RUNS, ("r1", "r2"), ("r1", "r2")]
    assert len(llm.requests) == 2


async def test_ad06_invalid_candidates_are_skipped_with_a_reason(main):
    """AD06: 不正な候補は会話を始めず、理由付きで skipped。処理した候補の後に survey の順で並ぶ。"""
    llm = ScriptedLLM(
        survey(
            candidate("devdb-query"),
            candidate("slack_api", kind="tool"),
            candidate("ghost", kind="existing"),
            candidate("slack_post", kind="tool", owners=("ghost",)),
            candidate("Bad-Name", kind="tool"),
            candidate("bad_name", kind="skill"),
            candidate("x", kind="plugin"),
            candidate("devdb-query"),
        ),
        final("keep"),
    )

    findings, _ = await check_run(activity(*RUNS), llm)

    assert outcomes(findings) == [
        ("devdb-query", "kept", "because"),
        ("slack_api", "skipped", "name taken"),
        ("ghost", "skipped", "unknown component"),
        ("slack_post", "skipped", "unknown owner"),
        ("Bad-Name", "skipped", "invalid name"),
        ("bad_name", "skipped", "invalid name"),
        ("x", "skipped", "unknown kind"),
        ("devdb-query", "skipped", "duplicate"),
    ]
    assert len(llm.requests) == 2


async def test_ad07_every_valid_candidate_is_handled(main):
    """AD07: 有効な候補は数の上限なく、survey の順に全て処理する。"""
    names = ("a-one", "a-two", "a-three", "a-four")
    llm = ScriptedLLM(survey(*(candidate(name) for name in names)), *(final("keep") for _ in names))

    findings, _ = await check_run(activity(*RUNS), llm)

    assert not hasattr(additions(), "MAX_CANDIDATES")
    assert outcomes(findings) == [(name, "kept", "because") for name in names]
    assert [user_json(request)["name"] for request in llm.requests[1:]] == list(names)


@pytest.mark.parametrize(
    "answer",
    [RuntimeError("llm down"), calls(("list_files", {"prefix": ""})), FinalAnswer("not json"),
     FinalAnswer(json.dumps({"candidates": "x"}))],
    ids=["llm-error", "tool-call", "not-json", "wrong-shape"],
)
async def test_ad08_a_failed_survey_is_one_failed_finding(main, answer):
    """AD08: survey の失敗は kind survey の failed 一つで、会話も fork もしない。"""
    _, store, _, _ = main
    llm = ScriptedLLM(answer)

    findings, _ = await check_run(activity(*RUNS), llm)

    (finding,) = findings
    assert (finding.detail["kind"], finding.detail["name"], finding.detail["outcome"]) == ("survey", None, "failed")
    assert finding.summary.startswith("survey: failed")
    if isinstance(answer, RuntimeError):
        assert "RuntimeError: llm down" in finding.detail["reason"]
    assert len(llm.requests) == 1
    store.write.assert_not_awaited()


# ---- 新しい部品の追加


async def test_ad09_a_new_skill_is_wired_forked_and_merged(main):
    """AD09: 新しい skill を general に宣言して inventory に載せ、main から fork して merge する。"""
    _, store, commit, files = main
    llm = ScriptedLLM(
        survey(candidate("devdb-query", purpose="query the dev DB with pymysql")),
        calls(("read_file", {"path": "agent/general.py"})),
        calls(*skill_writes()),
        final("changed", "added the skill"),
    )

    findings, _ = await check_run(activity(*RUNS), llm)

    ((forked, tree),) = saved(store)
    assert forked.parent == commit.id
    assert forked.message == "optimizer:additions skill devdb-query runs=r1,r2,r3"
    assert tree == {
        **files,
        "skills/devdb-query/SKILL.md": SKILL.encode(),
        "agent/general.py": general_source(skills=("evidence-summary", "devdb-query")).encode(),
        "skills/inventory.md": b"# Skills\n\n- devdb-query\n",
    }
    assert current_main().main.id == forked.id
    (finding,) = findings
    assert finding.summary.startswith("skill devdb-query: merged")
    assert finding.evidence == RUNS
    assert finding.detail == {"kind": "skill", "name": "devdb-query", "owners": ["general"], "outcome": "merged",
                              "reason": "added the skill", "base": commit.id, "candidate": forked.id,
                              "paths": ["agent/general.py", "skills/devdb-query/SKILL.md", "skills/inventory.md"]}
    data = user_json(llm.requests[1])
    assert (data["kind"], data["name"], data["purpose"]) == ("skill", "devdb-query", "query the dev DB with pymysql")
    assert data["owner_modules"] == {"general": "agent/general.py"}
    assert [entry["run_id"] for entry in data["evidence"]] == list(RUNS)
    messages, callables = llm.requests[1]
    assert messages[0].role == "system"
    assert {item["name"] for item in callables} == TOOLS
    (read_result,) = tool_results(llm.requests[2], 1)
    assert read_result["ok"] is True and "class GeneralAgent" in read_result["output"]


async def test_ad10_a_new_tool_is_declared_by_its_owner(main):
    """AD10: 新しい Tool を general が import して宣言し、merge 後の main の general がその Tool を持つ。"""
    llm = ScriptedLLM(
        survey(candidate("slack_post", kind="tool")),
        calls(write("tools/slack_post.py", SLACK_POST), write("agent/general.py", GENERAL_WITH_POST),
              write("tools/inventory.md", "# Tools\n\n- slack_post\n")),
        final("changed", "posting repeats"),
    )

    findings, _ = await check_run(activity(*RUNS), llm)

    (finding,) = findings
    assert finding.detail["outcome"] == "merged"
    assert finding.detail["paths"] == ["agent/general.py", "tools/inventory.md", "tools/slack_post.py"]
    owner = current_main().main_version.graph.agents["general"]
    assert "slack_post" in [item.name for item in owner.list_tools()]


async def test_ad11_a_new_agent_is_composed_and_made_a_child(main):
    """AD11: 新しい Agent を AGENTS と general の子に加え、merge 後の main に現れる。"""
    llm = ScriptedLLM(
        survey(candidate("devdb", kind="agent")),
        calls(write("agent/devdb.py", DEVDB_AGENT),
              write("agent/compose.py", compose_source([*AGENTS, "agent.devdb.DevdbAgent"], ("dispatch",))),
              write("agent/general.py", general_source(agents=("review", "devdb"))),
              write("agent/inventory.md", "# Agents\n\n- devdb\n")),
        final("changed", "a whole task type"),
    )

    findings, _ = await check_run(activity(*RUNS), llm)

    (finding,) = findings
    assert finding.detail["outcome"] == "merged"
    assert finding.detail["paths"] == ["agent/compose.py", "agent/devdb.py", "agent/general.py", "agent/inventory.md"]
    graph = current_main().main_version.graph
    assert "devdb" in graph.agents and "devdb" in graph.agents["general"].list_agent_names()


# ---- 既存の部品の説明


async def test_ad12_existing_may_rewrite_descriptions_and_prompts(main):
    """AD12 (a): existing は対象の説明と owner の prompt を書き換えて merge する。会話の入力に target がある。"""
    described = SLACK.replace("Call the Slack Web API.", "Call the Slack Web API, including chat.postMessage.")
    llm = ScriptedLLM(
        survey(candidate("slack_api", kind="existing")),
        calls(write("tools/slack.py", described), write("agent/general.py", general_source(prompt="Use slack_api for Slack."))),
        final("changed", "slack_api was not chosen"),
    )

    findings, _ = await check_run(activity(*RUNS), llm)

    (finding,) = findings
    assert (finding.detail["kind"], finding.detail["outcome"]) == ("existing", "merged")
    assert finding.detail["paths"] == ["agent/general.py", "tools/slack.py"]
    assert user_json(llm.requests[1])["target"] == {"kind": "tool", "path": "tools/slack.py"}
    assert type(current_main().main_version.graph.agents["general"]).tools[0].description.endswith("chat.postMessage.")


async def test_ad12_existing_may_not_change_logic(main):
    """AD12 (b): 対象の run を変えれば失敗が LLM へ返り、2 回とも変えれば rejected で fork しない。"""
    _, store, commit, _ = main
    changed = SLACK.replace('output={"method": args.get("method")}', 'output={"method": args.get("method"), "posted": True}')
    llm = ScriptedLLM(
        survey(candidate("slack_api", kind="existing")),
        calls(write("tools/slack.py", changed)),
        final("changed"),
        calls(write("tools/slack.py", changed)),
        final("changed"),
    )

    findings, _ = await check_run(activity(*RUNS), llm)

    feedback = last_message(llm.requests[3])
    assert feedback.role == "user" and "tools/slack.py" in feedback.content
    (finding,) = findings
    assert finding.detail["outcome"] == "rejected" and "tools/slack.py" in finding.detail["reason"]
    assert len(llm.requests) == 5
    store.write.assert_not_awaited()
    assert current_main().main.id == commit.id


# ---- 検査


SCOPE_CASES = {
    "non-owner": (
        [*skill_writes(), write("agent/review.py", entry_source("review") + "\n# touched\n")],
        [write("agent/review.py", entry_source("review"))],
        "agent/review.py",
        entry_source("review").encode(),
    ),
    "outside-the-skill": (
        [*skill_writes(), write("tools/devdb.py", "X = 1\n")],
        [delete("tools/devdb.py")],
        "tools/devdb.py",
        None,
    ),
    "owner-logic": (
        skill_writes(general=general_source(skills=("evidence-summary", "devdb-query"), helper=True)),
        [write("agent/general.py", general_source(skills=("evidence-summary", "devdb-query")))],
        "agent/general.py",
        general_source(skills=("evidence-summary", "devdb-query")).encode(),
    ),
}


@pytest.mark.parametrize("case", list(SCOPE_CASES), ids=list(SCOPE_CASES))
async def test_ad13_a_scope_violation_goes_back_and_the_fix_is_merged(main, case):
    """AD13 (a)-(c): 範囲外の変更・skill の外の追加・owner の論理の変更は LLM へ返し、直した 2 回目を merge する。"""
    _, store, commit, _ = main
    first, fix, path, content = SCOPE_CASES[case]
    llm = ScriptedLLM(survey(candidate()), calls(*first), final("changed"), calls(*fix), final("changed", "fixed"))

    findings, _ = await check_run(activity(*RUNS), llm)

    feedback = last_message(llm.requests[3])
    assert feedback.role == "user" and path in feedback.content
    (finding,) = findings
    assert finding.detail["outcome"] == "merged"
    assert finding.detail["paths"] == ["agent/general.py", "skills/devdb-query/SKILL.md", "skills/inventory.md"]
    ((forked, tree),) = saved(store)
    assert forked.parent == commit.id
    assert tree.get(path) == content


async def test_ad13_two_scope_violations_are_rejected(main):
    """AD13: 2 回とも範囲外を変えれば rejected で、fork せず main は変わらない。"""
    _, store, commit, _ = main
    touched = [*skill_writes(), write("agent/review.py", entry_source("review") + "\n# touched\n")]
    llm = ScriptedLLM(survey(candidate()), calls(*touched), final("changed"), final("changed"))

    findings, _ = await check_run(activity(*RUNS), llm)

    (finding,) = findings
    assert finding.detail["outcome"] == "rejected" and "agent/review.py" in finding.detail["reason"]
    store.write.assert_not_awaited()
    assert current_main().main.id == commit.id


async def test_ad13_writes_and_deletes_outside_the_scope_fail(main):
    """AD13 (d): agent/・tools/・skills/ の外への書込みと fork の元にあるファイルの削除は失敗の結果で、tree を変えない。"""
    _, store, _, _ = main
    refused = [write("harnesses/x.py", "X = 1\n"), write("sources/x.py", "X = 1\n"), write("../x.py", "X = 1\n"),
               delete("agent/review.py")]
    llm = ScriptedLLM(survey(candidate()), calls(*refused), final("changed"))

    findings, _ = await check_run(activity(*RUNS), llm)

    results = tool_results(llm.requests[2], len(refused))
    assert [result["ok"] for result in results] == [False] * len(refused)
    assert all(result["error"] for result in results)
    (finding,) = findings
    assert finding.detail["outcome"] == "kept" and finding.detail["reason"].startswith("changed without changes")
    store.write.assert_not_awaited()


async def test_ad14_a_new_tool_must_be_wired_into_its_owner(main):
    """AD14: general が import しない新しい Tool は 2 回とも検査で落ち、rejected。"""
    _, store, _, _ = main
    llm = ScriptedLLM(survey(candidate("slack_post", kind="tool")), calls(write("tools/slack_post.py", SLACK_POST)),
                      final("changed"), final("changed"))

    findings, _ = await check_run(activity(*RUNS), llm)

    feedback = last_message(llm.requests[3])
    assert feedback.role == "user" and "general" in feedback.content
    (finding,) = findings
    assert finding.detail["outcome"] == "rejected" and "general" in finding.detail["reason"]
    store.write.assert_not_awaited()


async def test_ad15_secret_values_are_rejected_without_being_reported(main):
    """AD15 (a): 秘密らしい名前の環境変数の値を含む変更は rejected。失敗は変数名だけを示し、値を出さない。"""
    _, store, _, _ = main
    leaked = skill_writes(skill=SKILL + f"\ntoken: {SECRET}\n")
    llm = ScriptedLLM(survey(candidate()), calls(*leaked), final("changed"), final("changed"))

    findings, _ = await check_run(activity(*RUNS), llm, environ=ENVIRON)

    feedback = last_message(llm.requests[3])
    assert "SLACK_TOKEN" in feedback.content and SECRET not in feedback.content
    (finding,) = findings
    assert finding.detail["outcome"] == "rejected" and "SLACK_TOKEN" in finding.detail["reason"]
    assert SECRET not in json.dumps(finding.to_json(), ensure_ascii=False)
    store.write.assert_not_awaited()


async def test_ad15_values_of_other_variables_are_allowed(main):
    """AD15 (b): 秘密らしくない名前(HOME)の値は検査で落ちない。"""
    llm = ScriptedLLM(survey(candidate()), calls(*skill_writes(skill=SKILL + "\nrepos: /Users/someone/repos\n")), final("changed"))

    findings, _ = await check_run(activity(*RUNS), llm, environ=ENVIRON)

    assert findings[0].detail["outcome"] == "merged"


# ---- fork・merge


async def test_ad16_a_fork_failure_goes_back_and_the_fix_is_merged(main):
    """AD16 (a): 読み込めない Tool の fork の失敗は LLM へ返り、直した 2 回目を merge する。"""
    _, store, commit, _ = main
    llm = ScriptedLLM(
        survey(candidate("slack_post", kind="tool")),
        calls(write("tools/slack_post.py", "def broken(:\n"), write("agent/general.py", GENERAL_WITH_POST)),
        final("changed"),
        calls(write("tools/slack_post.py", SLACK_POST)),
        final("changed"),
    )

    findings, _ = await check_run(activity(*RUNS), llm)

    feedback = last_message(llm.requests[3])
    assert "SyntaxError" in feedback.content and "tools/slack_post.py" in feedback.content
    ((forked, tree),) = saved(store)
    assert forked.parent == commit.id and tree["tools/slack_post.py"] == SLACK_POST.encode()
    assert findings[0].detail["outcome"] == "merged"


async def test_ad16_two_fork_failures_leave_main_unchanged(main):
    """AD16 (b): fork が 2 回とも失敗すれば fork_failed で、push せず main は変わらない。"""
    _, store, commit, _ = main
    llm = ScriptedLLM(
        survey(candidate("slack_post", kind="tool")),
        calls(write("tools/slack_post.py", "def broken(:\n"), write("agent/general.py", GENERAL_WITH_POST)),
        final("changed"),
        calls(write("tools/slack_post.py", "def still_broken(:\n")),
        final("changed"),
    )

    findings, _ = await check_run(activity(*RUNS), llm)

    assert findings[0].detail["outcome"] == "fork_failed"
    assert len(llm.requests) == 5
    store.keep.assert_not_awaited()
    store.push.assert_not_awaited()
    assert current_main().main.id == commit.id


@pytest.mark.parametrize(
    ("script", "reason"),
    [
        ((final("keep", "not worth it"),), "not worth it"),
        ((final("changed", "nothing"),), "changed without changes: nothing"),
        ((calls(*skill_writes()), final("keep", "on second thought")), "on second thought"),
    ],
    ids=["keep", "changed-without-writes", "writes-then-keep"],
)
async def test_ad17_kept_candidates_are_not_forked(main, script, reason):
    """AD17: keep・書込みのない changed・書込みの後の keep は kept で fork しない。"""
    _, store, commit, _ = main
    llm = ScriptedLLM(survey(candidate()), *script)

    findings, _ = await check_run(activity(*RUNS), llm)

    assert outcomes(findings) == [("devdb-query", "kept", reason)]
    store.write.assert_not_awaited()
    assert current_main().main.id == commit.id


def _moves_github_after_the_fork(store: Any, other: Any) -> None:
    """共有の main は、fork が版を書いた直後に他の push で ``other`` へ動く。"""
    writing = store.write.side_effect

    async def write_then_move(*args: Any) -> Any:
        written = await writing(*args)
        store.fetch.return_value = other.id
        return written

    store.write.side_effect = write_then_move


def _person_pushed(store: Any, commit: Any, files: dict[str, bytes], extra: dict[str, bytes]) -> tuple[Any, dict[str, bytes]]:
    other_files = {**files, **extra}
    other = make_commit(other_files, parent=commit.id, message="person")
    seed(store, (other, other_files))
    _moves_github_after_the_fork(store, other)
    return other, other_files


async def test_ad18_a_merge_after_main_moved_is_rebased(main):
    """AD18 (a): fork の後に共有の main が動けば、その上へ載せ直して merged、元の候補を rebased_from に残す。"""
    _, store, commit, files = main
    other, other_files = _person_pushed(store, commit, files, {"tools/other.txt": b"other"})
    llm = ScriptedLLM(survey(candidate()), calls(*skill_writes()), final("changed"))
    store.merge_tree.return_value = {**other_files, "skills/devdb-query/SKILL.md": SKILL.encode(),
                                     "agent/general.py": general_source(skills=("evidence-summary", "devdb-query")).encode(),
                                     "skills/inventory.md": b"# Skills\n\n- devdb-query\n"}

    findings, _ = await check_run(activity(*RUNS), llm)

    (forked, _), (rebased, _) = saved(store)
    assert forked.parent == commit.id and rebased.parent == other.id
    detail = findings[0].detail
    assert (detail["outcome"], detail["candidate"], detail["rebased_from"]) == ("merged", rebased.id, forked.id)
    assert current_main().main.id == rebased.id


async def test_ad18_a_conflicting_merge_fails_and_the_next_candidate_runs(main):
    """AD18 (b): 載せ直しの衝突は failed(merge: MergeConflict)で、次の候補を処理する。"""
    _, store, commit, files = main
    _person_pushed(store, commit, files, {"agent/general.py": general_source(prompt="person").encode()})
    store.merge_tree.side_effect = api().repo.MergeConflict(["agent/general.py"])
    llm = ScriptedLLM(survey(candidate(), candidate("slack-steps")), calls(*skill_writes()), final("changed"), final("keep"))

    findings, _ = await check_run(activity(*RUNS), llm)

    first, second = findings
    assert first.detail["outcome"] == "failed" and first.detail["reason"].startswith("merge: MergeConflict")
    assert (second.detail["name"], second.detail["outcome"]) == ("slack-steps", "kept")
    store.push.assert_not_awaited()
    assert current_main().main.id == commit.id


async def test_ad18_the_next_candidate_forks_from_the_merged_main(main):
    """AD18 (c): 二つ目の候補は一つ目の merge で進んだ main から fork する。"""
    _, store, commit, _ = main
    both = general_source(skills=("evidence-summary", "devdb-query", "slack-steps"))
    llm = ScriptedLLM(
        survey(candidate(), candidate("slack-steps")),
        calls(*skill_writes()),
        final("changed"),
        calls(write("skills/slack-steps/SKILL.md", "---\nname: slack-steps\ndescription: Slack の手順。\n---\n"),
              write("agent/general.py", both)),
        final("changed"),
    )

    findings, _ = await check_run(activity(*RUNS), llm)

    assert [finding.detail["outcome"] for finding in findings] == ["merged", "merged"]
    (first, first_tree), (second, second_tree) = saved(store)
    assert first.parent == commit.id and second.parent == first.id
    assert second_tree["skills/devdb-query/SKILL.md"] == first_tree["skills/devdb-query/SKILL.md"]
    assert current_main().main.id == second.id


@pytest.mark.parametrize(
    ("first", "reason"),
    [
        ((RuntimeError("llm down"),), "RuntimeError: llm down"),
        (tuple(calls(("list_files", {"prefix": ""})) for _ in range(40)), "40"),
        ((FinalAnswer("done"),), "JSON"),
    ],
    ids=["llm-error", "turn-limit", "not-json"],
)
async def test_ad19_a_failed_conversation_does_not_stop_the_next_candidate(main, first, reason):
    """AD19 (a)-(c): 会話の LLM の例外・応答の上限・JSON でない最終回答は failed で、次の候補を処理する。"""
    _, store, _, _ = main
    assert additions().MAX_TURNS == 40
    llm = ScriptedLLM(survey(candidate(), candidate("slack-steps")), *first, final("keep"))

    findings, _ = await check_run(activity(*RUNS), llm)

    failed, kept = findings
    assert failed.detail["outcome"] == "failed" and reason in failed.detail["reason"]
    assert (kept.detail["name"], kept.detail["outcome"]) == ("slack-steps", "kept")
    store.write.assert_not_awaited()


async def test_ad19_cancellation_during_the_survey_propagates(main):
    """AD19 (d): survey の応答中の取消しは伝わる。"""
    started = asyncio.Event()

    class Blocking(LLMClient):
        async def respond(self, messages, callables):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(check_run(activity(*RUNS), Blocking()))
    await asyncio.wait_for(started.wait(), 1)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_ad20_additions_is_registered_after_errors():
    """AD20: additions は errors・consolidation の後(tuning・behavior の前)に登録した観点で、checks=["additions"] はそれだけを選ぶ。"""
    from trackeverything.optimizer import CHECKS, Optimizer

    assert [check.name for check in CHECKS] == ["errors", "consolidation", "additions", "tuning", "behavior"]
    assert isinstance(CHECKS[2], additions().AdditionsCheck)
    optimizer = Optimizer(llms={}, read_runs=AsyncMock(), checks=CHECKS)
    assert optimizer.select(["additions"]) == (CHECKS[2],)


async def test_ad21_without_merge_a_new_component_is_only_forked(main):
    """AD21: ``merge`` が偽なら新しい部品は fork で候補を残すだけで main は変わらない(outcome ``forked``)。"""
    _, store, commit, _ = main
    llm = ScriptedLLM(
        survey(candidate("devdb-query", purpose="query the dev DB with pymysql")),
        calls(("read_file", {"path": "agent/general.py"})),
        calls(*skill_writes()),
        final("changed", "added the skill"),
    )

    findings, _ = await check_run(activity(*RUNS), llm, merge=False)

    ((forked, _),) = saved(store)
    (finding,) = findings
    assert finding.detail["outcome"] == "forked" and finding.detail["candidate"] == forked.id
    assert current_main().main.id == commit.id
    store.push.assert_not_awaited()


async def test_ad22_an_addition_cannot_write_the_system_preamble(singleton):
    """AD22: agent/SYSTEM.md への write_file は失敗の結果になり、同じ会話の他の書込みだけで fork・merge する。main の SYSTEM.md は変わらない。"""
    files = {**base_tree(), "agent/SYSTEM.md": b"RULES\n"}
    _, store, commit = await singleton(files)
    writes = skill_writes()
    llm = ScriptedLLM(survey(candidate()), calls(write("agent/SYSTEM.md", "OTHER RULES\n"), *writes), final("changed", "added the skill"))

    findings, _ = await check_run(activity(*RUNS), llm)

    refused, *accepted = tool_results(llm.requests[2], 1 + len(writes))
    assert refused["ok"] is False and "agent/SYSTEM.md" in refused["error"]
    assert all(result["ok"] for result in accepted)
    ((forked, tree),) = saved(store)
    assert forked.parent == commit.id
    assert tree == {**files, **{args["path"]: args["content"].encode() for _, args in writes}}
    assert findings[0].detail["outcome"] == "merged"
    assert "agent/SYSTEM.md" not in findings[0].detail["paths"]
    assert current_main().main_version.tree["agent/SYSTEM.md"] == b"RULES\n"
