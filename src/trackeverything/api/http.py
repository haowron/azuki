"""Host と同じ event loop 上で動く HTTP サーバー。signal は Runtime が所有する。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn
from fastapi import FastAPI

import trackeverything.config as config

log = logging.getLogger(__name__)
_HTTP_START_TIMEOUT = 5.0


class _HostServer(uvicorn.Server):
    """signal は Host が所有する。uvicorn に独自の handler を入れさせない。"""

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield



class HttpServer:
    """HTTP の設定・開始・終了。Runtime へ start / stop だけを渡せる。"""

    def __init__(self, app: FastAPI, *, host: str = config.WEBHOOK_HOST, port: int = config.WEBHOOK_PORT) -> None:
        self._http_config = uvicorn.Config(app, host=host, port=port, log_level="info")
        self._http_server: uvicorn.Server | None = None
        self._http_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._http_config is None:
            return
        server = _HostServer(self._http_config)
        self._http_server = server
        self._http_task = asyncio.create_task(self._serve_http(server), name="te-webhook-server")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _HTTP_START_TIMEOUT
        while not server.started:
            if self._http_task.done() or loop.time() >= deadline:
                raise RuntimeError("HTTP server failed to start")
            await asyncio.sleep(0.01)


    @staticmethod
    async def _serve_http(server: uvicorn.Server) -> None:
        try:
            await server.serve()
        except SystemExit:
            # uvicorn の bind 失敗は SystemExit を使う。loop を止めず、Host 側で起動失敗に変換する。
            log.exception("HTTP server failed")
        except Exception:
            log.exception("HTTP server failed")


    async def stop(self) -> None:
        if self._http_server is not None:
            self._http_server.should_exit = True
        if self._http_task is not None:
            await self._http_task
        self._http_server = self._http_task = None
