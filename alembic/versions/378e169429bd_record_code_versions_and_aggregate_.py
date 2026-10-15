"""record code versions and aggregate error findings

logs・session_run に実行版の列を加え、起動記録(deployment)と失敗の集約
(finding・finding_occurrence・finding_cursor)の表を作る。新しいコードの Store が
``ensure_tables`` で先に表を作った DB でも失敗しないよう、作成は if_not_exists とする。

Revision ID: 378e169429bd
Revises: d873c2a94e10
Create Date: 2026-10-07 16:59:16.465202

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "378e169429bd"
down_revision: str | Sequence[str] | None = "d873c2a94e10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "deployment",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("code_version", sa.Text(), nullable=False),
        sa.Column("git_sha", sa.Text(), nullable=True),
        sa.Column("dirty", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False),
        sa.Column("runtime", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
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
        if_not_exists=True,
    )
    op.create_table(
        "finding_cursor",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("last_log_id", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("id = 1", name="finding_cursor_single_row"),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
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
        if_not_exists=True,
    )
    op.create_index("idx_finding_occurrence_finding", "finding_occurrence", ["finding_id"], unique=False, if_not_exists=True)
    op.add_column("logs", sa.Column("code_version", sa.Text(), nullable=True), if_not_exists=True)
    op.add_column("session_run", sa.Column("code_version", sa.Text(), nullable=True), if_not_exists=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("session_run", "code_version")
    op.drop_column("logs", "code_version")
    op.drop_index("idx_finding_occurrence_finding", table_name="finding_occurrence")
    op.drop_table("finding_occurrence")
    op.drop_table("finding_cursor")
    op.drop_table("finding")
    op.drop_table("deployment")
