"""固定された review 報告と表示資源だけを匿名で読み取る HTTP 境界。

報告全体の読込みは ``asyncio.to_thread`` で Host の event loop の外で行う。
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response

import trackeverything.config as config

_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEADERS = {
    "Cache-Control": "private, no-store, no-transform",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
    # script の CSP は発行時の HTML に固定し、旧報告の hash を維持する。
    "Content-Security-Policy": "frame-ancestors 'none'",
}


def _read_file(parts: tuple[str, ...], *, head: bool) -> tuple[bytes, int]:
    """root の別名は許容し、配下の全要素を symlink を辿らず開く。"""
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(Path(config.REPORTS_DIR), os.O_RDONLY | os.O_DIRECTORY))
        for part in parts[:-1]:
            descriptors.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptors[-1]))
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptors[-1])
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise OSError("not a regular file")
            # 検証した同じ descriptor から読むため、検証後の差替えでも境界を越えない。
            content = b"" if head else source.read()
            return content, info.st_size if head else len(content)
    except OSError:
        raise HTTPException(status_code=404, headers=_HEADERS) from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


async def _response(request: Request, token: str, parts: tuple[str, ...], media_type: str) -> Response:
    if not _HEX64.fullmatch(token) or request.scope.get("raw_path") != request.scope["path"].encode("utf-8"):
        raise HTTPException(status_code=404, headers=_HEADERS)
    content, size = await asyncio.to_thread(_read_file, parts, head=request.method == "HEAD")
    return Response(content, media_type=media_type, headers={**_HEADERS, "Content-Length": str(size)})


def create_reports_router() -> APIRouter:
    """任意パスや一覧を持たず、完成済みの二種類のファイルだけを返す。"""
    router = APIRouter()

    @router.api_route("/reports/{report_id}.html", methods=["GET", "HEAD"], include_in_schema=False)
    async def report(request: Request, report_id: str) -> Response:
        return await _response(request, report_id, (report_id + ".html",), "text/html")

    @router.api_route("/reports/assets/{asset_hash}/mermaid.min.js", methods=["GET", "HEAD"], include_in_schema=False)
    async def mermaid(request: Request, asset_hash: str) -> Response:
        return await _response(request, asset_hash, ("assets", asset_hash, "mermaid.min.js"), "text/javascript")

    return router
