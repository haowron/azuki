"""session_run の状態の skipped を interrupted に置き換える

Agent の終了状態から ``skipped`` を除いた(pre-harness の拒否と作業場の使用中は
``error``)。既存の ``skipped`` の行は ``error`` へ書き換える。起動時の回収で閉じる
未完了の run は ``interrupted`` として記録できるようにする。制約名は基線と同じ
``session_run_status_check``。

配備は旧版の稼働中に upgrade するため、旧版が書く ``skipped`` は制約にまだ残す
(追加を先に行い、削除は次の release の revision で行う)。

downgrade は ``interrupted`` の行を ``error`` へ戻し(この revision 前の回収と同じ扱い)、
元の制約を作り直す。``skipped`` から書き換えた行は戻せない。

Revision ID: 0df1dafa45b0
Revises: a80261235d1a
Create Date: 2026-10-11 21:11:42.603920

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0df1dafa45b0"
down_revision: str | Sequence[str] | None = "a80261235d1a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "session_run"
_CONSTRAINT = "session_run_status_check"
# skipped は旧版の書込みとの互換のためだけに残す。
_WITH_INTERRUPTED = "status IN ('running', 'ok', 'error', 'max_steps', 'skipped', 'cancelled', 'interrupted')"
# この revision より前の状態。
_WITH_SKIPPED = "status IN ('running', 'ok', 'error', 'max_steps', 'skipped', 'cancelled')"


def upgrade() -> None:
    """skipped の行を error にし、interrupted を許可する制約へ作り直す。"""
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.execute("UPDATE session_run SET status = 'error' WHERE status = 'skipped'")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _WITH_INTERRUPTED)


def downgrade() -> None:
    """interrupted の行を error にし、元の制約へ戻す。"""
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.execute("UPDATE session_run SET status = 'error' WHERE status = 'interrupted'")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _WITH_SKIPPED)
