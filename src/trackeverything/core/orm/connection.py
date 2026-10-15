"""共有 SQLAlchemy async engine と ``AsyncSession`` の取得。

engine の所有は ORM パッケージの責務である(Store はここから受け取り、
``core.builtin.database`` は設定を読んでここへ渡す。逆方向の import は
循環するため禁止)。``pool_pre_ping`` で DB 再起動後に接続を張り直し、
``pool_recycle`` で長寿命接続を防火壁の内側に収める。

engine と ORM は ``sqlalchemy.ext.asyncio`` の async engine と ``AsyncSession``(psycopg の
非同期接続)で使う。queue と runtime はそれぞれ一つの event loop で engine を使う。
暗黙の遅延読込みは行わない(``expire_on_commit=False``、関係は明示の query で読む)。

Store の構築は I/O を行わない(engine は最初の操作で接続する)。
表の作成・変更は Alembic(``alembic upgrade head``)だけが行い、
Host は起動時に schema が head であることを確認する(``core.orm.connection.require_schema_head``)。

DSN は URL 形式(``postgresql://…``) と libpq keyword 形式の両方を受け付ける。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Lock

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy import Connection
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

_PG_SCHEMES = ("postgresql://", "postgres://")

_ENGINES: dict[str, AsyncEngine] = {}
_ENGINES_LOCK = Lock()


def sqlalchemy_url(dsn: str) -> str:
    """The DSN with the ``+psycopg`` driver qualifier SQLAlchemy needs.

    A plain ``postgresql://`` URL would make SQLAlchemy pick its default
    psycopg2 dialect, which is not installed. Already-qualified URLs pass
    through untouched; libpq keyword-form DSNs are routed through
    ``URL.create``'s query part, which the psycopg dialect reads as
    connection keywords. The URL serves the shared SQLAlchemy engine.
    """
    if dsn.startswith("postgresql+psycopg://"):
        return dsn
    for scheme in _PG_SCHEMES:
        if dsn.startswith(scheme):
            return f"postgresql+psycopg://{dsn[len(scheme):]}"
    params = {key: str(value) for key, value in conninfo_to_dict(dsn).items() if value is not None}
    return URL.create("postgresql+psycopg", query=params).render_as_string(hide_password=False)


def engine_for(dsn: str) -> AsyncEngine:
    """One shared engine per URL — repeated callers reuse the same pool."""
    url = sqlalchemy_url(dsn.strip())
    with _ENGINES_LOCK:
        engine = _ENGINES.get(url)
        if engine is None:
            engine = create_async_engine(url, pool_pre_ping=True, pool_recycle=1800)
            _ENGINES[url] = engine
        return engine


def sessions_for(dsn: str) -> async_sessionmaker[AsyncSession]:
    """A session factory bound to the shared engine of ``dsn``."""
    return async_sessionmaker(engine_for(dsn), expire_on_commit=False)


#: checkout に同梱した Alembic revision を schema の正本として使う。
_ALEMBIC_INI = Path(__file__).resolve().parents[4] / "alembic.ini"


def schema_head() -> str:
    """この checkout の Alembic revision の head。"""
    head = ScriptDirectory.from_config(Config(str(_ALEMBIC_INI))).get_current_head()
    if head is None:
        raise RuntimeError("alembic has no revision")
    return head


def _revision(connection: Connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


async def _current_revision(dsn: str) -> str | None:
    """DB に記録された schema の revision。未適用の DB は None。"""
    async with engine_for(dsn).connect() as connection:
        return await connection.run_sync(_revision)


async def require_schema_head(dsn: str) -> None:
    """DB の schema が head でなければ起動を拒否する。表の作成・変更は Alembic だけが行う。"""
    head = await asyncio.to_thread(schema_head)
    try:
        current = await _current_revision(dsn)
    except Exception as exc:
        raise RuntimeError(f"database is unreachable for the schema check: {type(exc).__name__}: {exc}") from exc
    if current != head:
        raise RuntimeError(
            f"database schema is at {current or 'no revision'}, expected {head}: "
            "run `bash scripts/run_local.sh alembic upgrade head` before starting"
        )
