"""project channel 対応表を追加する

Revision ID: 2f4a4cb34a22
Revises: bbb29e1c182d
Create Date: 2026-10-02 18:08:39.373819

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Alembic の revision 識別子。
revision: str = "2f4a4cb34a22"
down_revision: str | Sequence[str] | None = "bbb29e1c182d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """project ごとの播報先 channel 対応表を追加する。"""
    # Alembic autogenerate の生成差分。
    op.create_table(
        "project_channel",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("project"),
        sa.UniqueConstraint("channel_id"),
    )
    # 生成差分の終わり。


def downgrade() -> None:
    """project 対応表を削除する。"""
    # Alembic autogenerate の生成差分。
    op.drop_table("project_channel")
    # 生成差分の終わり。
