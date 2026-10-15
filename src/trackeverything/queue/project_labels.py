"""project ラベルの候補。不使用(データ保持)の ``subscription`` 表から ``project`` 列だけを読取り専用で使う。"""

from __future__ import annotations

from sqlalchemy import select

import trackeverything.config as config
from trackeverything.core.orm import sessions_for
from trackeverything.queue.models import Subscription


async def list_project_labels() -> list[str]:
    """``SELECT DISTINCT project`` の結果を名前順で返す。この表へは書き込まない。"""
    dsn = config.DATABASE_URL.strip()
    if not dsn:
        raise ValueError("DATABASE_URL is required: PostgreSQL is the only persistence backend")
    async with sessions_for(dsn)() as session:
        labels = await session.scalars(select(Subscription.project).where(Subscription.project.is_not(None)).distinct())
        return sorted(label for label in labels if label)
