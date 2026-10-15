"""部品の版の旧い表を削除する

部品の版は git の commit になり(``core/versioning``)、PostgreSQL の版庫の四つの表
``component_ref``・``component_tree``・``component_version``・``component_blob`` はどのコードも読まない。
直前の版(3c9c43b 以降)も読まないので、旧版の稼働中に適用できる。
downgrade は四つの表を空で作り直す(削除したデータは戻らない)。

Revision ID: a583fc7a918f
Revises: f9d64c8d566b
Create Date: 2026-10-14 17:24:38.221045

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a583fc7a918f"
down_revision: str | Sequence[str] | None = "f9d64c8d566b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_table("component_tree")
    op.drop_table("component_ref")
    op.drop_table("component_version")
    op.drop_table("component_blob")


def downgrade() -> None:
    """Downgrade schema."""
    op.create_table(
        "component_blob",
        sa.Column("hash", sa.Text(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("hash"),
    )
    op.create_table(
        "component_version",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("parent_id", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["parent_id"], ["component_version.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "component_ref",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("version_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["version_id"], ["component_version.id"]),
        sa.PrimaryKeyConstraint("name"),
    )
    op.create_table(
        "component_tree",
        sa.Column("version_id", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("blob_hash", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["blob_hash"], ["component_blob.hash"]),
        sa.ForeignKeyConstraint(["version_id"], ["component_version.id"]),
        sa.PrimaryKeyConstraint("version_id", "path"),
    )
