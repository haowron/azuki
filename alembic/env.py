"""Alembic 環境 — スキーマの正本は ``trackeverything.core.orm``。

接続先は ``DATABASE_URL``(PostgreSQL のみ)。alembic.ini の sqlalchemy.url は
使わず、常に稼働中アプリと同じ設定から解決する。未設定の場合は明確に失敗する。

- autogenerate: ``orm/models.py`` を編集して ``alembic revision --autogenerate``
- 適用: ``alembic upgrade head``(既存配備は初回だけ ``alembic stamp head``)
- 検証: ``alembic check`` で model と DB の差分がないことを確認する
"""

from __future__ import annotations

from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, pool

from trackeverything.core.orm import Base
from trackeverything.core.orm.connection import sqlalchemy_url
from trackeverything.queue import models as subscription_models  # noqa: F401 -- 不使用(データ保持)の購読表を同じ metadata へ登録する

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file with Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_object(
    object_: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    """ORM が所有する表だけを管理する。

    ``apscheduler_jobs`` は APScheduler の SQLAlchemyJobStore が所有する。
    DB にだけ存在する表(モデルから class を除いた表を含む)は対象外とし、
    autogenerate が drop しないようにする。表の削除は revision に手で書く。
    モデルにある表の列・索引・制約の差分は阻害しない。
    """
    return not (type_ == "table" and name != "alembic_version" and reflected and compare_to is None)


def _database_url() -> str:
    import trackeverything.config as app_config

    dsn = app_config.DATABASE_URL.strip()
    if not dsn:
        raise SystemExit("DATABASE_URL is required: PostgreSQL is the only persistence backend")
    return sqlalchemy_url(dsn)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (connect through the app's DSN)."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
