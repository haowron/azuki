"""A stand-in Host checkout for the component hooks (VG17, VG23, VG24).

``stand_in_host(root)`` copies the real ``scripts/component-hooks`` into ``root/scripts/component-hooks``
and writes ``root/scripts/run_local.sh``, a launcher that records its arguments in ``root/calls.log`` and
fails the check of a commit that holds ``tools/BROKEN`` with ``tools/BROKEN:1: ValueError: broken component``.
The hooks find their Host from their own location, so no ``TE_HOME`` is set.
"""

import shutil
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "component-hooks"

LAUNCHER = """#!/usr/bin/env bash
# python -m trackeverything.core.versioning check --repo <repository> <sha>
host="$(cd "$(dirname "$0")/.." && pwd)"
printf '%s\\n' "$*" >> "$host/calls.log"
repo="$6"
sha="$7"
if git -C "$repo" cat-file -e "$sha:tools/BROKEN" 2>/dev/null; then
  echo "tools/BROKEN:1: ValueError: broken component" >&2
  exit 1
fi
echo "ok $sha"
"""


def stand_in_host(root: Path) -> Path:
    """Create the stand-in Host at ``root``; return its hooks directory (the value for ``core.hooksPath``)."""
    hooks = root / "scripts" / "component-hooks"
    shutil.copytree(HOOKS, hooks)
    (root / "scripts" / "run_local.sh").write_text(LAUNCHER, encoding="utf-8")
    return hooks


def calls(root: Path) -> list[str]:
    """The checks the stand-in Host's launcher ran, oldest first."""
    log = root / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []
