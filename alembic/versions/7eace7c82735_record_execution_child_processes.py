"""実行中の子 process の起動 intent と同一性を記録する

Revision ID: 7eace7c82735
Revises: 9b811afa712d
Create Date: 2026-10-11 23:53:25.959601

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Alembic の revision 識別子。
revision: str = '7eace7c82735'
down_revision: str | Sequence[str] | None = '9b811afa712d'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """子 process の回復用の表と索引を追加する。"""
    op.create_table('execution_child',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('attempt_id', sa.Text(), nullable=False),
    sa.Column('namespace', sa.Text(), nullable=False),
    sa.Column('message_id', sa.Text(), nullable=False),
    sa.Column('targets', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('pid', sa.Integer(), nullable=True),
    sa.Column('birth_time', sa.Double(), nullable=True),
    sa.Column('state', sa.Text(), server_default=sa.text("'starting'"), nullable=False),
    sa.ForeignKeyConstraint(['attempt_id'], ['execution_attempt.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_execution_child_unfinished', 'execution_child', ['namespace', 'state'], unique=False)


def downgrade() -> None:
    """この revision の表と索引だけを戻す。"""
    op.drop_index('idx_execution_child_unfinished', table_name='execution_child')
    op.drop_table('execution_child')
