"""組み込みの Store とログ記録器の生成口。接続と schema の確認は core.orm が所有する。

``config.DATABASE_URL`` は必須である。未設定の環境は静かな fallback ではなく
明確な失敗にする(旧 SQLite fallback は削除済み)。全 Store は
:mod:`trackeverything.core.orm` が URL ごとに持つ共有 engine を使う。

Callers use the ``open_*`` factories here and operate the returned object
through its backend-agnostic interface
(e.g. :class:`~trackeverything.core.abc.execution_log.LogRecorder`). Table shapes
live in :mod:`trackeverything.core.orm`; engine ownership lives in
``core.orm.connection`` (stores import that module directly — importing
this one from a store module would be circular).
"""

from __future__ import annotations

import trackeverything.config as config
from trackeverything.core.abc.execution_log import LogRecorder
from trackeverything.core.builtin.channel_store import PostgresChannelStore
from trackeverything.core.builtin.codex_session_store import PostgresCodexSessionStore
from trackeverything.core.builtin.execution_log import PostgresExecutionLogger
from trackeverything.core.builtin.session_store import PostgresSessionStore

__all__ = [
    "open_channel_store",
    "open_codex_session_store",
    "open_execution_logger",
    "open_session_store",
]


def _require_dsn() -> str:
    dsn = config.DATABASE_URL.strip()
    if not dsn:
        raise ValueError(
            "DATABASE_URL is required: PostgreSQL is the only persistence backend"
        )
    return dsn


def open_execution_logger() -> LogRecorder:
    """Open the execution-log sink on the configured backend."""
    return PostgresExecutionLogger(_require_dsn())


def open_session_store() -> PostgresSessionStore:
    """session 基盤。PostgreSQL のみ。"""
    return PostgresSessionStore(_require_dsn())


def open_channel_store() -> PostgresChannelStore:
    """channel 対応表。PostgreSQL のみ。"""
    return PostgresChannelStore(_require_dsn())


def open_codex_session_store() -> PostgresCodexSessionStore:
    """app session と Codex CLI 会話の関連。PostgreSQL のみ。"""
    return PostgresCodexSessionStore(_require_dsn())
