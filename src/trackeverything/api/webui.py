"""Web UI: a dependency-free HTML dashboard at ``GET /``.

The page shell is ``api/static/index.html``; its CSS stays inline and its
vanilla JS lives in ``api/static/js/`` (``api.js`` — one function per
``/api/*`` endpoint, ``app.js`` — the shared shell, one module pair per
screen), served below ``/static/``. No framework, no build step, no CDN.
Data comes from ``/api/*`` with an optional ``X-Webhook-Token`` box for
deployments that set ``WEBHOOK_TOKEN``. Served by the same shared HTTP
server as the webhook ingress, injected by the process entry alongside the
other API routers.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"
_MEDIA_TYPES = {".css": "text/css", ".js": "text/javascript", ".html": "text/html", ".svg": "image/svg+xml"}


def create_webui_router() -> APIRouter:
    """Serve the dashboard shell at ``/`` and its assets at ``/static/*``."""

    router = APIRouter()

    @router.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        # no-cache = 常に etag 再検証。再部署後の古い JS/CSS をブラウザーが黙って使わないようにする。
        return FileResponse(_INDEX_HTML, media_type="text/html", headers={"Cache-Control": "no-cache"})

    @router.get("/static/{path:path}", include_in_schema=False)
    async def static_file(path: str) -> FileResponse:
        target = (_STATIC_DIR / path).resolve()
        if not target.is_relative_to(_STATIC_DIR) or not target.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(
            target,
            media_type=_MEDIA_TYPES.get(target.suffix, "application/octet-stream"),
            headers={"Cache-Control": "no-cache"},
        )

    return router
