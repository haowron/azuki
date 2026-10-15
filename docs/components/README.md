# Host subsystems and default components

[Documentation](../README.md) / [Shared architecture](../ARCHITECTURE.html) / [Component ownership](../EXTENSIONS.md)

The Host lives under `src/trackeverything/`. Its bundled `agent/`, `tools/`, and `skills/` supply the first local component
version. An installed component repository owns its additional agents, tools, Sources, harnesses, skills, and their docs.

| Default component | Inventory | Creation guide |
|---|---|---|
| Agents | [dispatch, general, optimizer](../../src/trackeverything/agent/inventory.md) | [Create an agent](../../src/trackeverything/agent/how-to-create.md) |
| Tools | [Base tools and skill loading](../../src/trackeverything/tools/inventory.md) | [Create a tool](../../src/trackeverything/tools/how-to-create.md) |
| Skills | No concrete skills bundled | [Authoring rules](../../src/trackeverything/skills/how-to-create.md) |

Defaults have no Sources or harnesses. Their interfaces belong to the [architecture contracts](../ARCHITECTURE.html).
`agent/compose.py` builds the graph; dispatch is the only entrance. Child agents are declared by name, and runtime
resolves them through the run's version handle. See [Versioning](VERSIONING.md) for loading and compatibility.

| Host subsystem | Contract | Implementation |
|---|---|---|
| Queue and process separation | [Queue](QUEUE.md) | `queue/`, `api/app.py`, `core/builtin/app.py` |
| Scheduled publishing | [Scheduling](SCHEDULING.md) | `integration/schedule.py`, `schedule` and `reservation` tables |
| HTTP and report serving | [FastAPI](FASTAPI.md) | `api/` |
| Dashboard | [Views](DASHBOARD_SPEC.md) | `api/static/` |
| Component versions | [Versioning](VERSIONING.md) | `core/versioning/`, `core/shared_state.py` |
| Sessions and channel links | [Sessions](SESSION.md) | `core/builtin/`, `core/orm/` |
| Discord intake | [Discord](DISCORD.md) | `integration/discord.py` |
| Slack intake | [Slack](SLACK.md) | `integration/slack.py` |
| Optimization | [Optimizer](OPTIMIZER.md) | `optimizer/`; bundled entry agent `optimizer` |

[Optional application components](../EXTENSIONS.md) own conversations, development and review workflows, document
creation, notifications, and Sentry automation. Their detailed specifications and evaluations live with their code.
