"""HTTP API adapters + web UI (outer application layer).

FastAPI lives here — outside ``core`` on purpose: the runtime is fully usable
without any HTTP management surface. Each module builds one router that reads
the runtime; the queue process's composition root injects it into the shared
HTTP server (``api/app.py`` → ``create_webhook_app(producer.publish,
extra_routers=...)``), so core never imports this package. Every router
authenticates with ``auth.require_token``.

- `catalog` → `trackeverything.api.catalog.create_snapshot_catalog_router`
  (``GET /api/catalog`` — live agent/tool/harness graph + declared inventory)
- `usage` → `trackeverything.api.usage.create_usage_router`
  (``GET /api/usage`` — per-agent/tool invocation counts from the logs)
- `runs` → `trackeverything.api.runs.create_runs_router`
  (``GET /api/runs[/{run_id}]``, ``GET /api/sessions/{session_id}`` — run-flow screen)
- `webui` → `trackeverything.api.webui.create_webui_router`
  (``GET /`` — dashboard shell + static assets over the management API)
- `reports` → `trackeverything.api.reports.create_reports_router`
  (``GET|HEAD /reports/*`` — fixed review documents and versioned Mermaid)
- `__main__` / `app` / `http` → the queue process: its entry (``python -m trackeverything.api``),
  composition root (producer, connectors, these routers, ``/readyz``) and uvicorn server

See `inventory.md` for the shipped routers and the resource-group blueprint.
"""

from trackeverything.api.catalog import create_snapshot_catalog_router
from trackeverything.api.reports import create_reports_router
from trackeverything.api.runs import create_runs_router
from trackeverything.api.usage import create_usage_router
from trackeverything.api.webui import create_webui_router

__all__ = [
    "create_reports_router",
    "create_runs_router",
    "create_snapshot_catalog_router",
    "create_usage_router",
    "create_webui_router",
]
