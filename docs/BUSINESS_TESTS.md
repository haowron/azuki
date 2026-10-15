# Business test structure and cases

The basic case table below has **24 scenarios**: 16 Python cases and 8 Node.js scenarios.
Each row is one business scenario; a single example is not expanded into exhaustive combinations of invalid types.
Each case ID appears in the name or docstring of the test that implements it.

## Verified boundaries

- Intake calls the shared Webhook acceptance and the Discord and Slack receiving connections, and checks publishing the raw event,
  pruning, and session assignment ([Queue][QUEUE]). Conversation execution calls one Runtime execution (with session recording).
  The stand-ins for the Producer, channel Store, session Store, and dispatch agent are mocks holding the fixed results and call
  records each scenario needs. LLMs use fixed responses.
- Cases for the asyncio execution infrastructure (deadlines, cancellation, the Host event loop, the LLM concurrency limit) are in
  `tests/entry/`. VR48 checks the contract shapes, and VR49 the record of a run cancelled at its deadline.
  Concurrency is checked with events, not sleeps.
- Component versioning uses small component trees and a `VersionStore` mock that returns fixed reads and records writes. It
  checks main, sync, fork, merge, per-run version pinning, and release. The git version store, startup from GitHub, sync,
  merge (rebase and push), local mode (the first main from the package, the working copy, pushes checked by the
  `pre-receive` hook, merge by fast-forward), the check CLI, and the `pre-push` hook run against real git in temporary
  directories, with a bare repository standing in for GitHub and a stand-in Host for the hooks; no network is used.
- Tests check the result body, status, and business data, plus the required destination, target, storage, posting, and
  non-execution. The object under test is never mocked.
- File content such as reports is checked as real files in `tmp_path`. No PostgreSQL, always-on service, nginx, or real LLM is
  started. Child processes are refused except git in the versioning cases that use it.
- HTTP uses TestClient; the frontend uses Node.js with minimal DOM/fetch stubs. Successful screen rendering and translation
  quality are never treated as automatically passed.
- Detailed checks of a rule live at the boundary that directly owns it.
- Component code (agents, tools, skills, harnesses, Sources) lives in the component repository and is outside pytest; it is
  checked by loading (`check`) and developed with evaluations.

A small shared ScriptedLLM, pytest's `monkeypatch`, and the standard `unittest.mock` are used.
No new generic harness, fake DB, fake Git, or tests of fixtures themselves are written.
Product responsibilities and public APIs are not changed just to swap an existing boundary.

## Breakdown of the basic case table

| Category | Cases |
|---|---:|
| Conversation execution | 1 |
| HTTP operations | 15 |
| Dashboard operations | 8 |
| Total | 24 |

The Node calls are wrapped in a single pytest wrapper. The business collection count is **17**:
16 Python business cases and 1 Node wrapper.
The number of business scenarios is managed as **24**; the wrapper does not count as an extra business scenario.
Separately, 5 timing regressions across system sleep and each feature's additional regressions run.
Component versioning cases are 47 VR IDs (VR01–VR08, VR12, VR14–VR19, VR21–VR26, VR28–VR31, VR35, VR37–VR39, VR41–VR43,
VR45–VR49, VR52–VR61; 65 functions, 82 after parametrization), 28 VG IDs for the git store, local mode, the working copy,
and the hooks (VG01–VG28; 36 functions, 38 cases),
and PC01–PC02 for the Host's child-request entry point (2 cases). Their contract is [Component versioning][VERSIONING] and, for
PC, [Queue][QUEUE].
Placement: `tests/versioning/` holds 42 VR IDs (59 functions, 76 cases; VR56–VR61, the system preamble, are in
`test_system_md.py`) and VG01–VG28; `tests/entry/` holds VR46, VR48, and VR49
(4 functions, 4 cases); `tests/api/` holds VR47 (1 case); `tests/queue/` holds VR52 and PC01–PC02 (3 cases).
VR47 doubles as the catalog scenario in the table below, and VR46 as the run execution boundary scenario.
Adding VL48 and VL54 from outside versioning gives 79 IDs, 105 functions, and 124 cases. VL48 targets the supervisor script
itself and starts bash with a temporary checkout and a fake app (it never starts the real service; `tests/entry/test_supervisor.py`).
VL54 is the rejection of re-entry into `TaskLock`; its contract is in [Component versioning][VERSIONING].
The schema-version check at startup is M1–M3 ([`test_schema.py`](../tests/entry/test_schema.py)): at head, startup continues;
a DB older than head or a DB that cannot be reached is refused. Head is read from the repository's Alembic, and only the DB read is mocked.
The total pytest collection count is **425**. The queue and the intake's raw-event publishing are covered by the 40 Q, RI,
and SQ scenarios plus VR52 and PC01–PC02, 43 scenarios in total (70 after parametrization).
The contract for Q, RI, SQ, and PC is [Queue][QUEUE]; for VR52, [Component versioning][VERSIONING].
The current form of the publish entry point, ACK, and unreadable messages, and the unified-request scenario UR01
(`tests/queue/`), follow [Queue][QUEUE].
The form the Runtime executes also follows [Queue][QUEUE].
The table below is the breakdown of the basic cases and is distinct from the overall counts.
The timing regressions check clock selection on macOS, on Linux, and the fallback clock; parent/child timing of
Agent/Tool/Harness/LLM; and the timing of failed LLM calls.
The source of truth is [Execution logs](LOGGING.md); the cases are in
[`test_execution_timing.py`](../tests/entry/test_execution_timing.py).

## Case list

The input column includes the values and states the mocks return.
The "Spec" column is the current business contract; simplifying tests never changes the product spec.
A row with consecutive operations is verified as one usage flow.

### Conversation execution

External intake cases (Discord, Slack, GitHub) are RI01–RI16 and Q29 in [Queue][QUEUE]. RI13–RI16 cover the owner's Discord reactions
for feedback; the feedback behavior itself is a component outside pytest, with its FB01–FB16 contract in [Discord](EXTENSIONS.md#conversations).
Paging long replies and notifications is also a component outside pytest, with its LP01–LP19 contract in [Discord](EXTENSIONS.md#conversations).

| Check | ID | Target | Input and mock | Expected business result | Spec |
|---|---|---|---|---|---|
| ✅ | [B05](../tests/entry/test_execution.py) | `Runtime` | Run, one after another, `EventRequest`s for Discord messages in an existing session. The Store and dispatch stand-ins are mocked. | Records a new run in the same session, passes this event to dispatch, and returns this run's result. | [SESSION], [QUEUE] |

### HTTP operations

| Check | ID | Target | Input and mock | Expected business result | Spec |
|---|---|---|---|---|---|
| ✅ | [H10](../tests/api/test_information.py) | Report viewing | Open a published report URL. A temporary HTML file and assets are prepared. | The stored HTML and the resources it needs can be fetched. | [HTTP], [REVIEW] |
| ✅ | [H11](../tests/api/test_information.py) | Report viewing | Open a URL pointing to a file outside the report directory. | Refuses without publishing it. | [HTTP] |
| ✅ | [VR47](../tests/api/test_information.py) | Catalog | Open the Agent / Tool list. A small component graph built with `build_agents` and a declaration-only agent that no entrance connects to (it declares child names) are prepared. | The API result shows entrances, parent-child relations, and tool users; the declaration-only agent's declarations also become parent-child edges. | [HTTP], [VERSIONING] |
| ✅ | [H13](../tests/api/test_information.py) | Usage | Fetch agent and tool usage. `read_usage` returns fixed aggregates. | Returns per-component totals, the success/failure breakdown, the last-used time, and the aggregation start time. | [HTTP], [LOG] |
| ✅ | [H17](../tests/api/test_gateway.py) | gateway | Open the management API with a valid Bearer. Upstream HTTP is mocked. | Forwards to the correct upstream with authentication and does not forward the user's Bearer. | [GATEWAY] |
| ✅ | [H18](../tests/api/test_gateway.py) | gateway | Open the management API with an invalid Bearer. | Returns 401 and does not forward upstream. | [GATEWAY] |
| ✅ | [H19](../tests/api/test_gateway.py) | gateway | Receive a Webhook with a valid GitHub signature. The upstream is mocked. | Forwards the verified payload to the target upstream. | [GATEWAY] |
| ✅ | [H20](../tests/api/test_gateway.py) | gateway | Receive a Webhook with a valid Slack signature. The upstream is mocked. | Forwards the verified payload to the target upstream. | [GATEWAY] |
| ✅ | [H21](../tests/api/test_gateway.py) | gateway | Receive a GitHub Webhook whose body was altered after signing. | Rejects it and does not forward upstream. | [GATEWAY] |
| ✅ | [H22](../tests/api/test_information.py) | Run fetch | A completed run with a root agent, a tool, and a child agent (an LLM and a failing tool). Long arguments and responses; the LLM and agent requests contain a secret string. Reads are mocked. | Returns the steps in start order with their parents, each with status, time, cost, tokens, error causes, and a preview of at most 300 characters. Contains no secret string. | [HTTP], [LOG], [RUNS] |
| ✅ | [H23](../tests/api/test_information.py) | Running run | There is no root agent record; `session_run` is running. | Status `running` and `complete=false`, returning the recorded steps and their original parent IDs. | [HTTP], [LOG], [RUNS] |
| ✅ | [H24](../tests/api/test_information.py) | Unknown ID | Query an ID containing `/` that exists as neither a run nor a session. | Both the run and the session query return 404 with a reason. | [HTTP], [LOG], [RUNS] |
| ✅ | [H25](../tests/api/test_information.py) | Session run list | A session with 3 runs: succeeded, failed, and running. Reads return them in no particular order. | Returns the run summaries in start order (entry agent, status, time, step count, cost), the total, and the parent session. | [HTTP], [LOG], [RUNS] |
| ✅ | [H26](../tests/api/test_information.py) | Recent runs | Open the list with `limit=2`. Reads return 2 items. | Passes the given count to the read and returns run summaries newest first. | [HTTP], [LOG], [RUNS] |
| ✅ | [H27](../tests/api/test_information.py) | Run flow authentication | Fetch a run without a token while a token is configured. | Returns 401 and does not call the read. | [HTTP], [LOG], [RUNS] |

### Dashboard operations

| Check | ID | Target | Input and mock | Expected business result | Spec |
|---|---|---|---|---|---|
| ✅ | [U06](../tests/browser/dashboard_logic.test.cjs) | Catalog and usage | Open the catalog and usage views. Fixed API responses are used. | Shows agent relations and execution status; items that never ran are identifiable. | [UI] |
| ✅ | [U07](../tests/browser/dashboard_logic.test.cjs) | Run diagram | Root agent → tool → child agent (LLM → failing tool → LLM) → tool, a step whose parent is unrecorded, and a name containing `"<b>`. | In the order start → root → result, places the child agent's frame in the parent's column, groups the unrecorded step in a separate frame, shows time and status, and escapes strings. The timeline has one bar per step and highlights failures. | [UI], [RUNS] |
| ✅ | [U08](../tests/browser/dashboard_logic.test.cjs) | Replay | The same run as U07. | Keeps start order, nesting, and differences in length; total time is 4–45 seconds. At intermediate times separates done, running, and pending; at the end all steps are done. | [UI], [RUNS] |
| ✅ | [U09](../tests/browser/dashboard_logic.test.cjs) | Session view | A stored token exists. Enter a session ID. The run query returns 404 and the session query returns 3 runs. | Queries the run and then the session with the token, lays out the 3 runs in start order, and fetches the latest run's flow. The ID goes into the URL. | [UI], [RUNS] |
| ✅ | [U10](../tests/browser/dashboard_logic.test.cjs) | Pick from recent runs | Open `#/runs`. The list returns 2 runs. Select the first, then a tool block. | The list shows entry agent, status, start, duration, and session; selecting changes the URL and the run query, and shows the status, overall time, and the default structure diagram. Selecting a block lists the calls to that tool with number, time, and status. | [UI], [RUNS] |
| ✅ | [U11](../tests/browser/dashboard_logic.test.cjs) | Structure diagram | A child agent, LLM calls of the same model, a tool called 3 times (1 failure), a tool called by 2 agents, a skill's body read and function, a step whose parent is unrecorded, and a name containing `"<b>`. | Groups each name into one block across the 4 lanes Agent, LLM, Tool, and Skill, and draws one arrow per step from its caller, numbered in start order. Repeats are separate arrows to the same block, failures show `✗` and the block's failure count, and strings are escaped. | [UI], [RUNS] |
| ✅ | [U12](../tests/browser/dashboard_logic.test.cjs) | Structure replay | The same run as U11. | Arrows appear when their step starts; a block called repeatedly alternates between running and done on each call; at the end everything is done. | [UI], [RUNS] |

## Layout and execution

| Directory | Target |
|---|---|
| `tests/entry/` | Sessions, execution boundary, asyncio execution infrastructure, the Host base tools, the repair of unresolved fatal tool failures |
| `tests/queue/` | Publishing, consuming, ACK, processes; the intake's raw-event publishing and handover to dispatch; dispatch named runs |
| `tests/api/` | HTTP operations and gateway authentication |
| `tests/versioning/` | Component versioning (the git store, startup, sync, fork, merge, local mode and the working copy, the check CLI and the hooks, per-run version pinning, release, the system preamble `agent/SYSTEM.md`) |
| `tests/browser/` | The 8 Node.js dashboard operations and optional visual check procedures |

`tests/conftest.py` shares the temporary storage and the ban on external connections; `tests/versioning/conftest.py` allows git,
and only git, for the cases that use real repositories, with the user's git configuration ignored.
`tests/fakes.py` holds the fixed LLM and short waits for concurrency scenarios.
No DB is created or initialized. Node.js 18 or later is used, with no dedicated harness plugin or fake DB.
The pytest wrapper passes `--test-reporter=tap` and confirms the 8 scenarios succeed without depending on Node.js's default reporter.

```bash
uv run pytest
uv run ruff check src tests
uv run mypy src
```

Test-only changes need no restart or deployment of the live service.

## Coverage

Tests check the results of public entry points and what is passed to dependencies: target, destination, body, storage,
posting, and non-execution.
The following mechanisms are not covered by independent automated tests:

- Individual inspection of BaseAgent (except the repair loop, AG01–AG07), Context, Harness, registration and composition, the internal LLM adapter, and prompt wording.
- ORM, DDL, migrations, real DB transactions and compatibility, real Redis stream operations, Host concurrency control and restarts.
- Internal rules of the Host part of code_version (`<host version>`). The agent's own version that versioning adds to records (`<host version>+c.<id8>`) is checked by VR28.
- Git against GitHub itself (SSH, deploy keys) and the real component repository; CLI compatibility and process cleanup; deploy,
  demo, one-time migration, and environment setup scripts.
- Real LLM judgment and translation quality; real-browser rendering and layout.

A mock's success is never treated as proof that the external service really works.

## Automatic investigation of assigned issues

Fetching the Issue is a Source in the component repository and outside pytest; the behavior is described in [Issue workflow][ISSUE].
RI01–RI03 check the format validation at acceptance and publishing the raw event; routing an assignment to the owner is a
dispatch agent decision and outside pytest.

## Sentry auto-fix

SA10 (`tests/entry/test_service_definitions.py`) checks that the always-on service definitions contain no scheduled Sentry job
(it starts from a row in the `schedule` table). The Sources `sentry_issues` and `sentry_issue` and the behavior of the selecting
agent, triage, fix, verification, and publication are components outside pytest and are confirmed in an acceptance dev run
([Sentry auto-fix][SENTRY]).

## Scheduled trigger

These tests check ST01–ST08 (spec, inputs, and expected results in [Scheduling][SCHED]) and the change to Q51.
The 8 scenarios (12 after parametrization) of `tests/queue/test_schedule.py` check publishing at the cron time, not making up
times that passed before startup, the grace period, applying table changes, invalid definitions, failures to read the table
and to publish, and registration in and shutdown with the queue process.
Table reads, the Producer, and time are swapped; no PostgreSQL and no real-time waits are used. The table schema is checked
with `alembic upgrade`, `downgrade`, and `check` on an empty scratch DB.

They also check RV01–RV08 (in the same [case table][SCHED]) and the change to ST01–ST08 (reading the reservation table as empty).
The 8 scenarios (10 after parametrization) in the same file check publishing a one-shot reservation at its time and never
again; not publishing reservations already past when first read or after a restart; the grace period; applying table
changes and cancellations; invalid reservations; a failure reading the reservation table kept separate from the cron table;
publish failures; and the separation and waiting of a reservation that has the same id as a cron row.
The agents `reservation` and `reservation_fire` are outside pytest and are confirmed by the
[checks outside pytest](components/SCHEDULING.md#checks-outside-pytest).

## Optimizer

These tests check OP01–OP09 and OP39 (spec, inputs, and expected results in [Optimizer][OPT]) and the change to Q51.
The 10 scenarios (15 after parametrization) of `tests/optimizer/test_optimizer.py` check sharing observations and the order
of reports, isolating check failures, check selection, invalid input, excluding its own run, read failures, not installed,
propagation of cancellation, duplicate check names, and passing `merge` to every check. The entry agent `optimizer` is a component outside pytest. Execution-record reads, time, and checks are stand-ins; SQL reads and
starting by a named run are confirmed in an acceptance dev run. The entry agent's delegation is outside pytest.
The main version id comes from the version store singleton opened with a stand-in store.

## The optimizer's `errors` check

These tests check OP11–OP22, OP40, OP42, OP44, and VR55 (spec, inputs, and expected results in [Optimizer][OPT]), and the changes to OP01–OP09 and VR45.
The 19 scenarios (25 after parametrization) of `tests/optimizer/test_errors.py` check identifying the deepest failure;
repairing only errors of the main version at observation time; excluding old versions and agents whose version cannot be
identified; repairs that fork from main, merge, and push; answers that are not `fixed` and answers without writes; the write
scope; retrying fork failures; rebasing onto a GitHub main that moved and failing on a conflict; ordering and capping by error
count; conversation failure and cancellation; forking without merging when `merge` is false; and `agent/SYSTEM.md`, which a
repair can read but not write, and which a person's push between fork and merge keeps through the rebase. The 2 scenarios
of `tests/versioning/test_singleton.py` check the version store singleton. Error reads and the LLM are stand-ins. The version store is a real `Repo` opened with a stand-in store that
can read written versions and advances GitHub's main on a fast-forward push, so fork loading, rebases, and pushes really run.
OP44 opens the singleton over a real git mirror of a bare repository on disk that stands in for GitHub, so its rebase is git's own.
SQL reads of `logs` and repairs by a real LLM are confirmed in an acceptance dev run.

## The optimizer's `consolidation` check

These tests check OP23–OP38, OP41, and OP43 (spec, inputs, and expected results in [Optimizer][OPT]) and the change to OP22.
The 26 scenarios (32 after parametrization) of `tests/optimizer/test_consolidation.py` check the registry; the catalog and the
single usage read; `unreachable` agents, unconnected declarations, `unused` agents with the age and protection rules, and
`unused_edges` for tools and skills; cleaning and capping the survey and its failure; merging duplicates on the advancing main;
the reference check; the working-tree scope and `grep`; `keep` and changes without writes; retrying fork failures; rebasing onto
a moved shared main and failing on a conflict; the group limit and groups whose target is gone; conversation failure,
cancellation, and an empty run; forking without merging when `merge` is false; and `agent/SYSTEM.md`, which can be neither
written nor deleted. Usage reads, the `schedule` and `reservation` references, and the LLM are stand-ins. Component
trees are small generated agents and tools, and the version store is the same real `Repo` over the stand-in store as for
`errors`, so fork loading, rebases, and pushes really run. SQL reads of `logs`, `schedule`, and `reservation` and consolidation by
a real LLM are confirmed in an acceptance dev run.

## The optimizer's `additions` check

These tests check AD01–AD22 (spec, inputs, and expected results in [Optimizer][OPT]) and the changes to OP22 and OP23.
The 32 scenarios (41 after parametrization) of `tests/optimizer/test_additions.py` check the digest of `general` runs and
`shell`/`bash` commands (excluded rows, clipping, the size limit, and runs consumed by earlier additions); validating survey
candidates (evidence in 3 distinct runs, names, owners, duplicates, no candidate limit) and survey failures; adding a
skill, a tool, or an agent wired into its owner, and rewriting the description of an existing component; the scope, code,
wiring, and secret checks with one retry; fork failures; `keep`; rebasing onto a main that moved, failing on a conflict,
and forking the next candidate from the merged main; conversation failure and cancellation; the registry; forking
without merging when `merge` is false; and `agent/SYSTEM.md`, which an addition cannot write. Work reads,
the LLM, and the environment are stand-ins. The version store is the same real `Repo` over a stand-in store as in the
`errors` tests, so fork loading, rebases, and pushes really run. SQL reads of `logs` and additions by a real LLM are
confirmed in an acceptance dev run.

## The optimizer's `tuning` check

These tests check TU01–TU18 (spec, inputs, and expected results in [Optimizer][OPT]) and the changes to OP22, OP23, and AD20.
The 18 scenarios (21 after parametrization) of `tests/optimizer/test_tuning.py` check the effort detector; the repeated-input,
repeated-request, and duplicate-trigger detectors; the step-shape detectors (call chains, large tool results, cache, light
children, skills read every time, prefetch); a replayed effort change that is merged with its trailers; gates that reject a
tier that changes decisions or saves nothing, a current tier that cannot judge, and savings inflated by failed replays;
forking without merging; reverting a tuning that lowered the success rate and the cooldown; watching, keeping, and reverting
without gain; reverting only the `llm` declaration after a person's edit and through a person's merge commit; editing only
the `llm` declaration; comparing decisions and the blind judge; reporting other waste only for main's agents; no new effort
change while one is watched; no replay with too few recorded steps; cooling a tuning a person undid but not a fork-only one;
failures that do not stop the other parts; and the last `llm` assignment and inheriting classes. The `logs` readers and the
LLMs are stand-ins; the version store is the same singleton as the `errors` tests, so the replay, edit, fork, merge, and
revert run for real. The `logs` SQL and the replay with real LLMs are confirmed in an acceptance dev run against a copy of the
live `logs` in the development database.

## The optimizer's `behavior` check

The `behavior` check has no cases of its own; OP23 and AD20 check its place in the registry. Its candidate handling is the
conversation, checks, fork, and merge in `optimizer/proposal.py` that AD09–AD21 run through the `additions` check. Its
readers (`optimizer/behavior/`) are checked by a read-only run with real credentials, and its survey by a run with a real
LLM.

## Run flow view

These tests check H22–H27 and U07–U12 ([Run flow view][RUNS]).
6 HTTP tests and 6 dashboard tests check run steps, summaries, previews, and redaction; running and unknown IDs; the session
run list; recent runs; authentication; the sequence diagram, the 4-lane structure diagram, and the timeline; the replay time
warp and block states; and ID resolution and selection.
Read functions and Mermaid are mocked. SQL reads and real-browser rendering and replay are confirmed at acceptance by reading
the live DB read-only, and are not part of the automated test coverage.

## Run execution boundary

The design is in [Architecture][RUN]; Runtime execution and execution requests are in [Queue][QUEUE].
The 7 scenarios of `tests/entry/test_runtime.py` check one Runtime execution. The Runtime runs dispatch regardless of the
request's content; the storage boundary and the dispatch stand-in are mocked.
Concurrency of runs in the same session is checked by RI11, deadlines by VR49, ACK, reads, and the concurrency limit by
SQ03–SQ09, and shutdown by VR52. The dispatch named run is a component outside pytest.

| Check | ID | Input and mock | Expected business result | Spec |
|---|---|---|---|---|
| ✅ | RS02 | Run without a session_id. | Creates a new 32-digit hexadecimal run_id and a session, and passes the same IDs to the start record and the Context. | [RUN] |
| ✅ | RS03 | Run an `EventRequest` for a named run with a session_id and a parent. | Passes that ID and parent_session_id, the entrance name `dispatch`, data `{"event": …}`, and request None to the start record, and the same session, parent, and message_id to the Context. The payload is not interpreted. | [RUN], [QUEUE] |
| ✅ | RS06 | Saving the start fails. | A `session` error. Neither the agent nor the result storage is called. | [RUN] |
| ✅ | RS08 | The version has no `dispatch`. | Stores an `entrance` error as the result. | [RUN], [QUEUE] |
| ✅ | RS09 | The agent raises an unexpected exception. | Stores an `execution` error as the result. | [RUN] |
| ✅ | SR02 | Cancel a running task. | Ends `session_run` as cancelled and does not ACK. | [QUEUE] |
| ✅ | VR46 | Run the Host startup, and call `publish_child` inside and outside a run. | Startup only installs the Producer in `queue/child.py` and binds no component module. Inside a run it publishes an internal EventRequest `<message_id>:auto-start` with `Producer.publish`; outside a run it refuses. | [QUEUE], [VERSIONING] |

## Host base tools

The base tools `read`, `write`, `edit`, and `bash` are Host-owned (`core/builtin/base_tools.py`) and built into every `BaseAgent`,
together with the shared base helpers component Tools import (`run_captured`, `run_bounded`, `text_window`, `bounded_int`), so
they are tested here even though component behavior is not. File cases use real files in the temporary workspace; the `bash` and
process cases start the real `/bin/bash` only (every other process start stays forbidden) ([`test_base_tools.py`](../tests/entry/test_base_tools.py)).

| ID | Scenario | Expected result |
|---|---|---|
| BT01 | Build a minimal `BaseAgent` that declares no tools | It has `read`, `write`, `edit`, `bash`, sharing the same instances with other agents |
| BT02 | Register a Tool named `read` (or `bash`) on an agent | `ValueError: tool already registered`, so the version's composition fails |
| BT03 | `read` with `offset` and `limit` | Returns those lines, `total_lines`, and `next_offset`; null at the end |
| BT04 | `read` a large file with the defaults | Capped by `READ_LINES` or `READ_CHARS`, with `next_offset` |
| BT05 | `read` a non-UTF-8, missing, or empty path | An `ok=False` ToolResult; no exception |
| BT06 | Relative, `~`, and absolute paths | Relative under `REPOS_DIR`, `~` expanded, absolute kept; `bash` runs in `REPOS_DIR` |
| BT07 | `write` a new nested path | Parents created, `created=true`, bytes written without newline conversion |
| BT08 | `write` an existing file | Replaced, `created=false` |
| BT09 | `edit` with a unique match | Replaced and saved |
| BT10 | `edit` with no match or several matches | `ok=False`; the file is unchanged |
| BT11 | `edit` with empty or unchanged text | `ok=False`; the file is unchanged |
| BT12 | `bash` success and a non-zero exit | Combined stdout/stderr and `exit_code`; the environment is inherited; a non-zero exit is `ok=False` with the output |
| BT13 | `bash` past its timeout | The whole process group, including a background child, is stopped; `ok=False` |
| BT14 | `bash` output larger than `OUTPUT_CHARS` | Only the tail is kept |
| BT15 | Cancel a running `bash` call | The process group is stopped and `CancelledError` propagates |
| BT16 | Imports of `core/builtin/base_tools.py` | No component module (`agent/`, `tools/`, `skills/`, `harnesses/`, `sources/`) |
| BT17 | `run_captured` success and timeout | Returns exit code, stdout and stderr; on timeout raises `TimeoutError` after the process group is stopped |
| BT18 | `run_bounded` output past `output_limit` | `error="output_limit"`; stdout is cut at the limit |
| BT19 | `run_bounded` past its deadline, or with an expired deadline | `error="timeout"`; the process group is stopped; an expired deadline starts nothing |
| BT20 | `text_window` and `bounded_int` | Windows of `CHUNK_CHARS` with `next_offset`; out-of-range offsets and invalid integers raise `ValueError` |

## Repair of unresolved fatal tool failures

`BaseAgent._execute_llm_loop(call, repair_tool_failures=True)` asks once for a correction when a FinalAnswer leaves failures of
`fatal_tools` unresolved, and never discards a finished answer ([Architecture][AGENT]). The 7 scenarios of
[`test_agent_repair.py`](../tests/entry/test_agent_repair.py) run a small agent whose fatal Tool `probe` fails once, with a
ScriptedLLM, through the public `run()`, so the notes `_adjudicate` attaches are part of the result.

| ID | Scenario | Expected result |
|---|---|---|
| AG01 | The answer after the repair prompt still leaves the `probe` failure | `error`; the response is that answer; `errors` holds the `probe` note once |
| AG02 | The first answer leaves a fatal failure | The third LLM call carries one repair prompt with the failure and a request for the complete answer again |
| AG03 | The failing Tool is not in `fatal_tools` | `ok` with the answer unchanged |
| AG04 | The LLM reruns `probe` successfully after the repair prompt | `ok` with the answer after the repair |
| AG05 | `max_steps=2`: `probe` fails, an answer gets the repair prompt, then the budget runs out | `error`; the response is that answer; `errors` holds the `probe` note once |
| AG06 | `max_steps=1`: `probe` fails and the budget runs out before any answer | `error`; the response is `Required operations remain incomplete: probe: probe refused`; one `probe` note |
| AG07 | `max_steps=1`: a successful Tool call, then the budget runs out | `max_steps` with the `budget` note |

## Verification results

On 2026-10-15, the 425 pytest cases and the 8 Node.js scenarios run by their wrapper passed. Case IDs link to their test files.

| Check | Result |
|---|---|
| `uv run pytest -q` | 425 passed (the 8 Node scenarios count as 1 wrapper case) |
| `uv run ruff check src tests` | Passed |
| `uv run mypy src` | 107 source files, no issues |
| `git diff --check` | Passed |

The default suite uses no DB, external API, or real LLM; real git runs only in temporary directories for the versioning cases.
It checks business results from public entry points. The ORM models add `discord_message` and `run_feedback` (revision
`c1646cbc9caa`); `alembic upgrade head`, `downgrade -1`, `upgrade head`, and `alembic check` (no new operations) were run on
an empty scratch DB.

## Spec references

[SESSION]: components/SESSION.md "Session infrastructure"
[REVIEW]: EXTENSIONS.md#development-and-reviews "PR and local review"
[HTTP]: components/FASTAPI.md "Management API and report viewing"
[LOG]: LOGGING.md "Execution logs"
[GATEWAY]: ../gateway/README.md "Shared gateway"
[UI]: components/DASHBOARD_SPEC.md "Dashboard operations"
[RUNS]: components/DASHBOARD_SPEC.md#s4-run-flow-runs "Run flow view"
[RUN]: ARCHITECTURE.html#contract-2 "Input, Context, and result"
[AGENT]: ARCHITECTURE.html#contract-3 "Agent loop and harnesses"
[QUEUE]: components/QUEUE.md "Queue, intake, and Runtime execution"
[VERSIONING]: components/VERSIONING.md "Component versioning (git component repository)"
[ISSUE]: EXTENSIONS.md#development-and-reviews "Investigation on assignment"
[SENTRY]: EXTENSIONS.md#scheduled-components "Sentry auto-fix test cases"
[SCHED]: components/SCHEDULING.md#test-cases "Scheduling test cases"
[OPT]: components/OPTIMIZER.md#test-cases "Optimizer test cases"

- [SESSION](components/SESSION.md): session infrastructure
- [REVIEW](EXTENSIONS.md#development-and-reviews): PR and local review
- [HTTP](components/FASTAPI.md): management API and report viewing
- [LOG](LOGGING.md): execution logs
- [GATEWAY](../gateway/README.md): shared gateway
- [UI](components/DASHBOARD_SPEC.md): dashboard operations
- [RUNS](components/DASHBOARD_SPEC.md#s4-run-flow-runs): run flow view
- [RUN](ARCHITECTURE.html#contract-2): input, Context, and result
- [QUEUE](components/QUEUE.md): queue, intake, and Runtime execution
- [VERSIONING](components/VERSIONING.md): component versioning (git component repository)
- [ISSUE](EXTENSIONS.md#development-and-reviews): Issue workflow, investigation on assignment
- [SENTRY](EXTENSIONS.md#scheduled-components): Sentry auto-fix test cases
- [SCHED](components/SCHEDULING.md#test-cases): scheduled trigger and one-shot reservation test cases
- [OPT](components/OPTIMIZER.md#test-cases): optimizer test cases

## LLM history compatibility

Fallback telemetry is checked by [the 14 diagnostic boundary cases](../tests/entry/test_llm_fallback_telemetry.py).
Switch success, calls outside a run, concurrent runs, backup failure, primary success, errors not eligible for switching,
and cancellation run against a mock client, checking the correlation ID, the primary client's elapsed time, body redaction,
and that calls, results, and exceptions are preserved.
Concurrent calls in one run and parent agent must match distinct persisted LLM execution IDs. The call scope is restored
after success, a primary error, a backup error, cancellation, a recorder error, and nested tracked calls.

[The 9 send-contract cases](../tests/entry/test_llm_replay.py) mock the SDK boundary and check, for DeepSeek thinking,
missing, null, and empty-string values; preserving existing reasoning; other providers; thinking disabled; fallback from
a transient failure and from an HTTP 200 error body without choices; and that a body without choices or error stays a
parse error that is not switched. Tool call IDs and the original history are kept. Real API quality and performance are not part of this check.

[The 4 tier cases LT01–LT04](../tests/entry/test_llm_tiers.py) replace only the SDK boundary. With Z.AI they check the model and
reasoning effort that `max`, `high`, `low`, `flash`, and `flash_low` each send; then `flash_low` switching to DeepSeek on a
transient failure, every name being the primary client without Z.AI, and an agent that declares `flash_low` composing and
calling that client. Real API quality and performance are not part of this check.
