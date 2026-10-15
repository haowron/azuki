"""管理 API の共通認証。``WEBHOOK_TOKEN`` を設定した場合だけ ``X-Webhook-Token`` を要求する。"""

from __future__ import annotations

from fastapi import HTTPException, Request

import trackeverything.config as config


async def require_token(request: Request) -> None:
    """Reject the request when the shared webhook token does not match."""
    if config.WEBHOOK_TOKEN and request.headers.get("x-webhook-token") != config.WEBHOOK_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing X-Webhook-Token")
