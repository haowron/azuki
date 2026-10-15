"""業務テストの保存先を隔離し、未 mock の外部接続を拒否する。"""

import asyncio
import platform
import socket
import struct
import subprocess

import psycopg
import pytest

import trackeverything.config as config


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    # platform 判定が file コマンドを起動しないよう、実 pointer 幅を渡す。
    monkeypatch.setattr(platform, "architecture", lambda *args, **kwargs: (f"{struct.calcsize('P') * 8}bit", ""))
    for name in ("PROJECTS_DIR", "REPOS_DIR", "HITL_DIR", "REPORTS_DIR"):
        directory = tmp_path / name.lower()
        monkeypatch.setattr(config, name, str(directory))
    # 作業資料 store も一時領域へ。
    monkeypatch.setattr(config, "DOCS_DIR", str(tmp_path / "docs_dir"), raising=False)
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "WEBHOOK_TOKEN", "")
    for name in (
        "DISCORD_BOT_TOKEN",
        "DISCORD_NOTIFY_CHANNEL",
        "SLACK_TOKEN",
        "SLACK_CHANNEL_ID",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "NOTION_TOKEN",
        "APIDOG_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("外部接続・CLI 起動は業務ケースの境界で mock してください")

    async def forbidden_async(*args, **kwargs):
        pytest.fail("外部接続・CLI 起動は業務ケースの境界で mock してください")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", forbidden_async)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_async)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden_async)
