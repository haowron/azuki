# HTTP management modules

[components](../../../docs/components/README.md) / [FastAPI management documentation](../../../docs/components/FASTAPI.md)

- `auth.py`: `require_token`, the shared token validation for the `/api` routers (catalog, usage, runs). `X-Webhook-Token` is required only when `WEBHOOK_TOKEN` is set.
- `catalog.py`: `GET /api/catalog`. Connection graph and declarations of running Agents, Tools, Harnesses, and Sources.
- `usage.py`: `GET /api/usage`. Call counts for agent / tool events in `logs`; aggregation in `core/builtin/execution_log.read_usage`.
- `runs.py`: `GET /api/runs`, `GET /api/runs/{run_id}`, `GET /api/sessions/{session_id}`. Run summaries, ordered steps, bounded previews, and error causes for the run-flow screen; reads in `core/builtin/execution_log.read_*`. LLM / Agent requests are never returned.
- `webui.py`: Router serving `static/index.html` and `static/*` assets.
- `reports.py`: Anonymous GET / HEAD for completed PR review HTML and Mermaid assets pinned by hash. Reads only regular files within the dedicated root.
- `static/index.html`: CSS and HTML shell. Header navigation switches between Run flow (default `#/runs`), Agent / Tool catalog, and Usage.
- `static/js/api.js`: `DashboardApi`, the per-endpoint contracts for `/api/catalog`, `/api/usage`, `/api/runs`, and `/api/sessions`.
- `static/js/app.js`: Dashboard shell: save and restore the API token, inject it into `api.js`, and select the default screen.
- `static/js/usage-model.js`: Pure usage logic: match aggregates to registered entries, sort, and break down statuses.
- `static/js/usage.js`: Usage screen control: fetch, render aggregates, and filter search results.
- `static/js/catalog-model.js`: Pure catalog logic: construct Mermaid diagrams, search, and summarize arguments.
- `static/js/catalog.js`: Catalog screen control: switch screens, fetch, render diagrams, and zoom.
- `static/js/runs-model.js`: Pure run-flow logic: step tree, Mermaid flowchart / gantt / session diagrams, replay time warp and per-time states.
- `static/js/runs.js`: Run-flow screen control: resolve run or session ids, recent runs, render, replay, follow, and step details.
- `static/js/vendor/mermaid.min.js`: Bundled Mermaid 11. Loaded by the catalog and run-flow screens and copied to persistent assets by hash during report publication. No CDN is used.

The FastAPI management documentation is authoritative for routes, contracts, authentication, and composition-time injection.
