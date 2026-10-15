"""部品の版を保存する表を追加する

版が所有する 5 フォルダの tree を版として保存する。``component_blob`` はファイルの内容(SHA-256 ごとに一行)、
``component_version`` は版、``component_tree`` は版のパス → 内容、``component_ref`` は ``main`` などの参照。
表の追加だけなので、旧版の稼働中に適用できる。downgrade は四つの表をデータごと削除する。

Revision ID: eaa29408f5ac
Revises: d490c91bce9c
Create Date: 2026-10-13 15:34:17.737494

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "eaa29408f5ac"
down_revision: str | Sequence[str] | None = "d490c91bce9c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
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


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("component_tree")
    op.drop_table("component_ref")
    op.drop_table("component_version")
    op.drop_table("component_blob")
