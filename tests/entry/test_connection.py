"""DB01: ORM の接続の取得。DSN ごとに一つの async engine を共有し、取得は DB に接続しない。"""

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from trackeverything.core.orm import connection

URL_DSN = "postgresql://db01@127.0.0.1:5432/db01"
KEYWORD_DSN = "host=127.0.0.1 port=5432 dbname=db01 user=db01"


async def test_db01_one_async_engine_per_dsn_and_sessions_without_expiry():
    sessions = connection.sessions_for(URL_DSN)
    first, second = sessions(), sessions()
    try:
        engine = connection.engine_for(URL_DSN)
        assert isinstance(first, AsyncSession) and first.bind is engine and second.bind is engine
        assert first.sync_session.expire_on_commit is False
        assert isinstance(engine, AsyncEngine) and engine.dialect.driver == "psycopg" and engine.dialect.is_async
        # DB の再起動後に張り直し、長寿命の接続を防火壁の内側に収める pool の設定。
        assert engine.pool._pre_ping is True and engine.pool._recycle == 1800
        keyword = connection.engine_for(KEYWORD_DSN)
        assert keyword is not engine and keyword.dialect.is_async
        assert keyword.url.query["host"] == "127.0.0.1" and keyword.url.query["dbname"] == "db01"
    finally:
        await first.close()
        await second.close()
