"""基類へ注入する実行ログの記録口。"""

from typing import Protocol

from trackeverything.core.builtin.domain.execution_log import LogEvent


class LogRecorder(Protocol):
    """記録先の実装を基類から分離する最小契約。"""

    async def record(self, event: LogEvent) -> None: ...
