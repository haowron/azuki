"""ORM パッケージの公開面。アプリが使う ORM class と共有 engine をここから出す。

不使用(データ保持)の表の class は ``models`` にだけ定義し、その import で同じ metadata へ登録する。
"""

from __future__ import annotations

from trackeverything.core.orm.connection import engine_for, sessions_for, sqlalchemy_url
from trackeverything.core.orm.models import (
    Base,
    Channel,
    CodexSession,
    DiscordMessage,
    ExecutionLog,
    Memory,
    ProjectChannel,
    RunFeedback,
    Session,
    SessionRun,
    SessionSource,
    WorkspaceSlot,
)

__all__ = [
    "Base",
    "Channel",
    "CodexSession",
    "DiscordMessage",
    "ExecutionLog",
    "Memory",
    "ProjectChannel",
    "RunFeedback",
    "Session",
    "SessionRun",
    "SessionSource",
    "WorkspaceSlot",
    "engine_for",
    "sessions_for",
    "sqlalchemy_url",
]
