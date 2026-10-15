"""一回限りの予約の表を追加する

``reservation`` は queue プロセスが読む一回限りの予約(発火時刻・名前指定の実行の payload)。
表の追加だけなので、旧版の稼働中に適用できる。downgrade は表をデータごと削除する。

Revision ID: f9d64c8d566b
Revises: 933c688a5d18
Create Date: 2026-10-14 12:06:46.891885

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f9d64c8d566b"
down_revision: str | Sequence[str] | None = "933c688a5d18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "reservation",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("reservation")
