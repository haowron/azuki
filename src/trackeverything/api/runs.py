"""Run-flow API — one run's step tree, a session's runs, and the latest runs.

``GET /api/runs`` / ``GET /api/runs/{run_id}`` / ``GET /api/sessions/{session_id}``
は実行フロー画面(``#/runs``)の供給源。読み取りは
:mod:`trackeverything.core.builtin.execution_log` の ``read_*`` 関数(ORM)が担い、
この module は管理 API の契約(token 検証・要約の導出・抜粋と原因の整形)を置く。
LLM と Agent の request は返さない。仕様は ``docs/components/DASHBOARD_SPEC.md``。
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import trackeverything.config as config
from trackeverything.api.auth import require_token
from trackeverything.core.builtin.execution_log import (
    PREVIEW_LIMIT,
    RunEvent,
    RunTrace,
    read_recent_runs,
    read_run_trace,
    read_session_trace,
)

# 1 run で描く step の上限。最大の実 run は 208 event。
MAX_STEPS = 400
# session 画面に並べる最新 run の件数。
SESSION_RUNS = 50


class TokensView(BaseModel):
    """LLM usage of one step."""

    input: int | None
    output: int | None
    total: int | None


class StepView(BaseModel):
    """One logged step (agent / llm / tool / harness / ...) of a run."""

    id: str
    parent: str | None
    kind: str
    name: str
    started_at: str
    duration_ms: int
    status: str
    cost_usd: float | None
    tokens: TokensView | None
    errors: list[str]
    preview: str | None


class RunSummaryView(BaseModel):
    """Who ran, how it ended, and how long / costly it was."""

    run_id: str
    session_id: str | None
    agent: str | None
    # 根 Agent の状態 → session_run の状態(running = 実行中)→ incomplete。
    status: str
    # 根 Agent の記録があるか。
    complete: bool
    started_at: str | None
    duration_ms: int | None
    step_count: int
    cost_usd: float | None
    error_count: int


class RunDetailView(BaseModel):
    """Envelope for ``GET /api/runs/{run_id}``."""

    run: RunSummaryView
    steps: list[StepView]
    truncated: bool


class RunListView(BaseModel):
    """Envelope for ``GET /api/runs``."""

    items: list[RunSummaryView]


class SessionView(BaseModel):
    """Envelope for ``GET /api/sessions/{session_id}``."""

    session_id: str
    parent_session_id: str | None
    created_at: str
    total_runs: int
    runs: list[RunSummaryView]


def _instant(value: str | None) -> float:
    """Epoch seconds of an ISO 8601 text; unknown values sort last."""
    if not value:
        return math.inf
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return math.inf


def _ordered(events: list[RunEvent]) -> list[RunEvent]:
    return sorted(events, key=lambda event: (_instant(event.started_at), event.log_id))


def _clip(text: str | None) -> str | None:
    if text is None or text == "":
        return None
    return text if len(text) <= PREVIEW_LIMIT else text[:PREVIEW_LIMIT] + "…"


def _cost(value: str | None) -> float | None:
    try:
        return None if value is None else float(value)
    except ValueError:
        return None


def _tokens(usage_json: str | None) -> TokensView | None:
    try:
        usage = json.loads(usage_json) if usage_json else None
    except ValueError:
        return None
    if not isinstance(usage, dict):
        return None

    def count(key: str) -> int | None:
        value = usage.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    view = TokensView(input=count("input_tokens"), output=count("output_tokens"), total=count("total_tokens"))
    return None if view.input is None and view.output is None and view.total is None else view


def _error_text(item: Any) -> str | None:
    if item is None:
        return None
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        if "stage" in item and "cause" in item:
            return f"{item['stage']}: {item['cause']}"
        if "message" in item:
            prefix = item.get("code") or item.get("type")
            return f"{prefix}: {item['message']}" if prefix else str(item["message"])
    return json.dumps(item, ensure_ascii=False, default=str)


def _errors(error_json: str | None) -> list[str]:
    """Agent ``[{stage, cause}]`` → ``stage: cause``; others ``code|type: message``."""
    if not error_json:
        return []
    try:
        value = json.loads(error_json)
    except ValueError:
        return [_clip(error_json) or ""]
    items = value if isinstance(value, list) else [value]
    return [clipped for item in items if (clipped := _clip(_error_text(item))) is not None]


def _preview(event: RunEvent) -> str | None:
    # request は Tool の引数だけを使う。LLM・Agent の request は読まれていても使わない。
    if event.event_type == "tool":
        return _clip(event.request_head)
    if event.event_type == "llm" and event.calls:
        return _clip("呼び出し: " + ", ".join(event.calls))
    if event.event_type in ("agent", "llm", "harness", "cli"):
        return _clip(event.result_head)
    return None


def _step(event: RunEvent) -> StepView:
    return StepView(
        id=event.execution_id,
        parent=event.parent_execution_id,
        kind=event.event_type,
        name=event.component,
        started_at=event.started_at,
        duration_ms=event.duration_ms,
        status=event.status,
        cost_usd=_cost(event.cost_usd),
        tokens=_tokens(event.usage_json),
        errors=_errors(event.error_json),
        preview=_preview(event),
    )


def _summary(trace: RunTrace, events: list[RunEvent]) -> RunSummaryView:
    root = next(
        (event for event in events if event.event_type == "agent" and event.parent_execution_id is None), None
    )
    record = trace.record
    started_at: str | None
    duration_ms: int | None
    if root is not None:
        started_at, duration_ms = root.started_at, root.duration_ms
    elif events:
        first = min(_instant(event.started_at) for event in events)
        last = max(_instant(event.started_at) + event.duration_ms / 1000 for event in events)
        started_at = events[0].started_at
        duration_ms = round((last - first) * 1000) if math.isfinite(last - first) else None
    elif record is not None:
        started_at = record.started_at
        span = _instant(record.finished_at) - _instant(record.started_at)
        duration_ms = round(span * 1000) if math.isfinite(span) else None
    else:
        started_at, duration_ms = None, None
    costs = [cost for event in events if (cost := _cost(event.cost_usd)) is not None]
    return RunSummaryView(
        run_id=trace.run_id,
        session_id=None if record is None else record.session_id,
        agent=root.component if root is not None else (None if record is None else record.agent_name),
        status=root.status if root is not None else ("incomplete" if record is None else record.status),
        complete=root is not None,
        started_at=started_at,
        duration_ms=duration_ms,
        step_count=len(events),
        cost_usd=sum(costs) if costs else None,
        error_count=sum(1 for event in events if event.status == "error"),
    )


def create_runs_router() -> APIRouter:
    """``GET /api/runs[/{run_id}]`` と ``GET /api/sessions/{session_id}`` — 実行フロー。"""

    router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

    @router.get("/runs", response_model=RunListView)
    async def recent(limit: int = Query(20, ge=1, le=100)) -> RunListView:
        traces = await read_recent_runs(config.DATABASE_URL, limit=limit)
        return RunListView(items=[_summary(trace, _ordered(trace.events)) for trace in traces])

    @router.get("/runs/{run_id:path}", response_model=RunDetailView)
    async def run(run_id: str) -> RunDetailView:
        trace = await read_run_trace(config.DATABASE_URL, run_id, limit=MAX_STEPS + 1)
        if trace is None:
            raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
        events = _ordered(trace.events)
        shown = events[:MAX_STEPS]
        return RunDetailView(run=_summary(trace, shown), steps=[_step(event) for event in shown], truncated=len(events) > MAX_STEPS)

    @router.get("/sessions/{session_id:path}", response_model=SessionView)
    async def session(session_id: str) -> SessionView:
        trace = await read_session_trace(config.DATABASE_URL, session_id, limit=SESSION_RUNS)
        if trace is None:
            raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
        summaries = [_summary(run, _ordered(run.events)) for run in trace.runs]
        summaries.sort(key=lambda item: _instant(item.started_at))
        return SessionView(
            session_id=trace.session_id,
            parent_session_id=trace.parent_session_id,
            created_at=trace.created_at,
            total_runs=trace.total_runs,
            runs=summaries,
        )

    return router
