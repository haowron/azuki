"""Prepare process-level environment for tools that shell out.

Generated, idempotent setup that must exist before an agent composes shell
commands against git or the investigation CLIs. Called once at process start
(``main.serve``, awaited on the Host loop); each piece is independent and
best-effort — a failure is logged and skipped, never a startup abort:

- **git**: with ``GITHUB_TOKEN`` set, a global ``url.<auth>.insteadOf`` rule
  makes plain ``https://github.com/...`` clones/fetches authenticate. Written
  via ``git config --global`` (merges into an existing ``~/.gitconfig``) at
  runtime, so the token lives only in the service user's git config — never
  inside an LLM-visible command.
- **pi**: writes ``~/.pi/agent/models.json`` describing the GLM Coding Plan
  endpoint as an OpenAI-compatible provider. The key is referenced as
  ``$ZAI_API_KEY`` (pi expands env references), so no credential value ever
  lands in a file. The file is regenerated on every start.
- **workspace**: creates ``config.REPOS_DIR`` if missing.

Naming invariant: "Track / Tracking" everywhere, never "trace".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import trackeverything.config as config

log = logging.getLogger("trackeverything")
# git config は手元の設定ファイルを書くだけ。これを超えたら打ち切る。
_GIT_TIMEOUT = 10.0


async def prepare_runtime_environment() -> None:
    """Run every best-effort setup step; log and continue on each failure."""
    await _configure_git_auth()
    _write_pi_models()
    _ensure_workspace()


async def _configure_git_auth() -> None:
    """Let plain github.com URLs authenticate via a global insteadOf rule."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        return
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            "config",
            "--global",
            f"url.https://x-access-token:{token}@github.com/.insteadOf",
            "https://github.com/",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as e:
        log.warning("git auth setup skipped: %s", e)
        return
    try:
        async with asyncio.timeout(_GIT_TIMEOUT):
            await process.wait()
    except TimeoutError:
        process.kill()
        await process.wait()
        log.warning("git auth setup skipped: git config timed out")
        return
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    if process.returncode != 0:
        # stderr は token を含み得るため本文は出さない。
        log.warning("git auth setup skipped: git config exited %s", process.returncode)


def _write_pi_models() -> None:
    """Regenerate pi's model catalog with the GLM Coding Plan provider.

    Overwrites the file unconditionally: it is generated state, and a stale
    key/model must not survive a restart. A hand-maintained file is not a
    supported deployment — mount a fresh home instead.
    """
    if not os.environ.get("ZAI_API_KEY", "").strip():
        return
    doc = {
        "providers": {
            "glm": {
                "baseUrl": config.ZAI_BASE_URL,
                "api": "openai-completions",
                "apiKey": "$ZAI_API_KEY",
                "models": [
                    {"id": config.ZAI_DEFAULT_MODEL, "name": "GLM (Coding Plan)"},
                ],
            }
        }
    }
    try:
        path = Path.home() / ".pi" / "agent" / "models.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        log.warning("pi models.json setup skipped: %s", e)


def _ensure_workspace() -> None:
    """Create the shared command workspace root."""
    try:
        os.makedirs(config.REPOS_DIR, exist_ok=True)
    except OSError as e:
        log.warning("workspace setup skipped: %s", e)
