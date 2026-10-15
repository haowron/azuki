"""Slack の受信。Events API の検証と Socket Mode の対象配信を原形で投入する。

SDK の aiohttp 版 Socket Mode client と ``AsyncWebClient`` が Host の event loop 上で
接続・再接続を行う。Socket Mode は設定した team・channel の配信だけを投入し、投入に成功した
配信だけを ACK する(投入失敗では ACK せず SDK の再配送を待つ)。内容による判断は行わない。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.async_client import AsyncBaseSocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from trackeverything.core.abc.submission import EventRequest
from trackeverything.integration.connector import Connector, InvalidWebhookError, WebhookIntake, without_token
from trackeverything.queue.producer import Producer

log = logging.getLogger(__name__)


def socket_event(payload: Any, *, team_id: str, channel_id: str) -> EventRequest | None:
    """設定した team・channel の配信だけを原形の依頼にする(metadata による剪定)。"""
    if not isinstance(payload, dict) or payload.get("team_id") != team_id:
        return None
    event, event_id = payload.get("event"), payload.get("event_id")
    if not isinstance(event, dict) or event.get("channel") != channel_id or not isinstance(event_id, str) or not event_id.strip():
        return None
    return EventRequest(provider="slack", external_id=f"{team_id}:{event_id}", payload=without_token(payload))


class _SecretFilter(logging.Filter):
    """SDK の診断でも構成済みの認証値を出さない。"""

    def __init__(self, *secrets: str) -> None:
        super().__init__()
        self._secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self._secrets:
            message = message.replace(secret, "[redacted]")
        record.msg, record.args = message, ()
        # SDK の例外本文には request header が含まれる可能性がある。
        record.exc_info, record.exc_text = None, None
        return True


class SlackGateway:
    """SDK が接続と再接続を管理し、受信 handler が投入と ACK を行う。"""

    def __init__(self, producer: Producer, *, app_token: str, token: str, team_id: str, channel_id: str) -> None:
        for name, value in (("SLACK_APP_TOKEN", app_token), ("SLACK_TOKEN", token), ("SLACK_TEAM_ID", team_id), ("SLACK_CHANNEL_ID", channel_id)):
            if not value.strip():
                raise ValueError(f"{name} is required for Slack Socket Mode")
        self._producer = producer
        self._team_id, self._channel_id = team_id, channel_id
        self._app_token = app_token
        self._sdk_log = logging.getLogger(f"{__name__}.sdk")
        self._sdk_log.addFilter(_SecretFilter(app_token, token))
        self._token = token
        self._web: AsyncWebClient | None = None
        self._client: SocketModeClient | None = None
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        try:
            # SDK の client(aiohttp の session を含む)は Host の loop 上の開始境界でのみ作る。
            self._web = AsyncWebClient(token=self._token, logger=self._sdk_log)
            auth = await self._web.auth_test()
            if not auth.get("ok") or auth.get("team_id") != self._team_id:
                raise ValueError("Slack authorization does not match SLACK_TEAM_ID")
            self._client = SocketModeClient(app_token=self._app_token, web_client=self._web, logger=self._sdk_log)
            self._client.socket_mode_request_listeners.append(self.on_request)
            await self._client.connect()  # type: ignore[no-untyped-call]
        except Exception as exc:
            raise RuntimeError(f"Slack Socket Mode startup failed: {type(exc).__name__}") from None
        self._started = True
        log.info("Slack Socket Mode connected (team=%s, channel=%s)", self._team_id, self._channel_id)

    async def on_request(self, client: AsyncBaseSocketModeClient, request: SocketModeRequest) -> None:
        """投入失敗・受理拒否では ACK せず、SDK の再配送を受け付ける。対象外は ACK だけ返す。"""
        try:
            event = socket_event(request.payload, team_id=self._team_id, channel_id=self._channel_id) if request.type == "events_api" else None
        except Exception as exc:
            log.error("Slack event could not be read: %s", type(exc).__name__)
            event = None
        if event is None:
            await self._ack(client, request.envelope_id)
            return
        try:
            await self._producer.publish(event)
        except Exception as exc:
            log.error("Slack event publication failed: %s", type(exc).__name__)
            return
        await self._ack(client, request.envelope_id)

    @staticmethod
    async def _ack(client: AsyncBaseSocketModeClient, envelope_id: str) -> None:
        try:
            await client.send_socket_mode_response(SocketModeResponse(envelope_id=envelope_id))
        except Exception as exc:
            log.error("Slack event ACK failed: %s", type(exc).__name__)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.close()  # type: ignore[no-untyped-call]
            self._client = None
        self._started = False


def build_slack_gateway(producer: Producer) -> SlackGateway | None:
    """設定がある場合だけ producer の受信接続を作る。"""
    app_token = os.environ.get("SLACK_APP_TOKEN", "").strip()
    if not app_token:
        return None
    return SlackGateway(producer, app_token=app_token, token=os.environ.get("SLACK_TOKEN", ""),
                        team_id=os.environ.get("SLACK_TEAM_ID", ""), channel_id=os.environ.get("SLACK_CHANNEL_ID", ""))


def parse_slack(headers: Mapping[str, str], body: dict[str, Any]) -> WebhookIntake:
    """``url_verification`` は challenge を同期で返す。他は ``event_id`` があれば原形で投入する。"""
    if body.get("type") == "url_verification":
        return WebhookIntake(None, response={"challenge": body.get("challenge", "")})
    event_id = body.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise InvalidWebhookError("Slack event payload requires event_id")
    return WebhookIntake(EventRequest(provider="slack", external_id=event_id, payload=without_token(body)))


def slack_connector(gateway: SlackGateway | None) -> Connector:
    """Events API の ``POST /webhook/slack`` は常に登録し、Socket Mode の常駐接続は gateway がある場合だけ持つ。"""
    return Connector("slack", webhook=parse_slack, listener=gateway)
