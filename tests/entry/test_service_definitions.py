"""SA10: 常駐の queue・runtime の LaunchAgent 定義だけを生成し、Sentry の定時 job を作らない。

毎朝の Sentry 自動修正は ``schedule`` 表の行で起動する。生成だけを確かめ、登録(launchctl)は本人の操作で、
root の conftest が子プロセスを禁じている。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]


def _render_services() -> ModuleType:
    spec = importlib.util.spec_from_file_location("render_services", REPO / "scripts/render_services.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sa10_service_definitions_have_no_sentry_launchd_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "launchd"
    monkeypatch.setattr(sys, "argv", ["render_services.py", "--directory", str(tmp_path / "deploy"), "--output-dir", str(output)])

    _render_services().main()

    assert sorted(path.name for path in output.iterdir()) == ["com.trackeverything.queue.plist", "com.trackeverything.runtime.plist"]
    assert not (REPO / "scripts/publish_message.sh").exists()
    assert not (REPO / "config/sentry-autofix.json").exists()
