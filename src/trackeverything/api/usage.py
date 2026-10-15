"""Usage statistics API — per-agent / per-tool invocation counts.

``GET /api/usage`` は ``logs`` 表の ``agent`` / ``tool`` イベントを成分名ごとに
集計した呼び出し回数を返す。読み取りは
:func:`trackeverything.core.builtin.execution_log.read_usage`(ORM 集計)が担い、
この module は管理 API の契約(token 検証・応答形)だけを置く。
登録一覧との突き合わせ(未実行の Agent / Tool を 0 件として並べる)は画面側で
``GET /api/catalog`` と join する — 集計 API は DB の記録だけを正本にする。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

import trackeverything.config as config
from trackeverything.api.auth import require_token
from trackeverything.core.builtin.execution_log import read_usage


class ComponentUsageView(BaseModel):
    """One component (agent or tool name) with its invocation counts."""

    name: str
    total: int
    # status → count (agent: ok / error / max_steps / cancelled, tool: ok / error).
    by_status: dict[str, int]
    last_used_at: str | None


class UsageView(BaseModel):
    """Envelope for ``GET /api/usage``."""

    agents: list[ComponentUsageView]
    tools: list[ComponentUsageView]
    # Earliest recorded started_at per event type — the aggregation range.
    agents_since: str | None
    tools_since: str | None


def create_usage_router() -> APIRouter:
    """``GET /api/usage`` — Agent / Tool の呼び出し回数の集計。"""

    router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

    @router.get("/usage", response_model=UsageView)
    async def usage() -> UsageView:
        summary = await read_usage(config.DATABASE_URL)
        return UsageView(
            agents=[
                ComponentUsageView(
                    name=item.component,
                    total=item.total,
                    by_status=item.by_status,
                    last_used_at=item.last_used_at,
                )
                for item in summary.agents
            ],
            tools=[
                ComponentUsageView(
                    name=item.component,
                    total=item.total,
                    by_status=item.by_status,
                    last_used_at=item.last_used_at,
                )
                for item in summary.tools
            ],
            agents_since=summary.agents_since,
            tools_since=summary.tools_since,
        )

    return router
