# core — shared infrastructure

[Documentation](../README.md) / [Concrete components](../components/README.md)

The source of truth for shared contracts and execution rules is [Architecture in diagrams](../ARCHITECTURE.html).
Start from the overview diagram and expand the contracts you need. The diagrams and the detailed contracts live in the same HTML.
[The core code guide](code-guide.html) summarizes each module's role, public symbols, and where they are used.
For concrete implementations see each component document; for logging details see [LOGGING.md](../LOGGING.md).

Import base classes and interfaces from `trackeverything.core.abc`, and ORM-independent log and error values from
their defining modules under `trackeverything.core.builtin.domain`.
`builtin/runtime.py` is the execution infrastructure; `builtin/app.py` is the instance configuration that connects the outer implementations.
Sessions, logging, and error handling live in builtin; connections and the core tables in orm; versioning in versioning;
shared state in shared_state.py. The LLM, the Redis Streams queue, intake (Webhook, Discord, Slack), and HTTP are owned by the outer llm, queue, and integration packages respectively.

| What you want to read | Source-of-truth diagram and contract |
|---|---|
| Flow from intake through execution to ACK (Queue and Runtime) | [Message flow](../ARCHITECTURE.html#message-flow) · §8 |
| Responsibilities, placement, dependency direction | [Code layout](../ARCHITECTURE.html#layout) · §1 |
| Agent, Context, Harness, Tool, LLM | [One execution](../ARCHITECTURE.html#execution) / [Agents and concurrency](../ARCHITECTURE.html#agents) · §2–4 |
| Source, execution request, Runtime | [Differences between inputs](../ARCHITECTURE.html#execution) · §5 |
| Persistence, sessions, shared state | [State and logs](../ARCHITECTURE.html#state) · §7, 9 |
| Component updates, leases, shared state | [Version switching](../ARCHITECTURE.html#versioning) → [Versioning details](../components/VERSIONING.md) |
| Host start, stop, and concurrent execution | [Startup and shutdown](../ARCHITECTURE.html#lifecycle) · §8, 10 |
| HTTP, invariants, verification | [Contracts and related material](../ARCHITECTURE.html#reference) · §11–13 |
