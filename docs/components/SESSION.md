# Session infrastructure

[Docs index](../README.md) / [Shared contracts](../ARCHITECTURE.html)

This document is the source of truth for the implementation contract and acceptance cases of the session
infrastructure.

Application-specific uses of these tables (conversation delivery, issue development, and project notifications)
require [installed components](../EXTENSIONS.md). This document owns the Host's persistence contracts; each component
repository owns the workflow that reads or writes them.

## 1. Purpose

Sessions are created and reused for ongoing conversations, so that the whole agent run can refer to the same session
ID. The session ID of an execution request is optional; when unset, the Runtime issues a UUID.
If a given string exists it is reused; if it does not, a session is created with that string as its ID.

Discord resolves sessions through the channel mapping table, and Python force-loads the history as
`conversation_turns`. `conversation_turns` takes an optional `since` (lower bound of the start time, inclusive) and
`limit` (number of the latest successful request/response pairs) and applies them after merging with the ancestors.
Without them it returns all successful pairs. Stored records are kept, and the injected range per channel follows the
[Discord conversation contract](DISCORD.md#conversation-agents). The shared infrastructure owns IDs, storage, and history
reads.
Slack intake publishes the raw event to the queue without a session. The Runtime records a new session and run, and the
dispatch agent delegates to `slack`. The context for mentions of the owner comes from Slack threads and history, not
from the session history of Discord conversations.

## 2. ID resolution

| Session ID of the execution request | Behavior |
|---|---|
| `None` | Creates a new `uuid4().hex` and stores a session with that ID |
| A stored string | Reuses the session with the same ID |
| An unstored string | Stores a session using the given string as is as its ID |

There is no validation or normalization of UUID syntax, length, empty strings, or whitespace.
An empty string is also treated as "a given string" and is distinct from unset.
Case, leading and trailing whitespace, and Unicode are kept as received, and the same string always points to the same
session. A storage error at start does not run the agent and is returned as an error with a `session` ErrorNote.

A call without a session creates a new session every time. Entry points that continue a conversation pass the same
session ID (for example the Discord channel mapping).
Runs of the same session also run concurrently. The queue does not guarantee FIFO, and the history a run reads contains
only the runs whose results were stored by then.

The Runtime issues the run ID with `uuid4().hex` right before execution. Execution requests carry no run ID, so even
for the same input, a reclaimed message and a redelivery each become a new run.

## 3. Execution request, Context, run

The Runtime turns a queue execution request (`EventRequest`) into the dispatch entrance, `data={"event": ...}`,
`request=None`, and the optional session ID and its parent, and builds the `AgentContext` directly. It never passes
Sources or external SDK objects.
The session and parent are decided only by the message's `session_id` and `parent_session_id`; the Runtime never derives
a session from the payload or a Source.
A named run (a request with `provider="internal"` whose payload has `agent_name`) also uses a session only when the
publisher set `session_id`; otherwise it gets a new one-off session.

`AgentContext` is the shared state and step budget used during one run.
`AgentCall` holds a reference to the Context and the request specific to that agent call.

Sessions are handed over along this path.

```text
intake / queue (session_id is optional)
  → EventRequest(session_id=..., parent_session_id=...)
  → Runtime (check main and create the run handle, convert to the dispatch entrance, data={"event": ...} and session,
             issue run_id, decide the session, create or reuse the session and store the run input)
  → AgentContext(data=..., max_steps=..., run_id=..., session_id=..., parent_session_id=..., message_id=..., agents=...)
  → dispatch agent → child agents / harnesses / tools
  → Runtime (store the run result)
```

### Run input and session records

Intake (Discord and others in `integration/`) resolves the session and publishes the raw EventRequest with
`session_id` and the parent session. The CLI, automatic development, and the maintenance sweep publish the same
EventRequest, and the Runtime runs the dispatch entrance regardless of provider and payload.
`Runtime` (`core/builtin/runtime.py`) stores the input, runs the entry agent, and stores the result, once each.
The root of every run is recorded as `agent_name="dispatch"`, `request=None`, `data={"event": ...}`. Named runs are
recorded the same way, so in `session_run` the child's name and request remain only inside `data.event.payload`.
History reads restore the request text from the stored raw event (`request_text` in `tools/session_history.py`): for a
Discord event the message `content`, or the runtime string `(添付のみのメッセージ)` ("attachment-only message") when it
has only attachments; for other events the payload's `request`. A run whose request cannot be restored is not a
conversation turn. The responsibilities of `response` and `status` in AgentResult are kept.

The `session_id` of an execution request is `str | None`; `AgentContext.session_id` holds the `str` the Runtime
decided. Child agents refer to the same Context through the parent's `call_agent()` (internally `call.child()`), and
harnesses and tools get the same ID from the Context.
LLM JSON arguments never gain an application session switch.

### Relation to runs

Each run gets a new run ID, Context, and step budget.
Reusing the same session does not restore the budget or mutable Context of past runs.
Parent and child agents, harnesses, and tools belong to the same run and create no separate run rows.

The application session ID and the Investigator's CLI investigation session ID are different values.
The `session_id` argument of the Investigator stays for CLI investigation.
Releasing a CLI mapping does not delete the application session.

## 4. PostgreSQL storage

The only persistent backend is PostgreSQL (as in the whole project). There is no SQLite implementation, fallback, or
test. The session storage requires a PostgreSQL `DATABASE_URL`; when unset it is a configuration error (`ValueError`
from `open_session_store()`). `open_session_store()` opens a `PostgresSessionStore`. Alembic creates the tables and
indexes. Reads use `get_session(id)`, `get_run(run_id)`, and `list_runs(session_id)`.

### Session and run data contract

| Stored object | Keys and relations | Contents |
|---|---|---|
| `session` | `session_id` is a string PRIMARY KEY; the DB guarantees uniqueness | ID, creation time, parent session ID (default NULL) |
| `session_run` | `run_id` is the PRIMARY KEY. `session_id` is a NOT NULL foreign key to session | Entry agent, input, final response, status, start and end times, and the run version at insert time (`code_version`; for Runtime runs, `<Host version>+c.<first 8 digits of the version id>` with the entry agent's component version; completion and interrupted-run recovery do not change it) |

One session has many runs, and one run belongs to one session.
`run.session_id` has no UNIQUE constraint, keeping the one-to-many relation.
Reusing a stored session does not overwrite its creation time.
Conversation content is stored as each run's input and final response.
A structured response is a JSON-compatible value such as `{"summary": "Investigation result", "items": ["Item A"]}`.
The internal execution history of tools belongs to the existing logs and is linked by `run_id`.
After reopening the store, IDs, run mappings, and stored inputs, responses, and statuses can be retrieved.

### Creation and recording

The Runtime commits the session creation or lookup and the run input record
(`begin_run(run_id, session_id, agent_name=..., data=..., request=..., parent_session_id=...)`) in the same transaction
before starting the agent.
Concurrent creation of the same given string converges to one row through the PRIMARY KEY and conflict handling, and
each input gets its own run.
If creation or the run record fails, the transaction rolls back and neither the agent nor the LLM starts.
SQL values are passed as parameters; a given string is never embedded in SQL text.

After the run, the same Runtime stores the AgentResult's status and response on that run.
The initial status is `running`; JSON inputs and responses are stored as JSONB and times as TIMESTAMPTZ.
Statuses distinguish `running / ok / error / max_steps / cancelled / interrupted`. Agent exceptions and unregistered
entrances are `error`, and a run past its deadline is finished as `cancelled` with a `deadline` note. The cause of a
run-level failure is also kept in the response.
If storing the final result fails, the AgentResult is returned unchanged and the failure is logged. The run row stays
`running` and becomes `interrupted` in the recovery at the next startup.

When the runtime process starts, `serve()` in `core/builtin/app.py` changes `running` rows older than the current
startup time to `interrupted` with `recover_interrupted_runs(before=<startup time>)` and records the end time.
Inputs and existing responses are kept, and recovery runs no agent or tool.
Finished rows and rows at or after the startup time are not changed, and repeated calls do not update the end time.
Recovery runs only at runtime process startup. It commits in one transaction; if it fails, the runtime does not start
accepting new work. Operations run exactly one runtime process per DB (no lock enforces this).
The recovery mechanism for unfinished runs is outside the automated suite.

### Event records

`PostgresSessionStore.record_event(agent_name, data, request=None, *, session_id=None, response=None) -> None` is the
boundary for recording input without calling an agent.
It commits the session creation and the insertion of a finished `session_run` in one transaction, storing a run ID
(`uuid4().hex`), `status=ok`, start and end times, and the input. For external intake `response` is NULL, so no
response is fabricated.
`response` is used only for responses already settled outside, so that an assigned-issue investigation can leave its
delivered report in the issue channel's session. A redelivery of the same input is appended as a new run.
A storage failure rolls back and raises.

`conversation_turns` excludes runs without a response, so intake records are never treated as conversation responses.

### Parent session

`session.parent_session_id` is an optional reference, default NULL, and a foreign key to the same `session` table.
It is set only when `begin_run` creates a new session with the execution request's `parent_session_id`; on reuse of an
existing session the given value is ignored (immutable, like the creation time).
If the parent row does not exist yet, a placeholder row (without runs) is created in the same transaction as the child
to satisfy the foreign key. No placeholder is created for a parent given on reuse.
`parent_session_id == session_id` is rejected with ValueError, and nothing is written.
Because the parent is set only at creation and a parent row's own parent never changes, no cycle can be created
through the API.

References go upward only. Reading a session's history returns the union of the runs of that session and all its
ancestors in ascending `started_at` order, and never includes the runs of child sessions.
Even if a cycle is created by hand in the DB, the read stops at visited sessions.
The column is part of the Alembic schema; rows created before it existed have a NULL parent.

### Read tool and shared conversation-turn reads

`SessionHistoryTool` (`tools/session_history.py`, tool name `session_history`) returns the history of a stored session
read-only. `session_id` (string) can be omitted; when omitted, the same session ID is taken from the current run's
Context (an explicit value wins).
The output is `found`, `session` (ID, parent ID, creation time), `ancestry` (nearest parent first), and `runs` (the
request, final response, status, and times of the session and all ancestors in ascending `started_at` order, each row
with its `session_id`; the request is restored as described in
[Run input and session records](#run-input-and-session-records)). Ancestors are walked once and the result is reused for
both ancestry and run retrieval. `get_session` for each session and `list_runs` for each run set are used once each, and
the tables are not changed. An unknown ID returns `found: false` and is not a failure.
An unset `DATABASE_URL` or a connection failure becomes a ToolResult failure.
This tool returns all runs and is registered in the Python control of ImplementationAgent
(it is separate from `ConversationSessionTool`, which handles the Investigator's CLI investigation sessions).

`conversation_turns(store, session_id)` in the same module is the shared read for reproducing a conversation.
It merges the runs of the session and all ancestors in ascending `started_at` order and returns only `ok` runs with a
settled response and a restorable request as `(request, response)` pairs. The Discord conversation agent uses it for
forced injection before running ([DISCORD.md](DISCORD.md)), which is the source of truth for the injection character
budget and omission rules.
Summarizing the history is still an open item.

### Relation to Codex conversations

ImplementationAgent treats the app conversation ID and the Codex CLI thread UUID independently.
The `codex_session` table uses `(session_id, cwd, project, mode)` as its primary key and stores a unique
`codex_session_id` and the creation time. `session_id` is a foreign key to `session.session_id`.
A different stored ID is not overwritten. The existing contracts for arbitrary app ID strings and parent-child
relations are kept. The Context at call time selects the app conversation, and the Codex resume ID is chosen from the
table's real CLI records.

ImplementationAgent force-reads the same history tool and uses the finished runs of the session and its ancestors.
It excludes the current and unfinished runs, and keeps failed responses and input-only external records as background.
When resuming a CLI conversation, it rereads the app history and passes the same background to the independent local
reviewer.
The rules for omitted counts, private temporary files, and resume failures are owned by
[ImplementationAgent](../EXTENSIONS.md#development-and-reviews).
The migration is `bbb29e1c182d`.

### Owner ratings of runs

The owner's 👍/👎 on a bot Discord message is stored in `run_feedback` against the run that sent the message and
that run's `session_id`, copied from `session_run` when the rating is recorded. A session has no rating of its own; its
evaluation is the aggregate of its runs' rows. The migration is `c1646cbc9caa`; the rules are in
[Discord](DISCORD.md#feedback-on-bot-messages).

### Independent conversations for issue development

Issue channels are assigned with the shared `resolve_session` and do not inherit the parent or history of the general
channel. `channels_for_source(provider, source_url)` looks up the issue channels related to the same PR.
The CLI UUIDs of spec / grill / harness are stored in the existing memory store as `issue-codex:<hash>`, per app
session, cwd, project, agent, and action. Each step force-reads the finished conversation.
Deduplication of posts and state are owned by [Issue development](../EXTENSIONS.md#development-and-reviews).

## 5. Open items

The following are not yet specified and are not part of the current behavior or acceptance criteria.

- Summarizing or deleting session history, and redesigning the injection limit (full injection and the character
  budget for Discord conversations are specified in [DISCORD.md](DISCORD.md)).
- Deduplication of Discord messages other than issue development and of Slack events. Channel-to-session mapping uses
  [section 7](#7-channel-mapping-and-source-links).

Discord intake ([DISCORD.md](DISCORD.md)) resolves the session with the channel mapping table in
[section 7](#7-channel-mapping-and-source-links), and a thread passes its parent channel's session as
`parent_session_id`. The general channel gets no session.
The Runtime writes sessions and runs according to the storage contract in this document.
The PostgreSQL-only rule in this document defines the scope of the session implementation; it is not an instruction to
change other existing stores.

## 6. Business tests

B05 in `tests/entry/test_execution.py` runs a new request on an existing session and checks that begin / finish and the
result go to the same session. RS02, RS03, RS06, RS08, and RS09 in `tests/entry/test_runtime.py` check session ID
issuance and parent handover, a storage failure at start, a version without `dispatch`, and unexpected exceptions, and
RI11 in `tests/queue/test_execution.py` checks concurrent runs of the same session. The store and the dispatch agent are
mocked.
RI06 and RI07 in `tests/queue/test_discord_ingress.py` check channel and session resolution at intake.
Concurrent storage, rollback, migration, and restoring real Codex sessions are outside the default automated suite.

## 7. Channel mapping and source links

The mapping between external conversation rooms and sessions, and the external materials a session refers to, are
stored in PostgreSQL. The implementation is `core/builtin/channel_store.py` (`PostgresChannelStore`),
`core/builtin/runtime.py` (`Runtime`), and `tools/channel_link.py` (`ChannelLinkTool`, tool name `channel_link`).

| Table | Keys and relations | Contents |
|---|---|---|
| `channel` | `(provider, channel_id)` is the PRIMARY KEY. One session per channel | provider, channel_id, session_id, creation time |
| `session_source` | `(session_id, source_url)` is the PRIMARY KEY. Many sources per session | session_id, the raw source_url, time added |
| `project_channel` | `project` is the PRIMARY KEY and `channel_id` is unique. One channel is the announcement board of one project | The raw project label, channel_id, creation and update times |

- Session IDs are resolved from the table, not from a string convention (`discord:<id>`). The first time a UUID is
  assigned, and afterwards the same channel returns the same session (`resolve_session`).
  Concurrent creation converges to one row through the PRIMARY KEY and a reselect.
- `session_source` stores only the raw URL. Which source it is and how to act on it is decided by the reader from the
  URL, and the same URL can be linked to several sessions.
  `channel_link source_get(provider, source_url)` joins this table with `channel` and returns `channel_ids`. A missing
  mapping for a regular PR can also be restored from existing links.
  The dispatch agent reads these links through the channel store, classifies channels outside projects and their
  threads as `issue_pr` / `general`, and passes the URLs it read to the child as `ctx.data.source_urls`, in the order
  current session, then parent session, removing only exact duplicates.
- `project_channel` manages where project document updates are announced. The mapping is read and written with
  `project_get` / `project_set` of `channel_link`, and recreating a channel overwrites the same row. The reverse lookup
  `project_for_channel` is used by the dispatch agent to route messages in a project channel to the child `project_qa`.
  The source of truth for the contract is the update-announcement section of [Project memory](../EXTENSIONS.md#project-documents).
- `channel` and `session_source` have no foreign key to the `session` table. Session rows are created by `begin_run` on
  the execution side, so they may not exist yet when the channel mapping is created (before the first message).
- `Runtime` records every run in the tables of section 4 with the shared failure rules (if begin fails the agent does
  not run; if finish fails the result is unchanged and only a log entry remains). A run whose `session_id` is `None` is
  also recorded with a new session (PostgreSQL is the only backend; an environment without it fails at startup).
- Discord intake ([DISCORD.md](DISCORD.md)) resolves the session with `resolve_session` before publishing the
  EventRequest. A message whose resolution fails is not run in a wrong session and is not published.
- `pr_event` creates the mapping with `channel_link` when it creates a PR channel and links the PR URL.
  It reuses stored IDs and source links and never reattaches to another channel by name.
  `pr_cleanup` removes the source link after deleting the channel of a closed PR (the mapping row stays as history).
  A channel shared with an issue is kept when the PR closes and is deleted when `issue_cleanup` confirms the issue is
  closed. Removing a source link keeps the channel mapping row and the session / run history
  ([Issue development](../EXTENSIONS.md#development-and-reviews)).
  A link failure keeps the record of the created channel and sends a notification; a manual webhook redelivery fills
  in what is missing. An unset `DATABASE_URL` or a connection failure is returned as a ToolResult failure
  ([PR comment handling](../EXTENSIONS.md#development-and-reviews), section 3).

## Correlation with the queue

The run_id is issued by the Runtime and is a different value from the queue's `message_id` (stream id). The Runtime
passes `message_id` and the ACK function to the AgentContext, and `await ctx.ack(ctx.message_id)` ACKs. The Runtime
logs one line with the message ID, run_id, session_id, entry name, and status.
The Runtime does not ACK; the dispatch agent ACKs at the end of its handling.
For a run past `RUN_TIMEOUT`, the Runtime turns the result into a `cancelled` result with `deadline` and records it like
a normal end. The dispatch agent is cancelled before its ACK, so the message stays pending and runs again after
reclaim. This deadline is not propagated as a cancellation to the Runtime's caller.
Only when the Runtime's caller cancels the execution task does it try to record a `cancelled` end, re-raise the
cancellation, and leave the message unacknowledged and pending.
An explicit session is kept for reclaimed messages too; without one, each run creates a new session. Details:
[Queue](QUEUE.md).
`record_event()` appends external input as a finished run without starting an agent, and `record_event_in()` does the
same append in the caller's transaction.
