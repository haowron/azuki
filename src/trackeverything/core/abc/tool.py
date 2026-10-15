"""Tool の実行契約と、成功・失敗を値として返す ToolResult。"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from trackeverything.core.abc.context import AgentContext


@dataclass(frozen=True)
class ToolResult:
    """成功フラグ、観測する出力、失敗時の説明を持つ実行結果。"""

    ok: bool
    output: Any = None
    error: str | None = None


class Tool(ABC):
    """name・description・arguments を宣言し、run を実装する共通 ABC。

    LLM へは子 Agent と同じ callable メタデータを渡す。
    副作用は BaseAgent.call_tool 経由で実行し、ToolResult として観測する。
    """

    # 具体 Tool が宣言する callable メタデータ。
    name: ClassVar[str]
    description: ClassVar[str]
    arguments: ClassVar[dict[str, Any]]

    def llm_args(self, args: dict[str, Any]) -> dict[str, Any]:
        """LLM 由来の引数を副作用なしで変換する。Python 制御の呼出しは通らない。"""
        return args

    @abstractmethod
    async def run(self, args: dict[str, Any], *, ctx: AgentContext | None = None) -> ToolResult:
        """成功・失敗を ToolResult で返す。漏れた例外は call_tool が失敗値にする。

        I/O は await で行い、event loop を止める同期呼出しをしない。

        ctx は Python 側で注入し、LLM の JSON 引数に含めない。
        共有 data を読み書きでき、単体呼出しでは ctx を省略できる。
        """
        ...

    def error_key(self, args: dict[str, Any]) -> str | None:
        """失敗追跡の目標 key。None は目標非特定(unaddressed)を意味する。

        既定は引数全体 — 同一引数での再試行成功だけが失敗を解消する。
        ``(project, name)`` のような狭い目標識別を持つ Tool は上書きする。
        unaddressed の失敗は同 tool の任意の成功で解消される
        (``core.builtin.domain.errors.ToolFailures`` の規約)。
        """
        return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
