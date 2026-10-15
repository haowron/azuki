"""Node.js で画面の8の業務操作を実行する。"""

import shutil
import subprocess
from pathlib import Path

import pytest

_PROCESS = subprocess.Popen


def test_dashboard_business_scenarios(monkeypatch):
    node = shutil.which("node")
    if node is None:
        pytest.fail("画面操作の検証には Node.js 18 以上が必要です。")
    # 外部プロセス禁止の例外は、この固定 Node テスト実行だけ。
    monkeypatch.setattr(subprocess, "Popen", _PROCESS)
    result = subprocess.run(
        [node, "--test", "--test-reporter=tap", str(Path(__file__).with_name("dashboard_logic.test.cjs"))],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "# pass 8" in result.stdout
