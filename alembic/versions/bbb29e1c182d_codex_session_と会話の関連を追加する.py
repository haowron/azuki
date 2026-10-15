"""Codex session と会話の関連を追加する

Revision ID: bbb29e1c182d
Revises: caced5681a8e
Create Date: 2026-10-02 15:37:34.196181

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Alembic の revision 識別子。
revision: str = "bbb29e1c182d"
down_revision: str | Sequence[str] | None = "caced5681a8e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Codex 会話の関連表を追加する。"""
    # Alembic autogenerate の生成差分。
    op.create_table(
        "codex_session",
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("cwd", sa.Text(), nullable=False),
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("codex_session_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("mode IN ('implement', 'review')", name="codex_session_mode_check"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["session.session_id"],
        ),
        sa.PrimaryKeyConstraint("session_id", "cwd", "project", "mode"),
        sa.UniqueConstraint("codex_session_id"),
    )
    # 生成差分の終わり。


def downgrade() -> None:
    """Codex 会話の関連表を削除する。"""
    # Alembic autogenerate の生成差分。
    op.drop_table("codex_session")
    # 生成差分の終わり。
