"""queue プロセスの入口。``python -m trackeverything.api`` で HTTP と受信接続を起動する。"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import unquote

from trackeverything.api.app import serve
from trackeverything.process import configure_logging


class _ReportAccessLogFilter(logging.Filter):
    """報告のアクセス権にあたる path と query を通常のアクセスログから除く。"""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            target = args[2]
            decoded = unquote(target)
            while decoded != target:
                target, decoded = decoded, unquote(decoded)
            if "reports" in decoded.split("?", 1)[0].split("/"):
                record.args = (*args[:2], "/reports/[redacted]", *args[3:])
        return True


def main() -> None:
    configure_logging()
    logging.getLogger("uvicorn.access").addFilter(_ReportAccessLogFilter())
    asyncio.run(serve())


if __name__ == "__main__":
    main()
