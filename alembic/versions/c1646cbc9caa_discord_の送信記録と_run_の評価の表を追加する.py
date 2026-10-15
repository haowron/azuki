"""Discord の送信記録と run の評価の表を追加する

``discord_message`` は bot が送った Discord メッセージ(分割の各 chunk)と、送った run・Agent 実行の記録。
``run_feedback`` は本人がそのメッセージに付けた 👍/👎 と、👎 の追問・原因。
表の追加だけなので、旧版の稼働中に適用できる。downgrade は二つの表をデータごと削除する。

Revision ID: c1646cbc9caa
Revises: a583fc7a918f
Create Date: 2026-10-15 09:47:56.508381

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c1646cbc9caa"
down_revision: str | Sequence[str] | None = "a583fc7a918f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "discord_message",
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("delivery_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=True),
        sa.Column("execution_id", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("message_id"),
    )
    op.create_index("idx_discord_message_run", "discord_message", ["run_id"], unique=False)
    op.create_table(
        "run_feedback",
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("execution_id", sa.Text(), nullable=True),
        sa.Column("rating", sa.Text(), nullable=False),
        sa.Column("rated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("question_message_id", sa.Text(), nullable=True),
        sa.Column("reasons", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.CheckConstraint("rating IN ('up', 'down')", name="run_feedback_rating_check"),
        sa.PrimaryKeyConstraint("message_id"),
        sa.UniqueConstraint("question_message_id"),
    )
    op.create_index("idx_run_feedback_run", "run_feedback", ["run_id"], unique=False)
    op.create_index("idx_run_feedback_session", "run_feedback", ["session_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("idx_run_feedback_session", table_name="run_feedback")
    op.drop_index("idx_run_feedback_run", table_name="run_feedback")
    op.drop_table("run_feedback")
    op.drop_index("idx_discord_message_run", table_name="discord_message")
    op.drop_table("discord_message")
