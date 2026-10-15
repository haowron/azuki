"""VL48: the native supervisor restarts the app for a change of the checkout and drains running work first.

This case runs the real ``scripts/run_service.sh`` and ``scripts/start_runtime.sh``
with bash against a temporary checkout. ``run_local.sh`` is replaced by a fake app,
so the real service never starts.
"""

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# The root conftest forbids subprocesses; this approved case runs the supervisor script itself.
_REAL_POPEN = subprocess.Popen

FAKE_APP = """#!/bin/bash
echo "start $$" >> "$APP_LOG"
on_term() {
  echo "term $$" >> "$APP_LOG"
  if [ -f "$CONTROL/ignore_term" ]; then return; fi
  sleep 1
  echo "exit $$" >> "$APP_LOG"
  exit 0
}
trap on_term TERM
while true; do sleep 0.1; done
"""


def _wait_for(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _bump(path):
    path.write_text(path.read_text() + "# changed\n")
    stamp = path.stat().st_mtime + 5
    os.utime(path, (stamp, stamp))


def test_vl48_restart_path_waits_for_running_work(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
    (tmp_path / "scripts").mkdir()
    for script in ("run_service.sh", "start_runtime.sh"):
        shutil.copy(REPO / "scripts" / script, tmp_path / "scripts" / script)
    (tmp_path / "scripts/run_local.sh").write_text(FAKE_APP)
    package = tmp_path / "src/trackeverything"
    package.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "src/trackeverything/process.py", package / "process.py")
    (package / "core").mkdir(parents=True, exist_ok=True)
    (package / "core/host.py").write_text("VALUE = 1\n")
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "uv.lock").write_text("")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text("#!/bin/bash\nexit 0\n")
    (bin_dir / "uv").chmod(0o755)
    control = tmp_path / "control"
    control.mkdir()
    app_log = tmp_path / "app.log"
    app_log.touch()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "RUN_SERVICE_POLL": "0.2",
        "TE_SERVICE_PYTHON": sys.executable,
        "RUN_SERVICE_TERM_WAIT": "3",
        "APP_LOG": str(app_log),
        "CONTROL": str(control),
    }
    supervisor = subprocess.Popen(
        ["/bin/bash", str(tmp_path / "scripts/run_service.sh"), "runtime"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    def lines():
        return app_log.read_text().split("\n")

    def starts():
        return [line.split()[1] for line in lines() if line.startswith("start ")]

    try:
        assert _wait_for(lambda: len(starts()) == 1, 10)
        first = starts()[0]

        # (1) A Host change sends one TERM, waits for the app to finish, then relaunches.
        _bump(package / "core/host.py")
        assert _wait_for(lambda: len(starts()) == 2, 12)
        assert lines().count(f"term {first}") == 1 and f"exit {first}" in lines()
        second = starts()[1]

        # (2) An app that ignores TERM is killed only after the deadline.
        (control / "ignore_term").touch()
        _bump(package / "core/host.py")
        assert _wait_for(lambda: f"term {second}" in lines(), 10)
        termed = time.monotonic()
        assert _wait_for(lambda: len(starts()) == 3, 12)
        assert time.monotonic() - termed >= 2.5 and f"exit {second}" not in lines()
    finally:
        (control / "ignore_term").unlink(missing_ok=True)
        try:
            os.killpg(supervisor.pid, signal.SIGTERM)
            supervisor.wait(10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            os.killpg(supervisor.pid, signal.SIGKILL)
        for pid in starts():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid), signal.SIGKILL)
