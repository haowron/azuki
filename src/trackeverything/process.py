"""queue・runtime プロセスのログ設定、シグナル処理、変更監視。アプリの部品は import しない。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import signal
from pathlib import Path
from typing import Protocol


class Application(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


def configure_logging() -> None:
    """queue・runtime プロセスの標準出力へ同じ書式で記録する。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


async def run_host(app: Application) -> None:
    loop, stopped = asyncio.get_running_loop(), asyncio.Event()
    signals = (signal.SIGTERM, signal.SIGINT)
    for signum in signals:
        loop.add_signal_handler(signum, stopped.set)
    try:
        try:
            await app.start()
            await stopped.wait()
        finally:
            await app.stop()
    finally:
        for signum in signals:
            loop.remove_signal_handler(signum)


def restart_roles(path: str) -> set[str]:
    if path.startswith("src/trackeverything/"):
        path = path.removeprefix("src/trackeverything/")
    if path == "queue/consumer.py" or path.startswith(("integration/dispatch/", "llm/", "core/versioning/", "optimizer/")):
        return {"runtime"}
    if path.startswith("core/abc/") and path not in {"core/abc/submission.py"}:
        return {"runtime"}
    if path.startswith("core/builtin/") and path not in {"core/builtin/database.py", "core/builtin/session_store.py", "core/builtin/channel_store.py"}:
        return {"runtime"}
    if path.startswith("api/") or path in {"queue/cli.py", "integration/webhook/__init__.py",
                                           "integration/discord.py", "integration/slack.py", "integration/schedule.py", "integration/webhook/github.py",
                                           "integration/connector.py"}:
        return {"queue"}
    return {"queue", "runtime"}


def fingerprint(root: Path, role: str) -> str:
    paths = [*root.glob("src/trackeverything/**/*.py"), root / "pyproject.toml", root / "uv.lock", root / "scripts/run_local.sh", root / "scripts/run_service.sh"]
    digest = hashlib.sha256()
    for path in sorted(paths):
        relative = path.relative_to(root).as_posix()
        if path.is_file() and role in restart_roles(relative):
            digest.update(f"{relative}:{path.stat().st_mtime_ns}".encode())
    return digest.hexdigest()


if __name__ == "__main__":
    import sys

    print(fingerprint(Path(sys.argv[2]), sys.argv[1]))
