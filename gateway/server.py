# /// script
# requires-python = ">=3.14.2"
# dependencies = ["fastapi>=0.115,<1", "httpx>=0.28,<1", "uvicorn>=0.30,<1"]
# ///
"""他プロジェクトに依存しない、署名検証付き HTTP 転送サービス。"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"})

# ブラウザー用の一回限り入口。login-key は既存資格情報を持つ所有者が発行し、
# /login/<key> の page で固定 password の入力を求める。key と password の両方が
# 正しいときだけ session cookie を渡し、key はそこで失効する。これらの予約 path
# は上流へ転送しない。
SESSION_COOKIE = "__Host-gateway-session"
ISSUE_PATH = "/__gateway__/login-key"
LOGIN_PREFIX = "/login/"
# password の誤入力は key 1 本あたりこの回数まで。超えたら key を失効させる。
LOGIN_MAX_FAILURES = 8

# チャット投稿の link preview(unfurl)crawler。bot が貼った直後にサーバー側 preview が
# GET しに来る。crawler には何も渡さず無操作で返して余計なやり取りを発生させない。
UNFURLER_MARKERS = (
    "discordbot",
    "slackbot",
    "slack-link-expanding",
    "twitterbot",
    "facebookexternalhit",
    "telegrambot",
    "linespider",
    "skypeuripreview",
    "whatsapp",
)


def is_unfurler(user_agent: str) -> bool:
    lowered = user_agent.lower()
    return any(marker in lowered for marker in UNFURLER_MARKERS)


def login_page(error: str = "", *, password_configured: bool = True) -> str:
    """password 入力の最小 page。外部 asset を持たず単体で完結する。"""
    if not password_configured:
        notice = '<p class="error">共有パスワードが未設定のため、このログイン URL は現在使えません。所有者の設定をお待ちください。</p>'
    elif error:
        notice = f'<p class="error">{escape(error)}</p>'
    else:
        notice = ""
    return f"""<!doctype html>
<html lang="ja">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TrackEverything · ログイン</title>
<style>
body {{ margin: 0; display: grid; place-items: center; min-height: 100vh; background: #f5f6f8; color: #202733; font: 14px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans JP", sans-serif; }}
form {{ background: #fff; border: 1px solid #dce1e9; border-radius: 10px; padding: 28px; width: min(360px, 90vw); display: grid; gap: 12px; }}
h1 {{ margin: 0; font-size: 16px; font-weight: 600; }}
label {{ display: grid; gap: 4px; }}
input {{ font: inherit; padding: 10px; border: 1px solid #dce1e9; border-radius: 6px; }}
button {{ font: inherit; padding: 10px; border: 0; border-radius: 6px; background: #2256bd; color: #fff; cursor: pointer; }}
.error {{ margin: 0; color: #8f2934; font-size: 13px; }}
.note {{ margin: 0; color: #586373; font-size: 12px; }}
</style>
<form method="post" action="">
<h1>TrackEverything 管理画面へのログイン</h1>
{notice}
<label>共有パスワード<input type="password" name="password" autocomplete="current-password" required autofocus></label>
<button type="submit">ログイン</button>
<p class="note">この URL は一度だけ使えます(約 3 分以内)。</p>
</form>
</html>"""


def equal(expected: str, actual: str) -> bool:
    """Unicode の不正な資格情報も例外にせず定時間比較する。"""
    return hmac.compare_digest(expected.encode(), actual.encode())


def mac(secret: str, value: bytes) -> str:
    return hmac.new(secret.encode(), value, hashlib.sha256).hexdigest()


def required(raw: dict[str, Any], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name}: nonempty string required")
    return value


@dataclass(frozen=True)
class Route:
    prefix: str
    upstream: str
    enabled: bool
    strip_prefix: bool
    headers: dict[str, str]
    public_read_patterns: tuple[re.Pattern[str], ...]

    def matches(self, path: str) -> bool:
        return self.prefix == "/" or path == self.prefix or path.startswith(self.prefix + "/")


def parse_routes(raw: dict[str, Any]) -> list[Route]:
    result: list[Route] = []
    definitions = raw.get("routes")
    if not isinstance(definitions, list) or not definitions:
        raise ValueError("routes: nonempty list required")
    for definition in definitions:
        if not isinstance(definition, dict):
            raise ValueError("route: object required")
        prefix = required(definition, "prefix")
        if not prefix.startswith("/") or "?" in prefix or "#" in prefix or "\\" in prefix or ".." in prefix.split("/"):
            raise ValueError("prefix: absolute path required")
        prefix = prefix.rstrip("/") or "/"
        if any(route.prefix == prefix for route in result):
            raise ValueError("prefix: duplicate")
        upstream = required(definition, "upstream").rstrip("/")
        try:
            parsed = urlsplit(upstream)
            host = ipaddress.ip_address(parsed.hostname or "")
            port = parsed.port
        except ValueError:
            raise ValueError("upstream: loopback HTTP URL required") from None
        if parsed.scheme not in {"http", "https"} or not host.is_loopback or port is None or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
            raise ValueError("upstream: loopback HTTP URL with port required")
        enabled = definition.get("enabled", True)
        strip = definition.get("strip_prefix", False)
        if not isinstance(enabled, bool) or not isinstance(strip, bool):
            raise ValueError("enabled/strip_prefix: boolean required")
        if port in {raw.get("gateway_port", 8080), raw.get("auth_port", 18081)}:
            raise ValueError("upstream: gateway loop forbidden")
        headers = definition.get("headers", {})
        if not isinstance(headers, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not k or any(ch in k + v for ch in "\r\n") for k, v in headers.items()):
            raise ValueError("headers: string map required")
        patterns = definition.get("public_read_patterns", [])
        if not isinstance(patterns, list) or any(not isinstance(pattern, str) or not pattern for pattern in patterns):
            raise ValueError("public_read_patterns: list of nonempty patterns required")
        try:
            public_read_patterns = tuple(re.compile(pattern) for pattern in patterns)
        except re.error:
            raise ValueError("public_read_patterns: invalid pattern") from None
        result.append(Route(prefix, upstream, enabled, strip, headers, public_read_patterns))
    return sorted(result, key=lambda route: len(route.prefix), reverse=True)


def create_app(config: dict[str, Any], *, transport: httpx.AsyncBaseTransport | None = None, clock: Callable[[], float] = time.time) -> FastAPI:
    """原始 body を認証し、設定済みの上流へ一度だけ転送する。"""
    credentials = config.get("credentials", {})
    if not isinstance(credentials, dict):
        raise ValueError("credentials: object required")
    names = ("github_secret", "slack_signing_secret", "bearer_token")
    if any(not isinstance(credentials.get(name, ""), str) for name in names) or not any(credentials.get(name) for name in names):
        raise ValueError("credentials: at least one nonempty credential required")
    github_secret, slack_secret, bearer_token = (credentials.get(name, "") for name in names)
    login_password = credentials.get("login_password", "")
    if not isinstance(login_password, str):
        raise ValueError("credentials.login_password: string required")
    limit = config.get("max_body_bytes", 32 * 1024 * 1024)
    if type(limit) is not int or limit <= 0:
        raise ValueError("max_body_bytes: positive integer required")
    public_prefix = config.get("public_path_prefix", "")
    if not isinstance(public_prefix, str) or (
        public_prefix
        and (not public_prefix.startswith("/") or "?" in public_prefix or "#" in public_prefix or "\\" in public_prefix or ".." in public_prefix.split("/"))
    ):
        raise ValueError("public_path_prefix: simple absolute path required")
    public_origin = config.get("public_origin", "")
    if not isinstance(public_origin, str) or (public_origin and not (public_origin.startswith("http://") or public_origin.startswith("https://"))):
        raise ValueError("public_origin: absolute http(s) origin required")
    login_ttl = config.get("login_key_ttl_seconds", 180)
    session_ttl = config.get("session_ttl_seconds", 7 * 24 * 60 * 60)
    if type(login_ttl) is not int or login_ttl <= 0 or type(session_ttl) is not int or session_ttl <= 0:
        raise ValueError("login_key_ttl_seconds/session_ttl_seconds: positive integer required")
    routes = parse_routes(config)
    # 一回限りの login key と session はどちらもこの process の memory にだけ置く。
    # 再起動ですべて無効になり、外部 file への流出経路を持たない。
    pending_logins: dict[str, float] = {}
    login_failures: dict[str, int] = {}
    sessions: dict[str, float] = {}

    def sweep(now: float) -> None:
        for store in (pending_logins, sessions):
            for key in [key for key, expiry in store.items() if expiry <= now]:
                del store[key]
                login_failures.pop(key, None)

    def session_of(request: Request) -> str:
        for part in request.headers.get("cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE and value:
                return value
        return ""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 環境の proxy を継承せず、資格情報を loopback 外へ送らない。
        async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False, timeout=httpx.Timeout(300, connect=5)) as client:
            app.state.client = client
            yield

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    def denied(status: int = 403) -> Response:
        return Response("認証または接続の条件を満たしていません。", status_code=status, media_type="text/plain")

    async def body_of(request: Request) -> bytes | None:
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    @app.middleware("http")
    async def response_policy(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        directives = {part.strip().lower() for value in response.headers.getlist("cache-control") for part in value.split(",")}
        response.headers["Cache-Control"] = "private, no-store" + (", no-transform" if "no-transform" in directives else "")
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def receive(request: Request, path: str) -> Response:
        body = await body_of(request)
        if body is None:
            return denied(413)
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        bearer_valid = bool(bearer_token) and scheme.lower() == "bearer" and equal(bearer_token, token)
        github_valid = bool(github_secret) and equal("sha256=" + mac(github_secret, body), request.headers.get("x-hub-signature-256", ""))
        slack_valid = False
        timestamp = request.headers.get("x-slack-request-timestamp", "")
        if slack_secret and timestamp.isascii() and timestamp.isdecimal() and len(timestamp) <= 12 and abs(clock() - int(timestamp)) <= 300:
            slack_valid = equal("v0=" + mac(slack_secret, b"v0:" + timestamp.encode() + b":" + body), request.headers.get("x-slack-signature", ""))
        cred_valid = bearer_valid or github_valid or slack_valid
        actual_path = "/" + path

        # 予約 path: login-key の発行は既存資格情報を、/login/<key> は発行済み key を要求する。
        # どちらも上流へ転送しない。
        if actual_path.startswith("/__gateway__/"):
            if actual_path != ISSUE_PATH:
                return denied(404)
            if request.method not in ("GET", "HEAD"):
                return denied(405)
            if not cred_valid:
                return denied(401)
            now = clock()
            sweep(now)
            key = secrets.token_urlsafe(32)
            pending_logins[key] = now + login_ttl
            login_path = public_prefix.rstrip("/") + LOGIN_PREFIX + key
            payload: dict[str, Any] = {"key": key, "path": login_path, "expires_in": login_ttl, "password_configured": bool(login_password)}
            if public_origin:
                payload["url"] = public_origin.rstrip("/") + login_path
            return JSONResponse(payload)

        if actual_path == "/login" or actual_path.startswith(LOGIN_PREFIX):
            if actual_path == "/login" or request.method not in ("GET", "HEAD", "POST"):
                return denied(405)
            if is_unfurler(request.headers.get("user-agent", "")):
                # preview crawler には form も cookie も渡さない。
                return Response("login link の preview は無効です。ブラウザーで開いてください。", media_type="text/plain")
            key = actual_path[len(LOGIN_PREFIX):]
            expiry = pending_logins.get(key)
            if expiry is None or expiry <= clock() or login_failures.get(key, 0) >= LOGIN_MAX_FAILURES:
                return denied(401)
            if not login_password:
                # 固定 password は所有者が config で設定する。未設定の間は閉じる。
                return Response(login_page(password_configured=False), status_code=503, media_type="text/html")
            if request.method in ("GET", "HEAD"):
                return Response(login_page(), media_type="text/html")
            # POST: 固定 password が key と同時に正しいときだけ通す。
            fields = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
            supplied = (fields.get("password") or [""])[0]
            if not equal(login_password, supplied):
                failures = login_failures.get(key, 0) + 1
                if failures >= LOGIN_MAX_FAILURES:
                    pending_logins.pop(key, None)
                    login_failures.pop(key, None)
                    return denied(401)
                login_failures[key] = failures
                return Response(login_page("パスワードが一致しません。"), status_code=401, media_type="text/html")
            # 成功した時点で key を失効させる(一回限り)。
            pending_logins.pop(key, None)
            login_failures.pop(key, None)
            session = secrets.token_urlsafe(32)
            sessions[session] = clock() + session_ttl
            response = Response(status_code=303, headers={"Location": "../"})
            # __Host- 接頭辞が browser に Path=/・Secure・Domain なしを強制する。
            # 相対 Location "../" は公開 prefix 付き URL でも localhost 直でも
            # 呼び出し元の入口直下へ戻る。
            response.set_cookie(SESSION_COOKIE, session, max_age=session_ttl, path="/", secure=True, httponly=True, samesite="lax")
            return response

        session_token = session_of(request)
        session_valid = bool(session_token) and sessions.get(session_token, 0.0) > clock()
        route = next((entry for entry in routes if entry.matches(actual_path)), None)
        raw_path = request.scope.get("raw_path", request.url.path.encode())
        # 公開読取りは ASCII の正規 path だけ。percent encoding・空/相対 segment を
        # 許可せず、検査した path が上流 URL の組立で別の API に変化するのを防ぐ。
        public_read = bool(
            route and route.enabled
            and request.method in {"GET", "HEAD"}
            and raw_path == actual_path.encode()
            and re.fullmatch(r"/[A-Za-z0-9._~/-]*", actual_path)
            and "//" not in actual_path
            and not {".", ".."}.intersection(actual_path.split("/"))
            and any(pattern.fullmatch(actual_path) for pattern in route.public_read_patterns)
        )
        if not (cred_valid or session_valid or public_read):
            return denied(401)
        if route is None:
            return denied(404)
        if not route.enabled:
            return denied(503)

        target_path = raw_path
        if route.strip_prefix and route.prefix != "/":
            encoded_prefix = route.prefix.encode()
            if not target_path.startswith(encoded_prefix):
                return denied(400)
            target_path = target_path[len(encoded_prefix):] or b"/"
        query = request.scope.get("query_string", b"")
        target = httpx.URL(route.upstream).copy_with(raw_path=target_path + (b"?" + query if query else b""))
        if public_read and target.raw_path.split(b"?", 1)[0] != target_path:
            return denied(400)
        connection_tokens = {entry.strip().lower() for entry in request.headers.get("connection", "").split(",")}
        # cookie は gateway 自身の session だけを運ぶ。上流へ渡さない。
        excluded = HOP | connection_tokens | {"host", "content-length", "authorization", "x-gateway-url-key", "cookie"}
        headers = [(name, value) for name, value in request.headers.items() if name.lower() not in excluded]
        overridden = {name.lower() for name in route.headers}
        headers = [(name, value) for name, value in headers if name.lower() not in overridden]
        headers.extend(route.headers.items())
        client: httpx.AsyncClient = request.app.state.client
        upstream_request = client.build_request(request.method, target, headers=headers, content=body)
        try:
            upstream = await client.send(upstream_request, stream=True)
        except httpx.TimeoutException:
            return denied(504)
        except httpx.HTTPError:
            return denied(502)

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        response = StreamingResponse(stream(), status_code=upstream.status_code)
        removed = HOP | {entry.strip().lower() for entry in upstream.headers.get("connection", "").split(",")}
        response.raw_headers = [(name.lower(), value) for name, value in upstream.headers.raw if name.decode().lower() not in removed]
        return response

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18081)
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text())
        if not isinstance(config, dict):
            raise ValueError("configuration object required")
        app = create_app(config)
    except (OSError, ValueError):
        raise SystemExit("ゲートウェイ設定を読み込めません。必須項目とアクセス権を確認してください。") from None
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
