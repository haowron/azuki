# Host and extension configuration

[Documentation index](README.md) / [Optional components](EXTENSIONS.md)

[config.py](../src/trackeverything/config.py) defines Host settings and compatibility settings consumed by optional
components. A setting's presence does not install a component or enable its workflow. Credentials belong in the
untracked `.env`; `scripts/run_local.sh` loads them into the process environment. Environment changes require restarting
the affected process, as described in [deployment](OPERATIONS.md#deployment).

PostgreSQL is the only persistent backend: Compose's `db` service uses the `pgdata` volume. The app
[runs on the host](../README.md#run-the-application) and connects over loopback (`TE_DB_PORT`, default 5432).
`DATABASE_URL` is required; there is no SQLite fallback. Redis holds accepted requests separately.
Recurring requests use the `schedule` table and one-shot requests use `reservation`
([Scheduling](components/SCHEDULING.md)); row definitions are installation data, not built-in jobs.

The Host-owned [optimizer](components/OPTIMIZER.md) is invoked through the default `optimizer` component. Its optional
`behavior` check reads `SLACK_TOKEN`, `GITHUB_TOKEN` (or `GH_TOKEN`), `DISCORD_BOT_TOKEN`, and `DISCORD_ALLOWED_USER_ID`.
The default optimizer entry returns its report to the run records. An optional component entry can deliver it to the
Discord channel named `optimizer` in `DISCORD_GUILD_ID`, derived from `DISCORD_NOTIFY_CHANNEL` when unset
([report delivery](components/OPTIMIZER.md#report-delivery)).
Sentry jobs and their `SENTRY_*` settings belong to [scheduled components](EXTENSIONS.md#scheduled-components).

## Named runs

The queue's only execution request is `EventRequest`. To run an agent by name, put `agent_name` in the payload with
`provider="internal"`. The runtime hands the request to the dispatch agent, which interprets the payload and delegates to a declared
child ([Queue](components/QUEUE.md)).

| Field | Type / default | Description |
|---|---|---|
| `kind` | `"event.handle"`; optional because it is the default | Execution request kind. The legacy `agent.run` and `source.run` forms are unreadable messages and move to the dead stream |
| `provider` | `"internal"`, required | Publishing from something other than an external service |
| `external_id` | string (1-256 characters), required | Stable ID from the publisher (e.g. `cli:example-api-123`). Not used for deduplication |
| `payload.agent_name` | string, required | Name of a child declared by the dispatch agent (the defaults include `general` and `optimizer`). An undeclared name is a run-time error |
| `payload.request` | JSON, default null | The child's request |
| `payload.data` | object, default `{}` | The child's `ctx.data`; a non-object is a dispatch error |
| `session_id` / `parent_session_id` | string or null, default null | The session to run in and its parent. A null `session_id` means a new session |

These are the package-default dispatch rules. Its named run passes `data` and `request` to the child and does not set
authorization. Optional dispatch implementations may additionally interpret `source`, `source_kwargs`, and `project`;
their validation and identity rules belong to the installed [component contract](EXTENSIONS.md#project-documents).

```json
{"provider": "internal", "external_id": "cli:general-1",
 "payload": {"agent_name": "general", "request": "Summarize the current workspace"}}
```

The optimizer starts with `agent_name="optimizer"` and a `data` object holding `limit` (number of recent run records, 1-1000,
default 100), `checks` (names of the checks to run; default all), and `merge` (default true; false keeps every change as a fork
and leaves main unchanged) ([Optimizer](components/OPTIMIZER.md)).
The registered checks are `errors` (fixes agents that failed on the current main version), `consolidation` (deletes, merges, and
prunes components), `additions` (adds components for recurring work), `tuning` (lowers reasoning effort when a replay keeps
the decisions, watches and reverts its changes, and reports other waste), and `behavior` (takes over the operator's routine work on
Slack, GitHub, and Discord and reports what AI can take over); none uses `limit`. The example below runs them all
and lets them merge.

```json
{"provider": "internal", "external_id": "cli:optimizer", "payload": {"agent_name": "optimizer", "data": {"limit": 100}}}
```

Publish with `bash scripts/run_local.sh python -m trackeverything.queue.cli publish --file <JSON>`
([Queue](components/QUEUE.md)). The CLI accepts any `EventRequest` regardless of provider, so a dead-stream entry's
`data` can be republished as is once the cause is fixed. The queue itself does not fetch Sources.

## Source kwargs

The package defaults register no Sources. An installed component repository defines its `SOURCES` keys, constructor
arguments, fetch scope, and required credentials in `sources/inventory.md` and `sources/how-to-create.md`.
The Host's `Source` interface and the version-loading contract remain in [Architecture](ARCHITECTURE.html) and
[Versioning](components/VERSIONING.md). Source definitions perform no I/O at consumption; fetching belongs to a component tool.

Optional source credentials include `GITHUB_TOKEN` / `GH_TOKEN`, `NOTION_TOKEN`, `APIDOG_ACCESS_TOKEN`, and `SLACK_TOKEN`.
Scheduled Sentry sources additionally use `SENTRY_TOKEN`, `SENTRY_ORG`, `SENTRY_PROJECT`, `SENTRY_REPOSITORY`, and
`SENTRY_BRANCH`. See [extension ownership](EXTENSIONS.md) before enabling a named Source.
`SENTRY_CLIENT_REPOS` selects optional caller repositories. `SENTRY_TRIAGE_DB_DSN` is an optional read-only MySQL
connection used by the Sentry component; its reader verification, client requirements, and credential isolation are
defined by [scheduled components](EXTENSIONS.md#scheduled-components).

## Python agent skills

The default root is the `skills/` of the running version (a file view that expands the version's tree into a temporary
directory). It does not depend on the cwd. Changes to `skills/` in the component repository are used by runs that start after the
runtime adopts the pushed `main`. The optional `SKILLS_DIR` replaces the whole root; an empty value means the default. Every
declared package must exist under that root. `~` is expanded.
The PR reviewer's CLI skill (`REVIEW_SKILL_PATH`, [below](#cli-skill-placement)) is a separate setting.

YAML is parsed with PyYAML and tool arguments are validated with jsonschema. For creation, updates, and restart conditions see the
[skill authoring rules](../src/trackeverything/skills/how-to-create.md).

## Work docs store

`DOCS_DIR` defaults to `data/docs` in the running checkout. Optional document tools use it for project, issue/PR, and
Notion material. `DOCS_ISSUE_REPO` is an optional `owner/repo` used for number-only references; it is unset by default.
Their layout, normalization, search, and maintenance commands belong to
[project document components](EXTENSIONS.md#project-documents). The Host does not initialize or migrate this file store.

## Project documents

`PROJECTS_DIR` defaults to `<DOCS_DIR>/projects` (`data/docs/projects` in the running checkout).
`ROLE_PATH` defaults to `data/role.md`, and `REPO_CATALOG_PATH` to `data/repo-catalog.md`; prepare these local files
from the tracked examples when enabling their components. Deployment does not overwrite them. `REPOS_DIR` defaults to `data/repos`
and is also the Host base tools' working directory. `LOCAL_REPOSITORY_ROOTS` optionally lists local clone parent
paths separated by the OS path separator; it is empty by default.

These paths remain available through `config.py`. Document formats, directory naming, legacy file migration,
project classification, and announcements are owned by [project document components](EXTENSIONS.md#project-documents).
The Host's `project_channel` mapping and related stores remain in [Session](components/SESSION.md).

## Session infrastructure

Session recording (`Runtime`) requires PostgreSQL's `DATABASE_URL`.
`session / session_run` store IDs and the inputs and results of runs; when it is unset, startup is a configuration error.
For session resolution and recording see [Session infrastructure](components/SESSION.md).

## Investigation CLI conversations

`HITL_DIR` defaults to `data/hitl` in the running checkout and can be overridden by the environment variable.
It is a compatibility path for optional [development components](EXTENSIONS.md#development-and-reviews).
The Host separately owns `codex_session` associations through `PostgresCodexSessionStore`
([Session](components/SESSION.md)); external SDK conversation files remain owned by their SDK.

## Slack Socket Mode

A Slack app connects over Socket Mode and monitors one configured channel.
This connection does not use an HTTP Request URL; it publishes deliveries from the configured team and channel to the queue in
raw form. For publishing and ACK, see the [Slack intake contract](components/SLACK.md).

| Environment variable | Value and purpose |
|---|---|
| `SLACK_APP_TOKEN` | The same app's `xapp-...` token. Opens the Socket Mode connection with `connections:write` |
| `SLACK_TOKEN` | An `xoxp-...` user token. Reads events, history, and user info under the owner's authorization |
| `SLACK_TEAM_ID` | The workspace (team) ID, e.g. `T0123456789`. Checked against the connection's authorization and the receiving workspace |
| `SLACK_CHANNEL_ID` | The channel ID, e.g. `C0123456789`. Target of Host intake; optional tools define their own read scope |

Credentials are stored in `.env`, which is excluded from Git. When the app token is empty, only the Slack connection is skipped.
When it is set, the other three variables and PostgreSQL's `DATABASE_URL` are required, and an authorization mismatch is a startup error.
Start exactly one queue process and one runtime process per database. No lock prevents a second instance, so operations must ensure it.
The Slack ACK is returned after durable publishing; a delivery whose publish failed is not ACKed and waits for redelivery.
The runtime passes the raw event to the installed dispatch agent. Mention handling, investigation, and forwarding are
optional [conversation components](EXTENSIONS.md#conversations).

1. Under **Basic Information → App-Level Tokens**, create a token with only `connections:write` and save it as `SLACK_APP_TOKEN`.
2. Turn on **Socket Mode → Enable Socket Mode**.
3. Under **OAuth & Permissions → User Token Scopes**, set `channels:history`, `channels:read`, and `users:read`.
   To use `slack_search` for issue investigation and mention summaries, also add `search:read`, `groups:history`, `im:history`, and `mpim:history`.
   To also read the operator's emoji reactions in the optimizer's `behavior` check, add `reactions:read`.
4. Enable **Event Subscriptions** and register `message.channels` under **Subscribe to events on behalf of users**.
   Leave legacy bot events, including `app_mention`, empty. After changing OAuth scopes, reinstall the app to the workspace.
5. Make sure the owner is a member of the monitored channel, then start the app.

The app token is for the connection and grants no permission to post in channels. The Host intake does not post messages,
write reactions, or change read state. Additional scopes for optional tools are specified by their component contracts.
The app narrows user-event deliveries by workspace and channel, and out-of-scope events are not published.
Startup validates `auth.test` and connects Socket Mode; it does not post a test message.
For the official procedures see [Python SDK Socket Mode](https://docs.slack.dev/tools/python-slack-sdk/socket-mode/) and
[message.channels](https://docs.slack.dev/reference/events/message.channels/).

## Webhook ingress

The queue process validates transport fields and prunes by sender identity, then durably publishes raw `EventRequest`s.
The runtime always runs the installed dispatch agent, which owns business routing ([Queue](components/QUEUE.md)).

Discord uses the [Gateway intake](components/DISCORD.md). `DISCORD_BOT_TOKEN` enables it and
`DISCORD_ALLOWED_USER_ID` restricts input. `DISCORD_GENERAL_CHANNEL`, falling back to `DISCORD_NOTIFY_CHANNEL`, is the
channel whose requests carry no persistent session. Other channels resolve sessions through the Host channel store;
threads carry the parent channel's session when available. Components decide history, classification, and replies.

| Endpoint | Intake validation (failure returns 422 and publishes nothing) | Published |
|---|---|---|
| `POST /webhook/github` | JSON object, with `X-GitHub-Event` and `X-GitHub-Delivery` | Every event; only `x-github-event`, `x-github-delivery`, and `x-github-hook-id` headers are kept |
| `POST /webhook/slack` | JSON object, with `event_id` except for `url_verification` | `url_verification` returns `challenge` without publishing; other events are published without the top-level `token` |

A published delivery returns 202 `{"ok":true,"run":"queued","message_id":"…","accepted_at":…}`.
`message_id` is the Redis stream ID. Invalid JSON, a non-object body, or missing required fields returns 422;
unconfirmed durable acceptance returns 503. 202 means accepted, not finished. The queue performs no deduplication.

```bash
curl -s -X POST http://localhost:18080/webhook/github \
  -H 'Content-Type: application/json' \
  -H 'X-GitHub-Event: pull_request' -H 'X-GitHub-Delivery: manual-1' \
  -d '{"action":"opened","repository":{"name":"Hello-World","owner":{"login":"octocat"}},"pull_request":{"number":1234,"user":{"login":"octocat"}}}'
```

This posts to the launcher default upstream port. Add `X-Webhook-Token` when `WEBHOOK_TOKEN` is non-empty.
Public entry points use the gateway authentication in [Tunnel operations](CLOUDFLARE_TUNNEL.md).

The following settings remain available to optional components; the Host intake does not apply their business rules:

| Setting | Default / purpose |
|---|---|
| `DISCORD_GUILD_ID` | Optional guild for component-created channels; components may derive it from `DISCORD_NOTIFY_CHANNEL` |
| `DISCORD_GENERAL_HISTORY_LIMIT` / `DISCORD_GENERAL_HISTORY_HOURS` | Code policy `5` rounds / `2` hours for optional conversation history |
| `CLASSIFY_REVIEWER_LOGIN` | Empty; optional GitHub owner/reviewer matching |
| `REVIEW_BOT_LOGINS` | `coderabbitai[bot]`; comma-separated review-bot logins |
| `CI_FAILURE_WORKFLOWS` | Empty means all workflows; comma-separated workflow names |
| `PR_RETIRE_SWEEP_INTERVAL` | `3600` seconds; optional component reconciliation interval |

See [conversation components](EXTENSIONS.md#conversations) and
[development and review components](EXTENSIONS.md#development-and-reviews) for their routing and delivery contracts.

## Investigation on issue assignment

Issue investigation is an optional [development component](EXTENSIONS.md#development-and-reviews).
The launcher passes its configured credentials through the environment; scope and use are owned by the installed tool.

| Source | Environment variable | Required permissions and scope |
|---|---|---|
| GitHub | `GITHUB_TOKEN` / `GH_TOKEN` | Read and search issues and PRs of the target org |
| Slack | `SLACK_TOKEN` (user token) | `search:read`, `channels:history`, `groups:history`, `im:history`, `mpim:history`, `users:read`. Every channel and DM the owner can see |
| Notion | `NOTION_TOKEN` | Only pages shared with the integration |
| Gmail | `GOOGLE_APPLICATION_CREDENTIALS` | Path to an authorized_user JSON (client_id, client_secret, refresh_token) with the `gmail.readonly` scope |

`GOOGLE_APPLICATION_CREDENTIALS` names an authorized-user JSON file; its contents stay outside Git.
The Host's webhook accepts `issues` events without deciding whether investigation, cleanup, or another action should run.

## Management API

`WEBHOOK_PORT` is the app's listening port (default 18080 in `scripts/run_local.sh`; the shared gateway uses 8080), and
`WEBHOOK_TOKEN` is the shared token.
For what is authenticated, the headers, the dashboard settings, and every route see the [FastAPI management document](components/FASTAPI.md).

## Cloudflare Tunnel

The `issue_gateway_login` tool (issues login URLs from Discord) reads the following settings. The defaults point to the owner-only
gateway deployment files (written by `gateway/setup.py`).

| Environment variable | Default | Purpose |
|---|---|---|
| `TE_GATEWAY_AUTH_URL` | `http://127.0.0.1:18081` | Endpoint of the gateway auth service |
| `TE_GATEWAY_CONFIG_PATH` | `~/Library/Application Support/SharedGateway/config.json` | Private config holding the bearer and the public origin |
| `GATEWAY_LOGIN_PASSWORD` | (unset) | Source of truth for the shared password used together with the login key. `gateway/setup.py --upstream-env .env` syncs it into the private config |


The [source of truth for public routes, ports, and operations](CLOUDFLARE_TUNNEL.md) collects the Cloudflare settings, where
secrets are stored, the app's environment variables, and how to start, stop, and check connectivity.
The Tunnel's Service URL is `http://localhost:8080`. Cloudflare restricts the public prefix, and locally a request is authenticated
by one of GitHub, Slack, or Bearer before being forwarded upstream.
The webhook example above, which bypasses the gateway, uses the app's shared token; through the gateway, use the authentication and
public URL from that document.

## Running version

| Environment variable | Default | Purpose |
|---|---|---|
| `RUN_SERVICE_POLL` | `180` | Interval (seconds) at which the supervisor (`scripts/run_service.sh`) checks the modification times of host-side inputs |
| `RUN_SERVICE_TERM_WAIT` | `3600` | Maximum wait (seconds) for accepted work to finish when restarting for a host-side change. After it elapses, KILL |
| `RUN_TIMEOUT` | `3600` | Deadline (seconds, a positive integer) the runtime gives one run of the entrance (the dispatch agent). On expiry, the agent tree and the process groups it started are cancelled and the run is recorded as `cancelled`. A cancelled run is not ACKed and is rerun when reclaimed (up to 3 deliveries). An invalid value is a startup error |

The runtime runs the `main` adopted by its bare mirror, and files are not watched. People change components in the working
copy `components/` and push to the shared `main`: the component repository on GitHub when `COMPONENT_REPO_URL` is set,
otherwise the mirror itself, whose first `main` is the package's default components ([procedure](OPERATIONS.md#component-repository),
[component versioning](components/VERSIONING.md)).

| Environment variable | Default | Purpose |
|---|---|---|
| `COMPONENT_REPO_URL` | (empty) | URL of the component repository (for example `git@github.com:<owner>/<component-repository>.git`). Empty is local mode: the mirror holds the shared `main`, seeded from the package's default components |
| `COMPONENT_REPO_DIR` | `data/components.git` | The runtime's bare mirror (in local mode, the repository people push to) |
| `COMPONENTS_DIR` | `components` | The working copy, ignored by this repository's git. In local mode the runtime clones the mirror there at startup when it is missing |
| `COMPONENT_REPO_SSH_KEY` | (empty) | Deploy key with write access to the component repository, used for fetch and push. Empty uses git's default authentication |
| `COMPONENT_FETCH_INTERVAL` | `300` | Interval (seconds) at which the runtime reads the shared `main` (fetching GitHub's) and adopts it once it loads. `0` stops it |

## LLM provider configuration

Credentials go in `.env` and are kept out of Git. `build_llms` in `llm/compose.py` (host) picks the provider in this order.

| Setting | Selection |
|---|---|
| `ZAI_API_KEY` | Z.AI. If `DEEPSEEK_API_KEY` is also set, falls back to DeepSeek on transient failures |
| No Z.AI, `DEEPSEEK_API_KEY` set | DeepSeek |
| None of the above, `OPENAI_API_KEY` set | OpenAI |
| None | Startup error |

Fallback applies to HTTP 429 / 500 / 502 / 503 / 504, the SDK's `openai.APIConnectionError` (including timeouts),
and an HTTP 200 body that carries `error` without `choices` (Z.AI's answer to a request that waited past its queue limit).
A body without `choices` and without `error` stays a parse error and is not switched. Other errors are returned as is. Models, endpoints, timeouts, and concurrency are code settings in
[config.py](../src/trackeverything/config.py) and cannot be changed through environment variables.
`LLM_CONCURRENCY` caps model calls; it is not the queue concurrency (`RUN_CONCURRENCY`).
The warning on failover includes the run ID, the parent agent ID, and the primary client's elapsed time.
For what is measured and how secrets are masked see [Execution logging](LOGGING.md).

When DeepSeek thinking is enabled, the `reasoning_content` returned by assistant messages in the history is kept.
If the primary model did not return this value (missing or null), an empty string is sent explicitly so that the history meets the
fallback provider's format. The original history and tool call IDs are not changed.
Nothing is filled in for other providers or when thinking is disabled.
See [DeepSeek's history contract](https://api-docs.deepseek.com/guides/thinking_mode/).

With Z.AI as the primary, `build_llms` creates named clients `max` (the primary client), `high`, and `low`, one per GLM-5.3
reasoning effort, and `flash` and `flash_low`, GLM-5.3-Flash (`config.ZAI_FLASH_MODEL`) at `high` and `low` effort. Each agent class declares one name in `llm` and never swaps the client it received at initialization during a run.
A child agent does not inherit its parent's LLM; it uses its own declaration. The default is `max`.
The installed component repository owns individual agents' choices.
All five share the same endpoint, key, throttle, and run records. Without `ZAI_API_KEY`, all five names point to the single
primary client.

Example: set the Z.AI key and the fallback key in `.env` and start with the [startup procedure](../README.md).

```dotenv
ZAI_API_KEY=your_zai_key
DEEPSEEK_API_KEY=your_deepseek_key
```

## CLI skill placement

Optional review components read `REVIEW_SKILL_PATH`, defaulting to `claude/skills/reviewer` in the running checkout.
The directory holds a `SKILL.md` with `name` / `description` frontmatter. `claude/` is gitignored, so this is local
configuration. Its loading and use belong to [review components](EXTENSIONS.md#development-and-reviews);
it is separate from version-owned Python agent skills.

## Codex ImplementationAgent

The following configuration remains available to optional
[development components](EXTENSIONS.md#development-and-reviews). `scripts/run_local.sh` loads `.env`; changed values
take effect after restarting the runtime.

| Setting / environment variable | Default | Purpose |
|---|---|---|
| `IMPLEMENTATOR_MODEL` | `gpt-6.1-sol` | Model passed to Codex's `-m` |
| `IMPLEMENTATOR_REASONING_EFFORT` | `xhigh` | Reasoning effort passed to `-c 'model_reasoning_effort="..."'` |
| `WEB_SEARCH_MODEL` | Same as `IMPLEMENTATOR_MODEL` | Model the `web_search` tool uses for Codex web search |
| `WEB_SEARCH_REASONING_EFFORT` | `medium` | Default reasoning effort for `web_search` (overridable per call) |

The Host retains the `codex_session` association table and store. SDK permissions, per-call effort, service tier,
resumption, authentication selection, and independent-review behavior are component contracts.

## PR Review Agent

Review and fix workflows are optional [development components](EXTENSIONS.md#development-and-reviews).
The table includes retained `config.py` settings and environment inputs consumed directly by components;
installing the Host does not install the workflows.

| Deployment path environment variable | Default |
|---|---|
| `REVIEW_REPOSITORIES` | Target repositories of `review` and `submit_pr_review` (comma-separated `owner/repo`; empty by default, so nothing is reviewed until it is set). Other repositories return `unsupported_repository`. The routine operations on own PRs (review request, squash merge after approval) target the same repositories |
| `PR_AUTOMATION_REVIEWER` | GitHub login asked to review the operator's own PR when it becomes Ready for review (empty by default: no request) |
| `PR_AUTOMATION_IGNORED_CHECKS` | Case-insensitive regular expression of check and commit-status names whose failures or unfinished runs do not stop the squash merge after approval (empty by default: no check is ignored) |
| `REVIEW_CLI_PATH` | `claude` |
| `REVIEW_SKILL_PATH` | Reviewer skill directory (default: `claude/skills/reviewer` in the running checkout) |
| `REVIEW_KNOWLEDGE_PATHS` | `data/review/knowledge` in the running checkout. Separate multiple paths with the OS path separator (`:` on Linux/macOS) |
| `REVIEW_DB_DSN` | Unset. Read-only DB connection passed to review's CC run (`mysql://user:pass@host:3306/db` form). When set, it is passed to the input's `db_dsn` and the child process environment, allowing source checks (DDL, schema comparison) with read-only queries such as SELECT / SHOW. Never use it for writes or schema changes |
| `REPORTS_DIR` | `data/review-reports` in the running checkout. Holds the published pages for PR reviews, generated documents, and issue investigation reports |
| `REPORTS_BASE_URL` | Unset. Set the full public HTTPS URL `https://<hostname>/<public-prefix>/reports` |
| `WATCH_COMMENT_WINDOW` | Maximum number of comments per API for the PR watch comment window (default `100`) |
| `FIX_ALLOWED_REPOS` | Allowlist of repositories `code_fix` may modify (comma-separated `owner/repo`; default same as `REVIEW_REPOSITORIES`) |
| `FIX_TIMEOUT` | Overall deadline for `code_fix` (seconds; default `1800`; uses the same CLI as review, `REVIEW_CLI_PATH`, in write mode) |
| `FIX_PENDING_TTL` | How long a fix awaiting confirmation is kept (seconds; default `86400`) |
| `FIX_COMMIT_NAME` / `FIX_COMMIT_EMAIL` | Author of the commits CC creates in the checkout (defaults: `track-everything` / `track-everything@localhost`) |

Path defaults are relative to the running checkout. `REVIEW_KNOWLEDGE_PATHS` uses the OS path separator; place local
reference material outside Git. `REVIEW_TIMEOUT=1800` seconds and `REVIEW_OUTPUT_LIMIT_BYTES=1048576` are code-owned
policy values available to components. The launcher's `API_TIMEOUT_MS` defaults to `600000` for the external CLI.
`MAX_STEPS` and `LLM_CONCURRENCY` govern Host LLM calls, not calls made inside a component's external SDK process.

`REPORTS_DIR` is also a Host input: [FastAPI](components/FASTAPI.md#public-pages-pr-reviews-documents-issue-investigation-reports)
serves its finished HTML and pinned assets. Components publish complete files there and construct public links from
`REPORTS_BASE_URL`; the Host does not derive it from request headers. Old files survive ordinary deployments, and a
missing file returns 404. Public paths and authentication are in [Tunnel operations](CLOUDFLARE_TUNNEL.md).
Review result states, publication timing, notification, and material protection belong to the component contract.

## Coding SDK

The dependencies `openai-codex` and `claude-agent-sdk` remain installed by `uv sync` for optional
[development components](EXTENSIONS.md#development-and-reviews). The Host does not invoke either SDK during ordinary
startup. Components own worker messages, credential filtering, permissions, session resumption, and SDK results.

The shared Host process helpers `run_bounded`, `run_captured`, and `stop_process_group` remain available to tools.
They bound process output and deadlines and clean up process groups on completion or cancellation; see
[tool authoring](../src/trackeverything/tools/how-to-create.md).

### Codex ImplementationAgent

The supported model and effort settings are listed [above](#codex-implementationagent).
An installed component decides per-call options; the Host does not configure an SDK conversation itself.

## Output language

`OUTPUT_LANGUAGE = "Japanese"` and `DISCORD_LANGUAGE = "Simplified Chinese"` remain code-owned settings in
`src/trackeverything/config.py`, consumed by optional components. They have no environment override; changes take
effect after deployment and runtime restart. Component prompts, translation, delivery, and formatting follow
[conversation component contracts](EXTENSIONS.md#conversations).

Fixed transport acknowledgments belong to the Host intake code. The package defaults do not install a translator or
Discord delivery tool.

## Agent and tool full mesh (experimental)

| Environment variable | Default and purpose |
|---|---|
| `AGENT_FULL_MESH` | `0`. `1 / true / yes` puts every tool and every child agent into every agent's function calling |

When enabled, the version's composition module `agent/compose.py` collects the tool and agent names of every agent in the version
and adds the undeclared ones to every agent (tools as shared instances, child agents by name, obtained at run time from the run
handle). Declared composition is not overwritten, and no agent calls itself as a child. This experiment lets the LLM itself choose
the best combination of agents and tools, and entry agents can also be called as children of other agents.
Circular delegation is bounded by the shared step budget (`MAX_STEPS`) and `BaseAgent`'s delegation depth limit, and such a run ends
as error / max_steps. It is off by default, leaving each agent's declared composition unchanged.
See the [composition contract](ARCHITECTURE.html).

## Redis queue and processes

[QUEUE.md](components/QUEUE.md) is the source of truth for startup, the SDK/CLI, settings, storage, and tolerated errors.
The queue process (`scripts/start_queue.sh`) starts only intake and publishing, and the runtime process (`scripts/start_runtime.sh`)
only consumption and execution.
Set `QUEUE_REDIS_URL / QUEUE_NAMESPACE / RUN_CONCURRENCY` in `.env`.
The defaults are loopback Redis, `trackeverything`, and 20 concurrent runs. Pending messages not ACKed for 30 minutes are reclaimed,
and those delivered more than 3 times move to the dead stream.
`TE_REDIS_PORT / TE_REDIS_MAXMEMORY` are the Compose port and byte limit.
Development is isolated from the live environment with a dedicated DB and namespace, HTTP port 18090, and empty bot credentials for
intake and notifications.
