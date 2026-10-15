"""RI06〜RI09・RI13〜RI16: Discord の gateway frame を原形のまま投入し、session と身元の剪定だけを行う。"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from trackeverything.integration.discord import DiscordGateway


def frame(event, data):
    return json.dumps({"op": 0, "t": event, "s": 1, "d": data})


def message(**changes):
    data = {"id": "20", "type": 0, "channel_id": "10", "guild_id": "100", "author": {"id": "1", "username": "owner"},
            "content": "現在の仕様は?", "attachments": [], "embeds": [], "mentions": []}
    return data | changes


def receiver(parent_id=None):
    producer = AsyncMock()
    sessions = AsyncMock(side_effect=lambda provider, channel: f"session-{channel}")
    gateway = DiscordGateway(producer, token="fixture", owner_id=1, resolve_session=sessions, general_channel="99")
    gateway._client.get_channel = Mock(return_value=SimpleNamespace(parent_id=parent_id))
    return gateway, producer, sessions


async def test_ri06_channel_message_is_queued_raw_with_channel_session():
    gateway, producer, sessions = receiver()
    await gateway.on_socket_raw_receive(frame("MESSAGE_CREATE", message()))
    request = producer.publish.await_args.args[0]
    assert (request.provider, request.external_id, request.payload) == ("discord", "20", message())
    assert (request.session_id, request.parent_session_id) == ("session-10", None)
    sessions.assert_awaited_once_with("discord", "10")
    empty = message(id="21", content="")
    await gateway.on_socket_raw_receive(frame("MESSAGE_CREATE", empty))
    assert producer.publish.await_args.args[0].payload == empty
    thread_gateway, thread_producer, _ = receiver(parent_id=9)
    await thread_gateway.on_socket_raw_receive(frame("MESSAGE_CREATE", message(id="22", channel_id="11")))
    request = thread_producer.publish.await_args.args[0]
    assert (request.session_id, request.parent_session_id) == ("session-11", "session-9")


async def test_ri07_general_channel_message_has_no_session_and_skips_channel_table():
    gateway, producer, sessions = receiver()
    await gateway.on_socket_raw_receive(frame("MESSAGE_CREATE", message(channel_id="99")))
    request = producer.publish.await_args.args[0]
    assert (request.session_id, request.parent_session_id) == (None, None)
    sessions.assert_not_awaited()


@pytest.mark.parametrize("data", [
    message(author={"id": "1", "bot": True}),
    message(webhook_id="5"),
    message(author={"id": "2", "username": "someone"}),
], ids=["bot", "webhook", "not-owner"])
async def test_ri08_bot_webhook_and_other_users_are_not_queued(data):
    gateway, producer, sessions = receiver()
    await gateway.on_socket_raw_receive(frame("MESSAGE_CREATE", data))
    producer.publish.assert_not_awaited()
    sessions.assert_not_awaited()


BUTTON = {"id": "500", "type": 3, "token": "interaction-secret", "application_id": "7", "channel_id": "10", "guild_id": "100",
          "member": {"user": {"id": "1"}}, "data": {"custom_id": "code_push:fix-0123456789ab:" + "a" * 40, "component_type": 2},
          "message": {"id": "30", "channel_id": "10", "author": {"id": "7", "bot": True}, "content": "确认推送", "components": []}}


def clicked(user_id):
    interaction = Mock()
    interaction.id, interaction.type = 500, discord.InteractionType.component
    interaction.user = SimpleNamespace(id=user_id, bot=False)
    interaction.response.send_message, interaction.response.defer = AsyncMock(), AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


async def test_ri09_button_rejects_others_and_queues_owner_click_after_defer():
    gateway, producer, _ = receiver()
    other = clicked(2)
    await gateway.on_socket_raw_receive(frame("INTERACTION_CREATE", BUTTON))
    await gateway.on_interaction(other)
    other.response.send_message.assert_awaited_once_with("🤖 只有请求修改的本人可以确认。", ephemeral=True)
    other.response.defer.assert_not_awaited()
    producer.publish.assert_not_awaited()

    owner = clicked(1)
    order = Mock()
    order.attach_mock(owner.response.defer, "defer")
    order.attach_mock(producer.publish, "publish")
    order.attach_mock(owner.followup.send, "followup")
    await gateway.on_socket_raw_receive(frame("INTERACTION_CREATE", BUTTON))
    await gateway.on_interaction(owner)
    assert [item[0] for item in order.mock_calls] == ["defer", "publish", "followup"]
    request = producer.publish.await_args.args[0]
    assert (request.provider, request.external_id, request.session_id) == ("discord", "500", "session-10")
    assert request.payload == {key: value for key, value in BUTTON.items() if key != "token"}

    failed = clicked(1)
    producer.publish.side_effect = RuntimeError("not stored")
    await gateway.on_socket_raw_receive(frame("INTERACTION_CREATE", BUTTON))
    await gateway.on_interaction(failed)
    failed.followup.send.assert_awaited_once_with("🤖 未能保存执行请求。请重试。", ephemeral=True)


def reaction(**changes):
    data = {"user_id": "1", "channel_id": "10", "message_id": "30", "guild_id": "100", "member": {"user": {"id": "1", "username": "owner"}},
            "emoji": {"id": None, "name": "👍"}, "message_author_id": "7", "burst": False, "type": 0}
    return data | changes


@pytest.mark.parametrize(("channel", "parent", "sessions_expected"), [
    ("10", None, ("session-10", None)),
    ("11", 9, ("session-11", "session-9")),
    ("99", None, (None, None)),
], ids=["channel", "thread", "general"])
async def test_ri13_owner_reaction_is_queued_raw_with_channel_session(channel, parent, sessions_expected):
    gateway, producer, sessions = receiver(parent_id=parent)
    assert gateway._client.intents.guild_reactions and gateway._client.intents.dm_reactions
    data = reaction(channel_id=channel)
    await gateway.on_socket_raw_receive(frame("MESSAGE_REACTION_ADD", data))
    request = producer.publish.await_args.args[0]
    assert (request.provider, request.external_id, request.payload) == ("discord", "MESSAGE_REACTION_ADD:30:👍", data)
    assert (request.session_id, request.parent_session_id) == sessions_expected
    if channel == "99":
        sessions.assert_not_awaited()


async def test_ri14_owner_reaction_removal_is_queued_raw_and_named_by_event():
    gateway, producer, _ = receiver()
    removed = {"user_id": "1", "channel_id": "10", "message_id": "30", "guild_id": "100", "emoji": {"id": None, "name": "👎"}, "burst": False, "type": 0}
    await gateway.on_socket_raw_receive(frame("MESSAGE_REACTION_REMOVE", removed))
    request = producer.publish.await_args.args[0]
    assert (request.external_id, request.payload, request.session_id) == ("MESSAGE_REACTION_REMOVE:30:👎", removed, "session-10")
    custom = removed | {"emoji": {"id": "555", "name": "party"}}
    await gateway.on_socket_raw_receive(frame("MESSAGE_REACTION_REMOVE", custom))
    assert producer.publish.await_args.args[0].external_id == "MESSAGE_REACTION_REMOVE:30:555"


@pytest.mark.parametrize(("event", "data"), [
    ("MESSAGE_REACTION_ADD", reaction(user_id="2", member={"user": {"id": "2", "username": "someone"}})),
    ("MESSAGE_REACTION_ADD", reaction(user_id="7", member={"user": {"id": "7", "bot": True}}, emoji={"id": None, "name": "🫡"})),
    ("MESSAGE_REACTION_REMOVE_ALL", {"channel_id": "10", "message_id": "30", "guild_id": "100"}),
    ("MESSAGE_REACTION_REMOVE_EMOJI", {"channel_id": "10", "message_id": "30", "guild_id": "100", "emoji": {"id": None, "name": "👍"}}),
], ids=["not-owner", "bot-ack", "remove-all", "remove-emoji"])
async def test_ri15_other_reactions_and_bulk_removals_are_not_queued(event, data):
    gateway, producer, sessions = receiver()
    await gateway.on_socket_raw_receive(frame(event, data))
    producer.publish.assert_not_awaited()
    sessions.assert_not_awaited()


async def test_ri16_reaction_whose_session_cannot_be_resolved_is_not_queued(caplog):
    gateway, producer, sessions = receiver()
    sessions.side_effect = RuntimeError("channel table unavailable")
    await gateway.on_socket_raw_receive(frame("MESSAGE_REACTION_ADD", reaction()))
    producer.publish.assert_not_awaited()
    assert "Discord reaction was not durably accepted" in caplog.text
