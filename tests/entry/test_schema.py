"""M1〜M3: 起動時の schema 版の確認。DB の読取りだけを mock し、head は repository の Alembic から読む。"""

import pytest
from sqlalchemy.exc import OperationalError

from trackeverything.core.orm import connection as database

DSN = "postgresql://trackeverything@127.0.0.1:5432/trackeverything"


async def test_m1_database_at_head_lets_startup_continue(monkeypatch):
    head = database.schema_head()

    async def at_head(dsn):
        return head

    monkeypatch.setattr(database, "_current_revision", at_head)
    await database.require_schema_head(DSN)


async def test_m2_database_behind_head_refuses_startup(monkeypatch):
    async def behind(dsn):
        return "0123456789ab"

    monkeypatch.setattr(database, "_current_revision", behind)
    with pytest.raises(RuntimeError) as refused:
        await database.require_schema_head(DSN)
    message = str(refused.value)
    assert "0123456789ab" in message and database.schema_head() in message
    assert "alembic upgrade head" in message


async def test_m3_unreachable_database_refuses_startup(monkeypatch):
    async def unreachable(dsn):
        raise OperationalError("SELECT version_num FROM alembic_version", {}, ConnectionRefusedError("connection refused"))

    monkeypatch.setattr(database, "_current_revision", unreachable)
    with pytest.raises(RuntimeError) as refused:
        await database.require_schema_head(DSN)
    assert "unreachable" in str(refused.value) and "connection refused" in str(refused.value)
