"""運用の記録を観測し、Agent・Tool・Skill などの改善点を見つける Host の機能。

Host が所有し、版の 5 フォルダに含めない(optimizer 自身は候補の版に入らない)。runtime プロセスで動き、
dispatch の名前指定の実行 ``agent_name="optimizer"`` で起動する(docs/components/OPTIMIZER.md)。
"""

from trackeverything.optimizer.checks import CHECKS, Check, CheckContext
from trackeverything.optimizer.model import CheckOutcome, Finding, Observation, OptimizeRequest, Report
from trackeverything.optimizer.service import Optimizer, UnknownCheckError, handle, install

__all__ = [
    "CHECKS",
    "Check",
    "CheckContext",
    "CheckOutcome",
    "Finding",
    "Observation",
    "OptimizeRequest",
    "Optimizer",
    "Report",
    "UnknownCheckError",
    "handle",
    "install",
]
