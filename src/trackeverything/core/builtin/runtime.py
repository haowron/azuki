"""queue からメッセージを読み、最新の main の dispatch Agent を直接実行する実行基盤。transport は扱わない。

1 件ごとに版庫の ``refresh()`` で DB の main を読み、run の取得口(``repo.run()``)で dispatch の版を決めて
依頼(``EventRequest``)を ``AgentContext`` に変え、session_run の開始を記録してから dispatch Agent の ``run`` を
``RUN_TIMEOUT`` の範囲で実行し、終了を記録して取得口を解放する。子 Agent は ``ctx.agents`` から名前で得る。
Runtime は ACK しない。ACK・入口の選択・Source 定義の組立ては dispatch Agent が所有する。
同じ session の run も直列化せず並行に実行する。
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from contextlib import suppress
from copy import deepcopy
from typing import Any, Protocol

import trackeverything.config as config
from trackeverything.core.abc.call import AgentCall, AgentResult
from trackeverything.core.abc.context import AgentContext
from trackeverything.core.abc.submission import REQUEST_ID, EventRequest
from trackeverything.core.builtin.code_version import component_scope
from trackeverything.core.builtin.domain.errors import ErrorNote
from trackeverything.core.builtin.domain.execution_log import new_id
from trackeverything.core.builtin.session_store import PostgresSessionStore
from trackeverything.core.versioning.repo import Repo, RunVersions

log = logging.getLogger(__name__)


class MessageQueue(Protocol):
    """Runtime の読取り元。``next`` は 1 件(stream id と実行依頼)か、なければ None を返す。"""

    async def prepare(self) -> None: ...

    async def next(self) -> tuple[str, EventRequest] | None: ...

    async def ack(self, message_id: str) -> None: ...


class Runtime:
    """読取り loop・同時実行の上限・1 件の実行・停止を所有する。"""

    def __init__(self, queue: MessageQueue, repo: Repo, sessions: PostgresSessionStore, *,
                 concurrency: int = config.RUN_CONCURRENCY, max_steps: int = config.MAX_STEPS) -> None:
        if concurrency <= 0 or max_steps <= 0:
            raise ValueError("concurrency and max_steps must be positive")
        self.queue, self.repo, self.sessions = queue, repo, sessions
        self.max_steps = max_steps
        self._slots = asyncio.Semaphore(concurrency)
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = asyncio.Event()
        self._loop: asyncio.Task[None] | None = None
        self._started = False

    async def start(self) -> None:
        """consumer group を用意してから読取り loop を始める。"""
        if self._started:
            return
        self._started = True
        self._stopping.clear()
        await self.queue.prepare()
        self._loop = asyncio.create_task(self._read_loop(), name="trackeverything-runtime-read")

    async def stop(self) -> None:
        """新しい読取りを止め、実行中の全 task の終了を待ってから版庫を閉じる。"""
        if not self._started:
            return
        self._started = False
        self._stopping.set()
        if self._loop is not None:
            await asyncio.gather(self._loop, return_exceptions=True)
            self._loop = None
        while self._tasks:
            pending = tuple(self._tasks)
            await asyncio.gather(*pending, return_exceptions=True)
            # 全て完了済みの gather は loop へ制御を返さず done callback が走らないため、ここで外す。
            self._tasks.difference_update(pending)
        await self.repo.close()

    async def _read_loop(self) -> None:
        while not self._stopping.is_set():
            await self._slots.acquire()
            delivery = None
            try:
                if not self._stopping.is_set():
                    delivery = await self.queue.next()
            except Exception:
                log.exception("queue read failed")
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), 1)
            if delivery is None or self._stopping.is_set():
                # 停止の後に読んだメッセージは pending に残り、取り直しで再び実行される。
                self._slots.release()
                continue
            message_id, request = delivery
            context = contextvars.copy_context()
            context.run(REQUEST_ID.set, None)
            task = asyncio.create_task(self._handle(request, message_id), name=f"trackeverything-run-{message_id}", context=context)
            self._tasks.add(task)
            task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        self._slots.release()

    async def _handle(self, request: EventRequest, message_id: str) -> None:
        try:
            await self.execute(request, message_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("queue message %s remains pending", message_id)

    async def execute(self, request: EventRequest, message_id: str) -> AgentResult:
        """1 件を実行する。ACK は dispatch Agent が行い、ACK されなかったメッセージは pending に残って取り直される。"""
        token = REQUEST_ID.set(message_id)
        try:
            await self.repo.refresh()
            run = self.repo.run()
            try:
                result = await self._run(run, request, message_id)
            except Exception as exc:
                log.exception("queue message %s failed", message_id)
                result = _failed("execution", f"{type(exc).__name__}: {exc}")
            finally:
                self.repo.release(run)
        finally:
            REQUEST_ID.reset(token)
        return result

    async def _run(self, run: RunVersions, request: EventRequest, message_id: str) -> AgentResult:
        # イベントの原形と投入側が決めた session だけを dispatch Agent へ渡す。中身は解釈しない。
        name, payload, parent_session_id = config.DISPATCH_AGENT, None, request.parent_session_id
        version = run.version(name)
        data: dict[str, Any] = {"event": {"provider": request.provider, "external_id": request.external_id, "payload": dict(request.payload)}}
        run_id = new_id()
        session_id = request.session_id or new_id()
        ctx = AgentContext(
            data=deepcopy(data), max_steps=self.max_steps, run_id=run_id, session_id=session_id,
            parent_session_id=parent_session_id, message_id=message_id, acknowledger=self.queue.ack, agents=run,
        )
        agent = version.graph.agents.get(name) if name in version.graph.entrances else None
        # session_run は入口の版で記録する。
        with component_scope(version.label):
            try:
                await self.sessions.begin_run(run_id, session_id, agent_name=name, data=data, request=payload, parent_session_id=parent_session_id)
            except Exception:
                log.exception("could not record the run start (run_id=%s)", run_id)
                return _failed("session", "could not record the run start")
            try:
                result = await self._call(agent, name, AgentCall(ctx=ctx, request=payload))
            except asyncio.CancelledError:
                cancelled = AgentResult(status="cancelled", errors=(ErrorNote("cancellation", "run cancelled by its caller"),))
                try:
                    await self.sessions.finish_run(run_id, cancelled)
                except Exception:
                    log.exception("could not record run cancellation (run_id=%s)", run_id)
                raise
            try:
                await self.sessions.finish_run(run_id, result)
            except Exception:
                # 結果は変えない。行は running のまま残り、次の起動時に interrupted になる。
                log.exception("could not record the run result (run_id=%s)", run_id)
        log.info("run finished (message_id=%s, run_id=%s, session_id=%s, agent=%s, status=%s)", message_id, run_id, session_id, name, result.status)
        return result

    async def _call(self, agent: Any, name: str, call: AgentCall) -> AgentResult:
        if agent is None:
            return _failed("entrance", f"unknown entrance: {name}")
        try:
            async with asyncio.timeout(config.RUN_TIMEOUT):
                result: AgentResult = await agent.run(call)
                return result
        except TimeoutError:
            return AgentResult(status="cancelled", errors=(ErrorNote("deadline", f"run exceeded {config.RUN_TIMEOUT:g}s"),))
        except Exception as exc:
            log.exception("agent failed (run_id=%s, agent=%s)", call.ctx.run_id, name)
            return _failed("execution", f"{type(exc).__name__}: {exc}")


def _failed(stage: str, cause: str) -> AgentResult:
    # session_run は response だけを保存するため、原因を response にも残す。
    return AgentResult(status="error", response=cause, errors=(ErrorNote(stage, cause),))
