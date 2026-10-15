"""カタログの HTTP 入口。常駐 producer は consumer の snapshot だけを読む。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends

from trackeverything.api.auth import require_token


def create_snapshot_catalog_router(read: Callable[[], Awaitable[dict[str, Any]]]) -> APIRouter:
    """HTTP は実行グラフを構成せず、版・時刻・生存状態を持つ投影を返す。"""
    router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

    @router.get("/catalog")
    async def catalog() -> dict[str, Any]:
        return await read()

    return router
