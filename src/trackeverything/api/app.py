"""生産側の構成ルート。HTTP と受信接続を一つの loop で管理する。"""

from __future__ import annotations

from fastapi import HTTPException

import trackeverything.config as config
from trackeverything.api import create_reports_router, create_runs_router, create_usage_router, create_webui_router
from trackeverything.api.catalog import create_snapshot_catalog_router
from trackeverything.api.http import HttpServer
from trackeverything.core.orm.connection import require_schema_head
from trackeverything.integration.connector import Connector, Listener
from trackeverything.integration.discord import build_discord_gateway, discord_connector
from trackeverything.integration.schedule import ScheduleTrigger, schedule_connector
from trackeverything.integration.slack import build_slack_gateway, slack_connector
from trackeverything.integration.webhook import create_webhook_app
from trackeverything.integration.webhook.github import github_connector
from trackeverything.process import run_host
from trackeverything.queue.producer import Producer


class QueueApplication:
    def __init__(self, producer: Producer, connectors: list[Connector]) -> None:
        self.producer, self.connectors = producer, connectors
        self._started: list[Listener] = []
        self.ready = False
        self.http_app = create_webhook_app(producer.publish, connectors=connectors, extra_routers=[
            create_snapshot_catalog_router(producer.stream.catalog), create_reports_router(), create_runs_router(),
            create_usage_router(), create_webui_router(),
        ])

        @self.http_app.get("/readyz")
        async def readiness() -> dict[str, bool]:
            if not self.ready:
                raise HTTPException(503, "producer is not ready")
            try:
                await producer.stream.ping()
            except Exception:
                raise HTTPException(503, "Redis is unavailable") from None
            return {"ok": True}

        self.http = HttpServer(self.http_app)

    async def start(self) -> None:
        try:
            await self.producer.stream.ping()
            # 旧アプリと同じ port のまま並行起動しないよう HTTP を先に確保する。
            self._started.append(self.http)
            await self.http.start()
            for connector in self.connectors:
                if connector.listener is not None:
                    self._started.append(connector.listener)
                    await connector.listener.start()
            self.ready = True
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        self.ready = False
        errors: list[Exception] = []
        started, self._started = self._started, []
        for listener in reversed(started):
            try:
                await listener.stop()
            except Exception as exc:
                errors.append(exc)
        await self.producer.close()
        if errors:
            raise ExceptionGroup("producer shutdown failed", errors)


async def serve() -> None:
    await require_schema_head(config.DATABASE_URL)
    producer = Producer.from_env()
    try:
        connectors = [github_connector()]
        discord = build_discord_gateway(producer)
        if discord is not None:
            connectors.append(discord_connector(discord))
        connectors.append(slack_connector(build_slack_gateway(producer)))
        connectors.append(schedule_connector(ScheduleTrigger(producer)))
        await run_host(QueueApplication(producer, connectors))
    finally:
        await producer.close()
