"""PostgreSQL の session と実行記録。Runtime が run ごとに開始と終了を記録する。

行の読み書きは :mod:`trackeverything.core.orm` のモデルを通す。
公開する Session / SessionRun dataclass はそのまま API 形状として残し、
ORM 行との変換はこの module の内側に閉じる。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from trackeverything.core.abc.call import AgentResult
from trackeverything.core.builtin.code_version import current_code_version
from trackeverything.core.builtin.domain.execution_log import new_id, snapshot
from trackeverything.core.orm import Session as SessionRow
from trackeverything.core.orm import SessionRun as SessionRunRow
from trackeverything.core.orm import sessions_for


@dataclass(frozen=True, slots=True)
class Session:
    session_id: str
    created_at: datetime
    parent_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class SessionRun:
    run_id: str
    session_id: str
    agent_name: str
    data: dict[str, Any]
    request: Any
    status: str
    response: Any
    started_at: datetime
    finished_at: datetime | None


def _row_to_session(row: SessionRow) -> Session:
    return Session(
        session_id=row.session_id,
        created_at=row.created_at,
        parent_session_id=row.parent_session_id,
    )


def _row_to_run(row: SessionRunRow) -> SessionRun:
    return SessionRun(
        run_id=row.run_id,
        session_id=row.session_id,
        agent_name=row.agent_name,
        data=dict(row.data_json),
        request=row.request_json,
        status=row.status,
        response=row.response_json,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


class PostgresSessionStore:
    """ID は原形で保存し、session と新規 run の作成を同一 transaction で行う。"""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("session storage requires PostgreSQL DATABASE_URL")
        self._dsn = dsn
        self._sessions = sessions_for(dsn)


    async def begin_run(
        self, run_id: str, session_id: str, *, agent_name: str, data: dict[str, Any], request: Any = None, parent_session_id: str | None = None,
    ) -> None:
        """session を再利用・作成し、Agent 開始前の入力記録を確定する。

        parent は新規 session 作成時にのみ設定し、再利用では既存値を維持する。
        親のplaceholder 行は新規作成時のみ作る(再利用で渡された parent は無視して残さない)。
        """
        if parent_session_id == session_id:
            raise ValueError("parent_session_id must differ from session_id")
        async with self._sessions() as session:
            existing = (
                await session.scalars(select(SessionRow).where(SessionRow.session_id == session_id))
            ).first()
            if existing is None and parent_session_id is not None:
                await session.execute(
                    pg_insert(SessionRow)
                    .values(session_id=parent_session_id)
                    .on_conflict_do_nothing(index_elements=["session_id"])
                )
            await session.execute(
                pg_insert(SessionRow)
                .values(session_id=session_id, parent_session_id=parent_session_id)
                .on_conflict_do_nothing(index_elements=["session_id"])
            )
            await session.execute(
                pg_insert(SessionRunRow).values(
                    run_id=run_id,
                    session_id=session_id,
                    agent_name=agent_name,
                    data_json=snapshot(data),
                    request_json=snapshot(request),
                    code_version=current_code_version(),
                )
            )
            await session.commit()

    async def finish_run(self, run_id: str, result: AgentResult) -> None:
        """最終結果を記録する。失敗は呼出し元へ返し、実行を再試行しない。"""
        async with self._sessions() as session:
            saved_id = await session.scalar(
                update(SessionRunRow)
                .where(SessionRunRow.run_id == run_id)
                .values(
                    status=result.status,
                    response_json=snapshot(result.response),
                    finished_at=datetime.now(UTC),
                )
                .returning(SessionRunRow.run_id)
            )
            if saved_id is None:
                raise LookupError("session run not found")
            await session.commit()

    async def recover_interrupted_runs(self, *, before: datetime) -> int:
        """単一 Host の起動前に、前プロセスの未完了 run を interrupted で閉じる。"""
        async with self._sessions() as session:
            recovered = (
                await session.scalars(
                    select(SessionRunRow)
                    .where(SessionRunRow.status == "running", SessionRunRow.started_at < before)
                    .with_for_update()
                )
            ).all()
            finished_at = datetime.now(UTC)
            for row in recovered:
                row.status = "interrupted"
                row.finished_at = finished_at
            await session.commit()
        return len(recovered)

    async def record_event(
        self, agent_name: str, data: dict[str, Any], request: Any = None, *, session_id: str | None = None, response: Any = None,
    ) -> None:
        """Agent を起動せず、外部で受け取った入力を完了済みの run として追記する。

        ``response`` は外部で確定済みの応答(例: Issue 調査の配送済み報告)を同じ run に残す。
        既定の外部受信は応答 NULL のまま。
        """
        async with self._sessions() as session:
            await self.record_event_in(session, agent_name, data, request, session_id=session_id, response=response)
            await session.commit()

    async def record_event_in(
        self, session: AsyncSession, agent_name: str, data: dict[str, Any], request: Any = None, *, session_id: str | None = None, response: Any = None,
    ) -> str:
        """呼出元の transaction にイベントを追記する。"""
        session_id = new_id() if session_id is None else session_id
        run_id = new_id()
        await session.execute(pg_insert(SessionRow).values(session_id=session_id).on_conflict_do_nothing(index_elements=["session_id"]))
        await session.execute(pg_insert(SessionRunRow).values(
            run_id=run_id, session_id=session_id, agent_name=agent_name,
            data_json=snapshot(data), request_json=snapshot(request),
            response_json=None if response is None else snapshot(response), status="ok",
            finished_at=datetime.now(UTC), code_version=current_code_version(),
        ))
        return run_id

    async def get_session(self, session_id: str) -> Session | None:
        """保存済み session を取得する。"""
        async with self._sessions() as session:
            row = (await session.scalars(select(SessionRow).where(SessionRow.session_id == session_id))).first()
            return None if row is None else _row_to_session(row)

    async def get_run(self, run_id: str) -> SessionRun | None:
        """入力・応答・状態を含む1回の記録を取得する。"""
        async with self._sessions() as session:
            row = (await session.scalars(select(SessionRunRow).where(SessionRunRow.run_id == run_id))).first()
            return None if row is None else _row_to_run(row)

    async def list_runs(self, session_id: str) -> list[SessionRun]:
        """指定 session の記録を取得する。Agent の実行順序制御は行わない。"""
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(SessionRunRow)
                    .where(SessionRunRow.session_id == session_id)
                    .order_by(SessionRunRow.started_at, SessionRunRow.run_id)
                )
            ).all()
            return [_row_to_run(row) for row in rows]
