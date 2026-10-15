"""AS24: Host は SIGTERM を受けると app を一度だけ止め、signal handler を元に戻す。"""

import asyncio
import inspect
import os
import signal

import pytest

from tests.async_helpers import WAIT
from trackeverything.core.builtin.app import TrackingRuntime


def _require_async_host():
    """同期の Host を起動して thread を残さないよう、先に契約の形を確かめる。"""
    for method in (TrackingRuntime.start, TrackingRuntime.stop):
        if not inspect.iscoroutinefunction(method):
            pytest.fail(f"{method.__qualname__} must be a coroutine function")


class _App:
    """開始と停止だけを持つ app の代役。"""

    def __init__(self):
        self.ready, self.stops = asyncio.Event(), []

    async def start(self):
        self.ready.set()

    async def stop(self):
        self.stops.append("stop")


async def test_as24_signals():
    _require_async_host()
    from trackeverything.process import run_host

    app = _App()
    before = signal.getsignal(signal.SIGTERM)
    host = asyncio.create_task(run_host(app))
    await asyncio.wait_for(app.ready.wait(), WAIT)
    handler = signal.getsignal(signal.SIGTERM)
    # 送信前の安全確認: asyncio の handler でなければ SIGTERM はテスト process を終了させる。
    assert getattr(handler, "__name__", "") == "_sighandler_noop", f"unexpected SIGTERM handler: {handler!r}"
    os.kill(os.getpid(), signal.SIGTERM)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(host, WAIT)
    assert app.stops == ["stop"]
    assert signal.getsignal(signal.SIGTERM) == before
