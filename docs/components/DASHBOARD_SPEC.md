# Dashboard view contract

Status: implemented and verified.

Scope: `src/trackeverything/api/static/` (the shell `index.html` and, under `js/`,
`api.js` = the API contract layer `DashboardApi`, `app.js` = the shared shell (stores the API token and selects the default view),
`catalog.js` / `catalog-model.js` / `catalog-poster.js` = Agent / Tool, `usage.js` / `usage-model.js` = usage,
`runs.js` / `runs-model.js` = run flow), plus the run-flow API in `src/trackeverything/api/runs.py`.
The management API contract is in [FASTAPI.md](FASTAPI.md); the shared rules follow
[ARCHITECTURE.html](../ARCHITECTURE.html). This document is shared by spec, harness, and implementation.

The dashboard UI text is Japanese. Labels quoted below are the literal UI strings, with an English gloss on first use.

## S1. View layout and switching

- The header nav switches between the run flow view (`実行フロー`, "run flow", `#/runs`), the Agent / Tool view
  (`#/catalog`), and the usage view (`実行統計`, "usage statistics", `#/usage`). The hash holds the current view.
  The nav link of the current view has `aria-current="page"`.
- The default view is the run flow. When the hash is none of these three views (including `#/`), `app.js` sends it to `#/runs`.
- `catalog.js` switches views. Each view fetches its own data when the hash reaches it and does not depend on other views' state.
- Management API calls go through `DashboardApi` in `api.js` (`/api/catalog`, `/api/usage`,
  `/api/runs`, `/api/sessions`). Failures keep the HTTP status so the caller can tell 404 from other errors.

## S2. Agent / Tool (`#/catalog`)

- Shows the runtime's stored snapshot returned by `GET /api/catalog`, with its save time and whether the runtime is online or awaiting an update.
- The diagrams (overview, execution flow, delegation graph, tool usage), expandable rows, search, section toggles, and
  the "unconnected only" filter follow [the Agent / Tool catalog section of FASTAPI.md](FASTAPI.md#agent--tool-catalog-catalog).
- The delegation graph draws only the declared relations Source / Webhook → entry agent → child agent.
  Source rows show the key, description, and implementing module.
- Manual refresh only; no automatic refresh.

## S3. Usage (`#/usage`)

- Matches the aggregates of `GET /api/usage` by name against the registered list of `GET /api/catalog`.
  Registered components that never ran are listed with 0 calls; names that exist only in the aggregates are listed as no longer registered.
- Row content, sort order, and the note on the aggregation range follow [the usage section of FASTAPI.md](FASTAPI.md#usage-stats-usage).
- Manual refresh only; no automatic refresh.

## S4. Run flow (`#/runs`)

Enter a run ID or session ID to see how that execution flowed, as Mermaid diagrams with a replay animation,
including each step's duration and result (status, errors, short preview).
The view reads the existing `logs`, `session_run`, and `session` tables only. It changes no schema and writes nothing.

### S4.1 Entry and ID resolution

- `#/runs` shows an input field and the latest 20 runs (`最近の実行`, "recent runs"). Each row has the run ID, status,
  entry agent, start time, duration, step count, and session ID (if any). Selecting a run ID moves to
  `#/runs/<encodeURIComponent(run_id)>`; selecting a session ID opens that ID the same way.
- Submitting the input trims surrounding whitespace and moves to `#/runs/<encodeURIComponent(value)>`. An empty value does nothing.
- `#/runs/<ID>` queries `GET /api/runs/{ID}`, and only on 404 queries `GET /api/sessions/{ID}`.
  When both return 404, it shows `run と session のどちらにも見つかりません: <ID>` ("not found as a run or a session").
  Other failures show the reason.
- 401 opens the existing connection settings. The token never appears on screen or in error text.
- The run view has a link back to the recent runs (`← 最近の実行`) and, when the run has a session, a link to that session's view.
- The shown ID is in the hash (`#/runs/<encodeURIComponent(ID)>`) and can be shared.
- Manual refresh only (`更新`, "refresh"); no automatic refresh, because it would interrupt the replay.
  A refresh of a session view keeps the selected run when it still exists.

### S4.2 API

All endpoints require the existing management token (`X-Webhook-Token`), only read the DB, and never start an execution.
Reads require the PostgreSQL `DATABASE_URL`.

| Endpoint | Response |
|---|---|
| `GET /api/runs?limit=20` | `{items}`: summaries of the latest runs, ordered by their most recent `logs` record. `limit` is 1–100, default 20 |
| `GET /api/runs/{run_id}` | `{run, steps, truncated}`: the run summary and up to 400 steps. 404 when neither `logs` nor `session_run` knows the run |
| `GET /api/sessions/{session_id}` | `{session_id, parent_session_id, created_at, total_runs, runs}`: the latest 50 runs (by `session_run` start) in start order. 404 when the session is unknown |

- The ID is the whole rest of the path (it may contain `/`).
- Steps are ordered by `started_at` ascending, ties by `logs.id`. Beyond 400 steps, the first 400 are returned and `truncated` is true.
- Run status: the status of the root agent record (an agent event without a parent) if one exists; otherwise the
  `session_run` status (`running` means in progress); otherwise `incomplete`. `complete` tells whether the root agent record exists.
- A run summary has `run_id`, `session_id`, `agent` (the entry agent), `status`, `complete`, `started_at`, `duration_ms`,
  `step_count`, `cost_usd` (total LLM cost), and `error_count` (failed steps). The duration is the root agent's when it exists,
  otherwise from the first recorded start to the last recorded end, otherwise from the `session_run` start to its finish.
- A step has `id` (execution ID), `parent`, `kind`, `name`, `started_at`, `duration_ms`, `status`, `cost_usd`,
  `tokens` (`{input, output, total}`), `errors`, and `preview`.
- `preview` is at most 300 characters, with `…` appended when cut. Tool: its arguments. Agent: the response body.
  LLM: the names of the tools it chose (`呼び出し: a, b`, "calls: a, b") or its response text. Harness and CLI: the start of the result JSON.
  Other kinds (such as Source) have no preview.
- `errors` turns an agent's `[{stage, cause}]` into `stage: cause`, and other errors into `code: message` or
  `type: message`, one entry each, each at most 300 characters.
- LLM and Agent requests (prompt, conversation, Context) are never returned. Reads go through the ORM, select only the
  needed columns, and never read the LLM request column.

### S4.3 Run diagrams

- A run has three diagrams: structure (`構成`, the default), sequence (`順序`), and timeline (`タイムライン`).
- Each step shows its kind with a mark (Agent `◆`, LLM `✦`, Tool `⚙`, Harness `⛨`, CLI `⌘`, Source `⇣`, Skill `❖`)
  and its status with a mark and color: `ok` success color `✓`; `error` failure color `✗`; `max_steps`, `cancelled`,
  and `interrupted` warning color `!`; `running` running color `…`; anything else neutral `·`.
  Status labels: `ok` `成功`, `error` `失敗`, `max_steps` `上限到達`, `cancelled` `中断`, `interrupted` `停止で未完了`,
  `running` `実行中`, `incomplete` `未完了`.
- Durations show milliseconds below 1 second, seconds with one decimal below 60 seconds, minutes and seconds below
  1 hour, and hours and minutes above that.
- Strings are escaped for Mermaid labels (`"`, `<`, `>`, `&`); whitespace, including newlines, collapses to a space.
- Each diagram can zoom in and out, fit the frame width (automatic fitting stops at 80%; wider diagrams scroll),
  and show its Mermaid source. When rendering fails, the view shows the reason and the source.
- A run with more than 400 steps draws the first 400 and says so.

**Structure (Agent, LLM, Tool, Skill lanes).**

- Four lanes (Mermaid subgraphs), always in the order Agent, LLM, Tool, Skill from left to right. An empty lane shows `なし` ("none").
- Blocks:
  - Agent lane: the start block (`開始`, "start") and one block per agent name, however many times that agent was called.
    For steps whose parent is not recorded in the run, one unrecorded-execution block (`未記録の実行`) per parent ID acts as the caller.
  - LLM lane: one block per model (`component`).
  - Tool lane: one block per tool name. Harness, Source, CLI, and other event kinds are also placed here per name and told apart by their mark.
  - Skill lane: one frame per skill name, holding a body-read block (`本文の読込`) for the `read_skill` calls and one block per
    function for the `skill__<skill name>__<function name>` tool events. The skill name of `read_skill` comes from the `name`
    argument in the preview, or `不明` ("unknown") when it cannot be read. Shared tools reused by a skill stay in the Tool lane.
- One arrow per step, from the caller block to the step's block. The caller is the parent agent's block; the start block
  when the step has no parent; the unrecorded-execution block when the parent is not recorded. Every call adds an arrow,
  even for the same caller and callee, and calls to the same tool from different agents point to the same tool block.
- An arrow's label is only the step's start-order number (1-based, the order of S4.2); a failed step's number is prefixed with `✗`.
- A block shows its mark and name, `N回` ("N calls"), the total duration, and the failure count (`✗k`) when there are failures;
  an LLM block also shows its total cost. Its color is the failure color when any call failed, otherwise the warning color when
  any call ended with a warning status, otherwise the running color when a call is running, otherwise the success color.
- Selecting a block lists its calls in start order (number, start time, duration, status, error causes, preview).
  The start block shows the run information of S4.5.

**Sequence.**

- A start node (`▶ 開始`), then the root column of steps, then a result node (`■ 結果`) with the run status and overall duration.
- An agent step is a frame (subgraph) whose first node shows the agent name, status, and duration. Its direct child steps are
  linked in start order. A child agent's frame sits in the parent's column at its start position, and its last step links
  to the parent's next step.
- Steps whose parent is not recorded in the run are grouped per parent ID into an unrecorded-execution frame placed in the
  root column in start order. Root-level events that are not agents (legacy `source` / `cli` roots) also go in the root column.
- An LLM node shows the tools it called (`→ a, b`) or `最終応答` ("final response"); a failed node shows its first error.

**Timeline.**

- A Mermaid gantt with one section per agent (plus `実行` ("execution") for root-level steps and `未記録の実行` for unrecorded parents),
  placing each step as a bar on real time. `error` bars are highlighted (`crit`). The timeline is not replayed.

### S4.4 Replay

- Replay compresses the start and end times of steps with a monotonic time warp: the gaps between boundaries are
  compressed logarithmically, which keeps order and nesting (a child starts after its parent starts and ends before it ends)
  and still shows longer steps as longer. The total 1× replay time is 750 ms per step, clamped to 4–45 seconds.
- During replay each node is pending (faded), running (highlighted border), or done (status color). When a step starts,
  a dot travels along the edge from the previous node.
- Controls: play / pause (`再生` / `一時停止`), restart (`最初から`), speed (0.5×, 1×, 2×, 4×), and a position slider.
  The real elapsed time at the replay position is shown as `経過 m:ss / <total>` ("elapsed").
- After the end and while paused, the view keeps the state at that point. Redrawing a diagram prepares the replay from
  the start; autoplay happens once. While playing, the diagram scrolls to follow the most recently started step.
- In the structure diagram, an arrow appears when its step starts and a dot travels along it. A block is pending before
  its first call, running while any of its calls is running, and done otherwise (it returns to running on each call).
- With `prefers-reduced-motion: reduce`, there is no replay: all steps are shown done, and the controls are disabled with the reason.
- Replay stops while the browser tab is hidden, the diagram is off screen, or another view is shown, and resumes when it becomes visible again.

### S4.5 Step details

- Selecting a node (click, Enter, or Space) shows its kind, name, status, start time (in the browser's time zone),
  duration, tokens (input, output, total), cost (USD), error causes, preview, execution ID, and parent execution ID.
- The start and result nodes show the run ID, session ID (as a link), entry agent, status (noting when there is no root agent
  record), start, overall duration, step count (noting when only the first 400 are shown), total LLM cost, and the number of failed steps.
- The unrecorded-execution frame explains that its steps have a parent execution ID but no parent agent record (which happens
  for running or abnormally terminated runs and legacy records) and shows the parent execution ID.

### S4.6 Session

- The session view shows the session ID, parent session ID, creation time, the total run count with counts per status,
  and a left-to-right diagram of the runs in start order. A run node shows the entry agent, status, duration, start time,
  and short run ID. Beyond 50 runs, it shows the latest 50 and the number omitted. A session without runs says so.
- Selecting a run node shows that run's diagrams (S4.3–S4.5) below the session diagram. Initially the latest run is selected.
- The session diagram lights up its runs in order with the same replay rules as S4.4.

## S5. Visuals, icons, accessibility

- Neutral colors form the base; a blue-to-purple gradient on the top band is the accent.
- Icon-only controls have a Japanese accessible name and a description visible on hover, focus, and similar.
  Every operation is reachable by keyboard, and focus is kept after the view updates.
- Touch targets are about 44 px. Long identifiers wrap so the full value stays readable,
  and the page never overflows horizontally.
- The view follows the OS light / dark setting. Primary text has a contrast of at least 4.5:1, and status icons keep a distinguishable contrast.
- Animated diagrams do not move under `prefers-reduced-motion: reduce`, while the tab is hidden, or while off screen.
- External strings are escaped. IDs in operation URLs are encoded.

## S6. Authentication and failures

- The API token from the connection settings is sent as `X-Webhook-Token` and stored in localStorage as `te_token`.
  The view also works with the current input value where storage is unavailable. Changing the token refetches the current view.
- A 401 opens the connection settings panel, where the user can recover. The token never appears in results or error text.
- A failed fetch and an empty result are displayed differently.

## S7. Implementation scope

- A single HTML file, CSS, vanilla JavaScript, and the management API.
- Diagram data is not embedded in the product; displayed values and counts derive from the actual GET responses.
- Icons are local SVG and native symbols. Mermaid and Anime.js use the bundled copies in `static/js/vendor/`;
  no external CDN, framework, or build step is added.
- In the run flow, pure logic (tree building, Mermaid generation, the replay time warp, display formatting) lives in
  `runs-model.js`, view control in `runs.js`, the HTTP contract in `api.js`, the API in `api/runs.py`, and the reads in
  `core/builtin/execution_log.py`. Replay is drawn with `requestAnimationFrame` so seeking and speed changes stay consistent.
- Update the view description in [FASTAPI.md](FASTAPI.md) together with the product implementation.

## S8. Verification of business operations

`tests/browser/dashboard_logic.test.cjs` runs the real view JavaScript with minimal DOM / fetch stubs, and Mermaid
replaced by a stub that returns a rendered result.
The cases are collected as U06–U13 in [the business test catalog](../BUSINESS_TESTS.md#dashboard-operations);
the run-flow API cases are H22–H27 there.

- Catalog relations and usage, including components that never ran.
- The run's sequence and structure diagrams, replay, the session view, selection from recent runs, and the display of a run
  cancelled by its deadline (shown as `中断` and counted as a warning, U13).

`tests/browser/test_dashboard.py` runs the 8 Node.js scenarios from pytest.
CSS values, brute-force checks of internal helpers, and automated checks of real-browser rendering and layout are out of scope.
SQL reads and real-browser rendering and replay are confirmed during acceptance by reading the live DB read-only, and are
not covered by the automated tests.
