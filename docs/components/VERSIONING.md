# Component versioning

The code of agents, tools, skills, harnesses, and Sources is versioned in git. A version is a commit, and runs use the
version that the runtime's bare repository (the mirror, `COMPONENT_REPO_DIR`) has adopted as `main`. A new run uses
`main` as of its start, and a running run finishes on the version it started with.
People work in the working copy `components/` inside the checkout (`COMPONENTS_DIR`), a git repository of its own that
the Host's git ignores. They change components by pushing to the shared `main`, and the optimizer merges its fixes into
the same `main`. The shared `main` is either:

- **GitHub** (`COMPONENT_REPO_URL` is set): the component repository on GitHub. The mirror fetches it, and the
  optimizer pushes to it.
- **Local** (no `COMPONENT_REPO_URL`): the mirror itself. People push to it from the working copy, and the first
  `main` is the package's minimal default set ([Local mode](#local-mode)).
The implementation is `core/versioning/` (version tree, git store, loading, version store, CLI) and
`core/shared_state.py`. Component composition (`core/builtin/compose.py`), the catalog (`core/builtin/catalog.py`), and
the displayed run version (`core/builtin/code_version.py`) live in the Host's `core/builtin/`.
This document is the source of truth for the versioning and lock contracts.

Versioning does exactly three things:

1. **Read versions**: it reads commits through the bare mirror and adopts the shared `main` once the current Host can
   load it.
2. **main, fork, merge**: it keeps the current version `main`, forks candidates from a version, and merges a candidate
   by putting it on top of the latest shared `main` (and, with GitHub, pushing it).
3. **Agent lookup**: it returns an agent instance by name. Agents are unaware of versions.

Creating candidate versions (the Host's `optimizer/`, see [Optimizer](OPTIMIZER.md)), comparing them (A/B), and rules
for choosing a version are outside versioning. Runs always use the latest adopted `main`.

## Usage

Editing by hand with GitHub:

1. `main` is A on GitHub and in the mirror. Every run uses A.
2. The person pulls in `components/`, edits `agent/review.py`, commits B, and pushes it to `main`. The clone's
   pre-push hook loads B with the Host that holds the hook first and stops the push with the path, line, and exception
   if B does not load.
3. Within `COMPONENT_FETCH_INTERVAL` (5 minutes) the runtime fetches B, loads it, and adopts it as `main`. Runs that
   start afterwards use B; running runs finish on A.

Editing by hand in local mode:

1. `main` is A in the mirror, and `components/` is a clone of it.
2. The person edits, commits B, and runs `git push`. The mirror's pre-receive hook loads B with the Host and refuses the
   push with the path, line, and exception if B does not load; otherwise `main` becomes B.
3. The next run's `refresh` loads B. Runs that start afterwards use B; running runs finish on A.

With the optimizer:

1. `main` is A, and `review` fails on A.
2. The optimizer's `errors` check rewrites `agent/review.py` in the tree of A and stores C with `fork(A, tree, message)`.
   `main` stays A.
3. `merge(C)` reads the shared `main` (fetching it from GitHub). If it is still A, C goes on as it is. If the person
   pushed D meanwhile, the merge combines A→C with A→D into C' (parent D, with a `Rebased-From: <C>` trailer) and
   loads C'. With GitHub the result is pushed, and only after the push succeeds does the mirror's `main` move; in local
   mode the mirror's `main` moves by fast-forward. The person receives the fix with the next `git pull`.
4. If C and D change the same lines, the merge raises `MergeConflict`; if another push lands between the read and the
   update, it raises `PushRejected`. Either way `main` does not move.

A version can be forked any number of times. A second merge of a candidate already contained in the shared `main`
changes nothing and returns `main`.

## Ownership

| Owned by the version (the package defaults, or the component repository) | Owned by the Host (this repository, one per process) |
|---|---|
| All files in `agent/`, `tools/`, `skills/`, `harnesses/`, `sources/` | Everything else (`core/`, `llm/`, `queue/`, `api/`, `integration/`, `optimizer/`, `config.py`, `pyproject.toml`, `uv.lock`, `scripts/`) |

The component repository (and the working copy `components/`) holds the five folders at its root with the same relative
paths as under `src/trackeverything/`, and may hold a `README.md`. Files outside the five folders are not part of a
version; the preamble every agent's LLM calls start with is therefore `agent/SYSTEM.md`
([System preamble](#system-preamble)). `.py` files in `agent/`, `tools/`, `harnesses/`, and `sources/` are included as
modules; everything else (including skill scripts and `agent/SYSTEM.md`) is included as resources. Components import
Host modules (`trackeverything.core`, `trackeverything.config`, `trackeverything.queue`), so the component repository
is not a standalone package: it loads only together with a Host. Third-party dependencies are the Host's (`pyproject.toml`).

The Host never imports or calls component modules. Components reach the Host through its modules; a component that
queues a new request with the running message as parent uses `publish_child` in `queue/child.py`
([Queue](QUEUE.md)). Periodic component work runs as agents started from the `schedule` table
([Scheduling](SCHEDULING.md)).

The component graph (agent instances, entrances, the Source dictionary, injection of `fetch_source`,
the mesh, skill binding) is built by `build(services, skills_root=...)` in the version-owned `agent/compose.py`, which
returns a `ComponentGraph`.

| `ComponentGraph` field | Content |
|---|---|
| `agents` | All agents of the version (name → instance) |
| `entrances` | Names of entrances that queue requests can start (currently only `dispatch`). A name not in `agents` is a `ValueError` |
| `sources` | `source` key → Source class (read by the catalog) |

`build_agents` in `core/builtin/compose.py` creates one instance per name from the list of agent classes and checks
that the child names each class declares in `agents` are in the list. Child instances are not bound; they are looked
up by name from the run handle at run time ([Run flow and version pinning](#run-flow-and-version-pinning)).
The Host passes shared services as `HostServices` (`llms`, mapping LLM names to clients; the run logger; the session
store; `full_mesh`; `skills_root`), and each agent receives one client by the `llm` name of its class. The runtime and
the check CLI create the same services with `open_host_services()` in `core/builtin/app.py`.
Scripts and tests without versions pass `StaticAgents`, a lookup over a fixed mapping, as `AgentContext.agents`.

### Effect on `BaseAgent`

An agent holds only the names of its children, never bound instances.

- `has_agent(name)`: whether `name` is a child name. `list_agent_names()`: the list of child names.
- `child_meta(name, ctx)`: gets the child from `ctx.agents` and returns `name`, `description`, and `arguments`; raises
  `KeyError` if it cannot be obtained.
- `extend_callables(tools, agent_names)`: adds tools and child names for the mesh (existing entries win; the agent's
  own name is not added).
- `version_label`: the version the instance belongs to (`"c.<id8>"`, default None), set by the version store when it
  loads a version.
- `configure_system(text)`: the version's `agent/SYSTEM.md`, bound by `prepare_components`
  ([System preamble](#system-preamble)).
- `build_agents` passes the logger to every instance. The catalog, skill binding, and `AGENT_FULL_MESH` traverse every
  agent in the component graph.

## System preamble

`agent/SYSTEM.md` is the fixed text every LLM call of a version's agents starts with: the standing instructions people
keep for the whole agent set, as `AGENTS.md` is for the coding agents that edit a repository. It lives in `agent/`, so it
is part of the version and is checked, adopted, pinned, and rolled back with the agents. People maintain it in the
component repository; the optimizer reads it but never writes or deletes it ([Optimizer](OPTIMIZER.md#check-contract)).

- Loading: after `compose.build`, `prepare_components` (`core/builtin/compose.py`, shared by the runtime, the check CLI,
  and the hooks) reads it from the version's tree, strips surrounding whitespace, and binds it to every agent of the
  graph with `configure_system()`. Component code takes no part. A missing, empty, or blank file adds nothing; the
  package defaults ship none.
- A file that is not UTF-8, or longer than `SYSTEM_LIMIT` (8,000) characters after stripping, fails the version's
  composition with `ValueError: agent/SYSTEM.md: …`: the hooks refuse the push and the runtime does not adopt it.
- Sending: `BaseAgent._respond_one_step()`, the one path of every LLM call an agent makes (the standard loop and agents
  that build their own messages, such as single-shot classification and translation), sends a copy of the messages with
  the text in front. When the first message is a system message, the text is prepended to it with a blank line between;
  otherwise it becomes a new first system message. The caller's messages are unchanged, so later steps, reused
  conversations, and repairs never repeat it. The first system message reads `agent/SYSTEM.md` → the agent's own prompt
  → the skill catalog → the call-order rule, so every agent's calls share the same prefix.
- Harnesses and the optimizer call their LLM client directly and receive nothing. There is no per-agent opt-out: the
  file holds only what is true for every agent.
- A run sends the text of the version it pinned. Run records keep the messages as sent, so `tuning` replays them with
  the text included.

| ID | Scenario | Expected result |
|---|---|---|
| [VR56](../../tests/versioning/test_system_md.py) | A version with `agent/SYSTEM.md` and a skill catalog; an agent's standard loop calls a tool, then answers | Both calls start with one system message `<SYSTEM.md>\n\n<own prompt>\n\n<skill catalog>`; the text appears once in each call |
| [VR57](../../tests/versioning/test_system_md.py) | An agent sends its own messages twice through `_respond_one_step`: (a) a system message first (b) only a user message | (a) The text is prepended to that system message (b) a system message with only the text comes first; the agent's own list is unchanged and the second call holds the text once |
| [VR58](../../tests/versioning/test_system_md.py) | `agent/SYSTEM.md` (a) missing (b) empty (c) blank | Every call is sent exactly as the agent built it |
| [VR59](../../tests/versioning/test_system_md.py) | A run pins v1 (`RULES_V1`); v2, which changes only `agent/SYSTEM.md`, is merged; a second run starts | v2 is a new version; the first run sends `RULES_V1` and the second `RULES_V2` |
| [VR60](../../tests/versioning/test_system_md.py) | (a) 8,000 characters after stripping (24,000 UTF-8 bytes) (b) 8,001 characters (c) not UTF-8 | (a) Loads and is sent whole (b)(c) `fork` and `validate` (the check CLI and the hooks) raise `ValueError` naming `agent/SYSTEM.md`; main unchanged |
| [VR61](../../tests/versioning/test_system_md.py) | In one run, the agent's pre harness calls its LLM client directly | The harness's call is sent as written; the agent's call starts with the text |

## Local mode

Without `COMPONENT_REPO_URL`, the mirror is the shared repository itself; nothing is fetched or pushed over the network.

- **First main**: when the mirror has no `main`, `start()` writes the regular files of the package's five folders
  (`src/trackeverything`; the same exclusions as a git tree) as a root commit `Initial components`, loads it, and makes
  it `main`. If it does not load, startup aborts and no `main` is created. Afterwards the package's folders are not read
  again; they only seed a new installation.
- **Working copy**: after `start()`, the runtime calls `ensure_working_copy(mirror, path)` (`core/versioning/store.py`), which
  clones the mirror into `COMPONENTS_DIR` (default `components/` of the checkout; branch `main`, `origin` the mirror's
  absolute path) when that path does not exist. An existing path is never
  touched. A failure is logged at warning level and the runtime keeps running. With `COMPONENT_REPO_URL` the runtime
  makes no working copy; a person clones the component repository there.
- **Pushes**: the runtime sets the mirror's `core.hooksPath` to the Host's `scripts/component-hooks` every time it opens
  it. Its `pre-receive` hook checks the new tip of `refs/heads/main` ([Check CLI and the hooks](#check-cli-and-the-hooks))
  and refuses the whole push if it does not load, refuses deleting `main`, and takes other refs unchecked. A pushed
  `main` is adopted by the next run's `refresh` (and by `sync`).
- **Merge**: the optimizer's merge moves the mirror's `main` by fast-forward (compare-and-swap); see
  [Version store](#version-store-repo).

The package defaults are the agents `dispatch` (named runs go to the declared child, every other event to `general`, ACK at the
end), `general` (an LLM loop with the base tools), and `optimizer` (the entry to the Host's optimizer), and the skill loading
mechanism with no concrete skills. The base tools `read`, `write`, `edit`, and `bash` are not part of any version: `BaseAgent`
builds them in from the Host (`core/builtin/base_tools.py`). There
are no Sources or harnesses. The catalogs are the package's `agent/inventory.md` and `tools/inventory.md`.

## Versions and the mirror

- A version is a git commit. Its id is the 40-digit commit SHA; its label in records is `c.<first 8 digits>`. Its
  `parent` is the first parent (a person's merge commit has two), and `created_at` is the committer time (whole
  seconds).
- A version's tree is the regular files under the five folders (relative POSIX path → content). Symlinks, submodules,
  `__pycache__`, `.pyc`, and names starting with `.` are not part of it. File modes are not kept.
- A new tree (fork) is limited to the five folders: paths outside them, `..`, absolute paths, and empty segments are a
  `ValueError`. Writing a version keeps the parent's files outside the five folders (for example `README.md`).
- The same tree, parent, message, and second give the same commit. Creating a version whose commit already exists moves
  its time one second on, so every fork is a new version.
- The runtime reads only the bare mirror `COMPONENT_REPO_DIR` (default `data/components.git` of the checkout). With
  `COMPONENT_REPO_URL` its remote `origin` is that URL, and fetch and push use the deploy key `COMPONENT_REPO_SSH_KEY`.
  Only those fetches and pushes use the network; reads in a run do not.

| Mirror ref | Meaning |
|---|---|
| `refs/heads/main` | The adopted `main`: a version the current Host loaded. The runtime uses only this |
| `refs/remotes/origin/main` | GitHub's `main` at the last fetch (not yet validated). GitHub mode only |
| `refs/te/forks/<id>` | Candidates of the optimizer. Kept so that versions are not lost; never pushed |

In local mode `refs/heads/main` is also the shared `main` that people push to.

The mirror's `main` moves only by a compare-and-swap from the `main` that was read (`git update-ref` with the old
value), so a move from a stale value fails.

## Version store (`Repo`)

`Repo(store, build, *, view_root=None, clock=<current UTC time>)` in `core/versioning/repo.py` owns `main`, sync, fork,
merge, and the per-run handles. Storage is delegated to a `VersionStore` (the git implementation `GitVersionStore` in
`core/versioning/store.py`, whose GitHub and local modes are two internal strategies), and the process loads only `main`
and the versions used by runs.

| Operation | Content |
|---|---|
| `start()` | Loads the mirror's `main`. If the mirror has none, takes the shared `main` (GitHub's, fetched), loads it, and adopts it; in local mode with no `main` at all, makes the package defaults the first `main` ([Local mode](#local-mode)). Raises on failure (startup aborts) |
| `refresh()` | If the mirror's `main` differs from the loaded `main`, loads it and makes it `main`. Returns the `main` `Commit` |
| `sync()` | Reads the shared `main` (fetching GitHub's; nothing is fetched in local mode); if it differs from the loaded `main`, loads it and, when it loads, adopts it as `main`. Returns the `main` `Commit`. The runtime calls it every `COMPONENT_FETCH_INTERVAL` |
| `fork(parent, tree, message)` | Writes `tree` as a new version whose parent is `parent` (a stored version; unknown or None is `UnknownVersion`), loads and validates it, and keeps it as a fork. Does not change `main` |
| `merge(version_id)` | Puts the candidate on top of the latest shared `main` when needed, advances the shared `main` to it (a push to GitHub, or a fast-forward of the mirror's `main`), and only then switches to it. Returns the `main` `Commit` |
| `validate(tree)` | Loads a tree and builds its component graph without storing it, then releases it. Used by the check CLI |
| `log()`, `tree(version_id)` | The versions reachable from `main` and the kept forks (newest first), the tree of a version |
| `run()`, `release(run)` | Create a per-run handle `RunVersions`, end its use |
| `close()` | Waits for cancelled loads to finish and releases every version. Later operations raise `RuntimeError` |

- `fork` (and the version a merge rebases) writes the git objects first, then loads the tree outside the event loop
  and builds the component graph (including the `ComponentGraph` validation), and only then creates the fork ref. If
  loading or building fails, everything loaded is removed and no ref is created; the unreferenced objects are left to
  git's garbage collection. A version loaded only for validation is released at the end of the operation unless it is
  `main` or in use.
- `merge`: unknown ids raise `UnknownVersion`. It reads the shared `main` (R; with GitHub, by fetching). If the candidate
  is already contained in R, it changes nothing and returns the mirror's `main` (the adoption of R is left to `sync`).
  If R is the candidate's parent, the candidate is loaded and goes on as it is. Otherwise the candidate's parent is the
  base of a three-way merge (`git merge-tree`) of R and the candidate; the result becomes a new version whose parent is
  R and whose message is the candidate's message plus `Rebased-From: <candidate id>`, and it is loaded and validated.
  With GitHub the result is pushed without force and without the mirror's hooks (it is already validated); in local mode the mirror's `main` moves to it only if R is still its
  ancestor (a fast-forward by compare-and-swap). Either refusal raises `PushRejected`. Only after that succeeds does the
  mirror's `main` move and this process switch to it. Conflicts raise `MergeConflict` (with the paths), network and git
  failures raise `GitError`, and load failures raise their exception; in every failure the mirror's `main` does not move.
- `refresh` and `sync` load a new `main` the same way: a failed fetch or read is logged at warning level, the adopted
  `main` stays, and the next call tries again. A `main` that does not load, or names a missing commit, is logged once at
  error level and not adopted (later calls skip that commit); runs stay on the adopted `main`. The shared
  `main` is the source of truth: a `main` outside the adopted history (force push) is adopted when it loads.
- With GitHub, the mirror's `main` never moves before a push succeeds. Otherwise a rejected push would leave the runtime
  on a version outside GitHub's history, and the next sync would have to drop it or refuse the other push.
- Loading in `start`, `refresh`, and `sync`, and `fork`, `merge`, `validate`, and `close` are serialized by one asyncio
  lock in the Repo. `refresh` does not take the lock when the mirror's `main` equals the loaded one. Runs that start at
  the same time load a new `main` only once.
- Id prefixes are not supported (an id is the full 40-digit SHA). The ids of loaded versions are not re-verified.

### One version store per process

The version store of a process is a singleton created with `open_repo(build, *, store=None)` (by the runtime) and used
through `current_repo()` (by the optimizer). Omitting `store` creates
`GitVersionStore(COMPONENT_REPO_DIR, COMPONENT_REPO_URL, ssh_key=COMPONENT_REPO_SSH_KEY, hooks=HOOKS_DIR, seed=PACKAGE_ROOT)`
(an empty URL is local mode; `HOOKS_DIR` is the checkout's `scripts/component-hooks`, `PACKAGE_ROOT` is `src/trackeverything`). A second `open_repo` while one is open, and `current_repo()`
before opening or after closing, raise `RuntimeError`. Closing the singleton removes it so it can be opened again. A
process never holds two version stores: both would share the module namespace of the same version, and releasing in
one would remove the other's modules.

## Check CLI and the hooks

`python -m trackeverything.core.versioning check [--repo <git working tree>] [<rev>]` loads the five folders of a
commit with the same `open_host_services()` and `prepare_components` as the runtime, after `require_schema_head`. The
defaults are the current directory and `HEAD`. It neither reads nor writes the mirror or `main`.

- Success prints `ok <SHA>` (exit code 0). An unknown revision, a module that fails to load, a composition error (for
  example an LLM name the Host does not have), and a failed setup (including the schema check) print one line with the
  version path and line and the exception to stderr (exit code 1).

The hooks live in the Host at `scripts/component-hooks/`. Each finds its Host checkout from its own location
(`<checkout>/scripts/component-hooks/<hook>`) and runs the check through that checkout's launcher:

```bash
bash "<checkout>/scripts/run_local.sh" python -m trackeverything.core.versioning check --repo <repository> <sha>
```

| Hook | Used by | Checks |
|---|---|---|
| `pre-push` | A clone of the component repository on GitHub: `git config core.hooksPath <checkout>/scripts/component-hooks` once per clone | Every update of `refs/heads/main` the push sends (deletions excluded); any failure stops the push |
| `pre-receive` | The mirror in local mode; the runtime sets its `core.hooksPath` | The new tip of `refs/heads/main`; any failure, or deleting `main`, refuses the whole push. The pushed objects are read from git's quarantine through the environment git passes |

- Point a clone's hooks at the checkout whose Host runs the components (on a machine with a separate deploy checkout,
  that one), so the check uses the deployed Host and its database.
- GitHub rejects pushes that are not fast-forwards, so a person pulls before pushing; the commit the hook checks is the
  content GitHub's `main` will have. The mirror in local mode takes a force push that loads.
- Pushes to other branches are not checked. A `pre-push` hook can be bypassed (`--no-verify`, a push from another machine
  or a cloud session); the runtime's `sync` still validates and does not adopt a `main` that does not load. A
  `pre-receive` hook cannot be bypassed by the pusher.

## Loading

- Version modules are loaded from the version tree's sources under the reserved namespace
  `_te_versions.v_<version id>`. Checkout files are not used.
- At load time only the AST imports are rewritten so that absolute imports of version-owned modules point to that
  version. Shared code (`core` and the like) uses the canonical modules, which keeps type identity and locks the same
  across all versions.
- A component that loads an owned module by string, such as `importlib.import_module("trackeverything.tools.x")`, is
  rejected at load time (use an import statement).
- The version's content is written to a read-only file view (a temporary directory); a module's `__file__` and the
  skill read location point there. Only when `SKILLS_DIR` is set is that fixed location used for skills.
- Loading and building the component graph happen outside the event loop with `asyncio.to_thread`.
- If the caller is cancelled during a load, the cancellation propagates immediately without waiting for the thread.
  The thread continues until the load finishes, and a version it managed to load is released at that point. A
  cancelled load is not recorded as a failure.
- If a cancelled load of the same version is still running, the next load waits for its release before loading again.
  The namespace and file view are determined by the version id, so concurrent loads would let one release remove the
  other's modules and file view.

## Run flow and version pinning

1. The Runtime calls `await repo.refresh()` and then creates `run = repo.run()`.
2. It gets the version `run.version(name)` of the entrance `config.DISPATCH_AGENT` and creates
   `AgentContext(..., agents=run)`. Every request is an `EventRequest`; the Runtime does not reference Sources.
3. If the name is not in that version's `graph.entrances`, the result is the error `unknown entrance: <name>`.
4. It runs the entrance's `run`. Children are obtained by name from `ctx.agents` through `call_agent`.
5. When the run ends (success, failure, cancellation, deadline), it calls `repo.release(run)`.

- `RunVersions.version(name)` records the `main` at the first lookup of that name (even if the version lacks the name)
  and returns the same version afterwards. It does not load anything (synchronous). Each recorded version gains one
  user, once. `agent(name)` is `graph.agents.get(name)` of that version.
- If `main` changes during a run, names not yet looked up resolve to the new `main`. Each agent's imports, tools, and
  skills are those of that agent's version.
- `BaseAgent.call_agent(call, name, request)` obtains and runs the instance only when `name` is a child name (the
  declared `agents`, or with `AGENT_FULL_MESH` every other agent of the version) and `call.ctx.agents` is present.
  Otherwise, or when the lookup returns nothing, the result is the error `agent not registered: <name>`.
- Children presented to the LLM are looked up when the callables are built (which fixes their version). Children in
  `hidden_callables` are not looked up until called.
- Instances of version-owned classes belong to a different class per version. If the agent that created an approval
  object (`issue_workflow.AutoDevelopment`) and the agent that checks it are on different versions, the check does not
  match and is treated as not authorized (fail closed).
- Even within the same session, the next run uses `main` as of that moment. Input, source payloads, and LLM arguments
  cannot select a version.

## Release

- `Repo.release(run)` decrements the users of the versions the run used and releases versions that are neither `main`
  nor in use. A second call does nothing. After release, `version()` and `agent()` raise `RuntimeError`.
- When `main` switches and the old `main` has no users, it is released immediately.
- Release removes the modules (aliases in `sys.modules` and attributes of the parent package), linecache entries, the
  file view, and the component graph. Commits and shared state remain, and references held outside the store (for
  example a retained agent instance) are not broken. Release does not mean physical memory is returned immediately.
- On stop, the Runtime waits for accepted runs and then releases every version with `close()`. Nothing is written to
  the store on stop.

## Records and catalog

- The Repo sets `version_label` of every agent in the version's component graph to `c.<first 8 digits of the version
  id>`. While running, `BaseAgent.run` sets `current_component_version` to it.
- The `code_version` of run, LLM, tool, and harness records is `<Host version>+c.<first 8 digits of that agent's
  version id>` (the Host version alone outside agents). The Runtime records the start and end of `session_run` with the
  entrance's version. Format and queries: [LOGGING.md](../LOGGING.md).
- The catalog is built from every agent in the `main` component graph, the `agents` declarations of each class, and the
  unconnected declarations in the `main` modules, and is published to Redis with the `main` `label` as `code_version`.
  The maintenance tick publishes with the already loaded `main` and does not `refresh`. `GET /api/catalog` reads this
  stored data and does not build an execution graph.

## Shared state

State that must be single across versions stays in the component module and is obtained by name with
`shared(name, factory)`. The same name returns the same object in every version. Allowed values are standard-library
or plain data and the Host's `TaskLock`; instances of version-owned classes are rejected (creating that version
fails). To change a value's shape, use a different name. Use the common `keyed_lock(namespace, key)` for exclusion per
key and `task_lock(name)` for named exclusion ([Locks](#locks)). Releasing a version does not clear shared state (it is
kept for the life of the process).

| Target | Method |
|---|---|
| `_PENDING_LOCK` and `_PUSHING` in `tools/fix/tool.py` | `task_lock()`, `shared()` |
| `issue_lock` in `tools/issue_workflow.py` | `async with keyed_lock("issue", …)` |
| The holder record of the file lock in `tools/project/locks.py` | `shared()` |
| Discord channel cache and send lock, the Gmail token | `shared()`, `task_lock()` |

Data that must survive a restart goes in files or an existing store (the push confirmation state is in the memory
store).

## Locks

Locks only wrap a single operation inside a tool. The Runtime and agents hold no exclusion over business units
(session, PR, issue, Source, project attempt). Duplicates, reordering, and conflicts caused by concurrent business units
are accepted.

| Location | Role |
|---|---|
| `core/builtin/runtime.py` | Records the start and end of `session_run` and calls the entry agent without a session lock |
| `core/shared_state.py` | `TaskLock` shared across versions (not reentrant), `task_lock()`, `keyed_lock()` |
| `tools/project/locks.py` | The project commit lock (shared and exclusive) and the exclusive file lock used by the work docs store |
| `agent/project.py` | Syncs project documents as optimistic commit attempts, at most `_ATTEMPTS` times |

- The Runtime does not serialize runs by session. Runs of the same session also run concurrently, limited only by
  `RUN_CONCURRENCY`.
- Agents do not acquire locks. dispatch, `pr_event`, `issue`, the issue steps, `project_memory`, and project have no
  exclusion per business unit.
- A lock wraps a single operation inside a tool and never calls an agent inside it. Locks shared across versions are
  obtained with `task_lock()` and `keyed_lock()`.
- `TaskLock` is not reentrant. When the holding task requests the same lock, it raises `RuntimeError` without
  waiting. Other tasks wait for release. Holding by an ancestor task is not detected.
- The project file lock has only the commit lock. A reacquisition by the same task passes as reentry for nesting inside
  a tool (reading documents inside a commit, manual arbitration, or a notification record). There is no execution lock
  and no ancestor-task detection.
- Project sync holds no lock in any attempt. If a document it read changed by commit time, the attempt is discarded
  and retried in a new conversation; if all `_ATTEMPTS` attempts are stale, the sync fails.
- `pr_event` does not check for later deliveries of the same PR and syncs docs every time.

| Concurrent unit | Possible result |
|---|---|
| Runs of the same session | Conversation history is recorded in a different order than published, and a run may not see what another run said |
| Deliveries for the same PR | Reviews, comment handling, and docs sync are duplicated |
| Investigation, steps, and Codex runs of the same issue | Duplicate investigations, a step advanced twice, concurrent writes to the same working directory |
| Syncs of the same Source or project | A commit becomes stale and is retried; if conflicts continue, the sync fails |

## Deployment and restart

- A component change needs neither a deploy of this repository nor any other step: the runtime adopts GitHub's `main`
  within `COMPONENT_FETCH_INTERVAL`, or a `main` pushed to the mirror in local mode at the next run.
- A deploy that changes Host inputs makes the supervisor drain and restart the affected process (watched paths:
  `process.restart_roles()`; procedure: [QUEUE.md](QUEUE.md#startup-and-installation)). The restarted runtime loads the
  mirror's `main`.
- A Host contract change (for example the arguments of `build_agents` or the LLM names) is deployed first in a form the
  current components still load with, and the components are pushed afterwards: the hooks check with the deployed
  Host.
- If a deploy still makes the adopted `main` unloadable, the runtime aborts startup and the supervisor restarts it every
  5 seconds. Startup reads only the mirror, so make a component `main` that loads and point the mirror's `main` at it
  by hand; the next startup loads it. With GitHub, push it there first and then:

```bash
git --git-dir ~/Services/trackeverything/data/components.git fetch origin +refs/heads/main:refs/remotes/origin/main
git --git-dir ~/Services/trackeverything/data/components.git update-ref refs/heads/main refs/remotes/origin/main
```

## Failure handling

| Case | Result |
|---|---|
| First startup: no mirror and GitHub unreachable, GitHub has no `main`, or GitHub's `main` does not load | Startup aborts. The mirror gets no `main` and nothing stays loaded |
| First local startup: the package defaults do not load | Startup aborts. The mirror gets no `main` and nothing stays loaded |
| The working copy cannot be made | Logged at warning level with its path; the runtime keeps running |
| At startup the mirror's `main` cannot be loaded (cannot be built, or names a missing commit) | Startup aborts |
| `refresh` cannot load a new mirror `main` | The version path and line and the exception are logged once at error level, and the loaded `main` stays in use. That version is not retried in this process |
| `refresh` cannot read the mirror's `main` or its tree | Logged at warning level, and the loaded `main` stays in use. The next run's `refresh` tries again |
| `sync` cannot fetch, or cannot read the fetched commit | Logged at warning level; the adopted `main` stays. The next sync tries again |
| `sync` cannot load GitHub's `main` | Logged once for that commit at error level and not adopted |
| Writing, loading, or building a new version (fork) fails (including a `ValueError` from `build_agents`) | Raises. Everything loaded is removed, no fork is kept, and `main` does not change |
| A merge conflicts, its rebased version does not load, the shared `main` moved before its update, or GitHub is unreachable | `MergeConflict`, the load exception, `PushRejected`, or `GitError`. Nothing is pushed and the mirror's `main` does not move |
| `check` fails | stderr and exit code 1; the `pre-push` hook stops the push and the `pre-receive` hook refuses it |

A `ValueError` from `build_agents` means an agent class without a name, two classes with the same name, a child name
not in the list, an LLM name not in `llms`, or a dependency on a name not in the list.
Failure messages contain no source lines or credentials (credentials in a git URL are masked), and no notification is
sent (logs and CLI stderr only).

## What the runtime does not do

- It never reads component files from a checkout; versions come only from the mirror. The only exception is the first
  `main` of a new local installation, made once from the package defaults.
- It does not watch files. Pushes reach the mirror through git (local mode), or the runtime fetches GitHub's `main` on an
  interval.
- It has no rules for choosing versions (A/B). It always uses the latest adopted `main`.
- It does not push forks; only a merge pushes, and only to `main`.
- It does not notify Discord about version load failures.
- It does not `refresh` in the maintenance tick.
