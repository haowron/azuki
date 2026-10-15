"""構成ルート。設定から LLM Provider・共有サービス・部品の版庫・Host を composition する。

Host は具体的な Agent・Tool・Source を import せず、呼ばない。部品は git の版で、部品のグラフは版が所有する
``agent/compose.py`` が組み立て、Host は版庫(``Repo``)の main と run ごとの取得口を通して使う。
部品が新しい実行依頼を投入する口は Host の ``queue/child.py`` で、起動時に Producer を登録する。
HTTP と受信 SDK は別 process の api/app.py が構成する。
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import trackeverything.config as config
from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.harness import PostHarness, PreHarness
from trackeverything.core.abc.tool import Tool
from trackeverything.core.builtin.catalog import _declared_in, build_catalog
from trackeverything.core.builtin.code_version import load_code_version
from trackeverything.core.builtin.compose import HostServices, prepare_components
from trackeverything.core.builtin.database import open_execution_logger, open_session_store
from trackeverything.core.builtin.execution_log import read_recent_runs
from trackeverything.core.builtin.runtime import MessageQueue, Runtime
from trackeverything.core.builtin.session_store import PostgresSessionStore
from trackeverything.core.orm.connection import require_schema_head
from trackeverything.core.versioning.repo import Repo, open_repo
from trackeverything.core.versioning.store import ensure_working_copy
from trackeverything.env_setup import prepare_runtime_environment
from trackeverything.llm.compose import build_llms
from trackeverything.optimizer import Optimizer
from trackeverything.optimizer import install as install_optimizer
from trackeverything.process import run_host
from trackeverything.queue import child
from trackeverything.queue.consumer import QueueConsumer
from trackeverything.queue.producer import Producer

log = logging.getLogger("trackeverything")


class TrackingRuntime(Runtime):
    """Runtime に実行側の保守 tick・共有の main の同期・Producer の後始末を加えたアプリ構成。"""

    def __init__(self, queue: MessageQueue, repo: Repo, sessions: PostgresSessionStore, *,
                 tick_hook: Callable[[], Awaitable[None]] | None = None, reconcile_interval: float = config.RECONCILE_INTERVAL,
                 sync_interval: float = 0, concurrency: int = config.RUN_CONCURRENCY) -> None:
        super().__init__(queue, repo, sessions, concurrency=concurrency)
        self._tick_hook, self._interval, self._sync_interval = tick_hook, reconcile_interval, sync_interval
        self._tick_stop = asyncio.Event()
        self._periodic: list[asyncio.Task[None]] = []
        self.producer: Producer | None = None

    async def start(self) -> None:
        await super().start()
        self._tick_stop.clear()
        if self._tick_hook is not None and self._interval > 0:
            self._periodic.append(asyncio.create_task(self._every(self._interval, self._tick_hook, "runtime maintenance")))
        if self._sync_interval > 0:
            self._periodic.append(asyncio.create_task(self._every(self._sync_interval, self._sync, "component repository sync")))

    async def _sync(self) -> None:
        await self.repo.sync()

    async def _every(self, interval: float, action: Callable[[], Awaitable[None]], label: str) -> None:
        while not self._tick_stop.is_set():
            with suppress(TimeoutError):
                await asyncio.wait_for(self._tick_stop.wait(), interval)
            if not self._tick_stop.is_set():
                try:
                    await action()
                except Exception:
                    log.exception("%s failed", label)

    async def stop(self) -> None:
        self._tick_stop.set()
        periodic, self._periodic = self._periodic, []
        if periodic:
            await asyncio.gather(*periodic)
        try:
            await super().stop()
        finally:
            if self.producer is not None:
                await self.producer.close()


def _skills_override() -> Path | None:
    """``SKILLS_DIR`` を指定した場合だけ、その固定の位置を使う(既定は各版の file view)。"""
    return Path(config.SKILLS_DIR).expanduser().resolve() if config.SKILLS_DIR else None


def open_host_services() -> HostServices:
    """全ての版が共有する Host のサービス。runtime と検証 CLI が同じ方法で作る(構成時に I/O しない)。

    LLM client は ``max``・``high``・``low``・``flash`` の名前で版へ渡し、各 Agent は類の ``llm`` で一つを選ぶ。
    z.ai 構成では GLM-5.3 の effort 違いと GLM-5.3-Flash の ``high`` で、``ZAI_API_KEY`` がなければ四つとも主 client に退化する。
    全 client は同じ Semaphore の throttle と実行記録で包む。
    """
    logger = open_execution_logger()
    return HostServices(
        llms=build_llms(logger),
        logger=logger,
        session_store=open_session_store(),
        full_mesh=config.AGENT_FULL_MESH,
        skills_root=_skills_override(),
    )


async def build_runtime() -> TrackingRuntime:
    """Host を組み立て、未起動の :class:`TrackingRuntime` を返す。

    版庫はこのプロセスの singleton(``open_repo``)で、mirror の main を読み込む(main が無ければ共有の main を採用し、
    ローカル方式でそれもなければ package の既定の部品を最初の main にする)。読み込めなければ例外で起動を中止する。
    ローカル方式では人の作業コピー(``COMPONENTS_DIR``)がなければ clone して作る(失敗は warning で、起動は続ける)。
    Runtime は run の開始ごとに mirror の main を確かめ、run の取得口で dispatch と子 Agent を得る。
    ``COMPONENT_FETCH_INTERVAL`` ごとに共有の main を同期する。
    """
    log.info("starting TrackEverything")
    services = open_host_services()
    repo = open_repo(prepare_components(services))
    try:
        await repo.start()
        if not config.COMPONENT_REPO_URL:
            await _ensure_working_copy()
        producer = Producer.from_env()
    except BaseException:
        await repo.close()
        raise

    async def _tick_maintenance() -> None:
        """保守 tick。目録の公開と heartbeat だけを行う(部品の保守は ``schedule`` の Agent ``maintenance`` が行う)。"""
        await publish_catalog(runtime)
        await producer.stream.heartbeat()

    # optimizer は runtime プロセスだけに置く。版の入口 Agent ``optimizer`` がこれを呼び、版庫は singleton を使う。
    install_optimizer(Optimizer(llms=services.llms, read_runs=partial(read_recent_runs, config.DATABASE_URL)))
    try:
        runtime = TrackingRuntime(QueueConsumer(producer.stream), repo, services.session_store, tick_hook=_tick_maintenance,
                                  sync_interval=config.COMPONENT_FETCH_INTERVAL,
                                  concurrency=int(os.environ.get("RUN_CONCURRENCY", str(config.RUN_CONCURRENCY))))
    except BaseException:
        await producer.close()
        await repo.close()
        raise
    runtime.producer = producer
    return runtime


async def _ensure_working_copy() -> None:
    """ローカル方式の作業コピーを用意する。作れなければ warning を記録し、runtime は main で動き続ける。"""
    path = config.COMPONENTS_DIR
    try:
        if await ensure_working_copy(config.COMPONENT_REPO_DIR, path):
            log.info("created the component working copy %s", path)
    except Exception:
        log.warning("could not create the component working copy %s", path, exc_info=True)


async def publish_catalog(runtime: TrackingRuntime) -> None:
    """読込み済みの main の目録を Redis へ公開する。"""
    if runtime.producer is None:
        return
    version = runtime.repo.main_version
    modules = version.modules()
    graph = version.graph
    snapshot = build_catalog(graph.agents, graph.sources, graph.entrances, webhook_entrances=(config.DISPATCH_AGENT,),
                             declared_agents=_declared_in(modules, "trackeverything.agent", (BaseAgent,)),
                             declared_tools=_declared_in(modules, "trackeverything.tools", (Tool,)),
                             declared_harnesses=_declared_in(modules, "trackeverything.harnesses", (PreHarness, PostHarness))).model_dump(mode="json")
    snapshot.update(code_version=version.label, as_of=datetime.now(UTC).isoformat(), entrances=sorted(graph.entrances))
    await runtime.producer.stream.save_catalog(snapshot)


async def prepare_runtime_state(runtime: TrackingRuntime) -> None:
    """部品が実行中のメッセージを親として依頼を投入できるよう、Producer を ``queue/child.py`` に登録する。"""
    child.install(runtime.producer)


async def serve() -> None:
    """実行側だけを起動する。受付前に旧 run の記録を閉じる。"""
    await require_schema_head(config.DATABASE_URL)
    await prepare_runtime_environment()
    await load_code_version()
    await open_session_store().recover_interrupted_runs(before=datetime.now(UTC))
    runtime = await build_runtime()
    try:
        await prepare_runtime_state(runtime)
        await publish_catalog(runtime)
        await run_host(runtime)
    finally:
        try:
            await runtime.stop()
        finally:
            # 起動前に失敗した場合も版庫(読込み済みの版と file view)を閉じる。停止済みなら何もしない。
            await runtime.repo.close()
