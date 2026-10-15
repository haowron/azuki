# Documentation guide

This repository documents the Host and its minimal default components. Application workflows are documented with the
component repository that implements them; [component ownership](EXTENSIONS.md) explains the boundary.
Each area below has one source of truth. Update that document with the code it describes.

| Area | Source of truth | Coverage |
|---|---|---|
| Architecture | [Architecture in diagrams](ARCHITECTURE.html) | Shared contracts, dependency direction, execution, and lifecycle |
| Core code | [Code guide](core/code-guide.html) | Module and symbol roles, usage, and constraints |
| Host and defaults | [Component index](components/README.md) | Host subsystems, default inventories, and creation guides |
| Queue | [QUEUE.md](components/QUEUE.md) | Producer, Redis Streams, ACK, redelivery, CLI, and process separation |
| Scheduling | [SCHEDULING.md](components/SCHEDULING.md) | Host polling of cron and reservation rows |
| HTTP and pages | [FASTAPI.md](components/FASTAPI.md) | API, report serving, authentication, and router injection |
| Dashboard | [DASHBOARD_SPEC.md](components/DASHBOARD_SPEC.md) | Run flow, catalog, and usage views |
| Discord and Slack intake | [DISCORD.md](components/DISCORD.md), [SLACK.md](components/SLACK.md) | Connections, input validation, filtering, and publishing |
| Component versions | [VERSIONING.md](components/VERSIONING.md) | Git store, loading, compatibility, per-run versions, and hooks |
| Sessions | [SESSION.md](components/SESSION.md) | Session, run, channel, and source-link persistence |
| Optimizer | [OPTIMIZER.md](components/OPTIMIZER.md) | Host-owned checks and their component entry point |
| Logging | [LOGGING.md](LOGGING.md) | Execution events, storage, costs, and queries |
| Configuration | [CONFIGURATION.md](CONFIGURATION.md) | Host environment, payloads, and component compatibility settings |
| Operations | [OPERATIONS.md](OPERATIONS.md) | Migrations, deployment, isolated development, and queue commands |
| Public routing | [CLOUDFLARE_TUNNEL.md](CLOUDFLARE_TUNNEL.md), [gateway](../gateway/README.md) | Optional gateway, authentication, and deployment |
| Verification | [BUSINESS_TESTS.md](BUSINESS_TESTS.md) | Cases, spec mapping, and mock boundaries |
| Development | [Contributing](../CONTRIBUTING.md), [AGENTS.md](../AGENTS.md) | Prerequisites, working rules, and required checks |

[ARCHITECTURE.md](ARCHITECTURE.md) is a short navigation entry for the architecture HTML.
[The dependency graph](dependencies.html) adds module import relations; edit [dependencies.mmd](dependencies.mmd)
and synchronize its content into the HTML.
The [core code guide](core/code-guide.html) is generated from [code-guide.json](core/code-guide.json) and the code's AST:

```bash
uv run python scripts/render_core_guide.py --check
uv run python scripts/render_core_guide.py
```

The check reports missing or stale descriptions and excerpts and differences between Figure 1 and actual imports.
Keep dated plans, runtime documents, and installation-specific notes outside the tracked specification tree.
