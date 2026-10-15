# Contributing

Read [the architecture](docs/ARCHITECTURE.html), [the documentation guide](docs/README.md), and [AGENTS.md](AGENTS.md)
before making changes. Keep the implementation and its owning specification together. Application-specific components
and their evaluations belong to the component repository; see [ownership](docs/EXTENSIONS.md).

Use the prerequisites and verification commands in [Development](README.md#development). The default test suite needs
no credentials or running service. For a live development instance, follow [the isolation procedure](docs/OPERATIONS.md#development-runs-isolated-from-the-live-service).

Keep changes focused, describe the resulting behavior and verification evidence, and document any remaining limitation.
Never include `.env`, runtime data, or installation-specific configuration. Schema and deployment changes follow
[Operations](docs/OPERATIONS.md).
