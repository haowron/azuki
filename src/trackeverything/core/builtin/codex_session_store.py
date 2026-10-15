"""app session と Codex thread の関連。保存済みの別 ID は上書きしない。"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert

from trackeverything.core.orm import CodexSession, sessions_for


class PostgresCodexSessionStore:
    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("Codex session storage requires PostgreSQL DATABASE_URL")
        self._dsn = dsn
        self._sessions = sessions_for(dsn)


    async def get(self, session_id: str, cwd: str, project: str, mode: str) -> str | None:
        async with self._sessions() as session:
            row = await session.get(CodexSession, (session_id, cwd, project, mode))
            return None if row is None else row.codex_session_id

    async def bind(self, session_id: str, cwd: str, project: str, mode: str, codex_session_id: str) -> None:
        """同 ID の再保存は冪等。並行した別 ID の保存は transaction を戻す。"""
        async with self._sessions() as session:
            await session.execute(
                pg_insert(CodexSession)
                .values(session_id=session_id, cwd=cwd, project=project, mode=mode, codex_session_id=codex_session_id)
                .on_conflict_do_nothing(index_elements=["session_id", "cwd", "project", "mode"])
            )
            row = await session.get(CodexSession, (session_id, cwd, project, mode))
            if row is None or row.codex_session_id != codex_session_id:
                raise ValueError("Codex session 関連が競合しました。保存済み ID は変更しません。")
            await session.commit()
