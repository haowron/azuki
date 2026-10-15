# Agent inventory (package defaults)

[components](../../../docs/components/README.md) / [Creating an agent](how-to-create.md)

These are the agents of the package's default version. A local installation (`COMPONENT_REPO_URL` unset) makes them its
first `main` and edits them from then on in the working copy `components/`; with a component repository configured, its own
agents are used instead ([Component versioning](../../../docs/components/VERSIONING.md)). Every agent also has the Host base tools `read`, `write`, `edit`, and `bash`, built into `BaseAgent`
([tool inventory](../tools/inventory.md)).

| Class / name | Responsibility | Tools / children |
|---|---|---|
| `DispatchAgent` / `dispatch` | The only entrance (Python control, no LLM). A named run (`provider="internal"` with `agent_name`) goes to the declared child it names, with its `data` as `ctx.data` and its `request`; every other event, whatever its provider, goes to `general` with the event's `content` or `request` text, or else the event JSON (at most 20,000 characters), as the request. ACKs the queue message at the end | base tools / `general`, `optimizer` |
| `GeneralAgent` / `general` | LLM loop that carries out the request with the base tools and returns the result. It has no reply tool, so the result stays in the run records | base tools / none |
| `OptimizerAgent` / `optimizer` | Named-run entry (no LLM) to the Host's optimizer (`src/trackeverything/optimizer/`, not part of a version). Passes `ctx.data` to `optimizer.handle()` and returns the report ([Optimizer](../../../docs/components/OPTIMIZER.md)) | base tools / none |

The package defaults have no Sources (`SOURCES` is empty) and no harnesses. `compose.py` wires the mesh when `AGENT_FULL_MESH` is set, and binds skills (there are no concrete skills).
