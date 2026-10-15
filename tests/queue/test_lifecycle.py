"""Q44〜Q58: queue と runtime を別プロセスとして構成し、投入の CLI を常駐サービスなしで使う。"""

import ast
import io
import json
import logging
import runpy
import shlex
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.exceptions import ResponseError

from tests.queue.helpers import QUEUE, module, redis, running
from trackeverything.core.abc.submission import EventRequest

ROOT = Path(__file__).resolve().parents[2]
# 根の conftest は subprocess を禁止する。起動スクリプトの構文検査だけで使う。
_REAL_POPEN = subprocess.Popen


def imports(path):
    names = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names += [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
    return names


@pytest.fixture
def isolated_logging(monkeypatch):
    """入口のログ設定を root に残さず、uvicorn.access の変更を各ケースの後で戻す。"""
    monkeypatch.setattr(logging, "basicConfig", Mock())
    access = logging.getLogger("uvicorn.access")
    saved = access.handlers[:], access.filters[:], access.level, access.propagate
    yield
    access.handlers[:], access.filters[:], access.level, access.propagate = saved


def test_q44_queue_composition_does_not_import_execution_graph():
    app = ROOT / "src/trackeverything/api/app.py"
    assert app.exists(), "queue process must have its own composition root"
    forbidden = ("trackeverything.core.builtin.app", "trackeverything.agent", "trackeverything.llm", "trackeverything.core.versioning")
    assert not any(item.startswith(forbidden) for item in imports(app))


async def test_q45_consumer_creates_group_without_reset_and_closes_only_own_resources():
    client = redis()
    client.xgroup_create.side_effect = ResponseError("BUSYGROUP Consumer Group name already exists")
    runtime = await running(client)
    await runtime.stop()
    client.xgroup_create.assert_awaited_once_with(QUEUE, "runtime", id="0", mkstream=True)
    client.xgroup_destroy.assert_not_awaited()
    client.aclose.assert_not_awaited()


def test_q48_catalog_snapshot_remains_readable_when_worker_is_offline():
    read = AsyncMock(return_value={"agents": [{"name": "general"}], "sources": [], "tools": [], "harnesses": [], "code_version": "version-1", "as_of": "2026-10-11T00:00:00Z", "online": False})
    app = FastAPI()
    app.include_router(module("api.catalog").create_snapshot_catalog_router(read))
    response = TestClient(app).get("/api/catalog")
    assert response.status_code == 200
    assert response.json()["code_version"] == "version-1" and response.json()["online"] is False
    assert response.json()["agents"][0]["name"] == "general"


def test_q51_supervisor_watch_sets_are_process_specific():
    policy = module("process")
    assert policy.restart_roles("core/builtin/run_service.py") == {"runtime"}
    assert policy.restart_roles("api/queue.py") == {"queue"}
    assert policy.restart_roles("api/__main__.py") == {"queue"}
    assert policy.restart_roles("api/app.py") == {"queue"}
    assert policy.restart_roles("api/http.py") == {"queue"}
    assert policy.restart_roles("queue/contracts.py") == {"queue", "runtime"}
    # 部品は checkout にない。5 フォルダの例外はなく、他の経路と同じく両方を再起動する。
    assert policy.restart_roles("agent/general.py") == {"queue", "runtime"}
    assert policy.restart_roles("queue/consumer.py") == {"runtime"}
    assert policy.restart_roles("integration/schedule.py") == {"queue"}
    assert policy.restart_roles("optimizer/service.py") == {"runtime"}


def test_q53_queue_entry_starts_only_queue_process(monkeypatch, isolated_logging):
    entry = ROOT / "src/trackeverything/api/__main__.py"
    assert entry.exists(), "python -m trackeverything.api must be the queue process entry"
    forbidden = ("trackeverything.core.builtin.app", "trackeverything.agent", "trackeverything.llm", "trackeverything.core.versioning")
    assert not [name for name in imports(entry) if name.startswith(forbidden)]
    serve = AsyncMock()
    monkeypatch.setattr("trackeverything.api.app.serve", serve)
    runpy.run_module("trackeverything.api", run_name="__main__")
    serve.assert_awaited_once_with()


def test_q54_runtime_entry_starts_only_runtime_process(monkeypatch, isolated_logging):
    entry = ROOT / "src/trackeverything/core/builtin/__main__.py"
    assert entry.exists(), "python -m trackeverything.core.builtin must be the runtime process entry"
    forbidden = ("trackeverything.api", "trackeverything.integration.discord", "trackeverything.integration.slack")
    assert not [name for name in imports(entry) if name.startswith(forbidden)]
    serve = AsyncMock()
    monkeypatch.setattr("trackeverything.core.builtin.app.serve", serve)
    runpy.run_module("trackeverything.core.builtin", run_name="__main__")
    serve.assert_awaited_once_with()


def test_q55_queue_cli_publishes_without_starting_service(monkeypatch, tmp_path, capsys, isolated_logging):
    cli = module("queue.cli")
    producer = AsyncMock()
    producer.publish.return_value = module("queue.contracts").PublishReceipt(message_id="1700000000000-0", accepted_at=1700000000.0)
    opened = AsyncMock()
    opened.__aenter__.return_value = producer
    from_env = Mock(return_value=opened)
    monkeypatch.setattr(cli.Producer, "from_env", from_env)
    serve = AsyncMock()
    monkeypatch.setattr("trackeverything.api.app.serve", serve)

    message = tmp_path / "message.json"
    named = {"provider": "internal", "external_id": "cli:general", "payload": {"agent_name": "general", "request": "調査する"}}
    message.write_text(json.dumps(named))
    cli.main(["publish", "--file", str(message)])
    [published] = producer.publish.await_args.args
    assert type(published) is EventRequest and published == EventRequest(**named)
    assert json.loads(capsys.readouterr().out) == {"message_id": "1700000000000-0", "accepted_at": 1700000000.0}

    from_env.reset_mock()
    rejected = (["status", "ID"], ["dlq"], ["replay", "ID"], ["publish", "--file", str(message), "--idempotency-key", "k"], ["start"], ["unknown"])
    for argv in rejected:
        with pytest.raises(SystemExit) as error:
            cli.main(argv)
        assert error.value.code != 0
    from_env.assert_not_called()
    serve.assert_not_awaited()


@pytest.mark.parametrize(
    ("script", "entry", "other"),
    [
        ("start_queue.sh", "trackeverything.api", "trackeverything.core.builtin"),
        ("start_runtime.sh", "trackeverything.core.builtin", "trackeverything.api"),
    ],
)
def test_q56_start_scripts_exec_only_their_own_entry(monkeypatch, script, entry, other):
    path = ROOT / "scripts" / script
    assert path.exists(), f"{script} must start its own process"
    text = path.read_text()
    commands = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    assert 'cd "$(dirname "$0")/.."' in commands
    assert [shlex.split(line) for line in commands if line.startswith("exec ")] == [["exec", "bash", "scripts/run_local.sh", "python", "-m", entry]]
    assert other not in text
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
    checked = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, check=False)
    assert checked.returncode == 0, checked.stderr


def test_q57_queue_entry_redacts_report_access_log(monkeypatch, isolated_logging):
    monkeypatch.setattr("trackeverything.api.app.serve", AsyncMock())
    runpy.run_module("trackeverything.api", run_name="__main__")
    access = logging.getLogger("uvicorn.access")
    output = io.StringIO()
    access.addHandler(logging.StreamHandler(output))
    access.setLevel(logging.INFO)
    # uvicorn と同じ (client, method, path, http_version, status) の 5 要素。
    for target in ("/reports/plain-secret?x=1", "/%72eports/encoded-secret?x=1", "/%2572eports/double-secret", "/api/runs?limit=20"):
        access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", target, "1.1", 200)
    lines = output.getvalue().splitlines()
    assert lines[:3] == ['127.0.0.1:5000 - "GET /reports/[redacted] HTTP/1.1" 200'] * 3
    assert "secret" not in output.getvalue() and "x=1" not in output.getvalue()
    assert lines[3] == '127.0.0.1:5000 - "GET /api/runs?limit=20 HTTP/1.1" 200'


def test_q58_queue_package_holds_no_http_or_receivers():
    """queue は Redis Streams だけを扱う。HTTP・受信接続とその構成は queue の外に置く。"""
    forbidden = ("fastapi", "starlette", "uvicorn", "trackeverything.api", "trackeverything.integration")
    found = {
        path.name: hits
        for path in sorted((ROOT / "src/trackeverything/queue").rglob("*.py"))
        if (hits := [name for name in imports(path) if name.startswith(forbidden)])
    }
    assert found == {}
