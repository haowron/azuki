"""版の検証 CLI。``python -m trackeverything.core.versioning check [--repo <git の作業 tree>] [<rev>]``。

部品のリポジトリの commit の 5 フォルダを、runtime と同じ Host のサービスで読み込んで検証する。mirror・main は読み書きしない。
部品のリポジトリの clone の pre-push hook とローカル方式の mirror の pre-receive hook(``scripts/component-hooks``)が、
その hook を持つ Host の checkout の launcher(``scripts/run_local.sh``)から main への push ごとに実行する。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

import trackeverything.config as config
from trackeverything.core.builtin.app import open_host_services
from trackeverything.core.builtin.compose import prepare_components
from trackeverything.core.orm.connection import require_schema_head
from trackeverything.core.versioning.repo import Repo, describe_failure
from trackeverything.core.versioning.store import GitVersionStore


async def check_command(repository: Path, revision: str) -> int:
    """commit を検証する。成功は ``ok <SHA>``(0)、失敗は stderr にパスと行・例外(1)。"""
    repo: Repo | None = None
    try:
        await require_schema_head(config.DATABASE_URL)
        components = prepare_components(open_host_services())
        source = GitVersionStore(repository)
        version_id = await source.resolve(revision)
        if version_id is None:
            print(f"unknown revision: {revision}", file=sys.stderr)
            return 1
        tree = await source.read_tree(version_id)
        repo = Repo(source, components)
        await repo.validate(tree or {})
    except Exception as error:
        print(describe_failure(error), file=sys.stderr)
        return 1
    finally:
        if repo is not None:
            await repo.close()
    print(f"ok {version_id}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trackeverything.core.versioning")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="load a commit of the component repository with this Host's services")
    check.add_argument("--repo", type=Path, default=Path.cwd(), help="git working tree or repository (default: the current directory)")
    check.add_argument("revision", nargs="?", default="HEAD", help="commit to check (default: HEAD)")
    args = parser.parse_args(argv)
    return asyncio.run(check_command(args.repo, args.revision))


if __name__ == "__main__":
    raise SystemExit(main())
