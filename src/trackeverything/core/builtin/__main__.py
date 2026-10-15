"""runtime プロセスの入口。``python -m trackeverything.core.builtin`` で消費と実行を起動する。"""

from __future__ import annotations

import asyncio

from trackeverything.core.builtin.app import serve
from trackeverything.process import configure_logging


def main() -> None:
    configure_logging()
    asyncio.run(serve())


if __name__ == "__main__":
    main()
