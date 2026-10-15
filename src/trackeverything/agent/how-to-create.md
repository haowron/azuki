# Creating an agent

[components](../../../docs/components/README.md) / [shared contracts](../../../docs/ARCHITECTURE.html)

An agent inherits `BaseAgent` and declares `name / description / arguments`. The public coroutine
`run(call) -> AgentResult` manages harnesses, logging and execution. To change the Python control flow, override
`async def _execute(call)`. Make processing that involves I/O (tools, child agents, LLM, stores, network) a coroutine
and `await` it, and keep `get_prompt` and `_build_*` synchronous functions without I/O. Load inputs that need I/O (such
as conversation history) in `_execute` before handing them to the standard loop. `run()` is the boundary that also owns
failures: exceptions and failure results without errors are converted into structured `errors`. A stage failure returns
an error result with `ErrorNote(stage, cause)`, and tools whose unrecovered failure must fail the run are declared in
`fatal_tools`. An entry that receives material from a Source definition (an event route of the github agent, or a
named run that specifies `source`) declares `fetch_source` in `fatal_tools` and `hidden_callables`, and fetches at the
start of `_execute` with `if (failed := await require_source(self, call)) is not None: return failed`
(`tools/source_fetch.py`; `agent/compose.py` injects it into every instance that declares `fetch_source` in
`fatal_tools`).

## Minimal implementation

```python
"""An agent that reviews the current input material."""

from __future__ import annotations

from typing import Any, ClassVar

from trackeverything.core.abc.agent import BaseAgent


class ReviewAgent(BaseAgent):
    """Checks the current input material and summarizes the result."""

    name: ClassVar[str] = "review"
    description: ClassVar[str] = "Review the current input material and summarize the result."
    arguments: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"request": {"type": "string"}},
    }

    def get_prompt(self) -> str:
        return "Check data.identity and data.payload and summarize the result."
```

Every agent, tool and harness has the same shape. Write a module docstring and a one-sentence class docstring (the
codebase writes them in Japanese; the examples here use English), and put `from __future__ import annotations` first.
Annotate class variables with `ClassVar[...]`, as the base class does, and declare only the values that differ from the
base, in the order `name / description / arguments / llm / tools / skills / agents / pre / post / on_error /
fatal_tools / hidden_callables`. Write the LLM-facing `description` in English. Order the methods `__init__` (only when
`agent/compose.py` injects values through `dependencies`) → `get_prompt` → `_build_*` → `_execute` → helpers.

The default `_execute()` repeats calling the LLM, running tools and child agents, and adding observations. It uses the
shared step budget of the Context and ends on a `FinalAnswer` or when the budget runs out. To use this loop in the
middle of custom control, call `await super()._execute(call)`. Child-agent calls in the same response run in parallel,
and tool calls run one at a time in call order.

Send every LLM call through `self._respond_one_step(call.ctx, messages, callables)`, never `self._llm.respond` directly:
it spends the shared step budget and puts the version's `agent/SYSTEM.md` in front of the messages it sends, without
changing your list. `get_prompt` therefore holds only what is specific to the agent; instructions that hold for every
agent belong in `agent/SYSTEM.md` ([System preamble](../../../docs/components/VERSIONING.md#system-preamble)).

## Input and delegation to children

`AgentCall.request` is specific to the call, while `AgentContext.data` and the step budget are shared between parent
and child. Child runs, whether from an LLM call or from Python control, go only through
`await self.call_agent(call, name, request)`: never call a child's `run` directly, and let `call_agent` build the call
passed to the child. Shared data is not copied. `call_agent` gets the declared child's instance by name from the run
handle (`ctx.agents`) and runs it in the same Context; for a name that is not a child, or whose instance cannot be
obtained, it does not run anything and returns an error result with
`ErrorNote("delegation", "agent not registered: <name>")`. `has_agent(name)` tells whether a name is a child, and
`child_meta(name, ctx)` returns the child's `name / description / arguments` to present to the LLM. A parent under
Python control waits for a child whose result it uses before making the next call. Children that do not read each
other's results can run in parallel by passing the `call_agent` coroutines to `settled()` (`agent/fanout.py`), which
returns the results in argument order. Children run in parallel must not read and write the same `ctx.data` /
`ctx.tool_state` keys across an `await`. The session and run IDs are shared in the same Context as well. Whether there
is a parent can be told from `current_parent_agent_execution_id` (`core.builtin.domain.execution_log`). Never use
values in the shared data to identify parent and child or to grant execution permissions. See the
[input contract](../../../docs/ARCHITECTURE.html#contract-2) and the
[session infrastructure](../../../docs/components/SESSION.md).

To narrow the input shown to the LLM, override `_build_user_message()`.

```python
def _build_user_message(self, call: AgentCall) -> str:
    return json.dumps({
        "request": call.request,
        "identity": call.ctx.data.get("identity"),
        "payload": call.ctx.data.get("payload"),
    })
```

## Declaring tools, child agents, harnesses and the LLM

Tools and harnesses are declared as tuples of types; child agents and the LLM are declared by name. `__init__` builds
the tools without arguments and the harnesses with this agent's LLM, and does not create child agents.

```python
class ReviewAgent(BaseAgent):
    llm: ClassVar[str] = "high"
    tools: ClassVar[tuple[type[Tool], ...]] = (MyTool,)
    agents: ClassVar[tuple[str, ...]] = ("detail",)
    pre: ClassVar[tuple[type[PreHarness], ...]] = (BusinessHoursHarness,)
    post: ClassVar[tuple[type[PostHarness], ...]] = (NoSecretLeakHarness,)
```

`llm` is a key of the Host's `HostServices.llms` (`max`, `high`, `low`, `flash`, `flash_low`), and the default is `max`. An agent receives
one LLM at initialization and never switches it during execution. Children do not inherit the parent's LLM and use the
LLM they declare. A stage that needs a different LLM goes into a child agent that declares that `llm`. Without Z.AI
configured, all five are the main client.

Add a new agent class to `AGENTS` in the version's `agent/compose.py`. `ENTRANCES` is `dispatch` only: an agent run
from the queue declares its name in the dispatch agent's `agents` and receives delegation through an event route or an
`internal` named run (`agent_name` in the payload). Pass dedicated constructor arguments such as the conversation
history or the Source dictionary to `dependencies` of `build()`, keyed by the agent's name. The Host's `build_agents`
(`core/builtin/compose.py`) creates each instance once as `cls(llms[cls.llm], logger=logger,
**dependencies.get(cls.name, {}))` and checks that the names in every instance's `agents` are in the list. Children are
obtained by name from the run handle, so the entry and every parent share the instance of a child with a given name.
Keep per-run mutable state in the Context, not in the instance. A class without a name, a duplicate name, an LLM name
missing from `llms`, a child name missing from the list, or `dependencies` for a name missing from the list is a
version composition failure (the main version at startup aborts the startup; the pre-push check stops the push; a fork
keeps nothing; the runtime does not adopt such a `main`).

`ComponentGraph.agents` holds every agent of the version, and `entrances` holds only the entrances the composition
module lists; a child-only agent never becomes an entrance. There is no `register_agent`, `list_agents` or
`replace_agent`; `build_agents` passes the logger to every instance. For declarations not connected to the graph, the
catalog (`core/builtin/catalog.py`) shows the declared child names as they are.

`BaseAgent` builds the Host base tools `read`, `write`, `edit`, and `bash` (`core/builtin/base_tools.py`) into every instance,
so do not declare them in `tools`; a Tool with one of those names fails composition. The standard LLM loop presents them
too. Control flows that choose the presented callables themselves (single-shot classification and the like) select the
base tools they need explicitly ([tool inventory](../tools/inventory.md)).

Declare tools and child agents that only Python control calls in `hidden_callables: ClassVar[frozenset[str]]` to remove
them from the LLM callables. `call_agent` can still call hidden children. The declaration is applied together with those
of the base classes. Use `register()`, `register_pre_harness()` and `register_post_harness()` for additions at
composition time. Children are added only through the `agents` declaration. Put tool side effects behind
`await self.call_tool()`. Harnesses are always evaluated and are never part of the LLM callables.

When the experimental setting `AGENT_FULL_MESH` is enabled, the composition adds all tools and the names of all agents
of the version to every agent through `extend_callables()`. Declared registrations take precedence, and an agent never
calls itself as a child. Delegation cycles are bounded by the shared step budget and the delegation depth limit
(`MAX_AGENT_DEPTH`). Individual agent implementations are not aware of this setting. See
[How to create a tool](../tools/how-to-create.md); harnesses are declared the same way (`pre` / `post`).

`call_tool()` under Python control uses state keys as they are. For LLM tool calls, the dispatcher passes the arguments
through `Tool.llm_args()`, which restricts `read / save` to the `note:` area for conversation notes. Never leave state
updates such as comparison baselines or processed input to the LLM.

## Declaring skills

Name the packages an agent uses explicitly with `skills: ClassVar[tuple[str, ...]] = ("evidence-summary",)`. The
composition calls `bind_skills()` once after the mesh. The standard loop adds the catalog to the system message; the body
arrives as the result of read_skill, and new tool schemas arrive in the next LLM callables. Direct generation alone
does not read the files. Dynamic tools are discarded per run and are never put into the fixed tools' `register()`. See
the [skill authoring rules](../skills/how-to-create.md).

## Loading as a version

Agents, tools, harnesses, sources and skills are owned by the version: they live in the component repository, and a
version is a commit of it. A change is pushed to the repository's `main` (the pre-push hook loads it with the deployed Host
first), the runtime adopts it within `COMPONENT_FETCH_INTERVAL`, and runs that start afterwards use it. A running run
finishes with the version it started with ([component versioning](../../../docs/components/VERSIONING.md)). The check, the
runtime's adoption, and a fork load every module and assemble the component graph before using a version, so follow these
rules.

- Import and constructors only define things. Network calls, saving, notification, starting processes and LLM calls
  belong in `run` / `_execute` at execution time.
- Keep caches and callbacks that must be single across versions in the module, and obtain them with
  `shared("<component>.<name>", factory)` (`core/shared_state.py`). Agents do not take locks; operations that need
  mutual exclusion go inside a tool ([How to create a tool](../tools/how-to-create.md)). Values are limited to the
  standard library or plain data, and a change of shape uses a different name.
- Read owned modules with import statements. Loading by string, such as
  `importlib.import_module("trackeverything.tools.x")`, is rejected by version loading, and loading the version fails.
- Bundled resources can be read relative to `Path(__file__)` (it points to the version's immutable file view). Files
  outside the five folders are read from Host locations in `config` (for example `config.STATIC_DIR`).
- Never pickle instances of component classes or pass them across versions (each version has its own classes).

## Verification

Agent behavior is outside pytest (the exceptions are the dispatch agent's button-approval re-validation VR51 and the
named run UR03). When you change an agent, make these pass.

```bash
uv run pytest
uv run ruff check src tests
uv run mypy src
```
