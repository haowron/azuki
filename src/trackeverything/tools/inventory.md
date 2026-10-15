# Tool inventory (package defaults)

[components](../../../docs/components/README.md) / [Creating a tool](how-to-create.md)

The package's default version has the skill loading mechanism. It seeds a local installation's first
`main`, after which tools are edited in `components/`; a component repository brings its own tools
([Component versioning](../../../docs/components/VERSIONING.md)). The base tools `read`, `write`, `edit`, and `bash` are
Host-owned (`core/builtin/base_tools.py`) and built into every agent by `BaseAgent`, so no version carries them; they are listed
here for reference.

| Tool / name | Used by | Configuration and responsibility |
|---|---|---|
| `ReadTool` / `read` | Host base tool, built into every agent | Reads a UTF-8 text file: up to `limit` lines (default 2,000, at most 50,000 characters) from line `offset` (1-based), with the total line count and `next_offset` when more lines remain. Non-UTF-8 files fail |
| `WriteTool` / `write` | Host base tool, built into every agent | Creates or overwrites a whole UTF-8 file and creates parent directories. Line endings are not converted |
| `EditTool` / `edit` | Host base tool, built into every agent | Replaces `old_text` with `new_text` only when it matches exactly once; otherwise fails without changing the file |
| `BashTool` / `bash` | Host base tool, built into every agent | Runs a bash command in the workspace root (`REPOS_DIR`) with the service's OS user and full environment, and returns the exit code and the last 50,000 characters of combined output. A non-zero exit is a failure. The timeout (default 120 s, at most 3,600 s) and a cancellation stop the command's process group. Deleting files goes through `bash` |
| `ReadSkillTool` / `read_skill` | Agents that declare `skills` | Progressive reading of declared skill packages and registration of their tools ([skill authoring rules](../skills/how-to-create.md)) |

Paths may be absolute, `~`-based, or relative to `REPOS_DIR`, and may lie outside it. File work runs off the event loop,
and every failure is a `ToolResult` whose failure target is the `path` or the `command`.
