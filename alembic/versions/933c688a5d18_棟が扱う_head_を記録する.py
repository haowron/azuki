"""棟が扱う head を記録する

``workspace_slot.head_sha`` は busy の棟が扱う PR の head。workspace pool は同じ PR・head の取得を排他する。
列の追加だけなので、旧版の稼働中に適用できる(旧版は列を読まず、NULL のまま取得する)。
downgrade は列をデータごと削除する。

Revision ID: 933c688a5d18
Revises: 5ebaea637844
Create Date: 2026-10-14 11:02:21.603972

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "933c688a5d18"
down_revision: str | Sequence[str] | None = "5ebaea637844"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("workspace_slot", sa.Column("head_sha", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("workspace_slot", "head_sha")
