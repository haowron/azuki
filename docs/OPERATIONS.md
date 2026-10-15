# Operating the service

For public routes, ports, and the optional Cloudflare Tunnel, see [docs/CLOUDFLARE_TUNNEL.md](CLOUDFLARE_TUNNEL.md).
The app listens on `WEBHOOK_PORT` (launcher default `18080`). A gateway in front of it ([gateway/](../gateway/README.md)) can own the
public port, authenticate requests, and forward them to the app.

Redis Streams holds unprocessed messages and the dead stream. [QUEUE.md](components/QUEUE.md) is the source of truth for storage settings, the SDK, the CLI,
tolerated errors, and startup. There are no subscriptions for periodic tracking.
For the scheduled trigger, the queue process reads the `schedule` table (id, cron, timezone, payload) and publishes a named run at each cron time.
There is no management API: rows are changed directly with SQL, and missed times are not caught up ([Scheduling](components/SCHEDULING.md)).
The polling loop also publishes due rows of the `reservation` table. Creating and consuming reservations requires
agents supplied by a component repository; the package defaults do not include them.
Register only scheduled agents declared by the installed dispatch; no application schedule rows are created automatically.
See [Scheduling](components/SCHEDULING.md) for the Host trigger and [optional components](EXTENSIONS.md#scheduled-components)
for application-specific jobs. The Host-owned optimizer is documented in [Optimizer](components/OPTIMIZER.md).
The tables `subscription`, `schedule_cursor`, `schedule_outbox`, `execution_receipt`, `execution_attempt`, and `execution_child` are
unused (data retained); only `subscription.project` is read, read-only, as a candidate for project labels.
`DATABASE_URL` is required; there is no SQLite fallback. Compose provides `db` and `redis`, with the volumes `pgdata / redisdata`.

Alembic is the only owner of the schema (`alembic/` at the repo root, wired to
`core/orm`'s `Base.metadata` and `DATABASE_URL` — `apscheduler_jobs`
is APScheduler-owned and excluded from autogenerate). Stores never create or
alter tables, and the app refuses to start unless the database is at
`alembic head` (`core.orm.connection.require_schema_head`). Edit the ORM
model in `core/orm/models.py`, then `uv run alembic revision
--autogenerate -m "…"` against an empty scratch DB and review the revision.
`uv run alembic check` must report "No new upgrade operations detected" — run
it as part of the same change. Deployment applies `alembic upgrade head` before the service restarts
([Deployment](#deployment)), while the previous version is still running: a revision must stay compatible with the
code it replaces (add first; drop or rename in a later release). Apply it by
hand with `bash scripts/run_local.sh alembic upgrade head`.
Never edit a revision that has run somewhere — add a new one.

The app runs on the host as two processes, `scripts/start_queue.sh` and
`scripts/start_runtime.sh` (both through `scripts/run_local.sh`).
Start exactly one queue process and one runtime process per DB, and run them side by side.
No lock enforces this: a second queue duplicates the bot connections,
and a second runtime closes the running runs as interrupted during its startup recovery.
Only queue holds the always-on Discord/Slack intake. Run store maintenance, imports, and queue publishing with the launcher of
the checkout that runs the service (`bash scripts/run_local.sh python …` there), so they resolve the same `DATABASE_URL`
and repo-local paths as the running app. The `docker compose exec db psql` commands work from any checkout of the
repository: `docker-compose.yml` pins `name: trackeverything`, so every checkout resolves the same db service and volumes.

## Deployment

- [scripts/render_services.py](../scripts/render_services.py) generates launchd definitions for the two processes
  (`com.trackeverything.queue` and `com.trackeverything.runtime`, each running `scripts/run_service.sh queue|runtime`).
  It neither rewrites the current launchd configuration nor starts anything; installation, switch-over, and the safe stop order
  are in [Queue](components/QUEUE.md#startup-and-installation).
- Each supervisor restarts its app when Host-owned files change (`restart_roles()` in `src/trackeverything/process.py` is the
  source of truth for what it watches): after TERM it waits for accepted work and sends KILL only when `RUN_SERVICE_TERM_WAIT`
  is exceeded. Host-side inputs are the Python under `src/` (including the package's default components), `pyproject.toml`, `uv.lock`,
  and `scripts/run_local.sh`.
- [scripts/deploy.py](../scripts/deploy.py) (`scripts/deploy.sh`) updates a deploy checkout by fast-forward only, from GitHub
  `origin/main` (`--remote`) or from the committed `main` of a local checkout (`--source`). It is serialized by a file lock,
  refuses a checkout with tracked changes or untracked files that are not ignored, keeps ignored paths (`.env`, `data/`,
  `claude/`), runs `bash scripts/run_local.sh alembic upgrade head` right after the fast-forward
  (returning to the original commit if the migration fails), and leaves the restart to the supervisor.
  `--install` registers a LaunchAgent that runs `--remote` periodically.
- Deployment updates only this repository. Component changes are pushed to the shared `main` and reach the running runtime
  within `COMPONENT_FETCH_INTERVAL`, or at the next run in local mode ([Component repository](#component-repository)). Deploy a Host contract change
  (for example the arguments of `build_agents`) in a form the current components still load with, then push the components.
  If the adopted main can no longer be loaded, runtime aborts startup; push a loadable main and point the mirror at it
  ([Component versions](components/VERSIONING.md#deployment-and-restart)).
- Changes to `.env` and to `scripts/run_service.sh`, `scripts/start_queue.sh`, and `scripts/start_runtime.sh` take effect
  after the affected processes are stopped, drained, and restarted. Shared settings affect both processes, HTTP/Slack/Discord
  intake settings affect queue, and execution and LLM settings affect runtime.
- A change to `.python-version` (an interpreter switch) must not go through automatic deployment: the `uv run` after the
  fast-forward rebuilds `.venv` under the running processes. Stop both processes, deploy, check the interpreter, and start them again.
- Static dashboard files are read on every request; a reload shows updates.
- Optional components can use `data/role.md` and `data/repo-catalog.md`. Create these local, ignored files from
  `data/role.example.md` and `data/repo-catalog.example.md` when the installed components need them; deployments preserve them.

## Component repository

Components live in the git working copy `components/` of the checkout (`COMPONENTS_DIR`; `/components/` is in this repository's
`.gitignore`). It holds `agent/`, `tools/`, `skills/`, `harnesses/`, and `sources/` at its root
([Configuration](CONFIGURATION.md), [Component versions](components/VERSIONING.md)). To change components, work there:

```bash
cd components
git pull
# edit, commit
git push origin main
```

- **Local mode** (no `COMPONENT_REPO_URL`): runtime makes `components/` on its first start as a clone of the mirror
  (`data/components.git`), whose first `main` is the package's default set. `git push` goes to the mirror, whose `pre-receive` hook loads the
  commit with this checkout's Host and refuses the push on failure with `<path>:<line>: <exception type>: <message>`. The next run uses the new main.
- **GitHub** (`COMPONENT_REPO_URL`, with the deploy key `COMPONENT_REPO_SSH_KEY`, in `.env`): clone the component repository into `components/`
  and enable the check once per clone with `git config core.hooksPath <checkout that runs the service>/scripts/component-hooks`. Its `pre-push`
  hook loads each commit pushed to `main` with that Host and stops the push on failure. Pull first: GitHub refuses a push that is not a
  fast-forward. Within `COMPONENT_FETCH_INTERVAL` runtime adopts the new main.
- No deployment or restart is needed. runtime logs `component version <id> is main (<message>)`; the next run uses it and runs in progress
  finish on their original version. A main that cannot be loaded is not adopted (`component version <id> could not be loaded; keeping <id>: …`).
- The optimizer's `errors`, `consolidation`, `additions`, `tuning`, and `behavior` checks merge their changes into the same `main` (rebased on top of newer pushes when
  needed), so `git pull` brings them into `components/`; `consolidation` deletes whole files. A change that conflicts is reported as `failed` and
  changes nothing.
- Check a commit by hand with `bash scripts/run_local.sh python -m trackeverything.core.versioning check --repo components [<rev>]` in the checkout
  that runs the service.

## Development runs (isolated from the live service)

`uv run pytest` and the other checks need no running service. When a live app instance is needed during development, keep
it clear of the always-on instance:

```bash
TE_DATABASE_URL=postgresql://trackeverything:<password>@127.0.0.1:5432/trackeverything_dev \
QUEUE_NAMESPACE=trackeverything_dev WEBHOOK_PORT=18090 DISCORD_BOT_TOKEN= DISCORD_NOTIFY_CHANNEL= SLACK_APP_TOKEN= SLACK_TOKEN= \
bash scripts/start_queue.sh
# Start runtime from another terminal with bash scripts/start_runtime.sh, using the same dev DB/namespace and empty notification settings,
# plus COMPONENT_REPO_URL=<path of a component clone> COMPONENT_REPO_DIR=<scratch mirror> COMPONENTS_DIR=<scratch path> to run that clone's main
# without pushing.
```

- Database: a dedicated development database (created empty; schema and Alembic experiments go there). **Never point
  development code at the live database** — a schema mismatch breaks the running service.
- Port: one that the live app and the gateway do not use.
- Discord/Slack credentials blanked: a second connection with the same bot token would kick the live Gateway connection.
- A second runtime against the live database marks the live runtime's running runs as interrupted at startup, and a second
  queue duplicates the bot connections. Stop the development queue and runtime when the check is done.

## Queue messages

Publish a one-off run as `EventRequest` JSON with the launcher of the checkout that runs the service. The CLI validates and publishes an
`EventRequest` regardless of provider (`external_id` is required, 1–256 characters, and is not used for deduplication).
To run an agent by name, use a named run: put `agent_name` in the payload with `provider="internal"`
(the fields are listed in [Queue](components/QUEUE.md#internal-payload);
`source` is a key of `SOURCES` in `agent/compose.py`; the shape of `source_kwargs` is in [Configuration](CONFIGURATION.md)).

```bash
cat > /tmp/message.json <<'JSON'
{"provider": "internal", "external_id": "cli:example-1",
 "payload": {"agent_name": "general", "request": "Summarize the files in the current directory"}}
JSON
bash scripts/run_local.sh python -m trackeverything.queue.cli publish --file /tmp/message.json
```

- The `message_id` in the output is the Redis stream id. There is no deduplication: publishing the same request twice runs it twice.
- A named run reaches only children declared by the dispatch agent of the running version (for the package defaults, `general` and
  `optimizer`; for a component repository, its dispatch's list).
  An undeclared `agent_name` or malformed `data` is recorded at run time as a dispatch error.
  Source-based runs require a component dispatch and its fetch tool; check that repository's source keys and payload contract before publishing.
- ACKed messages disappear from the stream. Check the outcome in the run records (the dashboard's run flow view, `docs/LOGGING.md`).
  A named run is also recorded with `dispatch` (request None) as its root run.
- A run cancelled by `RUN_TIMEOUT` is not ACKed and runs again after it is reclaimed (up to 3 deliveries).
- Messages over the delivery limit and unreadable messages (including the legacy `agent.run` and `source.run` forms) stay in the dead stream. Read them with
  `docker compose exec redis redis-cli XREVRANGE 'te:{trackeverything}:dead' + - COUNT 20`.
  To run one again, fix the cause and publish its `data` JSON as is with the CLI. Rewrite a legacy form into the `EventRequest` shape before publishing it.
- Runs consume LLM tokens (see `docs/LOGGING.md` for cost queries).

## Webhook-triggered runs

For one-off webhook payloads and example commands, use the [configuration reference](CONFIGURATION.md#webhook-ingress).
The intake publishes the raw `EventRequest`, and the dispatch agent decides the business route.
To verify a run, send it to a dev run with the notification environment variables removed
([Development runs](#development-runs-isolated-from-the-live-service)).

