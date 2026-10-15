# AGENTS.md

## Source of truth

Read [docs/ARCHITECTURE.html](docs/ARCHITECTURE.html) before implementing. It owns shared contracts and invariants.
Use [docs/README.md](docs/README.md) to find the document that owns an area, and update it with the code it describes.
The [Host and default-component index](docs/components/README.md) links to the component inventories and creation guides.
Application-specific agents, tools, skills, harnesses, Sources, and their specifications belong to their component repository
([ownership](docs/EXTENSIONS.md)).

## Local overrides

Read `AGENTS.local.md` from the root of the main checkout if present; it is gitignored and its rules take precedence here.
From a linked worktree, the main checkout is the parent directory of
`git rev-parse --path-format=absolute --git-common-dir`. Do not publish local rules, credentials, or runtime data.

## Development

- Use standard CPython 3.14, uv, uv_build, ruff, mypy strict, and pytest. [pyproject.toml](pyproject.toml) owns dependencies
  and tool settings. `.python-version` pins `3.14+gil`; a bare `3.14` may select a free-threaded build. pyenv does not resolve
  this specifier, so run project Python through `uv run`.
- The package lives under `src/trackeverything`. Use absolute imports: `from trackeverything.<module> import ...`.
- Project documentation (Markdown, HTML, inventories, READMEs) is written in English. Preserve code identifiers and
  quoted runtime strings exactly.
- Before finishing, run `uv run pytest`, `uv run ruff check src tests`, and `uv run mypy src`, and make all pass.
  [Business tests](docs/BUSINESS_TESTS.md) owns the cases and placement. The suite needs Git 2.40 or later and Node.js 18
  or later for the eight dashboard scenarios; PostgreSQL, Docker, and credentials are not needed.
- Component behavior is checked by loading (`check`) and evaluations in the component repository, outside the Host's
  pytest scope. Host tests use temporary component fixtures to verify versioning contracts.
- When changing the core code guide or code it describes, run `uv run python scripts/render_core_guide.py --check`.
  Edit `docs/core/code-guide.json` and regenerate `docs/core/code-guide.html` when needed.
- Keep one commit per logical change. Prefixes: `feat(<area>):`, `refactor:`, `chore:`, `docs:`, `test(<area>):`.

## Operating boundaries

Read [Operations](docs/OPERATIONS.md) before schema changes, deployment, running a development instance, or maintenance
against a running service. It owns the migration procedure, deployment, component updates, isolation, and queue commands.

- Alembic alone owns the schema. Generate revisions from ORM changes against a scratch DB, review them, and run
  `uv run alembic check`. Never edit an applied revision; keep migrations compatible with the running code.
- Run exactly one queue and one runtime per DB. Maintenance and publishing use the running checkout's launcher.
  Development uses a separate DB, Redis namespace, port, and component paths, with bot credentials disabled.
- Host code reaches the running service through deployment; component code through its repository's shared `main`.
  Do not automatically deploy interpreter changes. Tests and documentation alone need no service restart or deployment.
