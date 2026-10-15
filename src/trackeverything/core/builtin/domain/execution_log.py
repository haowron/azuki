"""Execution logging contracts and run-local correlation state."""

from __future__ import annotations

import json
import sys
import time
from contextvars import ContextVar
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class LogEvent:
    """One completed source, agent, or LLM execution."""

    event_type: str
    run_id: str
    execution_id: str
    parent_execution_id: str | None
    component: str
    started_at: str
    duration_ms: int
    status: str
    request: Any
    result: Any = None
    error: Any = None
    usage: Any = None
    cost_usd: str | None = None


current_run_id: ContextVar[str | None] = ContextVar("current_run_id", default=None)
current_agent_execution_id: ContextVar[str | None] = ContextVar("current_agent_execution_id", default=None)
current_llm_execution_id: ContextVar[str | None] = ContextVar("current_llm_execution_id", default=None)
current_parent_agent_execution_id: ContextVar[str | None] = ContextVar("current_parent_agent_execution_id", default=None)
#: 実行中の Agent の部品の版(``c.<版 id の先頭 8 桁>``)。版の外で作られた Agent では None。
current_component_version: ContextVar[str | None] = ContextVar("current_component_version", default=None)


def new_id() -> str:
    """Return an opaque unique execution identifier."""
    return uuid4().hex


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return datetime.now(UTC).isoformat()


def elapsed_clock() -> float:
    """記録用の単調時計。macOS と Linux ではシステム休眠も経過時間に含む。"""
    clock_id = getattr(time, "CLOCK_MONOTONIC_RAW" if sys.platform == "darwin" else "CLOCK_BOOTTIME", None)
    return time.monotonic() if clock_id is None else time.clock_gettime(clock_id)


def elapsed_ms(started: float) -> int:
    """elapsed_clock() と同じ時計で開始からのミリ秒を求める。"""
    return round((elapsed_clock() - started) * 1000)


def snapshot(value: Any) -> Any:
    """Take an immediate JSON-compatible copy without changing execution."""
    try:
        return json.loads(json.dumps(value, default=_json_default, ensure_ascii=False))
    except Exception as exc:
        return {
            "snapshot_error": {"type": type(exc).__name__, "message": str(exc)},
            "repr": _safe_repr(value),
        }


def _json_default(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(cast(Any, value))
    return str(value)


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except Exception as exc:
        return f"<repr failed: {type(exc).__name__}: {exc}>"
