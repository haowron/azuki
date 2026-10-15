"""観点 behavior: 本人の Slack・GitHub・Discord の行動から定型を見つけ、部品を加えて merge し、AI で代われるかを報告する。

行動の読取り(``collect_activity``)は Host のコードで、部品を import しない。仕様は docs/components/OPTIMIZER.md。
"""

from trackeverything.optimizer.behavior.check import BehaviorCheck
from trackeverything.optimizer.behavior.collect import collect_activity

__all__ = ["BehaviorCheck", "collect_activity"]
