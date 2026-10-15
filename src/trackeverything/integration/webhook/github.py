"""GitHub Webhook の形式を検証し、header の許可リストと body の原文を投入する。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from trackeverything.core.abc.submission import EventRequest
from trackeverything.integration.connector import Connector, InvalidWebhookError, WebhookIntake

#: 保存する header。認証 header(X-Webhook-Token・gateway が注入する値)は Redis に入れない。
_ALLOWED_HEADERS = ("x-github-event", "x-github-delivery", "x-github-hook-id")


def parse_github(headers: Mapping[str, str], body: dict[str, Any]) -> WebhookIntake:
    """イベント種類と配信 ID の header があれば、内容によらず原形で投入する。"""
    lowered = {key.lower(): value for key, value in headers.items()}
    event, delivery = lowered.get("x-github-event"), lowered.get("x-github-delivery")
    if not event or not delivery:
        raise InvalidWebhookError("X-GitHub-Event and X-GitHub-Delivery headers are required")
    payload: dict[str, Any] = {"headers": {key: lowered[key] for key in _ALLOWED_HEADERS if lowered.get(key)}, "body": body}
    return WebhookIntake(EventRequest(provider="github", external_id=delivery, payload=payload))


def github_connector() -> Connector:
    """GitHub の ``POST /webhook/github`` を登録する。"""
    return Connector("github", webhook=parse_github)
