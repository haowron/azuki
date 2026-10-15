"""PostgreSQL の channel 対応表と session の source 関連。

``channel`` は外部サービスの会話室(Discord の channel / thread など)と
session ID の対応を保持する。session ID は文字列の慣例(``discord:<id>``)に
頼らずこの表から解決し、1 channel に 1 session を割り当てる。

``session_source`` は1つの session が参照する外部資料の URL(PR・Notion 等)
を保持する。URL の解釈(どの source か、どう操作するか)は読み出し側が担い、
表は URL 原文のみを保存する。

``project_channel`` は project ごとの播報先 channel 対応を保持する。
project ラベルは原文で、channel_id は一意(1 channel を 1 project の播報板
として管理する)。対応表の契約は docs/components/SESSION.md、播報の方針は部品 repository の docs/PROJECT_MEMORY.md が正本。

``channel`` と ``session_source`` は ``session`` 表への外部キーを持たない。
session 行は会話記録側の ``begin_run`` が作るため、channel 対応の作成時点
(最初の発言より前)にはまだ存在しないことがある。行の読み書きは
:mod:`trackeverything.core.orm` のモデルを通す。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from trackeverything.core.orm import Channel as ChannelRow
from trackeverything.core.orm import ProjectChannel as ProjectChannelRow
from trackeverything.core.orm import SessionSource as SessionSourceRow
from trackeverything.core.orm import sessions_for


@dataclass(frozen=True, slots=True)
class ChannelMapping:
    provider: str
    channel_id: str
    session_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectChannelMapping:
    project: str
    channel_id: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SessionSource:
    session_id: str
    source_url: str
    added_at: datetime


class PostgresChannelStore:
    """channel→session の解決と session source 関連・project 対応の書き換えを所有する。"""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("channel storage requires PostgreSQL DATABASE_URL")
        self._dsn = dsn
        self._sessions = sessions_for(dsn)


    async def resolve_session(self, provider: str, channel_id: str) -> str:
        """1 channel に 1 session を割り当てる。既存なら再利用、なければ新規 UUID。

        同時作成は PRIMARY KEY と ON CONFLICT で1行に収束する。
        """
        async with self._sessions() as session:
            session_id = await self._select_session(session, provider, channel_id)
            if session_id is None:
                await session.execute(
                    pg_insert(ChannelRow)
                    .values(provider=provider, channel_id=channel_id, session_id=str(uuid4()))
                    .on_conflict_do_nothing(index_elements=["provider", "channel_id"])
                )
                await session.commit()
                session_id = await self._select_session(session, provider, channel_id)
        if session_id is None:
            raise LookupError("channel mapping missing after insert")
        return session_id

    async def get_channel(self, provider: str, channel_id: str) -> ChannelMapping | None:
        """保存済みの対応を取得する。"""
        async with self._sessions() as session:
            row = (
                await session.scalars(
                    select(ChannelRow).where(ChannelRow.provider == provider, ChannelRow.channel_id == channel_id)
                )
            ).first()
            if row is None:
                return None
            return ChannelMapping(
                provider=row.provider,
                channel_id=row.channel_id,
                session_id=row.session_id,
                created_at=row.created_at,
            )

    async def channel_for_session(self, provider: str, session_id: str) -> str | None:
        """session に対応する channel ID を返す。なければ None(読み取りだけ)。"""
        async with self._sessions() as session:
            row = (
                await session.scalars(
                    select(ChannelRow.channel_id).where(ChannelRow.provider == provider, ChannelRow.session_id == session_id)
                )
            ).first()
        return str(row) if row is not None else None

    async def attach_source(self, session_id: str, source_url: str) -> None:
        """session に source URL を紐付ける。重複は冪等に無視する。"""
        async with self._sessions() as session:
            await session.execute(
                pg_insert(SessionSourceRow)
                .values(session_id=session_id, source_url=source_url)
                .on_conflict_do_nothing(index_elements=["session_id", "source_url"])
            )
            await session.commit()

    async def channels_for_source(self, provider: str, source_url: str) -> list[ChannelMapping]:
        """同じ source URL に関連付いた channel を取得する。"""
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(ChannelRow)
                    .join(SessionSourceRow, ChannelRow.session_id == SessionSourceRow.session_id)
                    .where(ChannelRow.provider == provider, SessionSourceRow.source_url == source_url)
                )
            ).all()
            return [ChannelMapping(provider=row.provider, channel_id=row.channel_id, session_id=row.session_id, created_at=row.created_at) for row in rows]

    async def clear_sources_for_channel(self, provider: str, channel_id: str) -> int:
        """channel に対応する session の source 関連を全て消す。

        channel は削除済みでも対応行は履歴として残すため、対応がなければ
        0 を返し失敗にしない。
        """
        async with self._sessions() as session:
            outcome = cast(
                "CursorResult[Any]",
                await session.execute(
                    delete(SessionSourceRow).where(
                        SessionSourceRow.session_id.in_(
                            select(ChannelRow.session_id).where(
                                ChannelRow.provider == provider, ChannelRow.channel_id == channel_id
                            )
                        )
                    )
                ),
            )
            await session.commit()
            return int(outcome.rowcount)

    async def list_sources(self, session_id: str) -> list[SessionSource]:
        """session の source 関連を追加順に返す。"""
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(SessionSourceRow)
                    .where(SessionSourceRow.session_id == session_id)
                    .order_by(SessionSourceRow.added_at, SessionSourceRow.source_url)
                )
            ).all()
            return [
                SessionSource(
                    session_id=row.session_id,
                    source_url=row.source_url,
                    added_at=row.added_at,
                )
                for row in rows
            ]

    async def resolve_project_channel(self, project: str) -> str | None:
        """保存済みの project→channel 対応を返す。なければ None(読み取りだけ)。"""
        async with self._sessions() as session:
            row = (
                await session.scalars(select(ProjectChannelRow.channel_id).where(ProjectChannelRow.project == project))
            ).first()
        return str(row) if row is not None else None

    async def project_for_channel(self, channel_id: str) -> str | None:
        """channel_id → project の逆対応を返す。なければ None(project channel 外)。"""
        async with self._sessions() as session:
            row = (
                await session.scalars(select(ProjectChannelRow.project).where(ProjectChannelRow.channel_id == channel_id))
            ).first()
        return str(row) if row is not None else None

    async def set_project_channel(self, project: str, channel_id: str) -> None:
        """project→channel 対応を upsert する。channel の再作成は同じ行を上書きする。

        channel_id は一意 — 別 project が保持する channel への対応は
        整合性違反として呼出側へ例外を返す。
        """
        now = datetime.now(UTC)
        statement = pg_insert(ProjectChannelRow).values(
            project=project, channel_id=channel_id, created_at=now, updated_at=now
        )
        async with self._sessions() as session:
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["project"],
                    set_={"channel_id": statement.excluded.channel_id, "updated_at": statement.excluded.updated_at},
                )
            )
            await session.commit()

    async def list_project_channels(self) -> list[ProjectChannelMapping]:
        """全 project→channel 対応を作成順に返す(管理・照会用)。"""
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(ProjectChannelRow).order_by(ProjectChannelRow.created_at, ProjectChannelRow.project)
                )
            ).all()
            return [
                ProjectChannelMapping(
                    project=row.project,
                    channel_id=row.channel_id,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
                for row in rows
            ]

    @staticmethod
    async def _select_session(session: AsyncSession, provider: str, channel_id: str) -> str | None:
        row = (
            await session.scalars(
                select(ChannelRow.session_id).where(ChannelRow.provider == provider, ChannelRow.channel_id == channel_id)
            )
        ).first()
        return str(row) if row is not None else None
