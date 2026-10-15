"""報告、カタログ、統計と実行フローの参照。"""

from pathlib import Path
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import trackeverything.config as config
from tests.fakes import ScriptedLLM
from trackeverything.api import runs, usage
from trackeverything.api.catalog import create_snapshot_catalog_router
from trackeverything.api.reports import create_reports_router
from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.tool import Tool, ToolResult
from trackeverything.core.builtin.execution_log import (
    ComponentUsage,
    RunEvent,
    RunRecord,
    RunTrace,
    SessionTrace,
    UsageSummary,
)


def client_for(router):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_h10_published_report_and_assets_are_readable():
    root = Path(config.REPORTS_DIR)
    asset = root / "assets" / ("b" * 64)
    asset.mkdir(parents=True)
    (asset / "mermaid.min.js").write_text("window.mermaid = {};")
    html = "<!doctype html><html><body>レビュー本文</body></html>"
    (root / ("a" * 64 + ".html")).write_text(html)
    client = client_for(create_reports_router())
    report = client.get("/reports/" + "a" * 64 + ".html")
    assert report.status_code == 200 and report.text == html
    script = client.get("/reports/assets/" + "b" * 64 + "/mermaid.min.js")
    assert script.status_code == 200 and script.text == "window.mermaid = {};"


def test_h11_report_cannot_serve_file_outside_report_directory(tmp_path):
    root = Path(config.REPORTS_DIR)
    root.mkdir()
    secret = tmp_path / "private.html"
    secret.write_text("非公開資料")
    (root / ("a" * 64 + ".html")).symlink_to(secret)
    response = client_for(create_reports_router()).get("/reports/" + "a" * 64 + ".html")
    assert response.status_code == 404 and "非公開資料" not in response.text


class _Notify(Tool):
    """目録の Tool の利用者を確かめる固定の Tool。"""

    name = "notify"
    description = "notify fixture"
    arguments = {"type": "object", "properties": {}}

    async def run(self, args, *, ctx=None):
        return ToolResult(ok=True, output=None)


class _Investigator(BaseAgent):
    """二つの親から宣言される子の Agent。"""

    name = "investigator"
    description = "child fixture"


class _CatalogEntry(BaseAgent):
    """目録の入口・親子・Tool 関係を確かめる固定の Agent。"""

    name = "catalog_entry"
    agents = ("investigator",)
    tools = (_Notify,)


class _Orphan(BaseAgent):
    """部品グラフにあり、どの入口からも宣言されない Agent。"""

    name = "orphan"
    description = "declared only"
    agents = ("investigator",)


def test_vr47_catalog_exposes_graph_agents_tools_and_relationships():
    """目録は部品グラフの全 Agent と各類の agents の宣言から、入口・親子・Tool の利用者を示す。"""
    from trackeverything.core.builtin.catalog import build_catalog
    from trackeverything.core.builtin.compose import build_agents

    built = build_agents((_CatalogEntry, _Investigator, _Orphan), {"max": ScriptedLLM()})
    snapshot = build_catalog(built, {}, entrances=("catalog_entry",)).model_dump(mode="json")
    client = client_for(create_snapshot_catalog_router(AsyncMock(return_value=snapshot)))
    response = client.get("/api/catalog")
    assert response.status_code == 200
    agents = {item["name"]: item for item in response.json()["agents"]}
    tools = {item["name"]: item for item in response.json()["tools"]}
    assert agents["catalog_entry"]["entrances"] == ["catalog_entry"]
    assert "investigator" in agents["catalog_entry"]["children"] and "catalog_entry" in agents["investigator"]["parents"]
    assert agents["orphan"]["children"] == ["investigator"] and agents["orphan"]["entrances"] == []
    # 入口のない Agent の宣言も親子の辺になる(束縛した実例ではなく、部品グラフの全 Agent の宣言をたどる)。
    assert sorted(agents["investigator"]["parents"]) == ["catalog_entry", "orphan"]
    assert "catalog_entry" in tools["notify"]["used_by"]


def test_h13_usage_returns_counts_statuses_and_time_range(monkeypatch):
    summary = UsageSummary(
        agents=[ComponentUsage("review", 3, {"ok": 2, "error": 1}, "2026-10-07")],
        tools=[ComponentUsage("notify", 2, {"ok": 2}, "2026-10-06")],
        agents_since="2026-10-01",
        tools_since="2026-10-02",
    )
    read = AsyncMock(return_value=summary)
    monkeypatch.setattr(usage, "read_usage", read)
    response = client_for(usage.create_usage_router()).get("/api/usage")
    assert response.status_code == 200
    assert response.json() == {
        "agents": [{"name": "review", "total": 3, "by_status": {"ok": 2, "error": 1}, "last_used_at": "2026-10-07"}],
        "tools": [{"name": "notify", "total": 2, "by_status": {"ok": 2}, "last_used_at": "2026-10-06"}],
        "agents_since": "2026-10-01",
        "tools_since": "2026-10-02",
    }
    read.assert_awaited_once_with(config.DATABASE_URL)


def step(kind, name, execution_id, parent, at, duration_ms, status="ok", **extra):
    return RunEvent(
        event_type=kind,
        execution_id=execution_id,
        parent_execution_id=parent,
        component=name,
        started_at="2026-10-09T02:" + at + "+00:00",
        duration_ms=duration_ms,
        status=status,
        **extra,
    )


def record(run_id, status, started, finished, agent="ci_failure", session="sess-1"):
    return RunRecord(
        run_id=run_id,
        session_id=session,
        agent_name=agent,
        status=status,
        started_at="2026-10-09T02:" + started + "+00:00",
        finished_at=None if finished is None else "2026-10-09T02:" + finished + "+00:00",
    )


def test_h22_run_flow_returns_ordered_steps_with_timing_results_and_safe_previews(monkeypatch):
    usage_json = '{"input_tokens": 14288, "output_tokens": 703, "total_tokens": 14991}'
    events = [
        step("tool", "notify", "t3", "a1", "29:24.447300", 863),
        step("agent", "ci_failure", "a1", None, "08:25.940000", 1259380, result_head="R" * 500,
             request_head="SECRET-AGENT-REQUEST"),
        step("llm", "zai/glm-5.3", "l2", "a2", "08:43.000000", 9000, cost_usd="0.01", result_head="最終回答"),
        step("tool", "fetch_source", "t1", "a1", "08:25.941000", 1450, request_head='{"target": "pr"}'),
        step("tool", "run_command", "t2", "a2", "08:42.300000", 49, status="error",
             request_head='{"command": "' + "x" * 600, error_json='"exit code 1"'),
        step("agent", "investigator", "a2", "a1", "08:29.580000", 1169134, result_head="調査結果",
             error_json='[{"stage": "tool", "cause": "run_command failed"}]'),
        step("llm", "zai/glm-5.3", "l1", "a2", "08:29.581000", 12657, cost_usd="0.0230964", usage_json=usage_json,
             calls=("run_command", "notify"), request_head="SECRET-PROMPT"),
    ]
    trace = RunTrace(run_id="run-1", events=events, record=record("run-1", "ok", "08:25.900000", "29:25.400000"))
    read = AsyncMock(return_value=trace)
    monkeypatch.setattr(runs, "read_run_trace", read)
    response = client_for(runs.create_runs_router()).get("/api/runs/run-1")
    assert response.status_code == 200 and "SECRET" not in response.text
    body = response.json()
    assert body["truncated"] is False
    summary = body["run"]
    assert {key: summary[key] for key in ("run_id", "session_id", "agent", "status", "complete", "duration_ms")} == {
        "run_id": "run-1", "session_id": "sess-1", "agent": "ci_failure", "status": "ok", "complete": True,
        "duration_ms": 1259380,
    }
    assert summary["started_at"] == "2026-10-09T02:08:25.940000+00:00"
    assert summary["step_count"] == 7 and summary["error_count"] == 1
    assert abs(summary["cost_usd"] - 0.0330964) < 1e-9
    steps = {item["id"]: item for item in body["steps"]}
    assert [item["id"] for item in body["steps"]] == ["a1", "t1", "a2", "l1", "t2", "l2", "t3"]
    assert [steps[key]["parent"] for key in ("t1", "a2", "l1", "t3")] == ["a1", "a1", "a2", "a1"]
    assert steps["l1"]["kind"] == "llm" and steps["l1"]["duration_ms"] == 12657
    assert steps["l1"]["tokens"] == {"input": 14288, "output": 703, "total": 14991}
    assert steps["l1"]["cost_usd"] == 0.0230964 and steps["l1"]["preview"] == "呼び出し: run_command, notify"
    assert steps["l2"]["preview"] == "最終回答" and steps["l2"]["tokens"] is None
    assert steps["t1"]["preview"] == '{"target": "pr"}'
    assert steps["t2"]["status"] == "error" and steps["t2"]["errors"] == ["exit code 1"]
    assert len(steps["t2"]["preview"]) == 301 and steps["t2"]["preview"].endswith("…")
    assert steps["a1"]["preview"] == "R" * 300 + "…" and steps["a1"]["errors"] == []
    assert steps["a2"]["preview"] == "調査結果" and steps["a2"]["errors"] == ["tool: run_command failed"]
    read.assert_awaited_once_with(config.DATABASE_URL, "run-1", limit=401)


def test_h23_running_run_keeps_logged_steps_and_their_unlogged_parent(monkeypatch):
    events = [
        step("tool", "github_pr_context", "t1", "a-missing", "00:00.000000", 1000),
        step("llm", "zai/glm-5.3", "l1", "a-missing", "00:02.000000", 3000),
    ]
    trace = RunTrace(
        run_id="run-2",
        events=events,
        record=record("run-2", "running", "00:00.000000", None, agent="investigator"),
    )
    monkeypatch.setattr(runs, "read_run_trace", AsyncMock(return_value=trace))
    body = client_for(runs.create_runs_router()).get("/api/runs/run-2").json()
    assert body["run"]["status"] == "running" and body["run"]["complete"] is False
    assert body["run"]["agent"] == "investigator" and body["run"]["duration_ms"] == 5000
    assert [(item["id"], item["parent"]) for item in body["steps"]] == [("t1", "a-missing"), ("l1", "a-missing")]


def test_h24_unknown_id_is_not_found_as_run_or_session(monkeypatch):
    read_run = AsyncMock(return_value=None)
    read_session = AsyncMock(return_value=None)
    monkeypatch.setattr(runs, "read_run_trace", read_run)
    monkeypatch.setattr(runs, "read_session_trace", read_session)
    client = client_for(runs.create_runs_router())
    run = client.get("/api/runs/missing%2Frun:1")
    session = client.get("/api/sessions/missing%2Frun:1")
    assert run.status_code == 404 and "missing/run:1" in run.json()["detail"]
    assert session.status_code == 404 and "missing/run:1" in session.json()["detail"]
    read_run.assert_awaited_once_with(config.DATABASE_URL, "missing/run:1", limit=401)
    read_session.assert_awaited_once_with(config.DATABASE_URL, "missing/run:1", limit=50)


def test_h25_session_lists_its_runs_in_start_order_with_results(monkeypatch):
    first = RunTrace(
        run_id="r1",
        events=[
            step("agent", "discord", "a1", None, "00:00.000000", 1200),
            step("llm", "zai/glm-5.3", "l1", "a1", "00:00.100000", 900, cost_usd="0.01"),
        ],
        record=record("r1", "ok", "00:00.000000", "00:01.300000", agent="discord"),
    )
    second = RunTrace(
        run_id="r2",
        events=[
            step("agent", "discord", "a2", None, "05:00.000000", 800, status="error"),
            step("tool", "notify", "t2", "a2", "05:00.100000", 300, status="error"),
        ],
        record=record("r2", "error", "05:00.000000", "05:01.000000", agent="discord"),
    )
    third = RunTrace(run_id="r3", events=[], record=record("r3", "running", "09:00.000000", None, agent="discord"))
    trace = SessionTrace(
        session_id="sess-1",
        parent_session_id="sess-0",
        created_at="2026-10-09T01:59:00+00:00",
        total_runs=3,
        runs=[third, first, second],
    )
    read = AsyncMock(return_value=trace)
    monkeypatch.setattr(runs, "read_session_trace", read)
    response = client_for(runs.create_runs_router()).get("/api/sessions/sess-1")
    assert response.status_code == 200
    body = response.json()
    assert (body["session_id"], body["parent_session_id"], body["total_runs"]) == ("sess-1", "sess-0", 3)
    assert body["created_at"] == "2026-10-09T01:59:00+00:00"
    assert [
        (item["run_id"], item["agent"], item["status"], item["duration_ms"], item["step_count"], item["cost_usd"])
        for item in body["runs"]
    ] == [
        ("r1", "discord", "ok", 1200, 2, 0.01),
        ("r2", "discord", "error", 800, 2, None),
        ("r3", "discord", "running", None, 0, None),
    ]
    assert body["runs"][1]["error_count"] == 2 and body["runs"][2]["complete"] is False
    assert body["runs"][2]["started_at"] == "2026-10-09T02:09:00.000000+00:00"
    read.assert_awaited_once_with(config.DATABASE_URL, "sess-1", limit=50)


def test_h26_recent_runs_are_listed_newest_first(monkeypatch):
    newest = RunTrace(
        run_id="run-new",
        events=[step("agent", "pr_event", "a1", None, "30:00.000000", 4000)],
        record=None,
    )
    older = RunTrace(
        run_id="run-old",
        events=[step("agent", "discord", "a0", None, "10:00.000000", 2000, status="error")],
        record=record("run-old", "error", "10:00.000000", "10:02.000000", agent="discord", session="sess-9"),
    )
    read = AsyncMock(return_value=[newest, older])
    monkeypatch.setattr(runs, "read_recent_runs", read)
    response = client_for(runs.create_runs_router()).get("/api/runs?limit=2")
    assert response.status_code == 200
    assert [
        (item["run_id"], item["session_id"], item["agent"], item["status"], item["duration_ms"])
        for item in response.json()["items"]
    ] == [("run-new", None, "pr_event", "ok", 4000), ("run-old", "sess-9", "discord", "error", 2000)]
    read.assert_awaited_once_with(config.DATABASE_URL, limit=2)


def test_h27_run_flow_requires_configured_token(monkeypatch):
    read = AsyncMock()
    monkeypatch.setattr(runs, "read_run_trace", read)
    monkeypatch.setattr(config, "WEBHOOK_TOKEN", "fixture-token")
    assert client_for(runs.create_runs_router()).get("/api/runs/run-1").status_code == 401
    read.assert_not_called()
