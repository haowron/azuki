"""session_run の状態に cancelled を加える

run の期限(``RUN_TIMEOUT``)で取り消した実行を ``cancelled`` として記録できるよう、
``session_run`` の状態の CHECK 制約を作り直す。``max_steps`` は過去の行を読めるよう
引き続き許可する。制約名は PostgreSQL が無名の列制約に付ける既定名
``session_run_status_check``(基線の表定義と同じ)。

downgrade は ``cancelled`` を除いた元の制約へ戻す。``cancelled`` の行が残っていると
制約の作成が失敗するため、その場合は先に行を処理してから戻す。

Revision ID: a80261235d1a
Revises: 378e169429bd
Create Date: 2026-10-09 22:45:17.000000

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a80261235d1a"
down_revision: str | Sequence[str] | None = "378e169429bd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "session_run"
_CONSTRAINT = "session_run_status_check"
# 取消し(cancelled)を加えた状態。max_steps は履歴の行のために残す。
_WITH_CANCELLED = "status IN ('running', 'ok', 'error', 'max_steps', 'skipped', 'cancelled')"
# この revision より前の状態。
_WITHOUT_CANCELLED = "status IN ('running', 'ok', 'error', 'max_steps', 'skipped')"


def upgrade() -> None:
    """状態の CHECK 制約を作り直し、cancelled を許可する。"""
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _WITH_CANCELLED)


def downgrade() -> None:
    """cancelled を除いた元の CHECK 制約へ戻す。"""
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _WITHOUT_CANCELLED)
