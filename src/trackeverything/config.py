"""Application configuration — code-owned policy plus deployment paths.

This module is the single place application settings are defined. Callers
import it as a module (``import trackeverything.config as config``), so there
is no second loader or mirrored configuration object.

Resolution rules:

- **Deployment paths** — ``DOCS_DIR``, ``PROJECTS_DIR``, ``ROLE_PATH``, workspace paths
  and the review CLI/material/environment paths may be overridden by env.
- **Application policy** — provider endpoints/models, request timeout, LLM
  concurrency, the run-wide step budget, and the jobstore policy are code-owned
  constants. The environment does not configure them.
- **Credentials** remain environment variables because they must not be
  committed; credential selection belongs to the composition root.
- **Codex 実行設定** — implementation のモデル・既定推理強度は同名の環境変数で
  上書きでき、Codex のユーザー設定より優先する。fast は current run の依頼で指定する。

Naming invariant: "Track / Tracking" everywhere, never "trace".
"""

from __future__ import annotations

import os
from pathlib import Path

# --- persistence backend routing ---

# PostgreSQL は memory・session・ログなどの保存先。購読・実行証跡の表は不使用(データ保持)。
# DATABASE_URL は必須。配送は別の QUEUE_REDIS_URL を使う。
# scripts/run_local.sh は Compose の db を指す libpq DSN を解決する。
DATABASE_URL: str = os.environ.get("DATABASE_URL", "")

# --- default file paths (this checkout's data/ and config/ directories) ---
# config.py は <root>/src/trackeverything/ にある。
_ROOT = Path(__file__).resolve().parents[2]
# 作業資料 store の root。project・GitHub issue / PR・Notion ページの資料を
# 参照から決まる階層の Markdown として置く(部品 repository の docs/components/WORK_DOCS.md)。
DEFAULT_DOCS_DIR = str(_ROOT / "data" / "docs")
# The notification recipient's profile — one markdown file the compare agent
# interleaves into its summarize prompt: it grounds the relevance judgment
# (irrelevant changes are not notified), the summary's emphasis, and the
# notify level. Read at summarize time, so editing the file takes effect on
# the next run without a restart. Missing or empty file = no profile, no
# filtering (every real change notifies).
DEFAULT_ROLE_PATH = str(_ROOT / "data" / "role.md")
# Root of the shared command workspace: disposable per-repo clones the
# investigator agent works in (REPOS_DIR/<repo>). Never a person's working
# copy — clones are reset to the remote tip every run.
DEFAULT_REPOS_DIR = str(_ROOT / "data" / "repos")
# Topic → repository catalog driving the investigator agent's repo
# selection when the payload omits "repo": one markdown line per repo
# (`<owner>/<name> — what it covers`). Read at the start of each run, so
# editing the file takes effect on the next run without a restart.
# Missing file = no catalog; an omitted repo then ends the run with a
# one-line ask instead of a guess.
DEFAULT_REPO_CATALOG_PATH = str(_ROOT / "data" / "repo-catalog.md")
# 会話と調査 CLI session の対応を保存する。
DEFAULT_HITL_DIR = str(_ROOT / "data" / "hitl")

# Effective paths — env override applied here. ``str`` is the natural shape (env
# vars and the defaults are both str); callers that need a ``Path`` wrap it at
# the use site.
# 作業資料 store の root (DEFAULT_DOCS_DIR を参照)。
DOCS_DIR: str = os.environ.get("DOCS_DIR", DEFAULT_DOCS_DIR)
# Project 資料の root。各文書は PROJECTS_DIR/<project>/<name>.md (JSON ヘッダー付き)。
# 既定は作業資料 store の projects/ で、人が任意のエディタで読み書きできる。
# store の外を指すと起動時の旧配置からの移設を行わない。
PROJECTS_DIR: str = os.environ.get("PROJECTS_DIR") or str(Path(DOCS_DIR) / "projects")
# 番号だけの参照(15000・#15000)が指す Issue の repository (hub 板の Issue 置き場、``owner/repo``)。
# 未設定なら番号だけの参照は解決できず、``<repo> #n`` の引用も抽出しない。
DOCS_ISSUE_REPO: str = os.environ.get("DOCS_ISSUE_REPO", "").strip().lower()
# Issue の仕様作成で候補に示す手元の clone の親 directory (os.pathsep 区切り)。未設定なら候補を示さない。
LOCAL_REPOSITORY_ROOTS: tuple[str, ...] = tuple(
    str(Path(root).expanduser()) for root in os.environ.get("LOCAL_REPOSITORY_ROOTS", "").split(os.pathsep) if root.strip())
# Skill の固定の配置先。空なら各版の file view の ``skills/`` を使う。指定すると root 全体を置換する。
SKILLS_DIR: str = os.environ.get("SKILLS_DIR", "").strip()
#: 画面の静的ファイル(Host が所有)。版の部品はここを Host の資源として読む。
STATIC_DIR: str = str(Path(__file__).resolve().parent / "api" / "static")
# The recipient-profile file the compare agent reads (see above).
ROLE_PATH: str = os.environ.get("ROLE_PATH", DEFAULT_ROLE_PATH)
# Root of the shared command workspace (see DEFAULT_REPOS_DIR above).
REPOS_DIR: str = os.environ.get("REPOS_DIR", DEFAULT_REPOS_DIR)
# The repo catalog file (see DEFAULT_REPO_CATALOG_PATH above).
REPO_CATALOG_PATH: str = os.environ.get("REPO_CATALOG_PATH", DEFAULT_REPO_CATALOG_PATH)
# 調査 CLI session の対応保存先 (DEFAULT_HITL_DIR を参照)。
HITL_DIR: str = os.environ.get("HITL_DIR", DEFAULT_HITL_DIR)

# PR review の恒久 HTML と固定版の表示資源。公開 URL は配備設定のみから読む。
REPORTS_DIR: str = os.environ.get("REPORTS_DIR", str(_ROOT / "data" / "review-reports"))
REPORTS_BASE_URL: str = os.environ.get("REPORTS_BASE_URL", "")

# --- common HTTP gateway (cloudflared → nginx → auth service, owner's Mac) ---

# Local endpoint of the shared-gateway auth service. The gateway login tool
# posts here with the owner-only bearer to mint one-time dashboard logins.
# Loopback only; the deployment never exposes this port beyond the host.
GATEWAY_AUTH_URL: str = os.environ.get("TE_GATEWAY_AUTH_URL", "http://127.0.0.1:18081")
# Owner-only gateway config (bearer + public origin), written by
# gateway/setup.py. Read at tool-run time, so credential rotations apply
# without an app restart. Missing file = the tool reports "not configured".
GATEWAY_CONFIG_PATH: str = os.environ.get(
    "TE_GATEWAY_CONFIG_PATH",
    str(Path.home() / "Library/Application Support/SharedGateway/config.json"),
)

# --- LLM provider defaults ---

# Z.AI via the GLM Coding Plan endpoint (subscription quota, not pay-as-you-go
# balance). Same OpenAI-compatible surface as the standard endpoint; the key
# authenticates against the plan's credits. Caveat: cost_usd in the execution
# log keeps the pay-as-you-go price table — treat it as an equivalent-cost
# estimate, not billing.
ZAI_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
ZAI_DEFAULT_MODEL = "glm-5-turbo"
# The frontier z.ai model, separate from the default above. GLM-5.3 always
# reasons (thinking cannot be disabled) and takes a per-request effort level
# (low/high/max). ``build_zai_reasoning_llm`` in the composition root builds
# it on the same Coding Plan endpoint and key. ``build_llms`` names the
# clients ``max`` (the primary), ``high`` and ``low``, plus ``flash`` and
# ``flash_low`` on the lighter GLM-5.3-Flash at ``high`` and ``low`` effort;
# each Agent class picks one by its ``llm`` declaration (default ``max``; the
# component inventory lists the declarations). Without ``ZAI_API_KEY`` all
# five are the single primary.
ZAI_REASONING_MODEL = "glm-5.3"
ZAI_FLASH_MODEL = "glm-5.3-flash"

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"

OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENAI_DEFAULT_MODEL = "gpt-4o"

# Reasoning models can spend several minutes on one thinking turn. Keep this
# application policy in code rather than environment configuration.
DEFAULT_TIMEOUT = 600.0
LLM_TIMEOUT: float = DEFAULT_TIMEOUT

# process 全体で並行する LLM turn の上限。Run の枠数とは独立して
# ThrottledLLM の共有 semaphore に適用する。
DEFAULT_CONCURRENCY = 10
LLM_CONCURRENCY: int = DEFAULT_CONCURRENCY

# One LLM-turn budget is shared by every agent participating in a single run.
# Runtime writes it into AgentContext; every AgentCall shares that context.
# A ceiling, not a target — a run stops as soon as it finishes. Deep webhook
# investigations (ci_failure → investigator, which fetches platforms without a
# dedicated tool with the Host base tool bash) take many turns; 300 leaves room
# for the deepest graphs without truncating a final report. External
# CLI-internal LLM calls (review / investigation CLIs) are outside this budget.
MAX_STEPS: int = 300

# Wall-clock deadline for one run started by the Runtime (whole agent tree,
# its children and their subprocesses). On expiry the tree is cancelled and the
# run ends with status "cancelled". Seconds; positive integer.
DEFAULT_RUN_TIMEOUT = 3600


def _run_timeout(raw: str | None) -> int:
    if raw is None or not raw.strip():
        return DEFAULT_RUN_TIMEOUT
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"RUN_TIMEOUT must be a positive integer number of seconds, got {raw!r}") from None
    if value <= 0:
        raise ValueError(f"RUN_TIMEOUT must be a positive integer number of seconds, got {raw!r}")
    return value


RUN_TIMEOUT: float = _run_timeout(os.environ.get("RUN_TIMEOUT"))

# --- 実験: Agent と Tool の全接続 (mesh) ---

# オンのとき構成ルートが全入口から到達可能な Tool と子 Agent を集め、
# すべての Agent の function calling へ一括で載せる (宣言済みの構成は
# 上書きしない)。Agent と Tool の最良組み合わせを LLM 自身に選ばせる
# 実験用で、循環委譲は共有 step 予算 (MAX_STEPS) と BaseAgent の委譲深さ
# 上限が抑える。既定はオフで、各 Agent の宣言構成のまま。
AGENT_FULL_MESH: bool = os.environ.get("AGENT_FULL_MESH", "0").lower() in {"1", "true", "yes"}

# general channel で自動注入する成功問答。時間窓と件数上限を同時に適用する。
DISCORD_GENERAL_HISTORY_LIMIT: int = 5
DISCORD_GENERAL_HISTORY_HOURS: int = 2

# --- Output policy ---

# 生成する報告・資料・GitHub 本文は日本語。Discord の会話・配送表示は簡体字中国語。
# code-owned policy: 反映には配備とプロセス再起動が必要。
OUTPUT_LANGUAGE = "Japanese"
DISCORD_LANGUAGE = "Simplified Chinese"

# --- Queue と runtime の既定値 ---
# 並行数は構成ルートで環境変数 RUN_CONCURRENCY から上書きできる。取り直しの待ち時間と配送上限は固定。
RUN_CONCURRENCY = 20
#: この秒数を超えて ACK されない pending を XAUTOCLAIM で取り直す。
QUEUE_CLAIM_IDLE = 1800
#: 配送回数がこれを超えたメッセージは実行せず dead stream へ移す。
QUEUE_MAX_DELIVERIES = 3
#: queue の全てのメッセージ(EventRequest)を受け取る唯一の入口 Agent。core は依頼の中身で分流しない。
DISPATCH_AGENT = "dispatch"

# runtime の保守 tick の間隔(秒)。catalog の保存と queue の heartbeat を行う。0 で tick を止める。
RECONCILE_INTERVAL = 30.0

# --- 部品の版(core/versioning) ---

#: 部品のリポジトリ(GitHub)の URL。runtime の mirror はここから fetch し、optimizer の merge はここへ push する。
#: 空ならローカル方式: mirror が共有の main を持ち、人は作業コピー(COMPONENTS_DIR)からそこへ push する。
COMPONENT_REPO_URL: str = os.environ.get("COMPONENT_REPO_URL", "").strip()
#: runtime が版を読む bare の mirror。run ごとの読取りは network を使わない。
COMPONENT_REPO_DIR: str = os.environ.get("COMPONENT_REPO_DIR") or str(_ROOT / "data" / "components.git")
#: 人の作業コピー(TrackEverything の git からは除外)。ローカル方式では runtime が起動時になければ clone して作る。
COMPONENTS_DIR: str = os.environ.get("COMPONENTS_DIR") or str(_ROOT / "components")
#: fetch・push に使う deploy key のパス。空なら git の既定の認証を使う。
COMPONENT_REPO_SSH_KEY: str = os.environ.get("COMPONENT_REPO_SSH_KEY", "").strip()
#: runtime が共有の main を取得・検証・採用する間隔(秒)。0 で止める。
COMPONENT_FETCH_INTERVAL = float(os.environ.get("COMPONENT_FETCH_INTERVAL", "300"))

# --- Webhook ingress (deployment-configurable) ---

# queue process の event loop で HTTP を動かす。
# /webhook/{provider} は検証済みの入力を原形のまま投入する。
# bind address と port は環境変数で指定する。
DEFAULT_WEBHOOK_HOST = "0.0.0.0"
DEFAULT_WEBHOOK_PORT = 8080
WEBHOOK_HOST: str = os.environ.get("WEBHOOK_HOST", DEFAULT_WEBHOOK_HOST)
WEBHOOK_PORT: int = int(os.environ.get("WEBHOOK_PORT", str(DEFAULT_WEBHOOK_PORT)))
# Optional shared secret: when non-empty, every webhook route requires the
# same value in the X-Webhook-Token header (401 otherwise). Empty (default)
# = no auth. Per-provider HMAC verification (GitHub X-Hub-Signature-256,
# Slack X-Slack-Signature) is a follow-up for public exposure.
WEBHOOK_TOKEN: str = os.environ.get("WEBHOOK_TOKEN", "")

# --- Command workspace policy ---

# Wall-clock cap for one run_command execution. The dominant case is an
# investigation CLI exploring a repo (minutes); git operations are far
# shorter and share the cap. Code-owned policy.
DEFAULT_COMMAND_TIMEOUT = 1800.0
COMMAND_TIMEOUT: float = DEFAULT_COMMAND_TIMEOUT

# Codex 単発実装の設定。CLI のユーザー設定より優先し、fast は明示時だけ利用する。
IMPLEMENTATOR_MODEL: str = os.environ.get("IMPLEMENTATOR_MODEL", "gpt-6.1-sol")
IMPLEMENTATOR_REASONING_EFFORT: str = os.environ.get("IMPLEMENTATOR_REASONING_EFFORT", "xhigh")

# Codex の Web 検索(web_search Tool)。model は既定で実装と同じ、推理強度は呼出しごとに上書きできる。
WEB_SEARCH_MODEL: str = os.environ.get("WEB_SEARCH_MODEL", IMPLEMENTATOR_MODEL)
WEB_SEARCH_REASONING_EFFORT: str = os.environ.get("WEB_SEARCH_REASONING_EFFORT", "medium")

# Review は CLI 内部の LLM を使うため、外側の step 予算とは別に総期限を持つ。
REVIEW_CLI_PATH: str = os.environ.get("REVIEW_CLI_PATH", "claude")
REVIEW_SKILL_PATH: str = os.environ.get("REVIEW_SKILL_PATH", str(_ROOT / "claude" / "skills" / "reviewer"))
REVIEW_KNOWLEDGE_PATHS: tuple[str, ...] = tuple(filter(None, os.environ.get("REVIEW_KNOWLEDGE_PATHS", str(_ROOT / "data" / "review" / "knowledge")).split(os.pathsep)))
#: review の CC 実行に渡す読取り専用 DB 接続先(任意)。未設定なら外部 DB 証拠は無効。
REVIEW_DB_DSN: str = os.environ.get("REVIEW_DB_DSN", "")
REVIEW_TIMEOUT: float = 1800.0
REVIEW_OUTPUT_LIMIT_BYTES: int = 1024 * 1024
#: review_pr・submit_pr_review の対象 repository(``owner/repo`` のカンマ区切り)。未設定なら review 対象はない。
REVIEW_REPOSITORIES: tuple[str, ...] = tuple(filter(None, os.environ.get(
    "REVIEW_REPOSITORIES", "").replace(" ", "").split(",")))

# code_fix / code_push の境界。fix は review と同じ CLI (REVIEW_CLI_PATH) を
# write モードで使う。修正対象 repository のホワイトリスト(未設定なら review 対象と同じ)・
# 確認待ち pending の TTL・checkout commit の作者名は運用環境変数で指定する。
FIX_ALLOWED_REPOS: tuple[str, ...] = tuple(filter(None, os.environ.get(
    "FIX_ALLOWED_REPOS", ",".join(REVIEW_REPOSITORIES)).replace(" ", "").split(",")))
FIX_TIMEOUT: float = float(os.environ.get("FIX_TIMEOUT", "1800"))
FIX_PENDING_TTL: float = float(os.environ.get("FIX_PENDING_TTL", "86400"))
FIX_COMMIT_NAME: str = os.environ.get("FIX_COMMIT_NAME", "track-everything")
FIX_COMMIT_EMAIL: str = os.environ.get("FIX_COMMIT_EMAIL", "track-everything@localhost")

#: PR Webhook が担当を判断する運用者自身の reviewer login。未設定なら運用者の PR・担当を判定しない。
CLASSIFY_REVIEWER_LOGIN: str = os.environ.get("CLASSIFY_REVIEWER_LOGIN", "").strip()

#: PR webhook の compare 播報を許す review bot の login 一覧 (運用環境変数)。
#: PR webhook では一般の変更要約を既定で抑制し、自分の PR へのこの名単の
#: コメント/レビューイベントだけが PR channel へ播報される。
REVIEW_BOT_LOGINS: tuple[str, ...] = tuple(filter(None, os.environ.get(
    "REVIEW_BOT_LOGINS", "coderabbitai[bot]").replace(" ", "").split(",")))

#: ci_failure 入口が処理対象とする workflow 名のホワイトリスト (運用環境変数)。
#: workflow_run イベントの workflow.name と完全一致で判定し、空 (既定) のときは
#: 全 workflow の失敗を処理する。review_requested / closed / コメント系 webhook の
#: dispatch 判定に使う reviewer login は上の CLASSIFY_REVIEWER_LOGIN を共用する。
CI_FAILURE_WORKFLOWS: tuple[str, ...] = tuple(filter(None, os.environ.get("CI_FAILURE_WORKFLOWS", "").replace(" ", "").split(",")))

#: PR watch がコメント窓に載せる API ごとの最大件数 (PR_WATCH.md 第4節)。
WATCH_COMMENT_WINDOW: int = int(os.environ.get("WATCH_COMMENT_WINDOW", "100"))

# --- Workspace pool policy (code-owned) ---

# review / fix / investigator が共有する常駐 clone 棟(workspace_pool)の規約。
# 棟は repo ごとに初期 POOL_INITIAL_SLOTS 本で始まり、空きが無ければ上限なく増設、
# 行と directory は削除しない(grow-only)。PR 束縛は PR が閉じるまで続く。
# 同じ PR・head の実行は per-head admission で 1 件に制限する。
POOL_INITIAL_SLOTS = 3
# 現行の workspace_pool は使わない。import 前の main(旧版の部品)が busy 棟数上限として読むため、
# head 単位の排他を含む版が main になった後の配備で削除する。
POOL_MAX_BUSY = 3
# busy のまま放置された棟(crash 孤児)を reclaim するときの猶予秒。
# holder ごとの実行期限(REVIEW_TIMEOUT / FIX_TIMEOUT / FIX_PENDING_TTL)に加える。
POOL_RUN_GRACE = 300.0
# 移行期の泄漏 review-* / fix-* 一時 directory を掃除する対象経過秒。
POOL_LEGACY_MAX_AGE = 172800.0
