"""AS18: 終了状態と session_run の状態 schema。"""

from pathlib import Path
from typing import get_args

from sqlalchemy import CheckConstraint

from trackeverything.core.abc.call import AgentStatus
from trackeverything.core.orm.models import SessionRun

ROOT = Path(__file__).resolve().parents[2]


def test_as18_status_and_schema():
    assert set(get_args(AgentStatus.__value__)) == {"ok", "max_steps", "error", "cancelled"}
    checks = [str(item.sqltext) for item in SessionRun.__table__.constraints if isinstance(item, CheckConstraint)]
    status = next(text for text in checks if "status IN" in text)
    for value in ("running", "ok", "error", "max_steps", "cancelled", "interrupted"):
        assert f"'{value}'" in status, value
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    (head,) = script.get_heads()
    revisions = {revision.revision: revision for revision in script.walk_revisions(base="base", head=head)}
    source = Path(revisions["0df1dafa45b0"].path).read_text(encoding="utf-8")
    assert "interrupted" in source and "skipped" in source, "the status migration remains in the applied history"
