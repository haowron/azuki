"""optimizer の入力と、観測・観点・報告の値(I/O なし)。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from trackeverything.core.builtin.execution_log import RunTrace


class OptimizeRequest(BaseModel):
    """名前指定の実行の ``data``。``checks`` が None なら登録した観点を全て実行する。

    ``merge`` が偽なら版を変える観点は候補を fork で残すだけで merge しない。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    limit: int = Field(default=100, ge=1, le=1000)
    checks: list[str] | None = None
    merge: bool = True

    @field_validator("checks")
    @classmethod
    def _unique(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("check names must not repeat")
        return value


@dataclass(frozen=True)
class Observation:
    """観点が共有する観測。``runs`` は直近の実行記録(optimizer 自身の run を除く)。"""

    observed_at: datetime
    main_version: str
    runs: tuple[RunTrace, ...]


@dataclass(frozen=True)
class Finding:
    """観点が見つけた一つの改善点。``evidence`` は根拠の run_id など、``detail`` は観点ごとの JSON の詳細(任意)。"""

    summary: str
    evidence: tuple[str, ...] = ()
    detail: Mapping[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"summary": self.summary, "evidence": list(self.evidence)}
        if self.detail is not None:
            data["detail"] = dict(self.detail)
        return data


@dataclass(frozen=True)
class CheckOutcome:
    """一つの観点の結果。失敗した観点は ``error`` に ``<例外の型>: <message>`` を持つ。"""

    name: str
    status: Literal["ok", "failed"]
    findings: tuple[Finding, ...] = ()
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "status": self.status, "findings": [finding.to_json() for finding in self.findings]}
        if self.error is not None:
            data["error"] = self.error
        return data


@dataclass(frozen=True)
class Report:
    """一回の実行の報告。run の結果(``AgentResult.response``)として返す。"""

    observation: Observation
    checks: tuple[CheckOutcome, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "observed_at": self.observation.observed_at.isoformat(),
            "main_version": self.observation.main_version,
            "run_count": len(self.observation.runs),
            "checks": [outcome.to_json() for outcome in self.checks],
        }
