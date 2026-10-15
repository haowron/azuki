"""常駐サービスを起動しない投入コマンド。``python -m trackeverything.queue.cli publish --file <JSON>`` で使う。

JSON は ``EventRequest`` の形で、provider を問わない。dead stream の ``data`` はそのまま再投入できる。
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path

from trackeverything.core.abc.submission import EventRequest
from trackeverything.queue.contracts import PublishReceipt
from trackeverything.queue.producer import Producer


async def publish_file(producer: Producer, path: Path) -> PublishReceipt:
    request = EventRequest.model_validate_json(path.read_text())
    return await producer.publish(request)


async def run(args: argparse.Namespace) -> None:
    async with Producer.from_env() as producer:
        receipt = await publish_file(producer, Path(args.file))
        print(receipt.model_dump_json())


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m trackeverything.queue.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("--file", required=True)
    asyncio.run(run(parser.parse_args(argv)))


if __name__ == "__main__":
    main()
