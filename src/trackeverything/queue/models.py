"""queue プロセスが読む表。定時発火の定義 ``schedule``、一回限りの予約 ``reservation`` と、不使用(データ保持)の旧い購読・定時発火の表。

``schedule`` と ``reservation`` は ``integration/schedule.py`` が読むだけで書かない。``schedule`` の行は SQL で直接追加・変更・削除する。
``reservation`` の行は Agent ``reservation`` の Tool(``tools/reservation.py``)か SQL が作り、発火した ``reservation_fire`` と取消しが消す。
旧い表とデータは削除しない。``alembic check`` が変化なしを報告するよう ORM の定義だけを残す。
旧い表でコードが読むのは ``subscription.project`` だけ(project ラベルの候補、``queue/project_labels.py``)。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trackeverything.core.orm.models import Base


class Schedule(Base):
    """``schedule`` — 定時発火の定義。``cron`` の時刻に ``payload`` で名前指定の実行を投入する。"""

    __tablename__ = "schedule"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    #: 5 項目の cron 式。``timezone`` の時刻で解釈する。
    cron: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text, server_default=text("'Asia/Tokyo'"))
    #: ``internal`` の ``EventRequest`` の payload(名前指定の実行)。
    payload: Mapped[Any] = mapped_column(JSONB)


class Reservation(Base):
    """``reservation`` — 一回限りの予約。``at`` に ``payload`` で名前指定の実行を一度だけ投入する。"""

    __tablename__ = "reservation"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: ``internal`` の ``EventRequest`` の payload(名前指定の実行)。
    payload: Mapped[Any] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Subscription(Base):
    """``subscription`` — 不使用(データ保持)。``project`` 列だけを読取り専用で使う。"""

    __tablename__ = "subscription"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    # PostgreSQL の予約語。SQLAlchemy は参照時に自動的に "trigger" へ引用する。
    trigger: Mapped[str] = mapped_column("trigger", Text)
    schedule: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    kwargs: Mapped[str] = mapped_column(Text)
    project: Mapped[str | None] = mapped_column(Text)
    entrance: Mapped[str] = mapped_column(Text, server_default=text("'project_memory'"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("TRUE"))
    archived: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"))
    title: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (CheckConstraint("NOT archived OR NOT enabled"),)


class ScheduleCursor(Base):
    """``schedule_cursor`` — 不使用(データ保持)。"""

    __tablename__ = "schedule_cursor"
    subscription_id: Mapped[str] = mapped_column(Text, ForeignKey("subscription.id", ondelete="CASCADE"), primary_key=True)
    revision: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(Text)
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_occurrence: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_missed: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScheduleOutbox(Base):
    """``schedule_outbox`` — 不使用(データ保持)。"""

    __tablename__ = "schedule_outbox"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    subscription_id: Mapped[str] = mapped_column(Text)
    revision: Mapped[str] = mapped_column(Text)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB)
    delivered: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"))
    message_id: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (Index("idx_schedule_outbox_pending", "delivered", "scheduled_at"),)
