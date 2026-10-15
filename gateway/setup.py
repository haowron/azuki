"""共通 nginx と検証サービスの所有者専用設定・LaunchAgent を準備する。"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import secrets
import shutil
import subprocess
from pathlib import Path

LABELS = ("com.sharedgateway.auth", "com.sharedgateway.nginx")
ROOT = Path(__file__).resolve().parent
REPORT_READ_PATTERNS = [r"/reports/[0-9a-f]{64}\.html", r"/reports/assets/[0-9a-f]{64}/mermaid\.min\.js"]


def private_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
    path.chmod(0o600)


def nginx_config(directory: Path, prefix: str) -> str:
    """prefix は URL のルーティング情報であり、認証条件にしない。"""
    if prefix and not re.fullmatch(r"/[A-Za-z0-9/_-]+", prefix):
        raise ValueError("public prefix must be a simple absolute path")
    # $uri は percent encoding を復号済み。検証器には元の path を渡して、
    # 公開読取りの正規 path 判定を nginx の書換えで迂回させない。
    strip_prefix = f"        ~^{prefix}(/.*)$ $1;" if prefix else ""
    # 画面の資産・API は document URL 基準の相対 path なので、prefix 末尾の
    # 「/」なし入口は付けた場所へ一度だけ向け直す(prefix なしの相対解決を防ぐ)。
    slash_redirect = f"        location = {prefix} {{ return 302 {prefix}/; }}\n" if prefix else ""
    return f'''worker_processes 1;
daemon off;
pid "{directory}/nginx.pid";
error_log "{directory}/logs/nginx-error.log" crit;
events {{ worker_connections 1024; }}
http {{
    map $request_uri $upstream_uri {{
        default $request_uri;
{strip_prefix}
    }}
    map $uri $safe_uri {{
        default $uri;
        ~(^|/)reports/ /reports/[redacted];
        ~% /[redacted];
        "~*[0-9a-f]{{64}}" /reports/[redacted];
    }}
    log_format safe '$remote_addr [$time_local] "$request_method $safe_uri $server_protocol" $status $body_bytes_sent';
    access_log "{directory}/logs/nginx-access.log" safe;
    client_max_body_size 32m;
    client_body_temp_path "{directory}/body";
    proxy_temp_path "{directory}/proxy";
    server {{
        listen 127.0.0.1:8080;
        listen [::1]:8080;
        server_name _;
{slash_redirect}        location / {{
            proxy_pass http://127.0.0.1:18081$upstream_uri;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header Connection "";
            proxy_set_header X-Forwarded-For $remote_addr;
            proxy_set_header X-Gateway-Url-Key "";
            proxy_next_upstream off;
            proxy_connect_timeout 5s;
            proxy_read_timeout 300s;
            proxy_send_timeout 300s;
            proxy_buffering off;
        }}
    }}
}}
'''


def import_login_password(config: dict, env_text: str) -> bool:
    """`.env` の `GATEWAY_LOGIN_PASSWORD` を login_password へ反映する。

    値が未定義・空のときは既存値をそのまま保持する(未設定で入口が閉じた
    状態も維持する)。反映したら True を返す。WEBHOOK_TOKEN と違い、
    `--upstream-env` を渡すたびに現在値へ同期する。
    """
    for line in env_text.splitlines():
        if line.startswith("GATEWAY_LOGIN_PASSWORD="):
            value = line.partition("=")[2].strip().strip("\"'")
            if value:
                config.setdefault("credentials", {})["login_password"] = value
                return True
            return False
    return False


def install(directory: Path, prefix: str | None, upstream_env: Path | None, public_origin: str | None = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    for name in ("logs", "body", "proxy"):
        (directory / name).mkdir(exist_ok=True)
        (directory / name).chmod(0o700)
    config_path = directory / "config.json"
    if config_path.exists():
        config = json.loads(config_path.read_text())
    else:
        upstream_headers: dict[str, str] = {}
        if upstream_env is not None:
            for line in upstream_env.read_text().splitlines():
                if line.startswith("WEBHOOK_TOKEN="):
                    value = line.partition("=")[2].strip().strip("\"'")
                    if value:
                        upstream_headers["X-Webhook-Token"] = value
        config = {
            "credentials": {"bearer_token": secrets.token_hex(32), "github_secret": secrets.token_hex(32), "slack_signing_secret": ""},
            "gateway_port": 8080,
            "auth_port": 18081,
            "max_body_bytes": 32 * 1024 * 1024,
            "public_path_prefix": "",
            "routes": [{"prefix": "/", "upstream": "http://127.0.0.1:18080", "headers": upstream_headers}],
        }
    if prefix is not None:
        config["public_path_prefix"] = prefix.rstrip("/")
    # login-key 発行応答の url を組み立てる公開 origin(例: https://track.example.com)。
    # 既定では未設定で、その間は path だけを応答する。
    if public_origin is not None:
        config["public_origin"] = public_origin.rstrip("/")
    # このアプリの既定ルートだけを対象にする。他サービスの設定は保持する。
    for route in config.get("routes", []):
        if route.get("prefix") == "/" and route.get("upstream", "").rstrip("/") == "http://127.0.0.1:18080":
            route["public_read_patterns"] = list(REPORT_READ_PATTERNS)
    # 共有 login password は .env が正本。--upstream-env を渡すたびに同期する。
    if upstream_env is not None:
        import_login_password(config, upstream_env.read_text())
    nginx_text = nginx_config(directory, config.get("public_path_prefix", ""))
    private_write(config_path, (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode())
    private_write(directory / "nginx.conf", nginx_text.encode())
    # launchd は Documents 配下を読めない環境もあるため、単体 runtime を所有者専用領域へ配置する。
    private_write(directory / "server.py", (ROOT / "server.py").read_bytes())
    for name in ("nginx-access.log", "nginx-error.log", "auth-stdout.log", "auth-stderr.log", "nginx-stdout.log", "nginx-stderr.log"):
        log = directory / "logs" / name
        if not log.exists():
            private_write(log, b"")
        log.chmod(0o600)
    nginx = shutil.which("nginx") or "/opt/homebrew/opt/nginx/bin/nginx"
    uv = shutil.which("uv")
    if uv is None:
        raise ValueError("uv executable not found")
    subprocess.run([nginx, "-t", "-p", str(directory) + "/", "-c", str(directory / "nginx.conf")], check=True)
    commands = (
        [uv, "run", "--no-project", "--script", str(directory / "server.py"), "--config", str(config_path), "--port", "18081"],
        [nginx, "-p", str(directory) + "/", "-c", str(directory / "nginx.conf")],
    )
    agents = Path.home() / "Library/LaunchAgents"
    for label, command, name in zip(LABELS, commands, ("auth", "nginx"), strict=True):
        plist = {
            "Label": label,
            "ProgramArguments": command,
            "RunAtLoad": True,
            "KeepAlive": True,
            "WorkingDirectory": str(directory),
            "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            "StandardOutPath": str(directory / "logs" / f"{name}-stdout.log"),
            "StandardErrorPath": str(directory / "logs" / f"{name}-stderr.log"),
        }
        private_write(agents / f"{label}.plist", plistlib.dumps(plist))
    print(f"Private configuration: {config_path}")
    print("Ports: nginx=8080, verifier=18081, default upstream=18080")


def activate(directory: Path) -> None:
    domain = f"gui/{os.getuid()}"
    for label in LABELS:
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True, check=False)
        subprocess.run(["launchctl", "bootstrap", domain, str(Path.home() / "Library/LaunchAgents" / f"{label}.plist")], check=True)
    print(f"Started local gateway; private logs: {directory / 'logs'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path.home() / "Library/Application Support/SharedGateway")
    parser.add_argument("--public-prefix")
    parser.add_argument("--public-origin", help="login-key 応答の url に使う公開 origin (例: https://track.example.com)")
    parser.add_argument("--upstream-env", type=Path)
    parser.add_argument("--start", action="store_true")
    args = parser.parse_args()
    install(args.directory.resolve(), args.public_prefix, args.upstream_env, args.public_origin)
    if args.start:
        activate(args.directory.resolve())


if __name__ == "__main__":
    main()
