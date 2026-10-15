"""fence workspace acquisitions with leases

Revision ID: d873c2a94e10
Revises: 2f4a4cb34a22
Create Date: 2026-10-06 12:28:53.124356

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d873c2a94e10"
down_revision: str | Sequence[str] | None = "2f4a4cb34a22"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("workspace_slot", sa.Column("lease_id", sa.Text(), nullable=True))
    op.add_column("workspace_slot", sa.Column("owner_pid", sa.Integer(), nullable=True))
    op.add_column("workspace_slot", sa.Column("owner_process", sa.Text(), nullable=True))
    op.add_column("workspace_slot", sa.Column("unbind_requested", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("workspace_slot", "unbind_requested")
    op.drop_column("workspace_slot", "owner_process")
    op.drop_column("workspace_slot", "owner_pid")
    op.drop_column("workspace_slot", "lease_id")
