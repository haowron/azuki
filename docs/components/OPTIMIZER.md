# Optimizer

The optimizer is a Host feature that observes operational records and finds improvements to agents, tools, and skills.
It lives in `src/trackeverything/optimizer/`, is owned by the Host, and is not part of the five version-owned folders,
so the optimizer itself never enters a candidate version. It runs only in the runtime process.

Five checks are implemented. `errors` finds agents that failed in the last 24 hours, repairs the ones whose code can be
fixed in a fork of the current main in the version store, and merges the repair into the shared `main` (rebased on top
of newer pushes when needed): a push to the component repository on GitHub, or in local mode a fast-forward of the
runtime's mirror. The runtime then uses it. `consolidation` keeps the component set small while agents, tools, and
skills are added every day: it deletes what is unreachable or unused, merges what duplicates, and removes declarations
that are never used, each change as its own fork and merge. `additions` reads the last 7 days of `general` runs and
`shell`/`bash` commands, finds work that recurs in at least 3 runs, and turns it into a new skill, tool, or agent wired
into the agent that did the work, or rewrites the description of an existing component that already does it; each
change is merged the same way. `tuning` looks for wasted LLM steps, tokens, and time in the last 14 days of runs: it
lowers the `llm` of agents whose recorded LLM steps give the same decisions at a lower reasoning effort (checked by
replaying them), watches every change it merged and reverts the ones that made runs worse, and reports the other kinds
of waste it finds. `behavior` reads the operator's own last 14 days on Slack, GitHub, and Discord, finds routine work
that recurs in at least 2 episodes, merges the parts that fit the same guard rails as `additions` (a new skill, tool,
or agent, or a better description of an existing one), and reports every routine with whether AI can take it over.

Related: [Component versioning](VERSIONING.md) (the version store), [Queue](QUEUE.md) (named runs),
[Scheduling](SCHEDULING.md) (the `schedule` table).

## Layout

| File | Owner | Role |
|---|---|---|
| `optimizer/__init__.py` | Host | Public entry points `install(optimizer)` and `handle(data)` |
| `optimizer/model.py` | Host | Input `OptimizeRequest`; values `Observation`, `Finding`, `CheckOutcome`, `Report` |
| `optimizer/checks.py` | Host | Contract `Check` and `CheckContext`; the registry `CHECKS` |
| `optimizer/errors.py` | Host | The `errors` check (`ErrorsCheck`) |
| `optimizer/consolidation.py` | Host | The `consolidation` check (`ConsolidationCheck`) |
| `optimizer/additions.py` | Host | The `additions` check (`AdditionsCheck`) |
| `optimizer/tuning/` | Host | The `tuning` check (`TuningCheck`): `records.py` (`logs` reads), `detect.py` (detectors), `replay.py` (step replay and gate), `effort.py` (the `llm` edit), `watch.py` (watch values), `check.py` (the check) |
| `optimizer/behavior/` | Host | The `behavior` check (`BehaviorCheck`): `common.py` (episodes, events, HTTP), `slack.py`, `github.py`, `discord.py` (the platform readers), `collect.py` (`collect_activity`), `digest.py` (the survey input), `check.py` (the check) |
| `optimizer/proposal.py` | Host | The candidate conversation, its Python checks, fork, and merge shared by `additions` and `behavior` |
| `optimizer/worktree.py` | Host | The working-tree tools the checks give the LLM (`list_files`, `read_file`, `grep`, `write_file`, `delete_file`) |
| `optimizer/service.py` | Host | `Optimizer`: observe → run checks → report |
| `agent/optimizer.py` | Version | Entry agent `OptimizerAgent` (Python control): calls `handle()`; in the component repository it also posts the report to Discord ([Report delivery](#report-delivery)) |
| `agent/dispatch.py` | Version | Declares `optimizer` among the children reachable only by named runs |
| `agent/compose.py` | Version | Lists `OptimizerAgent` in `AGENTS` |
| `core/builtin/app.py` | Host | `build_runtime` creates the `Optimizer` and calls `install` |
| `core/versioning/repo.py` | Host | The process's version store singleton (`open_repo`, `current_repo`) |
| `process.py` | Host | `restart_roles`: a change under `optimizer/` restarts only the runtime |

- `optimizer/` imports types from `core` (`abc`, `builtin`, `orm`, `versioning`) and `llm`. It does not import version
  components, `queue`, or `api`.
- `Optimizer` receives Host services as plain values and functions: the LLM client map (`HostServices.llms`), the run
  record reader (`read_recent_runs` bound to `DATABASE_URL`), and a clock. The main version id comes from the version
  store singleton (`current_repo().main.id`).
- Because the optimizer is Host code, a change to it takes effect when the runtime restarts, not through the component repository.

## Startup

The optimizer starts as a dispatch named run with `agent_name="optimizer"`. The entry agent `agent/optimizer.py` is a
thin version-owned agent: it passes `ctx.data` to the Host's `optimizer.handle()` and returns the result. The component
repository's entry agent then posts the report to Discord ([Report delivery](#report-delivery)); the package's default
entry agent only returns it. Run records, `RUN_TIMEOUT`, the ACK by dispatch, and attribution of LLM cost to the run work
as for every other named run.

Publish one run with the deploy checkout's launcher:

```bash
cat > /tmp/optimizer.json <<'JSON'
{"provider": "internal", "external_id": "cli:optimizer",
 "payload": {"agent_name": "optimizer", "data": {"limit": 100}}}
JSON
bash ~/Services/trackeverything/scripts/run_local.sh python -m trackeverything.queue.cli publish --file /tmp/optimizer.json
```

The daily run of the `errors` check is the `schedule` row `optimizer-errors` (cron `0 5 * * *`, time zone
`Asia/Tokyo`, the column default). The queue process's [scheduled trigger](SCHEDULING.md) publishes it every day at
05:00 with `external_id` `schedule:optimizer-errors:<fire time>` and this payload:

```json
{"agent_name": "optimizer", "data": {"checks": ["errors"]}}
```

The 24-hour window the check reads equals the firing interval. The daily run of the `consolidation` check is the row
`optimizer-consolidation` (cron `30 6 * * *`) with the payload
`{"agent_name": "optimizer", "data": {"checks": ["consolidation"]}}`. It runs after `errors` on purpose: every merge
moves main, and `errors` repairs only failures of the current main, so moving main right after `errors` leaves `errors`
almost a full day of runs on one version. The daily run of the `additions` check is the row `optimizer-additions`
(cron `30 7 * * *`) with the payload `{"agent_name": "optimizer", "data": {"checks": ["additions"]}}`, after both for
the same reason. Its 7-day window is longer than the firing interval; runs that an earlier `additions` change already
used are left out ([Observation](#observation-1)). The daily run of the `tuning` check is the row `optimizer-tuning`
(cron `0 8 * * *`) with the payload `{"agent_name": "optimizer", "data": {"checks": ["tuning"], "merge": false}}`, after
the others for the same reason; `merge` stays false until its reports have been reviewed, so it only keeps candidates as
forks. The daily run of the `behavior` check is the row `optimizer-behavior` (cron `30 8 * * *`) with the payload
`{"agent_name": "optimizer", "data": {"checks": ["behavior"]}}`, last for the same reason. Its 14-day window is longer
than the firing interval; episodes that an earlier `behavior` change already used are left out
([Observation](#observation-2)). A run without `checks` runs every check, `tuning` and `behavior` included, with `merge`
true unless the input says otherwise. Rows are changed with SQL:

```bash
docker compose exec -T db psql -U trackeverything -d trackeverything -c \
  "INSERT INTO schedule (id, cron, payload) VALUES ('optimizer-errors', '0 5 * * *', '{\"agent_name\": \"optimizer\", \"data\": {\"checks\": [\"errors\"]}}');"
docker compose exec -T db psql -U trackeverything -d trackeverything -c \
  "INSERT INTO schedule (id, cron, payload) VALUES ('optimizer-consolidation', '30 6 * * *', '{\"agent_name\": \"optimizer\", \"data\": {\"checks\": [\"consolidation\"]}}');"
docker compose exec -T db psql -U trackeverything -d trackeverything -c \
  "INSERT INTO schedule (id, cron, payload) VALUES ('optimizer-additions', '30 7 * * *', '{\"agent_name\": \"optimizer\", \"data\": {\"checks\": [\"additions\"]}}');"
docker compose exec -T db psql -U trackeverything -d trackeverything -c \
  "INSERT INTO schedule (id, cron, payload) VALUES ('optimizer-tuning', '0 8 * * *', '{\"agent_name\": \"optimizer\", \"data\": {\"checks\": [\"tuning\"], \"merge\": false}}');"
docker compose exec -T db psql -U trackeverything -d trackeverything -c \
  "INSERT INTO schedule (id, cron, payload) VALUES ('optimizer-behavior', '30 8 * * *', '{\"agent_name\": \"optimizer\", \"data\": {\"checks\": [\"behavior\"]}}');"
```

### Input `OptimizeRequest` (`ctx.data`)

| Field | Type | Default | Constraint |
|---|---|---|---|
| `limit` | int | 100 | 1 to 1000 |
| `checks` | list of strings or null | null (every registered check, in registration order) | Registered names only, no duplicates |
| `merge` | bool | true | When false, checks that change versions keep their candidates as forks and never merge (main does not change) |

Unknown fields are rejected.

## Behavior

1. If no optimizer is installed in the process (any process other than the runtime, such as the check CLI), the run
   ends with the error `optimizer is not installed in this process`.
2. The input is validated. Invalid input ends the run with an error before anything is observed.
3. **Observe**: the run record reader is called once with `limit`, and the current run (`current_run_id`) is removed
   from the result. The observation carries the main version id and the observation time. A read failure ends the run
   with an error without running any check.
4. **Run checks**: the selected checks are awaited one at a time in registration order. Every check receives the same
   `CheckContext` (the observation, the LLM client map, and `merge`). An exception in a check is recorded as that check's failure
   (`<exception type>: <message>`), and the next check runs. Cancellation (`CancelledError`) is not recorded; it
   propagates.
5. **Report**: the outcome of every check goes into `response`. If any check failed, the status is `error` and
   `errors` holds one `ErrorNote(stage="check:<name>", cause=<failure>)` per failed check. Otherwise the status is `ok`.

### Report (`response`)

The report is the run result (`AgentResult.response`). Read it in the run flow view of the
[dashboard](DASHBOARD_SPEC.md) or in `session_run.response_json`.

```json
{"observed_at": "2026-10-13T09:00:00+00:00", "main_version": "<12-character main version id>", "run_count": 99,
 "checks": [{"name": "errors", "status": "ok", "findings": [{"summary": "...", "evidence": ["<run_id>"]}]},
            {"name": "prompts", "status": "failed", "error": "RuntimeError: ...", "findings": []}]}
```

Each finding has `summary` and `evidence`, plus `detail` (JSON) when the check adds details.

### Report delivery

The component repository's entry agent (`agent/optimizer.py`, a `DiscordOutputAgent` on the default `llm` `max`) posts every run's
report to Discord after `handle()` returns, whatever the run did: scheduled or manual, `merge` true or false, failed
checks, no findings, or an input or observation error.

1. **Channel**: the text channel named `optimizer` in `DISCORD_GUILD_ID` (derived from `DISCORD_NOTIFY_CHANNEL` when
   unset), looked up with `discord_api` (`text_channel_by_name` in `tools/discord/guilds`). A missing channel, or more
   than one with that name, is a delivery failure; no channel is created and nothing falls back to another channel.
2. **Message**: a count line per check, `optimizer <check> · <status> — <outcome> <count> · …` (`无发现` without findings,
   the error for a failed check, joined with ` | `), then a natural-language summary of the problems found and the changes
   made. One LLM step writes the summary in `OUTPUT_LANGUAGE` from a compacted report, and the `translator` child
   translates it into `DISCORD_LANGUAGE`. When the summary or the translation fails, the message is the count line alone.
   When the message does not fit in one Discord message (1,900 characters), the whole text is published as a page with
   `publish_page` (Markdown, titled `optimizer 报告 · <checks> · <JST time>`) and the message becomes the count line,
   `摘要超过了一条消息的长度, 已生成网页版。`, and the URL, the Markdown page that long conversation replies fall back to
   ([Delivery](DISCORD.md#delivery)). When publishing fails,
   the full text is sent split over several messages.
3. **Attachment**: `optimizer-<checks joined by +>-<YYYYMMDD-HHMM JST>.html`, rendered by `tools/optimizer_report`: one
   file with inline CSS and no scripts or external resources. Per check it shows the status, the error, and one card per
   finding (outcome, subject, full reason, changed paths, base and candidate versions, evidence, and the remaining
   `detail` fields in a collapsible section). Fixed labels are Simplified Chinese; the checks' text is kept as written.
   A merged or reverted candidate links to its commit on GitHub (from `COMPONENT_REPO_URL`); other candidates exist only
   in the mirror (`refs/te/forks/`) and are not linked.
4. **Sending**: `discord_file` (`tools/discord/file`) sends the message with the usual 🤖 prefix, line splitting, and
   disabled mentions, and attaches the file to the last part. It records the sent parts in `discord_message` with the
   run and the entry agent's execution, so the owner's 👍/👎 on a report rates the optimizer run
   ([feedback](DISCORD.md#feedback-on-bot-messages)).

Delivery never changes the run's `status` or `response`. A failure (channel lookup, HTTP error, network error) is logged
and added to `errors` as `ErrorNote(stage="notify")`. Without `DISCORD_BOT_TOKEN` nothing is sent and the summary is not
written. Diffs are not attached.

## Check contract

```python
class Check(Protocol):
    name: str
    async def run(self, ctx: CheckContext) -> Sequence[Finding]: ...
```

- `CheckContext` has `observation`, `llms` (name → `LLMClient`), and `merge` (default true). Checks may use an LLM.
- Checks do not write to the database or send anything outside except through the version store. Every check changes
  versions (fork and merge in the version store). With `merge` false they fork but never merge, and report the outcome
  `forked` with the candidate's id (kept under `refs/te/forks/<id>` in the mirror). A forked `additions` or `behavior`
  candidate still consumes its runs or episodes, like a merged one. `behavior` reads Slack, GitHub, and Discord, read
  only.
- No check changes `agent/SYSTEM.md`, the preamble people maintain for every agent's LLM calls
  ([Component versioning](VERSIONING.md#system-preamble)). `read_file` and `grep` see it, while `write_file` and
  `delete_file` refuse it with `not writable: agent/SYSTEM.md (maintained by people)`. `tuning` edits only the `llm`
  line of an agent class. A rebase on merge keeps a newer `agent/SYSTEM.md` that a person pushed meanwhile.
- Check names are unique. Registering two checks with the same name raises `ValueError` when the `Optimizer` is created.
- `CHECKS` registers only implemented checks, so an empty check can never look like a check that ran and found nothing.
- A JSON answer of the LLM (a survey or a final answer) is read with `answer_json` in `optimizer/worktree.py`: the whole
  answer (a surrounding ` ``` ` fence is removed), or, when that is not JSON, the last JSON object inside the text, so
  explanatory text before or after the JSON is ignored. An answer without a JSON object, or a JSON of another shape, is
  malformed.

Reserved check names:

| Name | Purpose |
|---|---|
| `errors` | Repair agents from everyday run failures (implemented) |
| `consolidation` | Delete, merge, and prune tools, agents, and skills (implemented) |
| `additions` | Add skills, tools, and agents for recurring work, or describe existing ones better (implemented) |
| `tuning` | Cut LLM steps, tokens, and time; covers the former `models` and `prompts` (implemented) |
| `behavior` | Take over the operator's routine work on Slack, GitHub, and Discord, and report what AI can take over (implemented) |

Out of scope for the checks: storing or sending the report (the entry agent sends it, see
[Report delivery](#report-delivery)), and tables of the optimizer's own (`tuning` keeps its state in the trailers of the
commits it makes, `additions` and `behavior` keep what they consumed in commit messages). Only `behavior` reads external
data.

## The `errors` check

`ErrorsCheck` in `optimizer/errors.py` reads failures from the `logs` table directly through `core/orm`
(`ExecutionLog` and `sessions_for(config.DATABASE_URL)`) and uses the process's version store singleton
(`current_repo()`). It calls `fork`, `merge`, `refresh`, and `main_version` on that store directly. The LLM is the
Host's `max` client; no external CLI is used.

| Constant | Value | Meaning |
|---|---|---|
| `WINDOW` | 24 hours | How far back from the observation time failures are read |
| `MAX_AGENTS` | 5 | Agents repaired per run |
| `MAX_TURNS` | 30 | LLM responses per agent conversation |
| `MAX_FORKS` | 2 | Fork attempts per agent |
| `LLM_NAME` | `max` | Host LLM used for repairs |
| `FAILED` | `error`, `max_steps` | Agent row statuses that count as failures |
| `WRITABLE` | `agent`, `tools`, `skills` | Version folders the LLM may write |
| `PROMPT_ERRORS` / `PROMPT_ERROR_CHARS` | 20 / 2000 | Failures per prompt and characters per failure |

### Flow

1. **Read**: read once, from `logs`, the rows with `event_type='agent'` that started at or after the observation time
   minus 24 hours and whose status is not `ok`. The columns are `run_id`, `execution_id`, `parent_execution_id`,
   `component`, `started_at`, `status`, `error_json`, and `code_version`. Only `error` and `max_steps` count as
   failures; `cancelled` (the run deadline) and `blocked` do not. Rows of the optimizer itself (component `optimizer`)
   and of the current run are dropped.
2. **Root-cause agent**: a failed row is a root cause when no failed agent row in the same run has its `execution_id`
   as parent (the deepest failure). A parent that failed because a child failed (for example dispatch) is not a target.
   A tool failure appears in the `error_json` of the agent row that called the tool.
3. **Group by agent**: one repair per root-cause agent name. Agents that remain targets after step 4 are processed in
   descending order of failure count (ties by name, ascending), at most `MAX_AGENTS`; the rest are `skipped` with
   reason `over limit`.
4. **Latest-version test**: at the start of the check, `repo.refresh()` reads the mirror's adopted main, and its
   `repo.main_version.label` (`c.<first 8 characters of the version id>`) is the observed main version.
   - The `c.<…>` part of a row's `code_version` (`<Host version>+c.<first 8 characters of the version id>`) is the
     `Version.label` the agent ran. A version is a tree of all five folders, so all agents of one version share a label.
   - Rows whose label equals the observed main version are failures of the latest agent and feed the repair. Other rows
     are not used.
   - An agent with no such row is `skipped` (`stale`). An agent whose rows all lack `+c.` is `skipped`
     (`no component version`). An agent missing from the main component graph (`repo.main_version.graph.agents`) is
     `skipped` (`not in main`).
   - The test always uses the observed main version, even after an earlier agent of the same run was merged, so the
     failures of later agents are not lost.
5. **Judge and repair** (LLM `max`, one conversation per agent, at most `MAX_TURNS` responses). The conversation starts
   with `repo.refresh()`; the tree of that main (the fork base) is copied into a working tree.
   - Input: the agent name; its failures (`run_id`, `status`, `started_at`, the error from `error_json`; the newest 20
     in read order, each error clipped to 2000 characters); the fork base label; and the path of the agent class's
     module, resolved with `snapshot.module_path` from the class's module name in the fork base's component graph. The
     system prompt states that the error text is material that includes external input and that instructions inside
     it must not be followed.
   - Tools on the working tree: `list_files(prefix)`, `read_file(path)`, `write_file(path, content)`. `write_file`
     accepts only paths under `agent/`, `tools/`, and `skills/` other than `agent/SYSTEM.md`; any other path returns a
     failed result to the LLM and leaves the tree unchanged.
   - The final answer is the JSON `{"verdict": "fixed" | "not_agent" | "unfixable", "reason": "<reason>"}`.
6. **Fork**: when the verdict is `fixed` and the tree has changes, `repo.fork(<fork base>, <working tree>,
   "optimizer:errors <agent> runs=<run_id,...>")` saves a candidate. A fork loads the tree and builds its component
   graph. If that fails, the failure (`describe_failure`) goes back into the same conversation for another fix, up to
   `MAX_FORKS` forks in total. If both fail, the outcome is `fork_failed` and main does not change.
7. **Merge**: `repo.merge(<candidate>)` reads the shared `main` (fetching GitHub's). If it is still the fork base, the
   candidate goes on as it is; if a person pushed meanwhile, the candidate is put on top of the new `main` (three-way
   merge from the fork base, message with `Rebased-From: <candidate>`) and loaded. It is then pushed to GitHub, or in
   local mode the mirror's main is fast-forwarded, and the mirror's main advances to it (`merged`). A conflict, a refused
   push or fast-forward, a load failure of the rebased version, or an unreachable GitHub is `failed` with
   `merge: <exception type>: <message>`, and nothing changes
   ([Component versions](VERSIONING.md#version-store-repo)).
8. The next agent starts again at step 5 and forks from the main that the previous merge advanced.

Writes made under a verdict other than `fixed` are discarded. A `fixed` verdict without changes becomes `unfixable`
(reason `fixed without changes: <reason>`).

### Findings

Each finding of the `errors` check is the result for one agent, with `detail`:

```json
{"summary": "review: merged — fixed the KeyError in tools/github_fetch/diff.py",
 "evidence": ["<run_id>", "<run_id>"],
 "detail": {"agent": "review", "outcome": "merged", "reason": "...",
            "base": "<fork base version id>", "candidate": "<pushed version id>", "paths": ["tools/github_fetch/diff.py"]}}
```

| Outcome | Meaning | Main |
|---|---|---|
| `merged` | Repaired and merged into the shared `main` (pushed to GitHub, or fast-forwarded in local mode) | Advances to the merged version |
| `fork_failed` | The repaired tree could not be loaded in either fork attempt | Unchanged |
| `not_agent` | Not an agent problem (external outage, input, Host bug, and so on) | Unchanged |
| `unfixable` | An agent problem that cannot be fixed, or a `fixed` verdict without writes | Unchanged |
| `forked` | Repaired and forked, but not merged because `merge` is false (`candidate` is the fork) | Unchanged |
| `skipped` | Not latest, version not identifiable, or over the limit (see `reason`) | Unchanged |
| `failed` | LLM exception, response limit reached, malformed final answer, or a merge failure (conflict, rejected push, load failure, unreachable GitHub) | Unchanged |

- `evidence` lists the run ids of the failures used (read order, no duplicates).
- `detail` always has `agent`, `outcome`, `reason`, `base`, `candidate`, and `paths`. `base` is the fork base (null
  for agents whose conversation never started), `candidate` is the saved candidate (null without a fork; for `merged`,
  the pushed version), and `paths` lists the changed paths in ascending order. When the merge rebased the candidate,
  `rebased_from` holds the original candidate.
- `reason` for `skipped` is `stale`, `no component version`, `not in main`, or `over limit`; for other outcomes it is
  the LLM's reason or the failure.
- `summary` is `<agent>: <outcome> — <reason>`.
- Findings are ordered: repaired agents in processing order, then agents over the limit, then the other skipped agents.
- A failure for one agent is recorded as its finding and the next agent runs. Cancellation (`CancelledError`)
  propagates. With no failures, the finding list is empty.

## The `consolidation` check

`ConsolidationCheck` in `optimizer/consolidation.py` reads the adopted main's component graph and tree from the version
store singleton, usage from the `logs` table, and the `agent_name` of every `schedule` and `reservation` payload. It
changes versions the same way as `errors` (`fork`, then `merge` into the shared `main`: a push to GitHub, or a
fast-forward of the mirror in local mode). Every change that loads in its fork and passes the reference check is merged;
nothing waits for a person. Components written by people and by agents are treated the same.

| Constant | Value | Meaning |
|---|---|---|
| `WINDOW` | 30 days | How far back usage is counted |
| `GRACE` | 14 days | Minimum age before a component can count as unused |
| `MIN_RUNS` | 20 | Runs of an agent in `WINDOW` before its unused declarations count |
| `MAX_SURVEY` | 5 | Duplicate groups taken from the survey |
| `MAX_GROUPS` | 5 | Groups handled per run (one LLM conversation each) |
| `MAX_TURNS` | 40 | LLM responses per group conversation |
| `MAX_FORKS` | 2 | Attempts per group; fork failures and reference-check failures count together |
| `LLM_NAME` | `max` | Host LLM used for the survey and the conversations |
| `WRITABLE` | `agent`, `tools`, `skills` | Version folders the LLM may write or delete in |

### Observation

1. `repo.refresh()` reads the mirror's adopted main. Its graph, loaded modules, and tree are the observed main.
2. Usage is read once from `logs` for rows that started at or after the observation time minus `WINDOW`: agent rows per
   component (runs and the latest start), and tool rows and agent rows per parent agent component (calls per declared
   tool and child). `read_skill` rows are counted per parent agent and skill name (`name` in `request_json`). The first
   row of each component over all of `logs` gives its first-seen time.
3. The Host base tools `read`, `write`, `edit`, and `bash` (built into `BaseAgent`) are not part of any version, so the
   inventory leaves them out. **Protected** components are never removed: the entrances, the agent `optimizer`, the tools the version registers on every agent, `fetch_source`, `read_skill`, and agents named by the `agent_name` of a `schedule` or `reservation`
   payload.
4. **Age**: a component is old when its module path exists in the newest ancestor of main (following `parent` from
   main in `repo.log()`) created at or before the observation time minus `GRACE`, or when its first-seen time in `logs`
   is at or before that time. A component with neither is new.
5. **Catalog** (the LLM input), built from the main graph and from the classes declared in its modules (found as the Host's
   catalog finds them, `_declared_in` in `core/builtin/catalog.py`):

   | Entry | Fields |
   |---|---|
   | `agents` | `name`, `description`, `llm`, `module`, `tools` (the class's `tools` declaration), `children` (`agents`), `skills`, `parents`, `reachable` (from an entrance through declared children), `runs`, `last_run`, `old`, `protected` |
   | `tools` | `name`, `description`, `arguments` (argument names), `module`, `declared_by` (agents whose class declares it), `calls`, `protected` |
   | `skills` | `name`, `description` (from `SKILL.md`), `declared_by`, `reads` |

### Groups

Python finds three kinds of groups and the survey adds a fourth. They are handled in this order:

| Order | Kind | Found by | Condition |
|---|---|---|---|
| 1 | `unreachable` | Python | An unprotected agent of the graph that no entrance reaches through declared children; an agent class with a `name` declared in the version's modules but not composed; a tool class with a `name` declared in the version's modules that no agent registers. Classes without a `name` and base classes of composed classes are not components. A skill package (`skills/<name>/SKILL.md`) that no agent declares. One group per component: graph agents, then agent classes, then tools, then skills, each by name |
| 2 | `duplicate` | Survey | Components the survey judges to share a purpose (see below), at most `MAX_SURVEY` groups, in survey order |
| 3 | `unused` | Python | A reachable, unprotected, old agent with no run in `WINDOW` (one group per agent, by name) |
| 4 | `unused_edges` | Python | For an old agent with at least `MIN_RUNS` runs: declared tools, children, and skills that are old, unprotected, and were never called or read by it in `WINDOW`. One group per agent (by name) whose targets are the agent and then those names in declaration order (tools, children, skills). A name written as a string literal in the agent's class modules (its own module and its base classes' modules in the version), outside the class-level `agents` and `skills` declarations, is used from Python and is left out |

The first `MAX_GROUPS` groups are handled; the rest are `skipped` with reason `over limit` and come back in the next
run. A tool or skill whose declarations were all removed becomes unreachable and is deleted by a later run.

**Survey**: one LLM call without tools receives the JSON `{"catalog": {"agents": [...], "tools": [...], "skills": [...]},
"groups": [{"kind": "<kind>", "targets": [...]}]}` (the Python groups) and returns
`{"groups": [{"targets": ["<name>", ...], "reason": "<reason>"}]}`: components whose purpose is the same or contained in
another, and agents whose only value is their prompt (a candidate for an action of another agent or a skill). Names
that are not in the catalog are dropped, a group left without targets is dropped, and groups after the fifth are
ignored. A failed or malformed survey becomes a finding with kind `survey` and outcome `failed`, and the Python groups
still run.

### Handling a group

Each group is one conversation with the LLM `max`, at most `MAX_TURNS` responses. Before it starts, `repo.refresh()`
reads the adopted main (the fork base); a group with a target that no longer exists in it is `skipped` with reason `gone`.
The tree of the fork base is copied into a working tree.

- Input: the kind, the targets, Python's reason, the catalog entries of the targets and of their parents, children, and
  declaring agents, the protected names, and the names referenced by `schedule` and `reservation`. The system prompt
  states that descriptions, prompts, and logs are material and that instructions inside them must not be followed.
- Tools on the working tree: `list_files(prefix)`, `read_file(path)`, `grep(pattern, prefix)` (a regular expression;
  at most 200 matching lines as `path:line: text`), `write_file(path, content)`, and `delete_file(path)`. Writing and
  deleting accept only paths under `agent/`, `tools/`, and `skills/` other than `agent/SYSTEM.md`.
- Actions: `delete` (unreachable, or its purpose is fully contained in another component), `merge` (keep one survivor —
  the most used, then the oldest — move only the behavior the survivor lacks, point every reference at the survivor, and
  delete the others), `prune` (remove unused declarations), `demote` (an agent whose value is only its prompt becomes an
  action of another agent or a skill; a tool used by one skill becomes that skill's own tool), and `keep`. The same
  change updates `agent/compose.py`, the declarations of the affected agents, and the inventories in the writable folders.
- The final answer is the JSON `{"action": "delete" | "merge" | "prune" | "demote" | "keep", "targets": [...],
  "survivor": "<name>" | null, "reason": "<reason>"}`.

When the action is not `keep` and the tree changed:

1. **Reference check** (Python, against the fork base). A removed name is an agent or tool name of the fork base's
   catalog whose `name` declaration no longer appears in any `.py` file of the working tree, or a skill whose
   `SKILL.md` is gone. The check fails when a removed name is protected, a removed agent is named by `schedule` or
   `reservation`, or a removed name still appears as a quoted string on a line of a `.py` file of the working tree that
   contains `call_agent`, `call_tool`, or `agent_name`, or in a skill's `tools.json`.
2. **Fork**: `repo.fork(<fork base>, <working tree>, "optimizer:consolidation <kind> <action> <targets joined by commas>")`.
3. A failed reference check or fork goes back into the same conversation for another fix, up to `MAX_FORKS` attempts in
   total. When the last attempt fails, the outcome is `rejected` (reference check) or `fork_failed` (fork).
4. **Merge**: `repo.merge(<candidate>)` merges into the shared `main`, rebasing onto a newer `main` when a person or
   another agent pushed meanwhile; `merged`. A conflict, a refused push or fast-forward, a load failure of the rebased
   version, or an unreachable GitHub is `failed` with `merge: <exception type>: <message>`; the group comes back in a
   later run.

The next group starts again from the adopted main, so it builds on the previous merge. Writes made under `keep` are
discarded. An action other than `keep` without changes ends as `kept` with reason `<action> without changes: <reason>`.

### Findings

Each finding is the result of one group:

```json
{"summary": "unused_edges general: merged — general never called execute_code; codegen covers it",
 "evidence": [],
 "detail": {"kind": "unused_edges", "targets": ["general", "execute_code"], "action": "prune", "survivor": null,
            "outcome": "merged", "reason": "...", "base": "<fork base version id>",
            "candidate": "<merged version id>", "paths": ["agent/general.py", "agent/inventory.md"]}}
```

| Outcome | Meaning | Main |
|---|---|---|
| `merged` | Changed and merged into the shared `main` (pushed to GitHub, or fast-forwarded in local mode) | Advances to the merged version |
| `rejected` | The reference check failed on every attempt | Unchanged |
| `forked` | Changed and forked, but not merged because `merge` is false (`candidate` is the fork) | Unchanged |
| `fork_failed` | The tree could not be loaded on every attempt | Unchanged |
| `kept` | The LLM chose `keep`, or chose a change but wrote nothing | Unchanged |
| `skipped` | `over limit`, or `gone` (a target no longer exists in main) | Unchanged |
| `failed` | LLM exception, response limit reached, malformed final answer, a merge failure, or a failed survey (kind `survey`) | Unchanged |

- `summary` is `<kind> <first target>: <outcome> — <reason>`. `detail` always has `kind`, `targets`, `action`,
  `survivor`, `outcome`, `reason`, `base`, `candidate`, and `paths` (null or empty where they do not apply); when the
  merge rebased the candidate, `rebased_from` holds the original candidate.
- Findings are ordered: a failed survey, then handled groups in handling order, then `over limit`.
- A failure in one group is recorded as its finding and the next group runs. Cancellation (`CancelledError`) propagates.
  With no groups and a survey that returns none, the finding list is empty.

## The `additions` check

`AdditionsCheck` in `optimizer/additions.py` reads recent work from the `logs` table directly through `core/orm`
(`ExecutionLog` and `sessions_for(config.DATABASE_URL)`), and the main version, its history, fork, and merge from the
version store singleton (`current_repo()`). It looks only at what the general agent did and at shell commands, because
that is where work without a dedicated component shows up. Every change that passes the checks below and loads in its
fork is merged; nothing waits for a person. The LLM is the Host's `max` client.

| Constant | Value | Meaning |
|---|---|---|
| `WINDOW` | 7 days | How far back from the observation time work is read |
| `WATCHED_AGENT` | `general` | The agent whose runs are read |
| `WATCHED_TOOLS` | `shell`, `bash` | The command tools read under any agent (`shell` in the component repository, `bash` in the package defaults) |
| `MIN_RUNS` | 3 | Distinct runs a candidate needs as evidence |
| `MAX_TURNS` | 40 | LLM responses per candidate conversation |
| `MAX_ATTEMPTS` | 2 | Attempts per candidate; check failures and fork failures count together |
| `LLM_NAME` | `max` | Host LLM used for the survey and the conversations |
| `REQUEST_CHARS` / `COMMAND_CHARS` / `ARGS_CHARS` | 500 / 800 / 300 | Clipping of agent requests, commands, and other tool arguments in the digest |
| `DIGEST_CHARS` | 100,000 | Size of the digest's `runs` JSON; the oldest runs are dropped beyond it |
| `WRITABLE` | `agent`, `tools`, `skills` | Version folders the LLM may write in |

### Observation

1. **Read**: read once, from `logs`, the rows that started at or after the observation time minus `WINDOW`: agent rows
   of `general`; agent and tool rows whose parent is a `general` row (not its `llm` or `harness` rows); and tool rows
   named `shell` or `bash` under any agent. Each row carries `run_id`, `execution_id`, `parent_execution_id`, `event_type`, `component`, `started_at`,
   `status`, `request_json`, and the component of its parent agent row.
2. **Exclude**: rows of the current run, rows whose parent agent is `optimizer`, and rows of consumed runs. A run is
   consumed when the first line of the message of a version in `repo.log()` created at or after the window start is
   `optimizer:additions <kind> <name> runs=<run_id,...>` and lists it. This covers a merged change, a fork kept after a
   failed merge, and a rebased version (whose first line is the same). Walking only main's ancestry would miss changes
   that a person's merge commit carries in its second parent, so every listed version counts.
3. **Digest**: one entry per run, in the order of its first row:

   ```json
   {"run_id": "<run_id>", "started_at": "<first row>",
    "general": [{"request": "post the summary to slack", "status": "ok",
                 "steps": [{"tool": "shell", "command": "curl -s https://slack.com/api/chat.postMessage", "status": "ok"},
                           {"tool": "slack_api", "args": "{\"method\": \"chat.postMessage\"}", "status": "error"},
                           {"agent": "codegen", "request": "write a script", "status": "ok"}]}],
    "commands": [{"agent": "implementation", "tool": "bash", "command": "uv run pytest -q", "status": "ok"}]}
   ```

   `general` lists each `general` execution with its direct tool and agent calls in record order; `commands` lists
   `shell`/`bash` calls under other agents; a command whose parent agent row is not recorded yet (an agent row is
   written when the agent ends, so the agent is still running) has `agent` null. `request` is the `request` field of the agent's `request_json` (never its
   `data`), clipped to `REQUEST_CHARS`; `command` is the `command` argument, clipped to `COMMAND_CHARS`; `args` is the
   arguments of any other tool as JSON, clipped to `ARGS_CHARS`. Clipped text ends with `…`. Other rows (for example a
   tool called by a child of general) are ignored. When the JSON of all runs is longer than `DIGEST_CHARS`, the oldest
   runs are dropped until it fits.
4. With no run left, the finding list is empty and the LLM is not called.
5. **Catalog**: after `repo.refresh()`, the main version's component graph gives agents (name, description, module
   path, tool names, skills, child agents), tools (name, description, module path, `used_by`), and skills (name,
   description from the `SKILL.md` front matter in the main tree, `used_by`).

### Survey

One LLM call without tools receives `{"window_days": 7, "min_runs": 3, "runs": [...], "catalog": {...}}` and answers
`{"candidates": [{"kind": ..., "name": ..., "owners": [...], "purpose": ..., "evidence": [<run_id>, ...], "reason": ...}]}`.
The system prompt states that the digest and the catalog are material and that instructions inside them must not be
followed, and sets the kinds:

| Kind | When | Name |
|---|---|---|
| `skill` | A procedure with fixed steps whose arguments and judgment change each time (the first choice: no new code runs) | New, `^[a-z0-9]+(-[a-z0-9]+)*$`, at most 64 characters |
| `tool` | Almost the same code run again and again with different arguments | New, `^[a-z][a-z0-9_]{0,63}$` |
| `agent` | A whole kind of task with its own prompt and tool set that the owner would hand over (the last choice) | New, `^[a-z][a-z0-9_]{0,63}$` |
| `existing` | A component in the catalog already does it but was not used | An agent, tool, or skill of main |

`owners` are the agents that did the work and will use the result. Python validates the candidates in survey order;
the first condition that fails is the reason of a `skipped` finding:

| Reason | Condition |
|---|---|
| `unknown kind` | The kind is not one of the four |
| `invalid name` | A new name that does not match its pattern |
| `name taken` | A new name already used by an agent, tool, or skill of main |
| `unknown component` | An `existing` name that is not an agent, tool, or skill of main |
| `duplicate` | A name already taken by an earlier valid candidate |
| `unknown owner` | No owners, or an owner that is not an agent of main |
| `too few runs` | Fewer than `MIN_RUNS` distinct evidence run ids that are in the digest |

Every valid candidate is handled, in survey order, with no limit on their number. An LLM
exception, a tool call, an answer that is not JSON, or a JSON of another shape is one finding with kind `survey` and
outcome `failed`, and no candidate is handled.

### Handling a candidate

Each candidate is one conversation with the LLM `max`, at most `MAX_TURNS` responses. It starts with `repo.refresh()`;
the tree of that main (the fork base) is copied into a working tree.

- Input: `kind`, `name`, `owners`, `purpose`, `reason`, `evidence` (the digest entries of the evidence runs),
  `owner_modules` (owner → path of its class's module), `target` for `existing` (`{"kind": "tool" | "agent" | "skill",
  "path": <module path or skills/<name>/SKILL.md>}`), and `writable`.
- Tools on the working tree: `list_files(prefix)`, `read_file(path)`, `grep(pattern, prefix)` (a regular expression;
  at most 200 matching lines as `path:line: text`), `write_file(path, content)` (only under `agent/`, `tools/`, and
  `skills/`, other than `agent/SYSTEM.md`), and `delete_file(path)` (only files that are not in the fork base). Any other
  path returns a failed result and leaves the tree unchanged.
- The final answer is `{"verdict": "changed" | "keep", "reason": "<reason>"}`. A new component is wired into every
  owner in the same change (`tools`, `skills`, or `agents` of the owner, `AGENTS` in `agent/compose.py` for an agent)
  and listed in the inventory of its folder; `existing` rewrites the target's description, the owners' prompts, and the
  owners' declarations.

When the verdict is `changed` and the tree differs from the fork base:

1. **Checks** (Python, against the fork base):
   - Scope: added files are under `skills/<name>/` (skill), `tools/` (tool), or `agent/` (agent); `existing` adds none.
     Modified files are the owners' modules, `agent/compose.py`, `agent/inventory.md`, `tools/inventory.md`,
     `skills/inventory.md`, and for `existing` the target's file.
   - Code: every modified `.py` file parses and keeps the syntax tree of the fork base apart from string literals,
     imports, and assignments to `tools`, `skills`, `agents`, and `AGENTS`. Logic never changes in an existing file.
   - Wiring: each owner's module has the name as a string literal (skill, agent, and `existing` skill or agent) or
     imports the tool's module (a module added in this change for a new tool, the target's module for an `existing`
     tool). For `existing`, an owner that already uses the target in the fork base (for example a base tool registered
     by `agent/compose.py`) also passes. For a new agent, `agent/compose.py` imports a module added in this change.
   - Secrets: no changed file contains the value (8 characters or more) of an environment variable of the runtime whose
     name contains `TOKEN`, `SECRET`, `PASSWORD`, `PASSWD`, `CREDENTIAL`, `KEY`, `DSN`, or `URL` (case-insensitive).
     The failure names the variable, never the value.
2. **Fork**: `repo.fork(<fork base>, <working tree>, "optimizer:additions <kind> <name> runs=<run_id,...>")`.
3. A failed check or fork goes back into the same conversation for another change, up to `MAX_ATTEMPTS` attempts in
   total. When the last attempt fails, the outcome is `rejected` (a check) or `fork_failed` (the fork).
4. **Merge**: `repo.merge(<candidate>)`, as in the `errors` check: `merged`, with `rebased_from` when the merge put the
   candidate on top of a newer main; a conflict, a refused push, or another exception is `failed` with
   `merge: <exception type>: <message>`.

Writes made under `keep` are discarded (`kept`). A `changed` verdict without changes is `kept` with reason
`changed without changes: <reason>`. The next candidate starts again from the database main, so it builds on the
previous merge.

### Findings

```json
{"summary": "skill devdb-query: merged — the dev DB checks repeat the same venv and pymysql steps",
 "evidence": ["<run_id>", "<run_id>", "<run_id>"],
 "detail": {"kind": "skill", "name": "devdb-query", "owners": ["general"], "outcome": "merged", "reason": "...",
            "base": "<fork base version id>", "candidate": "<merged version id>",
            "paths": ["agent/general.py", "skills/devdb-query/SKILL.md", "skills/inventory.md"]}}
```

| Outcome | Meaning | Main |
|---|---|---|
| `merged` | Changed and merged into the shared `main` | Advances to the merged version |
| `rejected` | The checks failed on the last attempt | Unchanged |
| `forked` | Changed and forked, but not merged because `merge` is false (`candidate` is the fork); its runs are consumed | Unchanged |
| `fork_failed` | The tree could not be loaded on the last attempt | Unchanged |
| `kept` | The LLM chose `keep`, or chose `changed` but wrote nothing | Unchanged |
| `skipped` | An invalid candidate (see `reason`) | Unchanged |
| `failed` | LLM exception, response limit reached, malformed final answer, merge exception, or a failed survey (kind `survey`) | Unchanged |

- `summary` is `<kind> <name>: <outcome> — <reason>` (`survey: failed — <reason>` for the survey).
- `evidence` lists the candidate's distinct evidence run ids that are in the digest, in the candidate's order.
- `detail` always has `kind`, `name`, `owners`, `outcome`, `reason`, `base`, `candidate`, and `paths` (null or empty
  where they do not apply), plus `rebased_from` after a rebased merge. `paths` lists the changed paths in ascending
  order.
- Findings are ordered: handled candidates in survey order, then invalid candidates.
- A failure for one candidate is its finding and the next candidate runs. Cancellation (`CancelledError`) propagates.

## The `tuning` check

`TuningCheck` in `optimizer/tuning/` reads the last 14 days of `logs` through `core/orm` and changes versions through the
version store singleton, like `errors`. One run has three parts, in this order:

1. **Watch** the tunings it merged in the last 7 days, and revert the ones that made runs worse.
2. **Effort**: find agents whose LLM steps are mostly reasoning with small answers, replay their recorded steps at a
   lower `llm`, and when the replay keeps the decisions and saves output or time, change the agent's `llm` declaration
   and merge it.
3. **Report** the other kinds of waste, without changing anything.

| Constant | Value | Meaning |
|---|---|---|
| `WINDOW` | 14 days | How far back `logs` is read |
| `MAX_EFFORT` | 3 | Effort candidates replayed per run (by LLM time, descending); the rest are `skipped` (`over limit`) |
| `MAX_REPORTS` | 20 | Reported findings per run (by estimated tokens, descending) |
| `JUDGE` | `max` | Host LLM that compares free-text answers |
| `SAMPLES` / `CONCURRENCY` / `MIN_VALID` | 12 / 4 / 8 | Replayed steps per agent, replays in flight, valid replays needed per tier |
| `AGREEMENT_MARGIN` / `MIN_SELF_AGREEMENT` / `SAVING_RATIO` | 0.1 / 0.8 / 0.7 | Gate thresholds (see below) |
| `WATCH` / `WATCH_MIN` / `COOLDOWN` | 7 days / 10 / 30 days | Watch period, executions needed on the tuned versions, period a reverted tuning is not proposed again |
| `OK_DROP` / `EFFORT_GAIN` | 0.05 / 0.95 | Revert thresholds (see below) |

### Reading `logs`

- **Agent rows** (`event_type='agent'`): execution, run, parent execution, component, start, duration, status, and the
  version label from `code_version`; plus a hash of `request`, a hash of the result's `response`, the hash of each
  element of the array values of an object `request`, and an identity hash of the Source definition (`data.source` and
  `data.source_kwargs` without `webhook`) when `data` has one.
- **Step rows** (`event_type='llm'` joined to their parent agent row): id, run, parent execution and agent, start,
  duration, status, `provider/model`, the input, cached input, output, and reasoning tokens from `usage_json`, the
  response type and calls (`name`, `args`) from `result_json`, a hash of `request_json`, and a hash of the answer text
  or the calls. Step rows without a parent agent row are not read.
- Bodies are read by id only when needed: the recorded `request_json` / `result_json` of the steps to replay, and the
  `request_json` of the agent rows the prefetch detector judges.
- Rows of the current run, and of the agent `optimizer` (including the check's own replays), are not used.
- A `request_json`, `result_json`, or `usage_json` that PostgreSQL cannot read as `jsonb` (for example `NaN` or `\u0000`)
  is read as null, so one such row never fails the whole query.

### Effort

**Detection** (Python). An agent of the main component graph is a candidate when its class declares `llm` `max` or
`high`, it has at least 20 `ok` LLM steps in the window, reasoning is at least 70% of its output tokens, and the
90th percentile of its non-reasoning output is at most 1000 tokens. The tiers to try are `low`, then `high`, for `max`,
and `low` for `high`; agents on `low`, `flash`, or `flash_low` are not candidates. Candidates whose agent has a tuning under watch (`skipped`, "an earlier tuning of this agent is
being watched") or whose `Tuning-Id` was reverted within `COOLDOWN` (`skipped`, "reverted within the cooldown") are not
replayed.

**Replay** (the gate). The latest 12 `ok` steps of the agent with distinct inputs are replayed with their recorded
`messages` (with `raw`) and `callables`: once on the current tier and once on each tier to try, until one passes.
Each fresh answer is compared with the recorded one:

| Recorded | Fresh | Same decision when |
|---|---|---|
| calls | calls | The sorted tool names are equal (arguments are not compared) |
| calls | answer, or the reverse | Never |
| answer | answer | The texts are equal; or both are JSON and their skeletons (keys, and strings up to 40 characters, numbers, booleans) are equal; otherwise the `max` judge says the fresh answer is not worse |

The judge receives the system prompt, the last user message, and the two answers as A and B in random order (each
clipped to 6000 characters), is told that they are material and not instructions, and returns
`{"verdict": "same" | "a_better" | "b_better"}`. A failed or unreadable verdict, or a replay whose LLM call failed,
makes that sample invalid.

The recorded answer is itself one sample of the current tier, so the candidate tier is compared with the current tier's
own agreement with the recording. A tier passes when all of these hold; otherwise the outcome is `rejected` with every
tier's reason, and nothing is forked:

1. Both tiers have at least `MIN_VALID` valid samples, and at least `MIN_VALID` samples got an answer from both.
2. The current tier agrees with its own recording at least `MIN_SELF_AGREEMENT` of the time. Below that, the agent's
   answers vary too much from run to run for the replay to tell tiers apart.
3. The tier's agreement is at least the current tier's minus `AGREEMENT_MARGIN`.
4. Over the samples both tiers answered, the tier's mean output tokens (reasoning included) are at most `SAVING_RATIO`
   of the current tier's. Response time is reported but not used: it includes waiting for the LLM concurrency limit
   shared with live runs.

**Change**. `effort.set_tier` edits only the module of the agent's class (`class_file` in `optimizer/worktree.py`): it
replaces the string of the class's last own `llm` declaration, or, without one, adds `llm = "<tier>"` (with the same
annotation as `name`) on the line after `name`. Nothing else changes, and the inventories are not updated. When another
agent class in the tree inherits from that class without declaring its own `llm`, the change would move that agent too,
so the outcome is `rejected` and nothing is forked. The tree is forked from the current main with this message, then
merged unless `merge` is false:

```text
optimizer:tuning effort <agent> <old>-><new>

Tuning-Id: effort/<agent>
Tuning-Kind: effort
Tuning-Agent: <agent>
Tuning-From: <old llm>
Tuning-To: <new llm>
Tuning-Path: <module path of the agent's class>
Tuning-Gate: {"tier": ..., "passed": true, "reason": ..., "baseline": {...}, "candidate": {...}}
Tuning-Baseline: {"executions": ..., "ok": ..., "steps": ..., "tokens": ..., "step_ms": ...}
```

`Tuning-Baseline` holds the agent's metrics over the window: executions (without `cancelled`), the share of `ok`, mean
LLM steps and tokens per execution, and the mean time of one LLM step.

### Watch and revert

The check keeps no table. It reads the tunings from `repo.log()` (every version reachable from main and from kept
forks): commits whose message starts with `optimizer:tuning ` and that carry the `Tuning-*` trailers above. A revert
starts with `optimizer:tuning revert ` and has `Tuning-Revert: <reverted version id>`.

Whether a tuning is **in effect** in a version is decided by content, not by ancestry: the version's file at
`Tuning-Path` declares `Tuning-To` on the agent's class. A merge commit made by `git pull` (which puts the tuning on its
second parent) or a person's own edit therefore does not change the answer.

For each tuning made within `WATCH` that is in effect on main, the agent's executions in the window are split by the
version they ran (`c.<…>` of `code_version`, resolved to a unique version of `repo.log()`): **after** are executions on
versions where the tuning is in effect, started at or after the tuning; **before** are executions on versions where it
is not, started at most `WATCH` before it. Rows whose version cannot be resolved are not used. Then:

| Condition | Outcome |
|---|---|
| Fewer than `WATCH_MIN` executions after | `watching` |
| The share of `ok` after is more than `OK_DROP` below before | revert |
| `effort`, and the mean LLM step time after is above `EFFORT_GAIN` times before | revert |
| Otherwise | `kept` |

"Before" is the measured group when it has at least `WATCH_MIN` executions, otherwise the tuning's `Tuning-Baseline`.
A revert sets the agent's `llm` back to `Tuning-From` in main's file at `Tuning-Path` with `set_tier`, keeping every
other change to that file. It is forked with `Tuning-Id`, `Tuning-Kind`, `Tuning-Agent`, `Tuning-From`, `Tuning-To`,
`Tuning-Path`, `Tuning-Revert`, and `Tuning-Reason`, and merged unless `merge` is false (`reverted` or `forked`).

Before the effort part, two sets are built from the same history:

- **Watched agents**: agents of tunings made within `WATCH` that are in effect on main. They get no new effort change
  (`skipped`, "an earlier tuning of this agent is being watched").
- **Cooling `Tuning-Id`s**: ids reverted by the check within `COOLDOWN`, and ids of tunings made within `COOLDOWN` that are
  on main's first-parent line but no longer in effect (a person set the tier back). They are not proposed again
  (`skipped`, "reverted within the cooldown"). A tuning that was only forked (`merge` false) is neither watched nor
  cooling, so a later run judges it again.

### Reported waste

These detectors only report (outcome `reported`). Findings of agents that are not in the main component graph are
dropped. `tokens` estimates the tokens the change could save (for duplicate triggers, an upper bound) and `seconds` the
LLM time.

| Kind | Found when | Typical change |
|---|---|---|
| `repeated_input` | The same `request_json` reached the agent's LLM at least 5 times | A constant or a cache keyed by the input |
| `repeated_request` | The same `request` reached the agent at least 3 times (groups covering half or more of its executions are its fixed instruction and are ignored), or elements of its request arrays repeat at least 3 times | A cache of the earlier decision |
| `prefetch` | In at least 80% of the latest 30 executions (at least 10) whose first step made calls, every string and number of 3 or more characters in those calls' arguments appears in the agent row's `request_json` (its `request` and `data`, which the LLM receives first) | Fetch them in Python before the loop |
| `call_chain` | At least 3 consecutive steps each made exactly one call to the same tool | Ask for parallel calls, or a tool that does the batch |
| `large_output` | One step's calls added at least 20,000 input tokens (next input − this input − this output) and at least 2 more steps followed | Clip the tool result and spill the rest to a file |
| `cache` | Over at least 10 executions, the first step's mean input is at least 5,000 tokens and less than 50% of it was cached | Put the stable part of the prompt first |
| `light_child` | A child agent that made calls finished in at most 2 steps on average, over at least 10 executions | A tool or a skill of the parent |
| `skill_inline` | `read_skill` of one skill in at least 80% of the agent's executions (at least 10) | Put the skill in the system prompt |
| `duplicate_trigger` | At least 5 executions started, for the same Source identity, while an execution of another run was still going or within 10 minutes of its start | Debounce or serialize per target |

### Findings

Every finding has `detail` with `kind`, `agent`, `outcome`, and `reason`; `summary` is `<kind> <agent>: <outcome> —
<reason>`, and `evidence` lists up to 10 run ids, newest first.

| Kind | Outcomes | Further `detail` |
|---|---|---|
| `watch` | `watching`, `kept`, `reverted`, `forked`, `fork_failed`, `failed` | `tuning`, `version`, `before`, `after`; `paths`, `base`, and `candidate` when forked |
| `effort` | `merged`, `forked`, `rejected`, `skipped`, `fork_failed`, `failed` | `detected`, `gates` (each with `baseline` and `candidate`: `samples`, `valid`, `agreement`, `measured`, `output_tokens`, `seconds`); `tier`, `paths`, `base`, `candidate` (and `rebased_from`) when forked |
| the reported kinds | `reported` | the detector's numbers, `tokens`, `seconds` |

Findings are ordered: watch, effort (replayed, then over limit), reported. A failure in one part is recorded as its
finding and the next runs: a failed watch of one tuning is `failed` for that tuning, a failed sample read or replay is
`failed` for that agent, and a failed request read for `prefetch` is one finding of kind `prefetch` with outcome
`failed` (the other reports still run). Cancellation propagates.

## The `behavior` check

`BehaviorCheck` in `optimizer/behavior/` reads the operator's own posts and actions on Slack, GitHub, and Discord over
the last 14 days and changes versions through the version store singleton (`current_repo()`), like `additions`. The
readers are Host code, so the check imports no component. It
finds the routine work the operator repeats, turns the parts that fit `additions`' guard rails into components or
better descriptions and merges them without waiting for a person, and reports every routine it found with whether AI
can take it over. The LLM is the Host's `max` client. The check sends nothing: the report is the run result, which the
entry agent posts ([Report delivery](#report-delivery)).

| Constant | Value | Meaning |
|---|---|---|
| `WINDOW` | 14 days | How far back from the observation time activity is read |
| `EXCLUDE_PERSONAL_REPOS` | true | GitHub repositories owned by the operator's own login are left out |
| `MIN_EPISODES` | 2 | Distinct episodes a candidate needs as evidence |
| `MAX_TURNS` | 40 | LLM responses per candidate conversation |
| `MAX_ATTEMPTS` | 2 | Attempts per candidate; check failures and fork failures count together |
| `LLM_NAME` | `max` | Host LLM used for the survey and the conversations |
| `DIGEST_CHARS` | 100,000 | Size of the digest; texts are shortened, then the end is cut |
| `WRITABLE` | `agent`, `tools`, `skills` | Version folders the LLM may write in |

### Reading activity

`collect_activity(days=14, exclude_personal_repos=True, now=<observation time>)` (`optimizer/behavior/collect.py`)
reads the three platforms concurrently, each as the owner of its credentials, and groups what it finds into episodes.
An episode is an issue or PR, a Slack thread, a channel's day, or a repository's day of commits in which the operator did
something within the window. It holds the operator's events (actor `self`) and the other people's events that
triggered or followed them.

| Platform | Episode | The operator's events | Other people's events kept as triggers or results | Credentials and visible scope |
|---|---|---|---|---|
| Slack | A thread, or a channel's day (JST) | Posts found by `search.messages` with `from:<@ID>` (one search returns up to 100 items, so a longer range is split in half by date; a day with more than 100 keeps the newest 100 and adds a note), and emoji reactions from `reactions.list` | For a reply, the thread parent and the preceding message by someone else; for a channel post, the preceding message by someone else within 2 hours; for a reaction, the message reacted to | `SLACK_TOKEN` (user token; `auth.test` gives the ID). Public channels, and private channels and DMs its user is a member of. Without `reactions:read`, reactions are skipped with a note. A reaction takes the time of the message reacted to (`approx_time`); only reactions to messages within the window are read, newest first, up to 1,000 |
| GitHub | An issue or PR, or one repository's day of commits on the default branch | Open, comment, review (including approvals) and line comments, merge, close, reopen, label, assign, review request, draft toggle, title change, milestone, branch deletion, force push, review dismissal, and commits | Open (kept as a starting point even before the window), comment, review, review request, merge, close, reopen, ready for review | `GITHUB_TOKEN` or `GH_TOKEN` (`GET /user` gives the login). GraphQL `timelineItems` of the issues and PRs found by `involves:` and `reviewed-by:` updated within the window; the operator's reviews and line comments come from the PR's `reviews(author:)`; commits on default branches by `author:` and committer date, without the ones already read from a PR. A close by a closing keyword (one with a `closer`) is a `GitHub (自動)` event, and a branch deletion within 60 seconds of the merge is dropped. Search returns up to 1,000 items per expression |
| Discord | A channel's day (JST) | Posts found by Search Guild Messages in each guild the bot is in (`author_id` and the window's snowflakes); while the index is being built (202, code 110000) it waits for `retry_after` and retries, up to 10 times | For a reply, the replied message; otherwise the preceding message by someone else (bots included) within 2 hours | `DISCORD_BOT_TOKEN` and `DISCORD_ALLOWED_USER_ID` (the operator). Channels in guilds the bot can read; no DMs |

- An episode has `id`, `platform`, `where`, `subject`, `url` (optional), and `events`. `id` is `e` followed by the first
  10 hexadecimal characters of the SHA-256 of its key (`slack:<channel>:<JST day or thread ts>`,
  `github:<repository>#<number>`, `github:<repository>:commits:<JST day>`, `discord:<channel>:<JST day>`), so the same
  thread, issue, PR, or channel day has the same id on every run.
- An event has `at` (UTC), `actor`, `kind`, `text` (whitespace collapsed; up to 600 characters for the operator and 200
  for others), `url` (optional), `ai_marked` (an operator event with 🤖 on its first line, a Claude co-author line, or
  `Generated with [Claude Code]`), and `approx_time`. At the same time, other people's events come first. Consecutive
  commits by the operator within an episode are one event with `count`, `until`, and the first five subjects.
- Episodes are ordered by the operator's first action within the window. `coverage` holds per platform `status` (`ok`,
  `failed`), the resolved ID, the number of episodes, the number of the operator's actions and counts per kind, `notes`,
  `scope`, and for a failure `error` (never a credential). A failure on one platform does not stop the others.
- A missing credential fails that platform (`SLACK_TOKEN is required`, `GITHUB_TOKEN is required`,
  `DISCORD_BOT_TOKEN is required`, `DISCORD_ALLOWED_USER_ID must be a Discord user ID`). Rate limits (429, and GitHub's
  rate-limit 403) are retried up to 4 times, following the given wait (1 to 60 seconds). Requests time out after 30
  seconds.

### Observation

1. **Read** the activity once. When every platform failed, the check fails with
   `RuntimeError: every platform failed: <platform>: <error>; ...` and nothing else runs.
2. **Exclude** consumed episodes. An episode is consumed when the first line of the message of a version in `repo.log()`
   created at or after the window start is `optimizer:behavior <kind> <name> episodes=<id,...>` and lists it (a merged
   change, a fork kept after a failed merge or with `merge` false, and a rebased version).
3. With no episode left, the only finding is `activity` and the LLM is not called.
4. **Digest** (text) in three parts: the observed scope (the window in JST, and per platform the counts, kinds, visible
   scope, notes, or the failure); the operator's actions per platform by JST hour and by weekday; and each episode as
   `## [<id>] [<platform>] <where> | <subject> <url>` followed by one line per event. A line starts with the JST time
   (`MM-DD(ddd) HH:MM`) when it is the first of its day, otherwise with the time since the previous line (`+N分`,
   `+H時間MM分`, `+N日` from 48 hours), then the actor (`本人` for the operator), the kind (with the count and end time of
   merged commits), and the text. It ends with `(時刻は反応先のメッセージ)` for an approximate time, 🤖 for `ai_marked`,
   and the URL of the operator's `message`, `reply`, `comment`, `review`, and `review_comment` events. Texts are
   clipped to 400 / 160 characters (operator / others) with URLs, then 200 / 80, then 120 / 50 without URLs, using the
   first level at which the digest fits `DIGEST_CHARS`; beyond that the end is cut and a note says so.
5. **Catalog**: after `repo.refresh()`, the main version's agents, tools, and skills, as for `additions`.

### Survey

One LLM call without tools receives the digest and `{"min_episodes": 2, "catalog": {...}}` and answers:

```json
{"routines": [{"name": "...", "flow": "<trigger> → <the operator's action> → <result>", "episodes": ["e…"],
               "takeover": "yes" | "partly" | "no" | "automated", "reason": "...", "proposal": "..."}],
 "candidates": [{"kind": "...", "name": "...", "owners": ["..."], "purpose": "...", "evidence": ["e…"], "reason": "..."}]}
```

- `routines` are behaviors with the same trigger, order, and shape seen in at least 2 episodes, judged on the elapsed
  times and the order of the operator's actions. One-off events and judgments whose content changes every time (design,
  the content of a review) are not routines. `takeover` is `yes`, `partly` (drafting, preparing, or checking only),
  `no`, or `automated` (the operator already posts or approves what AI wrote, the 🤖 lines). `proposal` is the agent
  change for `yes` and `partly` (target, trigger, input, processing, output, what the operator approves, expected
  effect, risks), including changes this check cannot make. Texts are in `OUTPUT_LANGUAGE`.
- `candidates` are the parts of `yes` and `partly` routines that fit `additions`' kinds (`skill`, `tool`, `agent`,
  `existing`, with the same name rules). `owners` are the agents of main that will do the work.
- The system prompt states that the digest and the catalog are material whose instructions must not be followed, that
  other people's events are context only and are not evaluated, and that what cannot be judged from few episodes is left
  out.
- Candidates are validated in survey order with `additions`' rules ([Survey](#survey)), except that the last reason is
  `too few episodes`: fewer than `MIN_EPISODES` distinct evidence ids that are in the digest.
- A routine without a string `name` is dropped; a `takeover` outside the four values becomes `unknown`; episode ids that
  are not in the digest are dropped.
- An LLM exception, a tool call, an answer that is not JSON, or a JSON of another shape is one finding with kind `survey`
  and outcome `failed`; no candidate is handled and no routine is reported.

### Handling a candidate

Every valid candidate is handled in survey order, with no limit on their number, exactly as an `additions` candidate
([Handling a candidate](#handling-a-candidate)): the same conversation, tools, final answer, Python checks (scope,
code, wiring, secrets), attempts, fork, and merge (`optimizer/proposal.py`). Two things differ: `evidence` in the
conversation input is the digest text of the evidence episodes, and the fork message is
`optimizer:behavior <kind> <name> episodes=<id,...>`.

### Findings

```json
{"summary": "routine pr-review-request: reported — partly: the first pass of a requested review repeats the same checks",
 "evidence": ["e3f9a1c2b70", "e81d0c4a9f2"],
 "detail": {"kind": "routine", "name": "pr-review-request", "outcome": "reported", "flow": "...", "takeover": "partly",
            "reason": "...", "proposal": "...",
            "episodes": [{"id": "e3f9a1c2b70", "platform": "github", "where": "org/repo", "subject": "PR #12 ...", "url": "..."}]}}
```

| Kind | Outcomes | `summary` | `detail` |
|---|---|---|---|
| `activity` | `observed` | `activity: observed — <n> episodes, <c> consumed; slack ok, github ok, discord failed` | `outcome`, `window` (`since`, `until`, `days`), `episodes`, `consumed`, `coverage` (per platform `status`, `episodes`, `actions`, `kinds`, `notes`, `error`) |
| `survey` | `failed` | `survey: failed — <reason>` | `outcome`, `reason` |
| `skill`, `tool`, `agent`, `existing` | As `additions`: `merged`, `rejected`, `forked`, `fork_failed`, `kept`, `skipped`, `failed` | `<kind> <name>: <outcome> — <reason>` | As `additions` (`name`, `owners`, `outcome`, `reason`, `base`, `candidate`, `paths`, `rebased_from`), plus `episodes` |
| `routine` | `reported` | `routine <name>: reported — <takeover>: <reason>` | `name`, `outcome`, `flow`, `takeover`, `reason`, `proposal`, `episodes` |

- `evidence` lists episode ids. `episodes` in `detail` gives `id`, `platform`, `where`, `subject`, and `url` for each.
- Findings are ordered: `activity`, a failed survey, handled candidates in survey order, invalid candidates, then
  routines in survey order.
- A failure for one candidate is its finding and the next candidate runs. Cancellation (`CancelledError`) propagates.

## Version store singleton

`core/versioning` keeps one version store per process. The runtime and the optimizer use it.

| Function | Behavior |
|---|---|
| `open_repo(build, *, store=None) -> Repo` | Creates this process's version store and makes it the singleton. Without `store`, it uses `GitVersionStore(config.COMPONENT_REPO_DIR, config.COMPONENT_REPO_URL, ssh_key=config.COMPONENT_REPO_SSH_KEY, hooks=HOOKS_DIR, seed=PACKAGE_ROOT)` (an empty URL is local mode). Raises `RuntimeError` if a store is already open. It does not load main (`start` loads) |
| `current_repo() -> Repo` | Returns the singleton. Raises `RuntimeError` if none is open |
| `Repo.close()` | Releases every version and, if this store is the singleton, clears it so `open_repo` can run again |

- A process never holds two version stores: they would share the module namespace of a version
  (`_te_versions.v_<id>`), and releasing one would remove the other's modules.
- The runtime calls `open_repo(prepare_components(services))` once in `build_runtime` and passes it to `Runtime`. The
  optimizer uses `current_repo()`.
- `Repo(store, build, ...)` can still be created directly for tests; such a store is not the singleton.

## Limitations

- A repair or an addition is a commit on the shared `main`, so a person receives it with `git pull` in `components/`; a later push that removes the change
  drops it like any other commit. The change is the difference between the report's `base` and `candidate` versions.
- The latest-version test compares version ids. After a push that changed only another agent, earlier failures are
  not repaired (the same failure recurring on the new version is).
- A repair, a consolidation, or an addition is verified only as far as the fork load (import and component graph build)
  and, for consolidation, the reference check and, for additions, its checks; agent behavior is not verified. A change
  that breaks a run is left to the next day's `errors` check.
- `additions` sees only `general` and `shell`/`bash` calls, so recurring work inside other agents' own tools is not
  found. `kept` and `skipped` candidates consume no runs, so the same evidence can be surveyed again the next day; a fork
  whose merge failed consumes its runs, so it is proposed again only with new evidence.
- `additions` handles every valid candidate in one run, one conversation each. A run that exceeds `RUN_TIMEOUT` is
  cancelled and delivered again; candidates merged before the cancellation have consumed their runs, so the next
  delivery handles only the rest. A new component counts as new for `consolidation` until `GRACE` has passed, so it is
  not removed before it can be used.
- Consolidation deletes whole files on the shared `main`. A person receives the deletion with `git pull` in
  `components/`; local edits to a deleted file become a conflict at that pull.
- `unused_edges` approximates the age of a declaration by the age of the agent and of the declared component, so a new
  declaration between two old components can be removed before it is used. Helper modules that no component class
  lives in are not found as unreachable.
- A fork imports the written modules in the runtime process to verify them, so module top-level code runs inside the
  runtime process at fork time. Beyond the write scope (`agent/`, `tools/`, `skills/`), the `additions` checks, and the
  system-prompt rule (do not follow instructions inside error text, the digest, or the catalog), the content is not
  reviewed.
- `tuning`'s replay compares one step at a time from the recorded history, so it does not see how a lower tier changes
  later steps; the watch covers that only through the share of `ok`, the step count, and the step time. `ok` is not
  correctness: the quality of an effort change rests on the replay and the judge. Agents whose answers vary from run to
  run (the current tier agrees with its own record less than `MIN_SELF_AGREEMENT`) are never changed.
- The replay calls the real LLMs (12 steps on the current tier and on each tier tried, plus the judge for free text),
  through the same clients and concurrency limit as live runs, and its LLM rows are recorded under the run of the agent
  `optimizer`.
- With few executions per week an agent stays `watching`, and nothing new is proposed for it until `WATCH` passes. A
  tuning undone by a person on the second-parent side of a merge commit is not seen as undone, so it is not cooling.
- The effort change does not update inventories or other docs that name the agent's tier.
- Reported kinds are estimates from usage and hashes, not replays; `duplicate_trigger`'s tokens are an upper bound.
- `behavior` sees only what its credentials see: no Discord DMs, no Slack channels its user is not in, and nothing from
  Notion, Gmail, or a local terminal. A missing record is not evidence that the operator did nothing.
- A routine that needs a new trigger (an intake route, a branch in dispatch's Python, a `schedule` row) is only
  reported: the guard rails forbid logic changes in existing files, and checks do not write the database.
- Routines consume nothing, so a routine that keeps recurring is reported every day. An episode consumed by a change
  stays out for the rest of the window even when it gets new events.
- The digest, including other people's clipped texts, is sent to the Host LLM.

## Test cases

Run record reads, the LLM, time, and checks are stand-ins; PostgreSQL and a real LLM are not used. The `errors` and
`consolidation` tests
open a real `Repo` as the singleton over the stand-in store in `tests/optimizer/conftest.py` (writes are readable, the
mirror's main moves by compare-and-swap, and a push advances GitHub's main only from the candidate's parent), so fork
load failures, rebases, and failed merges run for real. The `additions` tests use the same store over a tree with a
`general` agent, a `slack_api` tool, a skill, and the three inventories; its environment is a stand-in mapping. The
`tuning` tests use the same singleton, stand-in `logs` readers, and LLMs that answer from the request, so the replay,
the `llm` edit, the fork, the merge, and the revert run for real. OP44 runs the `errors` check over a real git mirror of
a bare repository on disk that stands in for GitHub, so the rebase is git's own. The `logs` SQL itself and the delegation of
`OptimizerAgent` are outside pytest; the `tuning` SQL and its replay with the real LLMs are checked by an acceptance run
against a copy of the live `logs` in the development database. The `behavior` check has no cases of its own: its candidate
handling is `optimizer/proposal.py`, which the `additions` cases run, and its readers and survey are checked by runs with
real credentials and a real LLM.

| ID | File | Scenario | Expected result |
|---|---|---|---|
| OP01 | [test_optimizer.py](../../tests/optimizer/test_optimizer.py) | Checks A and B registered; `handle` with the default input; the reader returns 3 runs | The reader is called once with `limit=100`; A and B run in order on the same observation (3 runs, main version, time); the report lists their findings in order with status `ok` |
| OP02 | [test_optimizer.py](../../tests/optimizer/test_optimizer.py) | A raises `RuntimeError("boom")`, B returns a finding | A is `failed` with `RuntimeError: boom`; B still runs and is `ok`; status `error` with one `check:A` note; `response` is the full report |
| OP03 | [test_optimizer.py](../../tests/optimizer/test_optimizer.py) | `checks=["B"]`, and `checks=["X"]` (unregistered) | Only B runs; the unknown name is an error with no read and no check |
| OP04 | [test_optimizer.py](../../tests/optimizer/test_optimizer.py) | Invalid input: `limit` 0, 1001, or a string; an unknown field; duplicate `checks` | Every case is an error with no read and no check |
| OP05 | [test_optimizer.py](../../tests/optimizer/test_optimizer.py) | The reader returns 3 runs including the current run | The observation and `run_count` have 2 runs, without the current run |
| OP06 | [test_optimizer.py](../../tests/optimizer/test_optimizer.py) | The reader raises | Error; no check runs |
| OP07 | [test_optimizer.py](../../tests/optimizer/test_optimizer.py) | `handle` before `install` | Error `optimizer is not installed in this process` |
| OP08 | [test_optimizer.py](../../tests/optimizer/test_optimizer.py) | The caller is cancelled while A runs | `CancelledError` propagates; B is not called |
| OP09 | [test_optimizer.py](../../tests/optimizer/test_optimizer.py) | `Optimizer` created with two checks of the same name | `ValueError` |
| OP11 | [test_errors.py](../../tests/optimizer/test_errors.py) | One run with dispatch (error) → review (error); another with triage (`max_steps`) and general (`cancelled`); error rows of the optimizer itself and of the current run | Targets are review and triage only (equal counts, so by name); failures are read once with the observation time minus 24 hours as the lower bound |
| OP12 | [test_errors.py](../../tests/optimizer/test_errors.py) | review has one failure on the observed main version and one on an older version | Only the main-version row reaches the LLM input |
| OP13 | [test_errors.py](../../tests/optimizer/test_errors.py) | All review failures are on older versions | `skipped` (`stale`); the LLM is not called |
| OP14 | [test_errors.py](../../tests/optimizer/test_errors.py) | (a) `code_version` without `+c.` (b) the agent is missing from the main component graph | Both `skipped` with a reason; the LLM is not called |
| OP15 | [test_errors.py](../../tests/optimizer/test_errors.py) | The LLM calls `read_file`, `write_file` (`tools/...`), then answers `{"verdict": "fixed"}` | The fork's parent is main, its tree is main plus the write, its message is `optimizer:errors review runs=...`; the merge advances main; the report has `merged`, `base`, `candidate`, `paths`; the first LLM input has the agent name, the failure cause, and the agent's module path |
| OP16 | [test_errors.py](../../tests/optimizer/test_errors.py) | (a) `not_agent` without writes (b) `unfixable` after writes (c) `fixed` without writes (d) a sentence before the `not_agent` JSON | (a)(d) `not_agent` (b)(c) `unfixable`; no fork; main unchanged |
| OP17 | [test_errors.py](../../tests/optimizer/test_errors.py) | `write_file` to `harnesses/x.py`, `sources/x.py`, `../x.py`, `core/x.py`, `agent/../x.py`, and to `agent/x.py`, `tools/x.py`, `skills/a/SKILL.md` | The first five return a failed result and leave the tree unchanged; the last three enter the working tree and become the candidate's `paths` |
| OP18 | [test_errors.py](../../tests/optimizer/test_errors.py) | (a) The first write has a syntax error so the fork fails, and the second succeeds (b) both forks fail | (a) The failure goes back to the LLM and the second fork is merged (`merged`) (b) `fork_failed`; main unchanged |
| OP19 | [test_errors.py](../../tests/optimizer/test_errors.py) | Another push moves GitHub's main between fork and merge: (a) to a version the candidate merges with cleanly (b) to a version that changes the same line | (a) The candidate is rebased onto the new main (parent = the other version) and pushed; `merged` with `candidate` = the pushed version and `rebased_from` = the original candidate (b) `failed` with `merge: MergeConflict…`; nothing pushed; the mirror's main unchanged |
| OP20 | [test_errors.py](../../tests/optimizer/test_errors.py) | review (3 failures) and triage (1 failure); separately, 6 target agents | review then triage; triage stays a target after review's merge and forks from the advanced main; with 6 agents, the one with the fewest failures is `skipped` (`over limit`) |
| OP21 | [test_errors.py](../../tests/optimizer/test_errors.py) | (a) LLM exception (b) no final answer in 30 responses (c) a non-JSON final answer, each followed by another agent; (d) cancellation during an LLM response | (a)-(c) That agent is `failed` without a fork and the next agent is processed (d) `CancelledError` propagates |
| OP22 | [test_errors.py](../../tests/optimizer/test_errors.py) | No failures in 24 hours | Empty findings; no LLM call or fork; the first registered check is `errors` |
| OP23 | [test_consolidation.py](../../tests/optimizer/test_consolidation.py) | The registry and the constants | `CHECKS` is `errors`, `consolidation`, `additions`, `tuning`, `behavior`; `checks` omitted runs all five in that order; the constants have the values in the table |
| OP24 | [test_consolidation.py](../../tests/optimizer/test_consolidation.py) | dispatch → a (tools t1, t2; child b; skill s) with usage | Usage is read once with the observation time minus 30 days; references are read once; the survey is one call without tools whose catalog has declarations (without the base tool), parents, reachability, runs, calls, reads, and the protected flags; no groups, no findings |
| OP25 | [test_consolidation.py](../../tests/optimizer/test_consolidation.py) | `orphan` is composed but no entrance reaches it | An `unreachable` group; the LLM deletes the module and the compose line; the fork (message `optimizer:consolidation unreachable delete orphan`) is merged; the finding has every `detail` field; the conversation has the five working-tree tools |
| OP26 | [test_consolidation.py](../../tests/optimizer/test_consolidation.py) | A tool class no agent registers (t9), an agent class not composed (ghost), a skill no agent declares (lone), and a nameless base class (Shared) | `ghost`, `t9`, then `lone` become `unreachable` groups and are merged, each forked from the previous merge; `Shared` is not a group |
| OP27 | [test_consolidation.py](../../tests/optimizer/test_consolidation.py) | Agents without runs: a (in a version 20 days old), b (new), c (first seen 20 days ago), d (first seen 20 days ago and named by `schedule`) | `unused` groups for a and c only; the catalog marks a and c old, b new, d protected |
| OP28 | [test_consolidation.py](../../tests/optimizer/test_consolidation.py) | (a) a has 25 runs and calls only t1; t2 is never called; t3 is called by name from Python; the child c is new (b) the same with 19 runs | (a) One `unused_edges` group `["a", "t2"]`; the pruned declaration is merged (b) no group |
| OP29 | [test_consolidation.py](../../tests/optimizer/test_consolidation.py) | a (25 runs) declares the old skills s (never read) and r (read once) | One `unused_edges` group `["a", "s"]` |
| OP30 | [test_consolidation.py](../../tests/optimizer/test_consolidation.py) | (a) The survey returns 7 groups including an unknown name and a group of only an unknown name; an unreachable agent exists (b) the survey answers with non-JSON or raises | (a) Unknown names and empty groups are dropped; the first five valid groups follow the `unreachable` group; the sixth handled group is `skipped` (`over limit`) (b) A `survey` finding `failed` comes first and the `unreachable` group is still handled |
| OP31 | [test_consolidation.py](../../tests/optimizer/test_consolidation.py) | The survey returns (a, b) and (p) | b is merged into the survivor a (message `optimizer:consolidation duplicate merge a,b`); p's change forks from that merge; main ends on the second candidate without `agent/b.py` |
| OP32 | [test_consolidation.py](../../tests/optimizer/test_consolidation.py) | (a) Deleting x leaves `call_agent(call, "x", …)` in a (b) deleting an agent named by `schedule` (c) deleting the base tool | (a) The violation (name and path) goes back to the LLM and the second attempt is merged (b)(c) `rejected` after two attempts; no fork; main unchanged |
| OP33 | [test_consolidation.py](../../tests/optimizer/test_consolidation.py) | Writes and deletes outside `agent/`, `tools/`, `skills/`, a delete of a missing file, and two `grep` calls | The first five fail with an error and change nothing; `grep` returns `path:line: text`, at most 200 lines |
| OP34 | [test_consolidation.py](../../tests/optimizer/test_consolidation.py) | (a) `keep` after writes (b) `delete` without writes (c) a sentence with a stray `{` before a fenced `keep` JSON | All `kept` (reason as given, or `delete without changes: …`); no fork |
| OP35 | [test_consolidation.py](../../tests/optimizer/test_consolidation.py) | (a) The first tree does not load and the second does (b) both fail | (a) The failure goes back to the LLM and the second fork is merged (b) `fork_failed`; main unchanged |
| OP36 | [test_consolidation.py](../../tests/optimizer/test_consolidation.py) | Another push moves the shared main between fork and merge: (a) a clean rebase (b) a conflict | (a) `merged` with `candidate` = the rebased version and `rebased_from` = the original (b) `failed` with `merge: MergeConflict…`; nothing pushed; main unchanged |
| OP37 | [test_consolidation.py](../../tests/optimizer/test_consolidation.py) | (a) Seven `unreachable` groups (b) a `duplicate` group whose target an earlier group deleted | (a) Five handled, two `skipped` (`over limit`) (b) `skipped` (`gone`) without a conversation |
| OP38 | [test_consolidation.py](../../tests/optimizer/test_consolidation.py) | (a) LLM exception (b) no final answer in 40 responses (c) a non-JSON final answer, each followed by another group (d) cancellation during an LLM response (e) no groups and an empty survey | (a)-(c) `failed` without a fork and the next group is handled (d) `CancelledError` propagates (e) Empty findings; only the survey call |
| AD01 | [test_additions.py](../../tests/optimizer/test_additions.py) | Rows in 7 days: general with an `llm` row and `shell`, `slack_api`, and `codegen` calls (its `data` holds a marker); `bash` under implementation; a tool under codegen; rows of the current run and under `optimizer` | The reader is called once with the observation time minus 7 days; the survey input has the run entries in the documented shape (request without `data`, steps in order, `commands` for implementation); the `llm` row, the codegen tool, the current run, and the optimizer row are absent; the catalog lists general, `slack_api` (used by general), and the skill with its description; the survey gets no tools |
| AD02 | [test_additions.py](../../tests/optimizer/test_additions.py) | (a) A 2000-character command, request, and tool argument (b) 5 runs whose JSON exceeds `DIGEST_CHARS` | (a) Clipped to 800, 500, and 300 characters plus `…` (b) The oldest runs are dropped: the newest 2 remain and their JSON fits |
| AD03 | [test_additions.py](../../tests/optimizer/test_additions.py) | `repo.log()` has additions versions in the window listing rA,rB (on main), rC (a kept fork), rD (rebased, with `Rebased-From`), an additions version older than the window listing rE, and a person's commit naming rF; rows exist for rA-rG | The digest has rE, rF, and rG only |
| AD04 | [test_additions.py](../../tests/optimizer/test_additions.py) | (a) No rows (b) Rows only in consumed runs | Empty findings; the LLM is not called |
| AD05 | [test_additions.py](../../tests/optimizer/test_additions.py) | Candidates with evidence (a) r1,r2,r3 (b) r1,r1,r2 (c) r1,r2 and a run id not in the digest | (a) Handled (b)(c) `skipped` (`too few runs`) with evidence r1,r2 |
| AD06 | [test_additions.py](../../tests/optimizer/test_additions.py) | Candidates: a new tool named `slack_api`, an `existing` named `ghost`, a new tool with owner `ghost`, a tool `Bad-Name`, a skill `bad_name`, kind `plugin`, and a valid skill twice | The first valid skill is handled; the others are `skipped` with `name taken`, `unknown component`, `unknown owner`, `invalid name`, `invalid name`, `unknown kind`, and `duplicate`, after it in survey order; one conversation |
| AD07 | [test_additions.py](../../tests/optimizer/test_additions.py) | 4 valid candidates | All 4 are handled in survey order; there is no candidate limit |
| AD08 | [test_additions.py](../../tests/optimizer/test_additions.py) | The survey (a) raises (b) answers with a tool call (c) answers non-JSON (d) answers `{"candidates": "x"}` | One `failed` finding with kind `survey`; no conversation and no fork |
| AD09 | [test_additions.py](../../tests/optimizer/test_additions.py) | A new skill: the LLM writes `skills/devdb-query/SKILL.md`, adds it to general's `skills`, and updates `skills/inventory.md` | The fork's parent is main, its tree is main plus the writes, its message is `optimizer:additions skill devdb-query runs=r1,r2,r3`; merged; the finding has the documented detail and evidence; the conversation input has the name, purpose, evidence entries, and `owner_modules`; the conversation gets the five tools |
| AD10 | [test_additions.py](../../tests/optimizer/test_additions.py) | A new tool: `tools/slack_post.py`, imported and declared by general, `tools/inventory.md` updated | Merged; general in the new main declares `slack_post` |
| AD11 | [test_additions.py](../../tests/optimizer/test_additions.py) | A new agent: `agent/devdb.py`, added to `AGENTS` and to general's `agents`, `agent/inventory.md` updated | Merged; the new main has `devdb` as a child of general |
| AD12 | [test_additions.py](../../tests/optimizer/test_additions.py) | `existing` `slack_api`: (a) only the tool's description and general's prompt change (b) the tool's `run` changes on both attempts | (a) Merged with both paths; the conversation input has `target` (b) The failure naming `tools/slack.py` goes back to the LLM; then `rejected`; no fork; main unchanged |
| AD13 | [test_additions.py](../../tests/optimizer/test_additions.py) | A skill candidate that also (a) changes `agent/review.py` (b) adds `tools/devdb.py` (c) changes general's logic, fixed on the second attempt; (d) writes and deletes outside the scope | (a)-(c) The failure goes back to the LLM and the second attempt is merged; on two violations the outcome is `rejected` (d) `write_file` to `harnesses/`, `sources/`, `../` and `delete_file` of a fork-base file return failed results and leave the tree unchanged |
| AD14 | [test_additions.py](../../tests/optimizer/test_additions.py) | A new tool file that general never imports, on both attempts | The failure names general; `rejected`; no fork |
| AD15 | [test_additions.py](../../tests/optimizer/test_additions.py) | A skill whose text contains (a) the value of `SLACK_TOKEN` on both attempts (b) the value of `HOME` | (a) The failure names `SLACK_TOKEN`; `rejected`; the finding never contains the value (b) Merged |
| AD16 | [test_additions.py](../../tests/optimizer/test_additions.py) | (a) The new tool has a syntax error, then a fix (b) Syntax errors on both attempts | (a) The fork failure goes back to the LLM and the second fork is merged (b) `fork_failed`; nothing pushed; main unchanged |
| AD17 | [test_additions.py](../../tests/optimizer/test_additions.py) | (a) `keep` (b) `changed` without writes (c) writes, then `keep` | `kept` without a fork; (b) has reason `changed without changes: …` |
| AD18 | [test_additions.py](../../tests/optimizer/test_additions.py) | (a) The shared main moves after the fork (b) the rebase conflicts, with a second candidate (c) two candidates that both change | (a) Merged on the new main with `rebased_from` (b) `failed` (`merge: MergeConflict …`), and the second candidate is still handled (c) The second forks from the first's merged version |
| AD19 | [test_additions.py](../../tests/optimizer/test_additions.py) | The first candidate's conversation (a) raises (b) reaches 40 responses (c) gives a non-JSON final answer, followed by a second candidate; (d) cancellation during the survey | (a)-(c) `failed` without a fork, and the second candidate is handled (d) `CancelledError` propagates |
| AD20 | [test_additions.py](../../tests/optimizer/test_additions.py) | The registry | `CHECKS` names are `errors`, `consolidation`, `additions`, `tuning`, `behavior`; `checks=["additions"]` selects only `additions` |
| OP39 | [test_optimizer.py](../../tests/optimizer/test_optimizer.py) | `handle({})`, then `handle({"merge": false})`, with checks A and B | Both checks receive `merge` true, then false; `{"merge": "no"}` is an input error (OP04) |
| OP40 | [test_errors.py](../../tests/optimizer/test_errors.py) | `merge` false; the LLM writes `tools/fixed.py` and answers `fixed` | `forked` with a candidate and the path; main unchanged; nothing pushed |
| OP41 | [test_consolidation.py](../../tests/optimizer/test_consolidation.py) | `merge` false; the unreachable `orphan` is deleted | `forked` with the kept candidate; main unchanged; nothing pushed |
| AD21 | [test_additions.py](../../tests/optimizer/test_additions.py) | `merge` false; the skill of AD09 is written | `forked` with the kept candidate; main unchanged; nothing pushed |
| OP42 | [test_errors.py](../../tests/optimizer/test_errors.py) | Main has `agent/SYSTEM.md`; the LLM calls `read_file` and `write_file` on it, then answers `fixed` | The read returns its content; the write fails with an error naming the path; `unfixable` without writes; no fork; main unchanged |
| OP43 | [test_consolidation.py](../../tests/optimizer/test_consolidation.py) | Main has `agent/SYSTEM.md`; the LLM calls `write_file` and `delete_file` on it, then answers `delete` | Both fail with an error naming the path; `kept` (`delete without changes: …`); no fork |
| OP44 | [test_errors.py](../../tests/optimizer/test_errors.py) | Real git: right after the repair's fork is written, a person pushes a change to `agent/SYSTEM.md` | The repair is rebased onto the person's commit and merged (`rebased_from` set); the new main has the person's `agent/SYSTEM.md` and the repair, and GitHub's main is that version |
| AD22 | [test_additions.py](../../tests/optimizer/test_additions.py) | Main has `agent/SYSTEM.md`; the LLM writes it together with the skill of AD09, then answers `changed` | The write to `agent/SYSTEM.md` fails and the others succeed; the candidate holds only the skill writes and is merged; main's `agent/SYSTEM.md` is unchanged |
| TU01 | [test_tuning.py](../../tests/optimizer/test_tuning.py) | Agents on `max`/`high`/`low` with various reasoning shares, answer sizes, and step counts; `optimizer`; an agent not in main | Only the `max` and `high` agents with ≥ 20 ok steps, ≥ 70% reasoning, and a visible-output p90 ≤ 1000 are candidates; tiers to try are `low`, `high` for `max` and `low` for `high` |
| TU02 | [test_tuning.py](../../tests/optimizer/test_tuning.py) | The same LLM input 6 times (4 distinct answers); repeated requests and request elements; a fixed instruction on every execution; 6 runs on one PR within 3 minutes of each other, another PR, a run 2 hours later and a second execution in that run | `repeated_input` counts 6 calls and 4 answers; `repeated_request` counts 3 repeats, 1 split answer, and 8 of 11 elements; the fixed instruction is not reported; `duplicate_trigger` counts 5 of 9 |
| TU03 | [test_tuning.py](../../tests/optimizer/test_tuning.py) | A 4-step `run_command` chain; a 29,500-token tool result carried 3 steps; 10% cache on 50,000-token first steps; a 2-step child with calls and a 1-step child without; a skill read every time; first calls whose arguments appear in 9 or in 7 of 10 inputs | `call_chain`, `large_output` (88,500 carried tokens), `cache`, `light_child` (only the child with calls), `skill_inline`, and `prefetch` (9 of 10; nothing at 7 of 10) with those numbers |
| TU04 | [test_tuning.py](../../tests/optimizer/test_tuning.py) | translator on `max` with 24 steps (plus one of the current run); `max` and `low` give the same answers, `low` with a tenth of the output | The 12 newest steps (not the current run's) are replayed on `max` and `low` only; only `agent/translator.py` changes; the merged main's translator uses `low`; the message and trailers carry the id, kind, agent, gate, and baseline |
| TU05 | [test_tuning.py](../../tests/optimizer/test_tuning.py) | `low` answers with calls; `high` answers the same with the same output | `rejected` with both gates (agreement 0%; no saving); no fork |
| TU06 | [test_tuning.py](../../tests/optimizer/test_tuning.py) | `merge` false; `low` fails on 2 of the 12 steps; no `high` client | `forked` on 10 valid samples with 100% agreement; main unchanged; nothing pushed |
| TU07 | [test_tuning.py](../../tests/optimizer/test_tuning.py) | Gates: agreement 8/10 and 7/10 against 9/10; 7 valid samples; 80% output; a current tier agreeing 7/10 with its record; 4 failed candidate replays at equal output | Pass only at 8/10 with a tenth of the output; the 70% current tier cannot judge; failed replays do not lower the measured output, so there is no saving |
| TU08 | [test_tuning.py](../../tests/optimizer/test_tuning.py) | A translator `max`→`low` tuning on main; 10 executions before (all ok), 10 after (5 errors) | `reverted`: main's translator declares `max` again and nothing else differs from before the tuning; the revert's parent is the tuning and its trailers name it with `low`→`max`; effort for translator is `skipped` (cooldown) |
| TU09 | [test_tuning.py](../../tests/optimizer/test_tuning.py) | After the tuning: 4 faster executions; 10 faster; 10 with the same step time | `watching`; `kept`; `reverted` |
| TU10 | [test_tuning.py](../../tests/optimizer/test_tuning.py) | (a) A person edited `agent/translator.py` after the tuning (b) the tuning sits on the second parent of a person's merge commit; the success rate fell in both | Both `reverted`: only the `llm` declaration goes back, and the person's edit and file stay; the merge commit's runs count as after |
| TU11 | [test_tuning.py](../../tests/optimizer/test_tuning.py) | `set_tier` on an annotated declaration with a comment, a class without `llm` (annotated and plain), an unknown agent, a non-literal `llm` | Only the value or one added line changes; `ValueError` for the last two |
| TU12 | [test_tuning.py](../../tests/optimizer/test_tuning.py) | Calls with the same and different tools; answer vs calls; JSON with equal and different short values; free text judged with 6 seeds in both directions; an unreadable verdict | Decisions as in the comparison table; the judge sees the fresh answer in both positions; the unreadable verdict is invalid |
| TU13 | [test_tuning.py](../../tests/optimizer/test_tuning.py) | A repeated input, cold first steps, and a repeated input of an agent not in main | Only the two main agents are `reported`, largest tokens first; no LLM call; no fork |
| TU14 | [test_tuning.py](../../tests/optimizer/test_tuning.py) | A translator `max`→`high` tuning under watch; translator still qualifies for `low` | Watch is `watching`; effort is `skipped` (being watched) without replay |
| TU15 | [test_tuning.py](../../tests/optimizer/test_tuning.py) | Only 7 recorded steps can be read for replay | `rejected` ("only 7 recorded steps to replay (need 8)") without an LLM call |
| TU16 | [test_tuning.py](../../tests/optimizer/test_tuning.py) | (a) A person set translator back to `max` after the tuning (b) the tuning is only a fork; translator qualifies again | (a) No watch; effort `skipped` (cooldown) (b) No watch; effort replayed and `merged` |
| TU17 | [test_tuning.py](../../tests/optimizer/test_tuning.py) | The sample read and the request read fail | effort `failed` with the cause; one `prefetch` finding `failed`; `repeated_input` is still `reported` |
| TU18 | [test_tuning.py](../../tests/optimizer/test_tuning.py) | A class assigning `llm` twice; classes inheriting a class with and without their own `llm` | The last assignment is edited; `inheritors` returns only the subclass without `llm` |
| VR55 | [test_singleton.py](../../tests/versioning/test_singleton.py) | (a) `current_repo()` before opening (b) after `open_repo` (c) a second `open_repo` while open (d) after `close()`, then `open_repo` again (e) `open_repo` without `store` (`GitVersionStore` is a stand-in) (f) the same with an empty `COMPONENT_REPO_URL` | (a) `RuntimeError` (b) the same store (c) `RuntimeError`, singleton unchanged (d) `RuntimeError`, then a new store opens (e) the store is built from `COMPONENT_REPO_DIR`, `COMPONENT_REPO_URL`, and `COMPONENT_REPO_SSH_KEY`, with `hooks=HOOKS_DIR` and `seed=PACKAGE_ROOT` (f) the same `GitVersionStore` with an empty URL (local mode); there is no package-only store |
