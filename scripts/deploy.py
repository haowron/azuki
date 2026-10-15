"""main の配備と、macOS の定期配備 LaunchAgent を管理する。"""

from __future__ import annotations

import argparse
import fcntl
import os
import plistlib
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

LABEL = "com.trackeverything.autodeploy"
# ローカル配備の取得元。既定はこの script を含む checkout(開発 checkout の scripts/deploy.sh から実行する)。
DEFAULT_SOURCE = Path(__file__).resolve().parents[1]
GIT_TIMEOUT = 120
MIGRATION_TIMEOUT = 600


def log(message: str, *, error: bool = False) -> None:
    print(f"{datetime.now().astimezone().isoformat(timespec='seconds')} deploy: {message}", file=sys.stderr if error else sys.stdout, flush=True)


def git(directory: Path, *args: str) -> str:
    """認証入力待ちと通信の無期限待ちを防ぐ。出力に資格情報を表示しない。"""
    return subprocess.run(
        ["git", "-C", str(directory), *args],
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        capture_output=True,
        text=True,
        check=True,
        timeout=GIT_TIMEOUT,
    ).stdout.strip()


def migrate(directory: Path) -> None:
    """配備 checkout の schema を Alembic の head へ上げる。アプリと同じ環境(run_local.sh)で実行する。"""
    try:
        result = subprocess.run(
            ["bash", str(directory / "scripts" / "run_local.sh"), "alembic", "upgrade", "head"],
            cwd=directory,
            capture_output=True,
            text=True,
            check=False,
            timeout=MIGRATION_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"schema の移行が {MIGRATION_TIMEOUT} 秒でタイムアウトしました。") from exc
    if result.returncode != 0:
        tail = "\n".join((result.stderr or result.stdout).strip().splitlines()[-5:])
        raise ValueError(f"schema の移行に失敗しました (終了コード {result.returncode}):\n{tail}")


@contextmanager
def deployment_lock(directory: Path) -> Iterator[None]:
    """配備と LaunchAgent の再登録を同じロックで直列化する。"""
    common_dir = Path(git(directory, "rev-parse", "--git-common-dir"))
    if not common_dir.is_absolute():
        common_dir = directory / common_dir
    # ロックファイルは残す。削除すると、待機中のプロセスと新規呼出しの inode が分離する。
    with (common_dir / "trackeverything-deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def deploy(directory: Path, source: Path, *, remote: bool) -> None:
    """取得・判定・更新を、手動配備と定期チェックで共通のロックに収める。"""
    with deployment_lock(directory):
        current = git(directory, "rev-parse", "HEAD")
        git(directory, "fetch", "--quiet", "--no-tags", "origin" if remote else str(source), "refs/heads/main")
        target = git(directory, "rev-parse", "FETCH_HEAD")
        if current == target:
            log(f"main は配備済み ({current[:12]})")
            return
        if remote:
            try:
                git(directory, "merge-base", "--is-ancestor", current, target)
            except subprocess.CalledProcessError as exc:
                if exc.returncode != 1:
                    raise
                raise ValueError("main が fast-forward ではありません。履歴の巻き戻し・分岐を確認してください。") from exc
        if git(directory, "status", "--porcelain", "--untracked-files=normal"):
            raise ValueError("配備先に未コミットの変更があります。変更を整理してから再実行してください。")
        git(directory, "reset", "--hard", target)
        try:
            migrate(directory)
        except (OSError, ValueError):
            # 旧版のまま動かし続ける。schema が head でない版へ再起動させない。
            git(directory, "reset", "--hard", current)
            log(f"schema の移行に失敗したため {current[:12]} に戻しました。", error=True)
            raise
        log(f"main を配備しました ({current[:12]} → {target[:12]})。schema は head です。監視対象の変更は supervisor が自動再起動します。")


def install(directory: Path, agents_dir: Path, interval: int, *, start: bool) -> None:
    """配備 checkout だけで動く低頻度チェックを登録する。"""
    with deployment_lock(directory):
        script = directory / "scripts/deploy.py"
        if not script.is_file():
            raise ValueError("先に scripts/deploy.py を配備 checkout へ配備してください。")
        logs = directory / "data"
        logs.mkdir(exist_ok=True)
        for name in ("autodeploy.log", "autodeploy.err.log"):
            path = logs / name
            path.touch(mode=0o600, exist_ok=True)
            path.chmod(0o600)
        agent = {
            "Label": LABEL,
            # venv のリンクを解決し、Documents 外にある実際の Python で標準ライブラリだけを使う。
            "ProgramArguments": [str(Path(sys.executable).resolve()), str(script), "--remote", "--directory", str(directory)],
            "WorkingDirectory": str(directory),
            "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")},
            "RunAtLoad": True,
            "StartInterval": interval,
            "StandardOutPath": str(logs / "autodeploy.log"),
            "StandardErrorPath": str(logs / "autodeploy.err.log"),
        }
        agents_dir.mkdir(parents=True, exist_ok=True)
        path = agents_dir / f"{LABEL}.plist"
        temporary = path.with_suffix(".plist.tmp")
        temporary.write_bytes(plistlib.dumps(agent))
        temporary.chmod(0o600)
        temporary.replace(path)
        if start:
            domain = f"gui/{os.getuid()}"
            subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True, check=False)
            subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
        log(f"定期配備を設定しました ({interval} 秒ごと): {path}")


def positive_integer(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("周期は正の秒数を指定してください。")
    return number


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--remote", action="store_true", help="origin の main を取得し、fast-forward の変更を配備する")
    mode.add_argument("--install", action="store_true", help="定期チェックの LaunchAgent を生成する")
    parser.add_argument("--directory", type=Path, default=Path(os.environ.get("TE_DEPLOY_DIR", str(Path.home() / "Services/trackeverything"))))
    parser.add_argument("--source", type=Path, default=Path(os.environ.get("TE_DEPLOY_SOURCE", str(DEFAULT_SOURCE))))
    parser.add_argument("--agents-dir", type=Path, default=Path.home() / "Library/LaunchAgents")
    parser.add_argument("--interval", type=positive_integer, default=1800, help="チェック周期の秒数 (既定: 1800)")
    parser.add_argument("--start", action="store_true", help="--install とともに指定し、登録した LaunchAgent を開始する")
    args = parser.parse_args()
    if args.start and not args.install:
        parser.error("--start は --install とともに指定してください。")
    try:
        directory = args.directory.resolve()
        if args.install:
            install(directory, args.agents_dir.resolve(), args.interval, start=args.start)
        else:
            deploy(directory, args.source.resolve(), remote=args.remote)
    except subprocess.CalledProcessError as exc:
        # Git の stderr には remote URL の資格情報が含まれる可能性がある。
        log(f"操作が失敗しました (終了コード {exc.returncode})。Git の接続と配備先を確認してください。", error=True)
        raise SystemExit(1) from exc
    except subprocess.TimeoutExpired as exc:
        log(f"Git 操作が {GIT_TIMEOUT} 秒でタイムアウトしました。次回のチェックで再試行します。", error=True)
        raise SystemExit(1) from exc
    except (OSError, ValueError) as exc:
        log(str(exc), error=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
