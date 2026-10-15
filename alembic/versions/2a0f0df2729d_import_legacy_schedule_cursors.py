"""旧定時予定の次回時刻を一度だけ cursor へ取り込む。

Revision ID: 2a0f0df2729d
Revises: 7eace7c82735
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import insert

revision: str = "2a0f0df2729d"
down_revision: str | Sequence[str] | None = "7eace7c82735"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def upgrade() -> None:
    """旧 scheduler 停止後に適用する。定義・pickle・確定済み cursor は変更しない。"""
    connection = op.get_bind()
    metadata = sa.MetaData()
    subscriptions = sa.Table("subscription", metadata, autoload_with=connection)
    cursors = sa.Table("schedule_cursor", metadata, autoload_with=connection)
    next_times: dict[str, datetime] = {}
    if sa.inspect(connection).has_table("apscheduler_jobs"):
        jobs = sa.Table("apscheduler_jobs", metadata, autoload_with=connection)
        next_times = {row.id: datetime.fromtimestamp(row.next_run_time, UTC)
                      for row in connection.execute(sa.select(jobs.c.id, jobs.c.next_run_time)) if row.next_run_time is not None}
    for row in connection.execute(sa.select(subscriptions)).mappings():
        if not row["enabled"] or row["archived"] or row["trigger"] == "webhook":
            continue
        next_fire = next_times.get(row["id"])
        if next_fire is None and row["trigger"] == "date" and row["source"] == "scheduled_task" and row["created_at"]:
            with suppress(ValueError):
                booked = _utc(row["schedule"])
                if booked > _utc(row["created_at"]):
                    next_fire = booked
        if next_fire is None:
            continue
        # この revision 時点の fingerprint 契約を固定する。現在のアプリコードは import しない。
        definition = {key: row[key] for key in ("id", "trigger", "schedule", "source", "project", "entrance", "enabled")}
        definition["kwargs"] = json.loads(row["kwargs"])
        if row["trigger"] == "interval":
            definition["schedule"] = int(row["schedule"])
        fingerprint = hashlib.sha256(json.dumps(definition, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()[:12]
        connection.execute(insert(cursors).values(subscription_id=row["id"], revision=uuid4().hex, fingerprint=fingerprint,
                                                  next_fire_at=next_fire).on_conflict_do_nothing(index_elements=["subscription_id"]))


def downgrade() -> None:
    """取込み済み cursor は維持する。旧 schema へ戻す場合は作成 revision が表を削除する。"""
