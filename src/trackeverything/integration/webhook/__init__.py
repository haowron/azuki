"""Webhook の共通受付。Server とワーカーの所有は Host に委ねる。

provider 固有の形式検証は Connector の ``webhook`` が持つ。この module は共通の受付
(token 検査・JSON object の検証・422 / 503 の対応・永続投入)だけを置く。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

import trackeverything.config as config
from trackeverything.core.abc.submission import EventRequest
from trackeverything.integration.connector import Connector, InvalidWebhookError, WebhookParser
from trackeverything.queue.contracts import PublishReceipt, QueueUnavailableError

__all__ = ["create_webhook_app"]

log = logging.getLogger(__name__)


def create_webhook_app(
    publish: Callable[[EventRequest], Awaitable[PublishReceipt]],
    extra_routers: Sequence[APIRouter] = (),
    *,
    connectors: Sequence[Connector] = (),
) -> FastAPI:
    """Build the FastAPI app: /healthz, the injected routers, and one POST route per webhook connector.

    ``publish`` は共通 Producer の永続投入口。受理を確認できなければ
    :class:`QueueUnavailableError` を送出し、503 で答える。

    ``extra_routers`` are mounted as-is after /healthz — the seam that lets the
    outer ``api/`` package serve its routes on this same server (routers are
    injected by the process entry, never imported here). ``connectors`` with a
    ``webhook`` each get ``POST /webhook/{name}`` in order; the token check,
    JSON validation and status mapping stay here so no provider can skip them.
    """
    names = [connector.name for connector in connectors if connector.webhook is not None]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"duplicate webhook connector names: {duplicates}")
    app = FastAPI(title="TrackEverything HTTP server", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    for router in extra_routers:
        app.include_router(router)

    def register(name: str, parse: WebhookParser) -> None:
        @app.post(f"/webhook/{name}", name=f"webhook_{name}")
        async def receive(request: Request) -> JSONResponse:
            if config.WEBHOOK_TOKEN and request.headers.get("x-webhook-token") != config.WEBHOOK_TOKEN:
                raise HTTPException(status_code=401, detail="invalid or missing X-Webhook-Token")
            try:
                body: Any = await request.json()
            except Exception:
                raise HTTPException(status_code=422, detail="request body must be JSON") from None
            if not isinstance(body, dict):
                raise HTTPException(status_code=422, detail="request body must be a JSON object")
            try:
                intake = parse(dict(request.headers), body)
            except (InvalidWebhookError, ValidationError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from None
            if intake.request is None:
                return JSONResponse(intake.response or {"ok": True})
            try:
                receipt = await publish(intake.request)
            except QueueUnavailableError:
                raise HTTPException(status_code=503, detail="queue is not accepting messages") from None
            log.info("webhook/%s: queued %s", name, intake.request.external_id)
            return JSONResponse({"ok": True, "run": "queued", **receipt.model_dump()}, status_code=202)

    for connector in connectors:
        if connector.webhook is not None:
            register(connector.name, connector.webhook)
    return app
