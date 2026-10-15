"""queue/runtime の LaunchAgent 定義を指定ディレクトリへ生成する。登録・起動は行わない。"""

from __future__ import annotations

import argparse
import plistlib
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default=Path.home() / "Services/trackeverything")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.directory.expanduser().resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for process in ("queue", "runtime"):
        label = f"com.trackeverything.{process}"
        values = {
            "Label": label,
            "ProgramArguments": ["/bin/bash", str(root / "scripts/run_service.sh"), process],
            "WorkingDirectory": str(root),
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(root / f"data/{process}.log"),
            "StandardErrorPath": str(root / f"data/{process}.err.log"),
        }
        path = args.output_dir / f"{label}.plist"
        with path.open("wb") as stream:
            plistlib.dump(values, stream)
        print(path)


if __name__ == "__main__":
    main()
