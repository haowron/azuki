"""観点 tuning が読む ``logs`` の行。

探査は軽い行(hash・長さ・usage・呼出しの名前と引数)だけを読み、重い本文は再生の標本と先読みの判定に要る分だけ id で読む。
SQL は PostgreSQL の jsonb を使う(``DATABASE_URL`` は PostgreSQL だけ)。jsonb として読めない値(NaN・\u0000 など)の行は
その値を null として扱い、一行のために照会全体を失敗させない。用例は読取り関数を代役にする。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

import trackeverything.config as config
from trackeverything.core.orm import sessions_for


@dataclass(frozen=True)
class AgentRow:
    """``logs`` の agent 行。

    ``request_hash`` は ``request`` の hash、``items`` は ``request`` が object のとき、その配列の値の要素ごとの hash。
    ``identity`` は ``data`` が Source の定義(``source`` と ``source_kwargs``、``webhook`` を除く)を持つときの hash。
    ``label`` は ``code_version`` の ``c.<…>``(版の外の行は None)。
    """

    execution_id: str
    run_id: str
    parent_execution_id: str | None
    component: str
    started_at: datetime
    duration_ms: int
    status: str
    label: str | None
    request_hash: str | None = None
    response_hash: str | None = None
    identity: str | None = None
    items: tuple[str, ...] = ()


@dataclass(frozen=True)
class StepRow:
    """``logs`` の llm 行一つ(一回の LLM 応答)。``agent`` と ``execution_id`` は親の agent 行。

    ``calls`` は応答が求めた呼出しの ``(name, args)``、``kind`` は ``final_answer``・``calls``(失敗した呼出しは空)。
    ``input_hash`` は request(messages と callables)全体の hash、``output_hash`` は回答の本文または呼出しの hash。
    """

    id: int
    run_id: str
    execution_id: str
    agent: str
    started_at: datetime
    duration_ms: int
    status: str
    model: str
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    kind: str = ""
    calls: tuple[tuple[str, Any], ...] = ()
    input_hash: str = ""
    output_hash: str = ""


@dataclass(frozen=True)
class Sample:
    """再生に使う llm 行の本文。``request`` は ``{"messages", "callables"}``、``result`` は記録した応答。"""

    id: int
    run_id: str
    request: Any
    result: Any


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _label(code_version: str | None) -> str | None:
    if not code_version or "+" not in code_version:
        return None
    label = code_version.rsplit("+", 1)[1]
    return label if label.startswith("c.") else None


_AGENT_SQL = text("""
SELECT l.execution_id, l.run_id, l.parent_execution_id, l.component, l.started_at, l.duration_ms, l.status, l.code_version,
       md5((j.v -> 'request')::text) AS request_hash,
       md5(coalesce(j.r -> 'response', 'null'::jsonb)::text) AS response_hash,
       CASE WHEN jsonb_typeof(j.v -> 'data') = 'object' AND (j.v -> 'data') ? 'source' THEN
            md5((j.v -> 'data' ->> 'source') || ':' ||
                CASE WHEN jsonb_typeof(j.v -> 'data' -> 'source_kwargs') = 'object'
                     THEN ((j.v -> 'data' -> 'source_kwargs') - 'webhook')::text ELSE '' END)
       END AS identity,
       ARRAY(SELECT md5(e::text)
             FROM jsonb_each(CASE WHEN jsonb_typeof(j.v -> 'request') = 'object' THEN j.v -> 'request' ELSE '{}'::jsonb END) kv,
                  jsonb_array_elements(CASE WHEN jsonb_typeof(kv.value) = 'array' THEN kv.value ELSE '[]'::jsonb END) e) AS items
FROM logs l, LATERAL (SELECT CASE WHEN pg_input_is_valid(l.request_json, 'jsonb') THEN l.request_json::jsonb ELSE 'null'::jsonb END AS v,
                             CASE WHEN pg_input_is_valid(coalesce(l.result_json, 'null'), 'jsonb')
                                  THEN coalesce(l.result_json, 'null')::jsonb ELSE 'null'::jsonb END AS r) j
WHERE l.event_type = 'agent' AND l.started_at >= :since
ORDER BY l.id
""")

_STEP_SQL = text("""
SELECT l.id, l.run_id, l.parent_execution_id, a.component, l.started_at, l.duration_ms, l.status, l.component,
       coalesce((x.u ->> 'input_tokens')::bigint, 0), coalesce((x.u ->> 'cached_input_tokens')::bigint, 0),
       coalesce((x.u ->> 'output_tokens')::bigint, 0), coalesce((x.u ->> 'reasoning_tokens')::bigint, 0),
       coalesce(x.r ->> 'type', ''),
       (SELECT coalesce(jsonb_agg(jsonb_build_array(c ->> 'name', c -> 'args')), '[]'::jsonb)
        FROM jsonb_array_elements(CASE WHEN jsonb_typeof(x.r -> 'calls') = 'array' THEN x.r -> 'calls' ELSE '[]'::jsonb END) c)::text,
       md5(l.request_json), md5(coalesce(x.r ->> 'text', (x.r -> 'calls')::text, ''))
FROM logs l
JOIN logs a ON a.execution_id = l.parent_execution_id AND a.event_type = 'agent',
LATERAL (SELECT CASE WHEN pg_input_is_valid(coalesce(l.result_json, 'null'), 'jsonb')
                      THEN coalesce(l.result_json, 'null')::jsonb ELSE 'null'::jsonb END AS r,
                 CASE WHEN pg_input_is_valid(coalesce(l.usage_json, 'null'), 'jsonb')
                      THEN coalesce(l.usage_json, 'null')::jsonb ELSE 'null'::jsonb END AS u) x
WHERE l.event_type = 'llm' AND l.started_at >= :since
ORDER BY l.id
""")


async def read_agent_rows(since: datetime) -> list[AgentRow]:
    """``since`` 以降に始まった agent 行(記録順)。"""
    async with sessions_for(config.DATABASE_URL)() as session:
        result = await session.execute(_AGENT_SQL, {"since": since.isoformat()})
        rows = result.all()
    return [
        AgentRow(execution_id=execution, run_id=run, parent_execution_id=parent, component=component, started_at=_time(started),
                 duration_ms=int(duration or 0), status=status, label=_label(code_version), request_hash=request_hash,
                 response_hash=response_hash, identity=identity, items=tuple(items or ()))
        for execution, run, parent, component, started, duration, status, code_version, request_hash, response_hash, identity, items
        in rows
    ]


async def read_step_rows(since: datetime) -> list[StepRow]:
    """``since`` 以降に始まった llm 行のうち、親の agent 行があるもの(記録順)。"""
    async with sessions_for(config.DATABASE_URL)() as session:
        result = await session.execute(_STEP_SQL, {"since": since.isoformat()})
        rows = result.all()
    return [
        StepRow(id=ident, run_id=run, execution_id=parent, agent=agent, started_at=_time(started), duration_ms=int(duration or 0),
                status=status, model=model, input_tokens=int(inputs), cached_tokens=int(cached), output_tokens=int(outputs),
                reasoning_tokens=int(reasoning), kind=kind, calls=tuple((str(name), args) for name, args in json.loads(calls)),
                input_hash=input_hash, output_hash=output_hash)
        for ident, run, parent, agent, started, duration, status, model, inputs, cached, outputs, reasoning, kind, calls, input_hash,
        output_hash in rows
    ]


async def read_samples(ids: Sequence[int]) -> list[Sample]:
    """llm 行の本文(id の順)。"""
    if not ids:
        return []
    async with sessions_for(config.DATABASE_URL)() as session:
        result = await session.execute(
            text("SELECT id, run_id, request_json, result_json FROM logs WHERE id = ANY(:ids)"), {"ids": list(ids)})
        found = {ident: Sample(id=ident, run_id=run, request=json.loads(request), result=json.loads(result_json or "null"))
                 for ident, run, request, result_json in result.all()}
    return [found[ident] for ident in ids if ident in found]


async def read_requests(execution_ids: Sequence[str]) -> dict[str, str]:
    """agent 行の ``request_json``(実行 id → 本文)。"""
    if not execution_ids:
        return {}
    async with sessions_for(config.DATABASE_URL)() as session:
        result = await session.execute(
            text("SELECT execution_id, request_json FROM logs WHERE event_type = 'agent' AND execution_id = ANY(:ids)"),
            {"ids": list(execution_ids)})
        return {execution: request for execution, request in result.all()}


@dataclass(frozen=True)
class Readers:
    """観点 tuning の読取り口。用例は代役を渡す。"""

    agents: Any = read_agent_rows
    steps: Any = read_step_rows
    samples: Any = read_samples
    requests: Any = read_requests
