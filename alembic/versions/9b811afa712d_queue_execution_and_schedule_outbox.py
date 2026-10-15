"""Queue の試行証跡と定時配送 outbox を追加する

Revision ID: 9b811afa712d
Revises: 0df1dafa45b0
Create Date: 2026-10-11 22:58:58.939830

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Alembic の revision 識別子。
revision: str = '9b811afa712d'
down_revision: str | Sequence[str] | None = '0df1dafa45b0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """旧版と共存できる表と索引を追加する。"""
    op.create_table('execution_attempt',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('namespace', sa.Text(), nullable=False),
    sa.Column('message_id', sa.Text(), nullable=False),
    sa.Column('token', sa.Text(), nullable=False),
    sa.Column('number', sa.Integer(), nullable=False),
    sa.Column('started_at', sa.Double(), nullable=False),
    sa.Column('deadline', sa.Double(), nullable=False),
    sa.Column('finished_at', sa.Double(), nullable=True),
    sa.Column('status', sa.Text(), server_default=sa.text("'running'"), nullable=False),
    sa.Column('run_id', sa.Text(), nullable=True),
    sa.Column('session_id', sa.Text(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('namespace', 'message_id', 'token', name='uq_execution_attempt_delivery')
    )
    op.create_index('idx_execution_attempt_message', 'execution_attempt', ['namespace', 'message_id', 'number'], unique=False)
    op.create_table('execution_receipt',
    sa.Column('namespace', sa.Text(), nullable=False),
    sa.Column('message_id', sa.Text(), nullable=False),
    sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('state', sa.Text(), server_default=sa.text("'pending'"), nullable=False),
    sa.Column('active_attempt_id', sa.Text(), nullable=True),
    sa.Column('session_id', sa.Text(), nullable=True),
    sa.Column('session_bound', sa.Boolean(), server_default=sa.text('FALSE'), nullable=False),
    sa.Column('run_id', sa.Text(), nullable=True),
    sa.Column('targets', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('deadline', sa.Double(), nullable=True),
    sa.Column('event_run_id', sa.Text(), nullable=True),
    sa.Column('forwarded', sa.Boolean(), server_default=sa.text('FALSE'), nullable=False),
    sa.CheckConstraint('attempts >= 0 AND attempts <= 3', name='ck_execution_receipt_attempts'),
    sa.PrimaryKeyConstraint('namespace', 'message_id')
    )
    op.create_table('schedule_outbox',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('subscription_id', sa.Text(), nullable=False),
    sa.Column('revision', sa.Text(), nullable=False),
    sa.Column('scheduled_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('spec', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('delivered', sa.Boolean(), server_default=sa.text('FALSE'), nullable=False),
    sa.Column('message_id', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_schedule_outbox_pending', 'schedule_outbox', ['delivered', 'scheduled_at'], unique=False)
    op.create_table('schedule_cursor',
    sa.Column('subscription_id', sa.Text(), nullable=False),
    sa.Column('revision', sa.Text(), nullable=False),
    sa.Column('fingerprint', sa.Text(), nullable=False),
    sa.Column('next_fire_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_occurrence', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_missed', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['subscription_id'], ['subscription.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('subscription_id')
    )


def downgrade() -> None:
    """この revision が追加した表と索引だけを戻す。"""
    op.drop_table('schedule_cursor')
    op.drop_index('idx_schedule_outbox_pending', table_name='schedule_outbox')
    op.drop_table('schedule_outbox')
    op.drop_table('execution_receipt')
    op.drop_index('idx_execution_attempt_message', table_name='execution_attempt')
    op.drop_table('execution_attempt')
