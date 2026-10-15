# Execution logs

Log values and correlation state are owned by `core/builtin/domain/execution_log.py`, and error values and the failure
ledger by `core/builtin/domain/errors.py`. Neither depends on the ORM.
`LogRecorder` is the minimal contract in `core/abc/execution_log.py`. Storage and queries are owned by
`builtin/execution_log.py`, which also converts to and from the ORM.
LLM call measurement and cost calculation in the outer `llm/tracking.py` and `llm/pricing.py` use the shared log
contract.

An LLM fallback leaves the exception type, `run_id`, `parent_execution_id`, `llm_execution_id`, and `primary_ms` in the process warning
log. `primary_ms` is the time from the start of the primary client's call to its failure; it includes retries inside
the SDK and waits inside the client, and excludes the time of the backup client. It uses the shared sleep-aware clock.
`llm_execution_id` is the existing `logs.execution_id` assigned by `TrackedLLM` for this logical call. It distinguishes
concurrent calls within the same run and parent agent. The call's ContextVar is restored on success, failure, or cancellation,
including nested calls. Without `TrackedLLM`, this ID is `None`; outside a run, the run and agent IDs are `None`.
When the LLM event is recorded, its ID gives an exact match. Older warnings are matched by run, parent agent, and time;
simultaneous calls can leave more than one candidate. Individual SDK retry times are not measured.
Prompts, responses, external exception bodies, and connection URLs are never added to the warning. Fallback
classification, call counts, results, and cancellation are unchanged.
Acceptance is checked by the 14 scenarios in `tests/entry/test_llm_fallback_telemetry.py`.

[Docs guide](README.md) / [core](core/README.md)

This document is the source of truth for the storage format, correlation, cost, and queries of logs.
Logs are stored only in the `logs` table of PostgreSQL (the `db` service, `DATABASE_URL`); there is no SQLite
fallback. It uses the ORM's shared engine and opens a short-lived session for each append. A failure to save a log is
reported to the process log and does not change the execution result.

## Recording boundary

| event_type | Recorded by | Contents |
|---|---|---|
| `agent` | `BaseAgent.run()` | Request, Context snapshot, AgentResult |
| `tool` | `BaseAgent.call_tool()` / `_dispatch()` | Called tool name (as the LLM specified it), arguments, success or failure, duration. The tool output is not stored because it is already in the agent event's conversation. Attempts to call an unknown name that is neither a tool nor a child agent are also recorded as `error` |
| `llm` | `TrackedLLM.respond()` | Messages, callables, response, usage, cost |
| `harness` | `BaseAgent.run()` | Pre/post checks and the ErrorHarness reaction on failure (`phase: error`). When post rejects, the result before rejection is also kept |
| `cli` | `ReviewTool` | CC review version, model, usage, duration, termination reason, the CLI's final output and how it was read |

New sources and agents are also recorded at the shared boundaries. LLM providers are wrapped in TrackedLLM at
composition time.
Fetching a source is recorded as a `tool` event of the in-run `fetch_source` call, and there is no separate `source`
event type (the snapshot itself is in the agent event's Context and conversation).
Agent and LLM requests / results are neither truncated nor redacted, so credentials they contain may be stored.
`cli` limits the payload size per event.
ReviewTool records one event per attempt after input validation, using the same run_id and parent execution_id as the
agent.
Failures during preparation are included, and `started_at` and `duration_ms` cover preparation through cleanup.
`result_json.phase` is the last phase reached, `phase_ms` is the duration of each phase, and `remaining_ms` is the
time left until the overall deadline (minimum 0).
The SDK runtime version is taken from the init message, or `unknown` if it cannot be obtained.
The phases are `source / workspace / checkout / materials / project_snapshot / input / cli / freshness` (the slot
reads the PR's version first and then fetches with the head specified).
`result_json.duplicate` marks an attempt that skipped starting the CLI in review mode because a review of the same head
was running or had succeeded.
`result_json.reviewer_states` keeps the structurally validated `complete / not_applicable / partial` of the 5 roles;
it is null when output was not obtained, the output was invalid, or in arbitration mode. Per-role durations are not
included.
`gap_counts` keeps limitation counts in the categories below. Not reached, not obtained, or not applicable is null;
checked with no limitation is 0.

| key | What is counted |
|---|---|
| `preparation` | Checkout and material preparation limitations settled before the CLI starts |
| `reported` | `gaps` in structurally validated output. In arbitration mode, the valid `gaps` of the output; null when they were invalid and ignored |
| `reviewers` | Number of reviewers that returned `partial` |
| `freshness` | 1 if the version lookup at the end failed, 0 if it succeeded. null in modes that do not look it up |

Counts are numbers of diagnostic items, not numbers of distinct problems. Diagnostics obtained before a CLI failure are
kept, and an execution failure is read together with the existing `status / error_json.code`.

Every attempt that started the CLI (all three modes, success or failure) keeps the CLI's final output in
`result_json.output`: the result envelope as JSON text, including the report or the judgments exactly as the model
returned them. It is cut at `REVIEW_OUTPUT_LIMIT_BYTES` (1 MiB) bytes, and `output_truncated` is true when cut. Both are
null / false when the CLI did not start (busy, duplicate, preparation failure).
`result_json.output_check` says how the output was read:

| key | Contents |
|---|---|
| `stage` | `ok`; `not_json` (the output is not JSON); `no_result` (no `result` element); `result_not_json` (no `structured_output` and the `result` text is not JSON); `output_limit` (the final result exceeds 1 MiB); `schema` (the report, or the outer shape of the judgments, does not match the contract); `mismatch` (arbitration only: the judgments were read but some part did not match) |
| `errors` | Validation failures as `<location>: <type>` without values, at most 10 (`schema`); in arbitration, also unknown top-level keys and `gaps: invalid` |
| `expected` | Arbitration only. The node_ids requested (`__actionable`), in thread order |
| `used` | The node_ids whose judgment was used |
| `missing` | Requested node_ids with no usable judgment (not returned, only invalid elements, or conflicting duplicates) |
| `extra` | node_ids returned that were not requested (including mistyped ids) |
| `invalid` | Elements that failed validation on their own: `{index, node_id, errors}` |
| `duplicated` / `conflicting` | Requested node_ids returned more than once / those whose duplicates differ |
| `discarded` | Number of returned elements not tied to a requested node_id |

Find the latest failed or partly matched reads with:

```bash
docker compose exec -T db psql -U trackeverything -d trackeverything -c "SELECT started_at, run_id, result_json::json->'output_check' FROM logs WHERE event_type = 'cli' AND (status = 'error' OR result_json::json->'output_check'->>'stage' <> 'ok') ORDER BY id DESC LIMIT 20;"
```

No additional CLI / LLM calls are made, and deadlines are unchanged.

The `duration_ms` of agents, tools, LLMs, harnesses, and the CLI and the CLI's `phase_ms` are measured with the shared
`elapsed_clock()`. macOS uses `CLOCK_MONOTONIC_RAW` and Linux uses `CLOCK_BOOTTIME`, both of which include system
sleep. Only environments without those clocks fall back to `time.monotonic()`. UTC clock adjustments do not change
durations. The CLI's execution deadline and `remaining_ms` use `time.monotonic()`, so recorded time that includes
sleep differs from deadline consumption.
macOS logs recorded before this clock was adopted exclude sleep, so `started_at + duration_ms` can fall before the
start of a child event. Do not attribute this difference to LLM, CPU, or lock time.
Acceptance is checked by the measurement cases in `tests/entry/test_execution_timing.py`.
Bodies, stderr, and external exception details are not added to the measurements.
When delivery of a Discord final reply or confirmation report fails, the agent's error keeps the obtained HTTP status,
the numeric error code, and the number of chunks that had already succeeded. The shared delivery boundary leaves the
same safe, fixed-form information in the warning log. Communication exceptions keep only the type name; external
response bodies and raw exception text are not added.
For details, see [Discord delivery](components/DISCORD.md#delivery).
A harness takes the UTC time and the monotonic clock before the check starts and records the check duration.
It does not record or return stderr, and converts execution errors into a fixed-form code / message.
Agent failures are structured at the `BaseAgent.run()` boundary, and `AgentResult.errors` appears in the same shape in
`result_json`.

## Fields and correlation

Sources and agents share the same `run_id`. An agent's parent ID points to the calling agent, and the parent ID of an
LLM / harness points to the agent that ran it.
An agent's own ID and parent ID are held in ContextVars scoped to the execution and restored when the child ends or
exits with an exception.
General's execution permission for dedicated commands also uses this ID.
The runtime records every run in PostgreSQL's `session_run` and associates it with `session_run.run_id`.
To get the execution logs of one session, select runs by `session_run.session_id` and join them with `logs.run_id`.
For the storage contract of the session infrastructure, see [SESSION.md](components/SESSION.md).

| Field | Contents |
|---|---|
| `event_type` | One of the event types above |
| `run_id` | Shared ID of one run |
| `execution_id` | Execution ID of the event |
| `parent_execution_id` | Parent execution ID. null for sources |
| `component` | Source / agent name, LLM provider/model, and so on |
| `started_at` / `duration_ms` | Start time in UTC ISO 8601 / duration |
| `status` | State of that execution boundary, such as `ok` / `error` |
| `request_json` / `result_json` / `error_json` | JSON input / result / list of failure notes. The `error_json` of an agent event is `[{stage, cause}]` (for exceptions, `cause` is `type: message`). When ok, it holds only advisory unresolved tool failures |
| `usage_json` / `cost_usd` | LLM usage / cost in USD. The review cli records the input/output tokens it could obtain, with a null cost |
| `code_version` | The recorded execution version. The host version is `<first 12 characters of the SHA>`, with `+dirty.<8-character diff hash>` appended when there are uncommitted changes. Records made while an agent runs append that agent's component version, `+c.<first 8 characters of the version id>`. `unknown` when it cannot be resolved; NULL for rows recorded before the column existed |

## Execution version

`PostgresExecutionLogger.record()` adds `code_version` to every event. Loggers that tools open individually get the
same value.
The host version is resolved once per process by `core/builtin/code_version`. It reads git in the running checkout
(the repo root of the package location), and is `unknown` when it cannot be resolved. The dirty check is limited to
`src`, `alembic`, `pyproject.toml`, `uv.lock`, and `scripts`.
Records made while an agent runs (agent, LLM, and tool events) carry `<host version>+c.<first 8 characters of the
version id>`, which adds that agent's version. The version id is the id of a version in the version store (the `<id>`
that import prints), and `session_run` records the version of the entry agent. A run uses the version first resolved
for each agent name, so even when runs on the old and new versions run concurrently in the same process, each record is
attributed to its own agent's version. Records outside agents (maintenance ticks, the queue process) carry only the
host version ([component versioning](components/VERSIONING.md)).

## LLM cost

OpenAIChatLLM keeps the provider / model, input, cached input, output, reasoning, and total tokens, the request id, and
the raw response in LLMMetadata. TrackedLLM computes the cost from the prices in
[llm_pricing.py](../src/trackeverything/llm/pricing.py).
When usage or the price is unknown, `cost_usd` is null, distinct from 0.
CC's internal calls do not go through `TrackedLLM`. The usage that CC returns and the model names in modelUsage are
recorded; anything not obtained is null / an empty list. The model name of a compatible endpoint is what the CLI
reports and does not guarantee the backend's real model or the billed amount.
Cached input is priced separately, and reasoning is included in output tokens, so it is not billed twice.

The empty `reasoning_content` added only when sending to DeepSeek does not rewrite the stored original history or the
raw response. Turns where reasoning could not be obtained remain distinguishable from turns where it was obtained.

## Query examples

The dashboard's usage view (`#/usage`, `GET /api/usage`) performs the same per-agent and per-tool call count
aggregation through the ORM. The run flow view (`#/runs`, `GET /api/runs/{run_id}` and
`GET /api/sessions/{session_id}`) shows one run's tree, timing, and results, and the sequence of runs in a session, as a
diagram with playback.
These reads do not return LLM or agent requests, and excerpts are limited to 300 characters. `tool` events exist only
for runs from 2026-10-06 onward, so tool call counts before that date are not available. The dashboard has no
full-payload query, so the following read the local DB.
SUM excludes nulls, so totals cover only calls whose price is known.

Queries use `psql` in the `db` service.

```bash
PSQL="docker compose exec -T db psql -U trackeverything -d trackeverything -c"

# Latest 20 events
$PSQL "SELECT id, event_type, component, status, duration_ms, cost_usd FROM logs ORDER BY id DESC LIMIT 20;"

# Tree of one run
$PSQL "SELECT id, event_type, component, parent_execution_id, status FROM logs WHERE run_id = 'RUN_ID' ORDER BY id;"

# Details of the latest LLM call
$PSQL "SELECT row_to_json(t) FROM (SELECT component, request_json, result_json, usage_json, cost_usd FROM logs WHERE event_type = 'llm' ORDER BY id DESC LIMIT 1) t;"

# Total cost
$PSQL "SELECT COALESCE(SUM(CAST(cost_usd AS REAL)), 0) FROM logs WHERE event_type = 'llm';"

# Cost per run
$PSQL "SELECT run_id, SUM(CAST(cost_usd AS REAL)) AS cost_usd FROM logs WHERE event_type = 'llm' GROUP BY run_id ORDER BY MAX(id) DESC;"
```

The owner's 👍/👎 on bot Discord messages are in `run_feedback`, one row per rated message, linked to the run, its
session, and the agent execution that delivered the message (`logs.execution_id`; for a conversation reply that is
`dispatch`, which delivers general's answer). A session's evaluation is the aggregate of its rows
([Discord feedback](components/DISCORD.md#feedback-on-bot-messages)).

```bash
# Latest ratings with the run, session, delivering agent, and number of reasons
$PSQL "SELECT f.rated_at, f.rating, f.run_id, f.session_id, l.component, jsonb_array_length(f.reasons) AS reasons FROM run_feedback f LEFT JOIN logs l ON l.execution_id = f.execution_id ORDER BY f.rated_at DESC LIMIT 20;"

# Evaluation per session (👍 and 👎 over its runs)
$PSQL "SELECT session_id, COUNT(*) FILTER (WHERE rating = 'up') AS up, COUNT(*) FILTER (WHERE rating = 'down') AS down FROM run_feedback GROUP BY session_id ORDER BY MAX(rated_at) DESC;"

# 👎 reasons, newest first
$PSQL "SELECT f.run_id, r->>'at' AS at, r->>'text' AS reason FROM run_feedback f, jsonb_array_elements(f.reasons) r WHERE f.rating = 'down' ORDER BY r->>'at' DESC LIMIT 20;"
```
