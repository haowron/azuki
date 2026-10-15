"""定時発火の定義の表を追加する

``schedule`` は queue プロセスが読む定時発火の定義(cron 式・時間帯・名前指定の実行の payload)。
表の追加だけなので、旧版の稼働中に適用できる。downgrade は表をデータごと削除する。

Revision ID: 5ebaea637844
Revises: eaa29408f5ac
Create Date: 2026-10-13 18:28:11.812764

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "5ebaea637844"
down_revision: str | Sequence[str] | None = "eaa29408f5ac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "schedule",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("cron", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), server_default=sa.text("'Asia/Tokyo'"), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("schedule")
