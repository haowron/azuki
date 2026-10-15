"""ST01〜ST08・RV01〜RV08: DB の定義による定時発火と一回限りの予約。表の読取り・Producer・時刻を差し替え、周回は時刻を与えて直接呼ぶ。"""

import asyncio
import logging
from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from tests.queue.helpers import module

JST = ZoneInfo("Asia/Tokyo")
WEEKDAY_9 = "0 9 * * 1-5"
DIGEST = {"agent_name": "general", "request": "昨日の更新をまとめて通知する"}
REMIND = {"agent_name": "reservation_fire", "data": {"reservation_id": "remind", "instruction": "週報を提出するよう知らせる"}}


def at(hour, minute, second=0, microsecond=0, day=13):
    """2026-10-13(火)を基準にした東京の時刻。"""
    return datetime(2026, 10, day, hour, minute, second, microsecond, tzinfo=JST)


def row(id_="weekday-digest", cron=WEEKDAY_9, timezone="Asia/Tokyo", payload=None):
    return module("integration.schedule").ScheduleRow(id=id_, cron=cron, timezone=timezone, payload=DIGEST if payload is None else payload)


def reservation(id_="remind", when=None, payload=None):
    when = datetime(2026, 10, 13, 9, 0, tzinfo=JST) if when is None else when
    return module("integration.schedule").ReservationRow(id=id_, at=when, payload=REMIND if payload is None else payload)


def producer():
    client = AsyncMock()
    client.publish.return_value = module("queue.contracts").PublishReceipt(message_id="1700000000000-0", accepted_at=1700000000.0)
    return client


@pytest.fixture
def table(monkeypatch):
    """``load_schedules`` が返す行。``table.rows`` を書き換えると次の周回の読取り結果が変わる。"""
    state = AsyncMock()
    state.rows = []

    async def load():
        await state.loaded()
        return list(state.rows)

    monkeypatch.setattr("trackeverything.integration.schedule.load_schedules", load)
    return state


@pytest.fixture(autouse=True)
def reserved(monkeypatch):
    """``load_reservations`` が返す予約の行。既定は空で、ST の用例は予約のない表として動く。"""
    state = AsyncMock()
    state.rows = []

    async def load():
        await state.loaded()
        return list(state.rows)

    monkeypatch.setattr("trackeverything.integration.schedule.load_reservations", load)
    return state


def published(client):
    return [call.args[0] for call in client.publish.await_args_list]


@pytest.mark.parametrize(
    ("timezone", "cron", "external_id"),
    [
        ("Asia/Tokyo", WEEKDAY_9, "schedule:weekday-digest:2026-10-13T09:00:00+09:00"),
        ("UTC", "0 0 * * 1-5", "schedule:weekday-digest:2026-10-13T00:00:00+00:00"),
    ],
)
async def test_st01_fires_once_at_the_cron_time(table, timezone, cron, external_id):
    table.rows = [row(cron=cron, timezone=timezone)]
    client = producer()
    trigger = module("integration.schedule").ScheduleTrigger(client)

    wait = await trigger.tick(at(8, 59, 50))
    assert published(client) == []
    assert wait == pytest.approx(10.0)

    await trigger.tick(at(9, 0, 0, 200_000))
    [request] = published(client)
    assert request.provider == "internal"
    assert request.external_id == external_id
    assert request.payload == DIGEST
    assert request.session_id is None and request.parent_session_id is None

    await trigger.tick(at(9, 0, 30))
    assert client.publish.await_count == 1


async def test_st02_does_not_catch_up_a_time_passed_before_the_first_read(table):
    table.rows = [row()]
    client = producer()
    trigger = module("integration.schedule").ScheduleTrigger(client)

    await trigger.tick(at(9, 0, 20))
    await trigger.tick(at(9, 0, 50))
    assert published(client) == []

    await trigger.tick(at(9, 0, 0, day=14))
    assert [request.external_id for request in published(client)] == ["schedule:weekday-digest:2026-10-14T09:00:00+09:00"]


@pytest.mark.parametrize(("late", "fires"), [(at(9, 1, 0), True), (at(9, 1, 1), False)])
async def test_st03_skips_a_time_later_than_the_grace(table, caplog, late, fires):
    table.rows = [row()]
    client = producer()
    trigger = module("integration.schedule").ScheduleTrigger(client)
    await trigger.tick(at(8, 59, 50))

    with caplog.at_level(logging.WARNING, logger="trackeverything.integration.schedule"):
        await trigger.tick(late)
    expected = ["schedule:weekday-digest:2026-10-13T09:00:00+09:00"] if fires else []
    assert [request.external_id for request in published(client)] == expected
    warned = [record for record in caplog.records if record.levelno == logging.WARNING and "weekday-digest" in record.getMessage()]
    assert bool(warned) is not fires

    await trigger.tick(at(9, 0, 0, day=14))
    assert published(client)[-1].external_id == "schedule:weekday-digest:2026-10-14T09:00:00+09:00"
    assert client.publish.await_count == len(expected) + 1


async def test_st04_follows_table_changes_on_the_next_cycle(table):
    client = producer()
    trigger = module("integration.schedule").ScheduleTrigger(client)
    a_before, a_after, b_payload = {"agent_name": "general", "request": "A1"}, {"agent_name": "general", "request": "A2"}, {"agent_name": "general", "request": "B"}

    table.rows = [row("a", "0 9 * * *", payload=a_before)]
    await trigger.tick(at(8, 0))
    table.rows = [row("a", "30 9 * * *", payload=a_before), row("b", "0 9 * * *", payload=b_payload)]
    await trigger.tick(at(8, 30))
    await trigger.tick(at(9, 0))
    assert [(request.external_id, request.payload) for request in published(client)] == [("schedule:b:2026-10-13T09:00:00+09:00", b_payload)]

    table.rows = [row("a", "30 9 * * *", payload=a_after), row("b", "0 9 * * *", payload=b_payload)]
    await trigger.tick(at(9, 10))
    await trigger.tick(at(9, 30))
    assert [(request.external_id, request.payload) for request in published(client)[1:]] == [("schedule:a:2026-10-13T09:30:00+09:00", a_after)]

    table.rows = [row("a", "30 9 * * *", payload=a_after)]
    await trigger.tick(at(9, 40))
    await trigger.tick(at(9, 0, day=14))
    assert client.publish.await_count == 2


@pytest.mark.parametrize(
    "invalid",
    [
        {"cron": "61 * * * *"},
        {"timezone": "Mars/Base"},
        {"payload": [1, 2]},
    ],
    ids=["cron", "timezone", "payload"],
)
async def test_st05_ignores_an_invalid_definition_without_affecting_others(table, caplog, invalid):
    table.rows = [row("broken", **invalid), row("valid")]
    client = producer()
    trigger = module("integration.schedule").ScheduleTrigger(client)

    with caplog.at_level(logging.ERROR, logger="trackeverything.integration.schedule"):
        await trigger.tick(at(8, 59, 50))
        await trigger.tick(at(9, 0, 0, 200_000))
    assert [request.external_id for request in published(client)] == ["schedule:valid:2026-10-13T09:00:00+09:00"]
    errors = [record for record in caplog.records if record.levelno == logging.ERROR and "broken" in record.getMessage()]
    assert len(errors) == 1


async def test_st06_keeps_the_previous_definitions_when_the_table_cannot_be_read(table, caplog):
    table.rows = [row()]
    table.loaded.side_effect = [None, RuntimeError("database is unavailable"), None]
    client = producer()
    trigger = module("integration.schedule").ScheduleTrigger(client)
    await trigger.tick(at(8, 59, 50))

    with caplog.at_level(logging.ERROR, logger="trackeverything.integration.schedule"):
        await trigger.tick(at(9, 0, 0, 200_000))
    assert [request.external_id for request in published(client)] == ["schedule:weekday-digest:2026-10-13T09:00:00+09:00"]
    assert [record for record in caplog.records if record.levelno == logging.ERROR]

    await trigger.tick(at(9, 0, 30))
    assert table.loaded.await_count == 3


async def test_st07_does_not_retry_a_failed_publish(table, caplog):
    table.rows = [row("first", "0 9 * * *"), row("second", "0 9 * * *")]
    client = producer()
    receipt = client.publish.return_value

    async def publish(request):
        if request.external_id.startswith("schedule:first:"):
            raise module("queue.contracts").QueueUnavailableError("Redis is unavailable")
        return receipt

    client.publish.side_effect = publish
    trigger = module("integration.schedule").ScheduleTrigger(client)
    await trigger.tick(at(8, 59, 50))

    with caplog.at_level(logging.ERROR, logger="trackeverything.integration.schedule"):
        await trigger.tick(at(9, 0, 0, 200_000))
    assert sorted(request.external_id for request in published(client)) == ["schedule:first:2026-10-13T09:00:00+09:00", "schedule:second:2026-10-13T09:00:00+09:00"]
    assert [record for record in caplog.records if record.levelno == logging.ERROR and "first" in record.getMessage()]

    await trigger.tick(at(9, 0, 40))
    assert client.publish.await_count == 2

    await trigger.tick(at(9, 0, day=14))
    assert sorted(request.external_id for request in published(client)[2:]) == ["schedule:first:2026-10-14T09:00:00+09:00", "schedule:second:2026-10-14T09:00:00+09:00"]


async def test_st08_queue_process_runs_the_schedule_listener_until_stopped(table, monkeypatch):
    app_module = module("api.app")
    client = producer()
    captured = []

    async def run_host(app):
        captured.append(app)

    monkeypatch.setattr(app_module, "require_schema_head", AsyncMock())
    monkeypatch.setattr(app_module.Producer, "from_env", classmethod(lambda cls: client))
    monkeypatch.setattr(app_module, "run_host", run_host)
    await app_module.serve()
    [app] = captured
    [schedule] = [connector for connector in app.connectors if connector.name == "schedule"]
    assert isinstance(schedule.listener, module("integration.schedule").ScheduleTrigger)

    # 年に一度の時刻の行は、待機中(30 秒)に停止を受ける。
    table.rows = [row(cron="0 0 1 1 *")]
    first_read = asyncio.Event()
    table.loaded.side_effect = lambda: first_read.set()
    trigger = module("integration.schedule").ScheduleTrigger(client)
    await trigger.start()
    await asyncio.wait_for(first_read.wait(), 1)
    await asyncio.wait_for(trigger.stop(), 1)
    reads = table.loaded.await_count
    await asyncio.sleep(0.05)
    assert table.loaded.await_count == reads == 1
    client.publish.assert_not_awaited()


def trigger_for(client):
    return module("integration.schedule").ScheduleTrigger(client)


async def test_rv01_fires_a_reservation_once_at_its_time(table, reserved):
    reserved.rows = [reservation()]
    client = producer()
    trigger = trigger_for(client)

    wait = await trigger.tick(at(8, 59, 50))
    assert published(client) == []
    assert wait == pytest.approx(10.0)

    await trigger.tick(at(9, 0, 0, 200_000))
    [request] = published(client)
    assert request.provider == "internal"
    assert request.external_id == "reservation:remind:2026-10-13T00:00:00+00:00"
    assert request.payload == REMIND
    assert request.session_id is None and request.parent_session_id is None

    # 行が残っていても(発火した run が消す前)、二度目は投入しない。
    await trigger.tick(at(9, 0, 30))
    await trigger.tick(at(9, 0, 0, day=14))
    assert client.publish.await_count == 1


async def test_rv02_does_not_fire_a_reservation_already_past_at_the_first_read(table, reserved):
    reserved.rows = [reservation()]
    client = producer()

    late = trigger_for(client)
    await late.tick(at(9, 0, 20))
    await late.tick(at(9, 0, 50))
    assert published(client) == []

    # 発火の直後に queue が再起動しても、新しい trigger は同じ予約を投入しない。
    fired = trigger_for(client)
    await fired.tick(at(8, 59, 50))
    await fired.tick(at(9, 0, 0, 200_000))
    restarted = trigger_for(client)
    await restarted.tick(at(9, 0, 20))
    assert [request.external_id for request in published(client)] == ["reservation:remind:2026-10-13T00:00:00+00:00"]


@pytest.mark.parametrize(("late", "fires"), [(at(9, 1, 0), True), (at(9, 1, 1), False)])
async def test_rv03_skips_a_reservation_later_than_the_grace(table, reserved, caplog, late, fires):
    reserved.rows = [reservation()]
    client = producer()
    trigger = trigger_for(client)
    await trigger.tick(at(8, 59, 50))

    with caplog.at_level(logging.WARNING, logger="trackeverything.integration.schedule"):
        await trigger.tick(late)
    expected = ["reservation:remind:2026-10-13T00:00:00+00:00"] if fires else []
    assert [request.external_id for request in published(client)] == expected
    warned = [record for record in caplog.records if record.levelno == logging.WARNING and "remind" in record.getMessage()]
    assert bool(warned) is not fires

    await trigger.tick(at(9, 2, 0))
    await trigger.tick(at(9, 0, 0, day=14))
    assert client.publish.await_count == len(expected)


async def test_rv04_follows_reservation_changes_on_the_next_cycle(table, reserved):
    client = producer()
    trigger = trigger_for(client)
    a_before, a_after = {"agent_name": "general", "request": "A1"}, {"agent_name": "general", "request": "A2"}
    b_payload, c_payload = {"agent_name": "general", "request": "B"}, {"agent_name": "general", "request": "C"}

    reserved.rows = [reservation("a", at(9, 0), a_before), reservation("c", at(10, 0), c_payload)]
    await trigger.tick(at(8, 0))
    reserved.rows = [reservation("a", at(9, 30), a_before), reservation("b", at(9, 0), b_payload), reservation("c", at(10, 0), c_payload)]
    await trigger.tick(at(8, 30))
    await trigger.tick(at(9, 0))
    assert [(request.external_id, request.payload) for request in published(client)] == [("reservation:b:2026-10-13T00:00:00+00:00", b_payload)]

    reserved.rows = [reservation("a", at(9, 30), a_after), reservation("b", at(9, 0), b_payload), reservation("c", at(10, 0), c_payload)]
    await trigger.tick(at(9, 10))
    await trigger.tick(at(9, 30))
    assert [(request.external_id, request.payload) for request in published(client)[1:]] == [("reservation:a:2026-10-13T00:30:00+00:00", a_after)]

    # 取り消した予約は時刻になっても投入しない。
    reserved.rows = [reservation("a", at(9, 30), a_after), reservation("b", at(9, 0), b_payload)]
    await trigger.tick(at(9, 50))
    await trigger.tick(at(10, 0))
    assert client.publish.await_count == 2


@pytest.mark.parametrize(
    "invalid",
    [
        {"id_": "broken", "payload": [1, 2]},
        {"id_": "x" * 240},
    ],
    ids=["payload", "external_id"],
)
async def test_rv05_ignores_an_invalid_reservation_without_affecting_others(table, reserved, caplog, invalid):
    broken = reservation(**invalid)
    reserved.rows = [broken, reservation("valid")]
    client = producer()
    trigger = trigger_for(client)

    with caplog.at_level(logging.ERROR, logger="trackeverything.integration.schedule"):
        await trigger.tick(at(8, 59, 50))
        await trigger.tick(at(9, 0, 0, 200_000))
    assert [request.external_id for request in published(client)] == ["reservation:valid:2026-10-13T00:00:00+00:00"]
    errors = [record for record in caplog.records if record.levelno == logging.ERROR and broken.id in record.getMessage()]
    assert len(errors) == 1


async def test_rv06_a_reservation_read_failure_does_not_affect_the_cron_table(table, reserved, caplog):
    table.rows = [row("daily", "0 9 * * *")]
    reserved.rows = [reservation()]
    reserved.loaded.side_effect = [None, RuntimeError("database is unavailable"), None]
    client = producer()
    trigger = trigger_for(client)
    await trigger.tick(at(8, 59, 50))

    with caplog.at_level(logging.ERROR, logger="trackeverything.integration.schedule"):
        await trigger.tick(at(9, 0, 0, 200_000))
    assert sorted(request.external_id for request in published(client)) == [
        "reservation:remind:2026-10-13T00:00:00+00:00",
        "schedule:daily:2026-10-13T09:00:00+09:00",
    ]
    assert [record for record in caplog.records if record.levelno == logging.ERROR]
    assert table.loaded.await_count == 2

    await trigger.tick(at(9, 0, 30))
    assert reserved.loaded.await_count == 3


async def test_rv07_does_not_retry_a_failed_reservation_publish(table, reserved, caplog):
    reserved.rows = [reservation("first"), reservation("second")]
    client = producer()
    receipt = client.publish.return_value

    async def publish(request):
        if request.external_id.startswith("reservation:first:"):
            raise module("queue.contracts").QueueUnavailableError("Redis is unavailable")
        return receipt

    client.publish.side_effect = publish
    trigger = trigger_for(client)
    await trigger.tick(at(8, 59, 50))

    with caplog.at_level(logging.ERROR, logger="trackeverything.integration.schedule"):
        await trigger.tick(at(9, 0, 0, 200_000))
    assert sorted(request.external_id for request in published(client)) == [
        "reservation:first:2026-10-13T00:00:00+00:00",
        "reservation:second:2026-10-13T00:00:00+00:00",
    ]
    assert [record for record in caplog.records if record.levelno == logging.ERROR and "first" in record.getMessage()]

    await trigger.tick(at(9, 0, 40))
    assert client.publish.await_count == 2


async def test_rv08_cron_and_reservation_with_the_same_id_fire_independently(table, reserved):
    table.rows = [row("daily", "0 9 * * *")]
    reserved.rows = [reservation("daily", at(9, 0, 10))]
    client = producer()
    trigger = trigger_for(client)

    assert await trigger.tick(at(8, 59, 55)) == pytest.approx(5.0)
    assert await trigger.tick(at(9, 0, 0)) == pytest.approx(10.0)
    assert [request.external_id for request in published(client)] == ["schedule:daily:2026-10-13T09:00:00+09:00"]

    await trigger.tick(at(9, 0, 10))
    assert [request.external_id for request in published(client)[1:]] == ["reservation:daily:2026-10-13T00:00:10+00:00"]
    assert client.publish.await_count == 2
