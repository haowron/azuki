"""optimizer の実行: 観測 → 観点 → 報告。

runtime プロセスの構成ルートが一つを ``install`` し、版の入口 Agent ``optimizer`` が ``handle`` を呼ぶ。
Host のサービス(実行記録の読取り・LLM client)は関数と値で受け取り、main の版 id はこのプロセスの版庫(singleton)から得る。
部品は import しない。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from trackeverything.core.abc.call import AgentResult
from trackeverything.core.abc.llm import LLMClient
from trackeverything.core.builtin.domain.errors import ErrorNote
from trackeverything.core.builtin.domain.execution_log import current_run_id
from trackeverything.core.builtin.execution_log import RunTrace
from trackeverything.core.versioning.repo import current_repo
from trackeverything.optimizer.checks import CHECKS, Check, CheckContext
from trackeverything.optimizer.model import CheckOutcome, Observation, OptimizeRequest, Report

log = logging.getLogger(__name__)

#: 実行記録の読取り。``read_recent_runs`` を DSN で束ねたもの(``limit`` 件、新しい順)。
RunReader = Callable[..., Awaitable[Sequence[RunTrace]]]


class UnknownCheckError(ValueError):
    """登録していない観点の名前。"""


def _now() -> datetime:
    return datetime.now(UTC)


class Optimizer:
    """観測を一度読み、選んだ観点を登録順に一つずつ実行して報告にまとめる。"""

    def __init__(self, *, llms: Mapping[str, LLMClient], read_runs: RunReader, checks: Sequence[Check] = CHECKS,
                 clock: Callable[[], datetime] = _now) -> None:
        names = [check.name for check in checks]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate check names: {', '.join(duplicates)}")
        self._llms = llms
        self._read_runs = read_runs
        self._checks = tuple(checks)
        self._clock = clock

    def select(self, names: Sequence[str] | None) -> tuple[Check, ...]:
        """実行する観点。None は全て。未登録の名前は ``UnknownCheckError``。"""
        if names is None:
            return self._checks
        known = {check.name for check in self._checks}
        unknown = [name for name in names if name not in known]
        if unknown:
            raise UnknownCheckError(f"unknown check: {', '.join(unknown)}")
        wanted = set(names)
        return tuple(check for check in self._checks if check.name in wanted)

    async def observe(self, limit: int) -> Observation:
        """直近 ``limit`` 件の実行記録から、実行中の自身の run を除いた観測。"""
        runs = await self._read_runs(limit=limit)
        own = current_run_id.get()
        return Observation(observed_at=self._clock(), main_version=current_repo().main.id,
                           runs=tuple(run for run in runs if run.run_id != own))

    async def check(self, observation: Observation, checks: Sequence[Check], *, merge: bool = True) -> Report:
        """観点を順に実行する。観点の例外はその観点の失敗にし、取消しは伝える。``merge`` は ``CheckContext`` へ渡す。"""
        ctx = CheckContext(observation=observation, llms=self._llms, merge=merge)
        outcomes: list[CheckOutcome] = []
        for item in checks:
            try:
                findings = tuple(await item.run(ctx))
            except Exception as exc:
                log.exception("optimizer check %s failed", item.name)
                outcomes.append(CheckOutcome(name=item.name, status="failed", error=f"{type(exc).__name__}: {exc}"))
            else:
                outcomes.append(CheckOutcome(name=item.name, status="ok", findings=findings))
        return Report(observation=observation, checks=tuple(outcomes))


_installed: Optimizer | None = None


def install(optimizer: Optimizer | None) -> None:
    """このプロセスの optimizer を設定する(None で外す)。runtime の構成ルートが呼ぶ。"""
    global _installed
    _installed = optimizer


def _failed(stage: str, cause: str) -> AgentResult:
    return AgentResult(status="error", response=cause, errors=(ErrorNote(stage, cause),))


def _describe(error: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(part) for part in item['loc']) or 'data'}: {item['msg']}" for item in error.errors())


async def handle(data: Mapping[str, Any]) -> AgentResult:
    """名前指定の実行の ``data`` で一回実行し、報告を run の結果にする。"""
    optimizer = _installed
    if optimizer is None:
        return _failed("optimizer", "optimizer is not installed in this process")
    try:
        request = OptimizeRequest.model_validate(dict(data))
        checks = optimizer.select(request.checks)
    except ValidationError as exc:
        return _failed("input", _describe(exc))
    except UnknownCheckError as exc:
        return _failed("input", str(exc))
    try:
        observation = await optimizer.observe(request.limit)
    except Exception as exc:
        log.exception("optimizer observation failed")
        return _failed("observe", f"{type(exc).__name__}: {exc}")
    report = await optimizer.check(observation, checks, merge=request.merge)
    errors = tuple(ErrorNote(f"check:{outcome.name}", outcome.error or "") for outcome in report.checks if outcome.status == "failed")
    return AgentResult(status="error" if errors else "ok", response=report.to_json(), errors=errors)
