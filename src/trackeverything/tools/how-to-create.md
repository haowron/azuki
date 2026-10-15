# Creating a tool

[components](../../../docs/components/README.md) / [Shared contracts](../../../docs/ARCHITECTURE.html)

A tool declares `name / description / arguments` and implements the coroutine
`async def run(args, *, ctx=None) -> ToolResult`.
Fixed tools are declared in an agent's `tools` and must be constructible without arguments.
The Host drives every run on one event loop, so I/O in `run` is awaited, and no synchronous call may block the loop.

## Minimal implementation

```python
"""A tool that writes a value into the shared agent data."""

from __future__ import annotations

from typing import Any, ClassVar

from trackeverything.core.abc.context import AgentContext
from trackeverything.core.abc.tool import Tool, ToolResult


class WriteDataTool(Tool):
    """Adds or replaces a value in the shared agent data."""

    name: ClassVar[str] = "write_data"
    description: ClassVar[str] = "Add or replace a value in the shared agent data."
    arguments: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"key": {"type": "string"}, "value": {}},
        "required": ["key", "value"],
    }

    async def run(self, args: dict[str, Any], *, ctx: AgentContext | None = None) -> ToolResult:
        if ctx is None:
            return ToolResult(ok=False, error="AgentContext is required.")
        key = args.get("key")
        if not isinstance(key, str) or not key or "value" not in args:
            return ToolResult(ok=False, error="A non-empty key and a value are required.")
        ctx.data[key] = args["value"]
        return ToolResult(ok=True, output=ctx.data[key])
```

Keep the declaration style (docstrings, `ClassVar` annotations, order) the same as in [Creating an agent](../agent/how-to-create.md#minimal-implementation).
The same assignment handles both adding and replacing. The Context is injected on the Python side and never appears in the LLM's JSON arguments.
Failures become `ToolResult(ok=False)`, and any exception that escapes is caught by `BaseAgent.call_tool()`.
When the failure tracking target is narrower than the full arguments (such as `(project, name)`), override
`error_key(args)`. A success clears earlier failures with the same key, and the run boundary uses the unresolved ones for arbitration.

Implement `llm_args` and `error_key` as ordinary functions without I/O.

## Writing I/O

- Use `httpx.AsyncClient` for HTTP (`async with httpx.AsyncClient(timeout=...) as client:` per call).
- Start child processes with `core.builtin.process_registry.spawn_exec` / `spawn_shell` and `start_new_session=True`, bound them with
  `asyncio.timeout(...)`, and on exit, timeout, or cancellation alike call
  `await stop_process_group(process)` from the Host's `core/builtin/base_tools.py` in `finally` to reap the group.
  For short runs such as Git, use `run_captured(argv, timeout=...)`, and for runs with an output cap `run_bounded(...)`, from the same
  Host module; page long text with its `text_window`. Do not copy these helpers into a component.
- Run synchronous-only libraries and heavy computation or disk work (tree hashing, zip extraction, rendering whole documents, and so on)
  outside the loop with `await asyncio.to_thread(fn, ...)`. Small file reads and writes can be done directly.
- Wait with `await asyncio.sleep(...)`. Never swallow `asyncio.CancelledError`
  (`except Exception` is fine; `except BaseException` only when re-raising).
- For shared state within a run where an await sits between the check and the update, either have concurrent calls await one task
  stored in `ctx.tool_state` or make the operation atomic with a lock (example: `fetch_source`).

A tool that separates the argument space between the LLM and Python control overrides the side-effect-free `llm_args(args)`.
Only the LLM dispatcher passes the converted arguments to `call_tool()`.
`read / save` prefixes the identity with `note:` at this entry point, separating state from conversation notes.
Invalid input is passed on to the validation in `run()`, and the input dict is never modified. Ordinary tools use the default, which converts nothing.

## Configuration and registration

A tool reads its own configuration and uses the shared store when one exists.
Open the store inside the `try` of `run` through `open_*()` in `core.builtin.database` (`shared_backend()` for memory),
and keep no connections or settings in the constructor. Constructor arguments are only for values injected by the composition root (the
Source table of `fetch_source`, skill bindings).

```python
class ReviewAgent(BaseAgent):
    tools: ClassVar[tuple[type[Tool], ...]] = (WriteDataTool,)
```

`ReviewAgent(llm)` constructs the declared tools.
Use `register(tool)` to add tools at composition time and `call_tool(ctx, call)` to run them.

## Use from skills

Shared tools register a name and factory with `bind_skills(..., tool_factories=...)` in `agent/compose.py`.
When a skill references one from tools.json, as in `{"ref": "github_search"}`, it is added to that call after the main file loads.
The existing instance is reused, and overriding it with a different implementation is rejected. The existing tool keeps its permission checks and Context injection.

To turn a skill's own script into a separate function, declare
name / description / arguments / script as described in the [skill authoring rules](../skills/how-to-create.md). The adapter goes through the ordinary call_tool
and returns invalid arguments, non-zero exit, timeout, invalid JSON, and oversized output as ToolResult failures.

## Loading as a version

Agents, tools, harnesses, Sources, and skills are version-owned: they live in the component repository, and a version is a
commit of it. A change pushed to the repository's `main` (checked by the pre-push hook) is adopted by the runtime within
`COMPONENT_FETCH_INTERVAL` and used from the next run that starts. A run in progress finishes on the version it started with
([Component versioning](../../../docs/components/VERSIONING.md)).
The check, the runtime's adoption, and a fork load every module and assemble the component graph before using it, so follow these rules.

- Imports and constructors only define things. Network access, saving, notifications, process starts, and LLM calls belong in `run` / `_execute` at execution time.
- Caches and callbacks that must be single across versions stay in the module and are obtained with
  `shared("<component>.<name>", factory)` (`core/shared_state.py`). For exclusion across awaits use
  `async with lock.hold():` on `task_lock("<component>.<name>")`, and for exclusion per key use `async with keyed_lock(namespace, key):`.
  A lock guards a single operation, and no agent is called inside it. Locks are not re-entrant: requesting the same lock while holding it
  raises `RuntimeError` without waiting.
  Values are limited to the standard library or plain data; use a different name when the shape changes.
- Load owned modules with import statements. Loading by string, such as `importlib.import_module("trackeverything.tools.x")`,
  is rejected when the version is loaded, and loading the version fails.
- Bundled resources can be read relative to `Path(__file__)` (which points to the version's immutable file view). Files outside the five folders
  are read from Host locations in `config` (for example, `config.STATIC_DIR`).
- Never pickle instances of component classes or pass them between versions (each version has its own classes).
- The Host never calls component modules. To queue a follow-up request with the running message as parent, call the Host's
  `publish_child(payload, key, *, session_id=None)` from `trackeverything.queue.child`. Periodic work is a tool run by an agent that
  a `schedule` row starts (for example `maintenance`).

## Verification

Tool behavior is out of scope for pytest. After a change, make the following pass.

```bash
uv run pytest
uv run ruff check src tests
uv run mypy src
```

Start child processes with `spawn_exec` / `spawn_shell`, and await `trackeverything.core.builtin.base_tools.stop_process_group`
in finally to reap the group before returning. Never start a subprocess on your own and bypass the cleanup.
