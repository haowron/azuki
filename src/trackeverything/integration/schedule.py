"""定時発火。PostgreSQL の ``schedule`` 表(cron)と ``reservation`` 表(一回限りの予約)を読み、時刻に名前指定の実行を投入する。

queue プロセスの常駐接続(``Connector("schedule")`` の listener)として一つの task で周回する。
表は読むだけで書かない。次の発火時刻はプロセス内だけに持ち、起動前・停止中・睡眠中に過ぎた時刻は補わない。
予約は初めて読んだ時点で未来の時刻だけを一度発火し、発火・見送りの後は行が残っていても発火しない(行は発火した run が消す)。
payload の中身は判断せず、``internal`` の ``EventRequest`` としてそのまま投入する(解釈は dispatch Agent)。

macOS の ``time.monotonic()`` は睡眠中に進まず、asyncio の timer はこの時計で待つ。長い一回の待機は
睡眠の分だけ遅れるため、待機を ``POLL_SECONDS`` 以下に区切り、各周回で壁時計と比べて判断する。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cronsim import CronSim, CronSimError
from sqlalchemy import select

import trackeverything.config as config
from trackeverything.core.abc.submission import EventRequest
from trackeverything.core.orm import sessions_for
from trackeverything.integration.connector import Connector
from trackeverything.queue.models import Reservation, Schedule
from trackeverything.queue.producer import Producer

log = logging.getLogger(__name__)

#: 一回の待機の上限(秒)。表の変更の反映と、睡眠から戻った後の判断の遅れの上限になる。
POLL_SECONDS = 30.0
#: これを超えて遅れた発火時刻は投入しない。
GRACE = timedelta(seconds=60)


@dataclass(frozen=True)
class ScheduleRow:
    """``schedule`` 表の一行。``payload`` は JSONB の値をそのまま持つ。"""

    id: str
    cron: str
    timezone: str
    payload: Any


@dataclass(frozen=True)
class ReservationRow:
    """``reservation`` 表の一行。``at`` は時間帯付きの時刻、``payload`` は JSONB の値をそのまま持つ。"""

    id: str
    at: datetime
    payload: Any


def _dsn() -> str:
    dsn = config.DATABASE_URL.strip()
    if not dsn:
        raise ValueError("DATABASE_URL is required: PostgreSQL is the only persistence backend")
    return dsn


async def load_schedules() -> list[ScheduleRow]:
    """``schedule`` 表の全行を id 順で読む。この表へは書き込まない。"""
    async with sessions_for(_dsn())() as session:
        rows = await session.scalars(select(Schedule).order_by(Schedule.id))
        return [ScheduleRow(row.id, row.cron, row.timezone, row.payload) for row in rows]


async def load_reservations() -> list[ReservationRow]:
    """``reservation`` 表の全行を id 順で読む。この表へは書き込まない。"""
    async with sessions_for(_dsn())() as session:
        rows = await session.scalars(select(Reservation).order_by(Reservation.id))
        return [ReservationRow(row.id, row.at, row.payload) for row in rows]


def _following(cron: str, zone: ZoneInfo, now: datetime) -> datetime:
    """``now`` より後の最初の時刻(``zone`` の時刻)。"""
    try:
        moment = next(CronSim(cron, now.astimezone(zone)), None)
    except CronSimError as exc:
        raise ValueError(f"invalid cron {cron!r}: {exc}") from exc
    if moment is None:
        raise ValueError(f"cron {cron!r} has no time within 50 years")
    return moment


def _request(row: ScheduleRow, moment: datetime) -> EventRequest:
    return EventRequest(provider="internal", external_id=f"schedule:{row.id}:{moment.isoformat()}", payload=row.payload)


def _reservation_request(row: ReservationRow) -> EventRequest:
    return EventRequest(provider="internal", external_id=f"reservation:{row.id}:{row.at.astimezone(UTC).isoformat()}", payload=row.payload)


@dataclass
class _Entry:
    row: ScheduleRow
    zone: ZoneInfo
    next_at: datetime


@dataclass
class _Once:
    """予約一件の状態。``next_at`` が None なら発火・見送りを終えたか、初めて読んだ時点で過ぎていた。"""

    row: ReservationRow
    next_at: datetime | None


def _entry(row: ScheduleRow, now: datetime) -> _Entry:
    """定義を確かめ、``now`` より後の最初の時刻を次の発火にする。不正な定義は ``ValueError``。"""
    if len(row.cron.split()) != 5:
        raise ValueError(f"cron {row.cron!r} must have five fields")
    try:
        zone = ZoneInfo(row.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone {row.timezone!r}") from exc
    next_at = _following(row.cron, zone, now)
    # payload と external_id は投入時と同じ規則で確かめる(pydantic の ValidationError は ValueError)。
    _request(row, next_at)
    return _Entry(row, zone, next_at)


class ScheduleTrigger:
    """表の定義に従って投入する常駐接続。Host の event loop 上で開始・停止する。"""

    def __init__(self, producer: Producer) -> None:
        self._producer = producer
        self._entries: dict[str, _Entry] = {}
        self._reservations: dict[str, _Once] = {}
        # 不正な定義は変わるまで error を繰り返さない。
        self._invalid: dict[str, ScheduleRow] = {}
        self._invalid_reservations: dict[str, ReservationRow] = {}
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="schedule-trigger")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            self._stop.set()
            await task

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                wait = await self.tick(datetime.now(UTC))
            except Exception:
                log.exception("schedule cycle failed")
                wait = POLL_SECONDS
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), wait)

    async def tick(self, now: datetime) -> float:
        """一回の周回。表を読んで定義を合わせ、過ぎた発火時刻を処理し、次に待つ秒数を返す。

        二つの表は別々に読み、読めなかった表だけ前の周回の定義を使う。
        """
        try:
            rows = await load_schedules()
        except Exception:
            log.exception("schedule table could not be read; keeping the previous definitions")
        else:
            self._sync(rows, now)
        try:
            reserved = await load_reservations()
        except Exception:
            log.exception("reservation table could not be read; keeping the previous reservations")
        else:
            self._sync_reservations(reserved, now)
        for entry in self._entries.values():
            if entry.next_at <= now:
                await self._fire(entry, now)
        for once in self._reservations.values():
            if once.next_at is not None and once.next_at <= now:
                await self._fire_once(once, now)
        upcoming = [entry.next_at for entry in self._entries.values()]
        upcoming += [once.next_at for once in self._reservations.values() if once.next_at is not None]
        return max(0.0, min([POLL_SECONDS, *((moment - now).total_seconds() for moment in upcoming)]))

    def _sync(self, rows: list[ScheduleRow], now: datetime) -> None:
        """変わらない定義は次の発火を保ち、新しい定義と変わった定義は ``now`` から数え直す。消えた行は忘れる。"""
        entries: dict[str, _Entry] = {}
        invalid: dict[str, ScheduleRow] = {}
        for row in rows:
            known = self._entries.get(row.id)
            if known is not None and known.row == row:
                entries[row.id] = known
            elif self._invalid.get(row.id) == row:
                invalid[row.id] = row
            else:
                try:
                    entries[row.id] = _entry(row, now)
                except ValueError as exc:
                    log.error("schedule %s is invalid and will not fire: %s", row.id, exc)
                    invalid[row.id] = row
        self._entries, self._invalid = entries, invalid

    def _sync_reservations(self, rows: list[ReservationRow], now: datetime) -> None:
        """変わらない予約は状態(発火済みを含む)を保ち、新しい予約と変わった予約は ``now`` と比べ直す。消えた行は忘れる。"""
        entries: dict[str, _Once] = {}
        invalid: dict[str, ReservationRow] = {}
        for row in rows:
            known = self._reservations.get(row.id)
            if known is not None and known.row == row:
                entries[row.id] = known
            elif self._invalid_reservations.get(row.id) == row:
                invalid[row.id] = row
            else:
                try:
                    # payload と external_id は投入時と同じ規則で確かめる(pydantic の ValidationError は ValueError)。
                    _reservation_request(row)
                except ValueError as exc:
                    log.error("reservation %s is invalid and will not fire: %s", row.id, exc)
                    invalid[row.id] = row
                    continue
                if row.at <= now:
                    log.info("reservation %s is past (%s) at its first read and will not fire", row.id, row.at.isoformat())
                entries[row.id] = _Once(row, row.at if row.at > now else None)
        self._reservations, self._invalid_reservations = entries, invalid

    async def _fire(self, entry: _Entry, now: datetime) -> None:
        """過ぎた発火時刻を一度だけ処理する。遅れが猶予を超えれば見送り、投入の失敗は再試行しない。"""
        moment, entry.next_at = entry.next_at, _following(entry.row.cron, entry.zone, now)
        await self._publish(f"schedule {entry.row.id}", moment, _request(entry.row, moment), now)

    async def _fire_once(self, once: _Once, now: datetime) -> None:
        """予約を一度だけ処理する。以後は行が残っていても発火しない。"""
        once.next_at = None
        await self._publish(f"reservation {once.row.id}", once.row.at, _reservation_request(once.row), now)

    async def _publish(self, name: str, moment: datetime, request: EventRequest, now: datetime) -> None:
        late = now - moment
        if late > GRACE:
            log.warning("%s skipped %s (%.0f s late)", name, moment.isoformat(), late.total_seconds())
            return
        try:
            receipt = await self._producer.publish(request)
        except Exception:
            log.exception("%s failed to publish %s", name, moment.isoformat())
            return
        log.info("%s published %s as %s", name, moment.isoformat(), receipt.message_id)


def schedule_connector(trigger: ScheduleTrigger) -> Connector:
    """定時発火の常駐接続。Webhook の route は持たない。"""
    return Connector("schedule", listener=trigger)
