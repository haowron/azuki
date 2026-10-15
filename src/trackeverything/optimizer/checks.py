"""観点の契約と登録口。

観点は観測を読み、改善点(``Finding``)を返す。LLM を使える。DB への書込み・外部への送信は行わず、
版の変更は全ての観点が版庫の singleton で行う(main から fork し、merge で共有の main を進める)。外部を読むのは ``behavior`` だけ。
観点の名前: ``errors``(実行の誤り)・``consolidation``(Tool・Agent・Skill の削除・統合)・``additions``(繰り返す作業の
Tool・Skill・Agent の追加と既存の説明)・``tuning``(実行の費用と時間の調整。予約していた ``prompts``・``models`` を含む)・
``behavior``(本人の Slack・GitHub・Discord の定型の引受けと報告)。全て実装済み。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from trackeverything.core.abc.llm import LLMClient
from trackeverything.optimizer.additions import AdditionsCheck
from trackeverything.optimizer.behavior import BehaviorCheck
from trackeverything.optimizer.consolidation import ConsolidationCheck
from trackeverything.optimizer.errors import ErrorsCheck
from trackeverything.optimizer.model import Finding, Observation
from trackeverything.optimizer.tuning import TuningCheck


@dataclass(frozen=True)
class CheckContext:
    """観点が受け取るもの。``llms`` は Host の LLM client(名前 → client)。

    ``merge`` が偽なら版を変える観点は fork で候補を残すだけで merge しない(main は変わらない)。
    """

    observation: Observation
    llms: Mapping[str, LLMClient]
    merge: bool = True


class Check(Protocol):
    """一つの観点。名前は登録した観点の中で一意。"""

    name: str

    async def run(self, ctx: CheckContext) -> Sequence[Finding]: ...


#: 実行する観点(登録順)。実装した観点だけをここに加える。
CHECKS: tuple[Check, ...] = (ErrorsCheck(), ConsolidationCheck(), AdditionsCheck(), TuningCheck(), BehaviorCheck())
