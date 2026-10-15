# Azuki

A Host runtime for versioned AI agents. It accepts events through HTTP, Discord, Slack, or the publish CLI, stores them
in Redis Streams, and executes them with agents loaded from a separate Git repository. PostgreSQL stores sessions and
execution evidence; the dashboard shows runs, the component catalog, and usage.

The default component set contains `dispatch`, `general`, and the `optimizer` entry agent. `general` can read, write,
edit, and run shell commands; its response stays in the run records. Chat replies, PR reviews, issue development, Sentry
auto-fix, and project-document workflows require additional components. See [component ownership](docs/EXTENSIONS.md).

[Documentation](docs/README.md) · [Architecture](docs/ARCHITECTURE.html) · [Contributing](CONTRIBUTING.md) · [Operations](docs/OPERATIONS.md)

## Layout

| Location | Responsibility |
|---|---|
| `src/trackeverything/core/` | Agent contracts, execution, persistence, and Git component versioning |
| `src/trackeverything/queue/` | Redis Streams, the shared Producer, reader, and publish CLI |
| `src/trackeverything/integration/` / `api/` | Event intake, scheduling, HTTP API, report serving, and dashboard |
| `src/trackeverything/llm/` / `optimizer/` | Host LLM adapters and component optimization |
| `src/trackeverything/agent/`, `tools/`, `skills/` | Defaults that seed a new local component repository |
| `tests/` | Host tests, including temporary Git repositories and dashboard logic |
| `alembic/` | Database migration history |
| `scripts/` / `gateway/` | Development and deployment commands; optional standalone HTTP gateway |
| `docs/` | Host contracts, configuration, and operations |
| `components/` / `data/` | Ignored component working copy and per-installation state; only generic data templates are tracked |

## Development

Use uv, standard CPython 3.14 (`.python-version` pins the GIL build), Git 2.40 or later, and Node.js 18 or later.
The psycopg driver needs the libpq client library. [pyproject.toml](pyproject.toml) owns dependencies and tool settings.

```bash
git clone https://github.com/haowron/azuki.git
cd azuki
uv sync --locked
uv run pytest
uv run ruff check src tests
uv run mypy src
uv run python scripts/render_core_guide.py --check
```

Tests mock external APIs, storage, and the LLM. They need no running PostgreSQL, Redis, Docker, or credentials.
Versioning tests use real Git in temporary directories; eight dashboard scenarios run through Node.js.
The [CI workflow](.github/workflows/ci.yml) runs these checks on pushes to `main` and pull requests.

## Run the application

Copy `.env.example` to `.env`, set an LLM provider key and the storage settings, and enable only the intake connections
you need. Runtime and queue are separate processes. Keep development storage, ports, and credentials separate from an
existing installation ([isolation](docs/OPERATIONS.md#development-runs-isolated-from-the-live-service)).

```bash
cp .env.example .env
# Edit .env before starting.
docker compose up -d db redis
bash scripts/run_local.sh alembic upgrade head
```

Run the following in separate terminals:

```bash
bash scripts/start_queue.sh     # python -m trackeverything.api
bash scripts/start_runtime.sh   # python -m trackeverything.core.builtin
```

Start exactly one queue and one runtime per DB. HTTP defaults to port 18080. The queue accepts input without LLM
credentials or a running runtime. On Ctrl-C, each process waits for work it has accepted.

Without `COMPONENT_REPO_URL`, runtime creates `data/components.git` with the default components and clones it into
`components/`. Edit and push from that working copy to change the next run's version. With `COMPONENT_REPO_URL` set,
runtime adopts loadable updates from that repository's `main` within `COMPONENT_FETCH_INTERVAL`.
See [component operations](docs/OPERATIONS.md#component-repository).

## Publish a run

Save an `EventRequest` to `message.json`:

```json
{"provider":"internal","external_id":"cli:investigation-1","session_id":"cli:investigation",
 "payload":{"agent_name":"general","request":"Investigate the target"}}
```

```bash
bash scripts/run_local.sh python -m trackeverything.queue.cli publish --file message.json
```

The dispatch agent delegates and ACKs at the end. Unacknowledged messages are reclaimed after 30 minutes; more than
three deliveries send a message to the dead stream. There is no deduplication.
Read the result in the dashboard's run view. [Queue](docs/components/QUEUE.md) defines publishing and delivery behavior.

## Optional deployment

The supplied LaunchAgent generation and automatic deployment scripts target macOS. The standalone gateway and
Cloudflare route are optional; they are not required for local use. Installation-specific paths and credentials belong
in local configuration. See [Operations](docs/OPERATIONS.md), [public routing](docs/CLOUDFLARE_TUNNEL.md), and
[the gateway](gateway/README.md).
