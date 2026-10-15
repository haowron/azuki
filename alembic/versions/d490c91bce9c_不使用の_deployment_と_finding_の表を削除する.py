"""不使用の deployment と finding の表を削除する

起動記録(deployment)と失敗の集約(finding・finding_occurrence・finding_cursor)の表を
データごと削除する。置き換える版のコードもこれらの表を読み書きしないため、旧版の稼働中に適用できる。
``alembic/env.py`` の ``include_object`` はモデルにない表を比較から外すので、drop は手で書く。
downgrade は 378e169429bd と同じ構造の空の表を作り直し、データは戻さない。

Revision ID: d490c91bce9c
Revises: 2a0f0df2729d
Create Date: 2026-10-13 14:31:08.277754

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d490c91bce9c"
down_revision: str | Sequence[str] | None = "2a0f0df2729d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index("idx_finding_occurrence_finding", table_name="finding_occurrence")
    op.drop_table("finding_occurrence")
    op.drop_table("finding_cursor")
    op.drop_table("finding")
    op.drop_table("deployment")


def downgrade() -> None:
    """Downgrade schema."""
    op.create_table(
        "deployment",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("code_version", sa.Text(), nullable=False),
        sa.Column("git_sha", sa.Text(), nullable=True),
        sa.Column("dirty", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False),
        sa.Column("runtime", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "finding",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("component", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("normalized_cause", sa.Text(), nullable=False),
        sa.Column("sample_cause", sa.Text(), nullable=False),
        sa.Column("first_seen", sa.Text(), nullable=False),
        sa.Column("last_seen", sa.Text(), nullable=False),
        sa.Column("count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("first_code_version", sa.Text(), nullable=True),
        sa.Column("last_code_version", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'open'"), nullable=False),
        sa.Column("fixed_in", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("status IN ('open', 'fixed', 'regressed', 'wontfix')", name="finding_status_check"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fingerprint"),
    )
    op.create_table(
        "finding_cursor",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("last_log_id", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("id = 1", name="finding_cursor_single_row"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "finding_occurrence",
        sa.Column("log_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("finding_id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("code_version", sa.Text(), nullable=True),
        sa.Column("seen_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["finding_id"], ["finding.id"]),
        sa.PrimaryKeyConstraint("log_id"),
    )
    op.create_index("idx_finding_occurrence_finding", "finding_occurrence", ["finding_id"], unique=False)
