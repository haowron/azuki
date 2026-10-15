# Creating a skill

[Creating an agent](../agent/how-to-create.md) / [Creating a tool](../tools/how-to-create.md)

## Location and standard format

Place the skill at `src/trackeverything/skills/<name>/SKILL.md`. The name is 1 to 64 characters of lowercase letters, digits, and single hyphens, does not start or end with a hyphen, and matches the parent directory. The description states the purpose and when to use the skill in Japanese, in 1 to 1024 characters.

The YAML frontmatter is read with a safe loader, which rejects duplicate keys, arbitrary object tags, and invalid types for required fields. Additional information such as license, compatibility, and metadata is accepted. `allowed-tools`, `context: fork`, `model`, and similar keys are treated as other products' metadata and do not change capabilities, the model, or execution control.

```text
skills/document-analysis/
  SKILL.md
  tools.json                 # only when the skill has functions
  scripts/extract_text.py
  references/analysis.md
  assets/                    # only when needed
```

The body describes the input, steps, tools used, output, and how to handle failures. Long material goes into references/, and the body states when each file is needed and its relative path. The body and description are written in Japanese, and identifiers stay as they are. No dedicated generator is needed; use the following template (shown in English here).

```markdown
---
name: document-analysis
description: Use when extracting document text and comparing or analyzing it while keeping the sources.
metadata:
  trackeverything-tools: tools.json
---

# Document analysis

Confirm the input files and the expected output.
Extract the text with the extract_text function shown in the load result.
Read references/analysis.md only when a detailed analysis is needed.
Use github_search when GitHub material is needed.
Separate facts, inferences, and missing material, and return them with sources. Do not treat a failed fetch as success.
```

A skill without tools omits `trackeverything-tools`. Ordinary community SKILL.md files also work.
Relative to the SKILL.md of the [Agent Skills standard](https://agentskills.io/specification), the tools.json below is an extension of this project. Function schemas are never inferred from free-form body text.

## Skill-owned tools and shared tools

```json
{
  "tools": [
    {
      "name": "extract_text",
      "description": "Extract the body text from a document.",
      "arguments": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": false
      },
      "script": "scripts/extract_text.py"
    },
    {"ref": "github_search"}
  ]
}
```

- A skill-owned function is named `skill__<skill-name>__<tool-name>`: at most 64 characters in total, using only letters, digits, `_`, and `-`. Its object schema is validated as JSON Schema Draft 2020-12, and invalid input is rejected before the script starts. External `$ref`s are never fetched over HTTP.
- The Python script reads JSON from stdin and writes exactly one JSON value to stdout. Diagnostics go to stderr. It is started without a shell, using `sys.executable` and the fixed absolute script path, with the package root as cwd. Scripts never run at load time.
- stdout and stderr are each read up to 1 MiB; exceeding the limit is a failure. `COMMAND_TIMEOUT` and the existing process group cleanup apply, so descendants are terminated on success, non-zero exit, or timeout alike. The environment contains only the command's `SAFE_ENV_KEYS`; never embed credentials in declarations or the body.
- Shared tools are referenced from the explicit factory table in `agent/compose.py`. When the tool is already in use as a fixed tool or by another skill, the existing instance is used, and overriding it with a different implementation is rejected. New shared tools are added to the table on the Python side.
- A single invalid declaration, unknown reference, or name collision rejects the whole package. Each tool keeps its existing runtime permission checks.

An ordinary script can also be driven from the body through the base tool `shell` (which every agent has) or the existing `run_command`. Each command tool's cwd restrictions and permissions stay unchanged. The file-reading boundary is not an OS sandbox.

## Wiring to agents and the LLM

```python
class MyAgent(BaseAgent):
    skills: ClassVar[tuple[str, ...]] = ("document-analysis",)
```

Composition builds every tool, agent, and the mesh, and then calls `bind_skills(agents, root=..., tool_factories=...)` once. The constructor alone does not read files. A declared skill that is missing or invalid fails the composition; undeclared packages are ignored.

The `LLMClient.respond(messages, callables)` API stays unchanged.

1. First messages: the original system prompt + the skill usage rules + a catalog of names and descriptions, the conversation history, and the current request.
2. `read_skill(name, path="SKILL.md")`: returns the full text, base_dir, sha256, and the registered function names in the tool message. The schemas enter the next callables.
3. Only when needed, `read_skill(name, path="references/analysis.md")`: returns one reference file. It does not follow links recursively, register more functions, or run scripts.

If one response requests the first load and a not-yet-exposed function at the same time, that function is rejected. Skills are not added automatically to single-shot LLM calls for classification, translation, or harnesses.

## Lifetime, failures, and distribution

Dynamic registrations and the read state belong to a single agent call and are isolated across parent and child, re-entry, and concurrent runs. They are discarded when the call ends and never saved to the session. Returning to the parent restores the parent's state. Names, descriptions, and the base directory are fixed when the version is composed. Changes take effect from the next run that starts on the version the runtime adopted as `main`.

On a running Host, skills are read from the version's immutable file view. A run in progress uses the body, references, declarations, and scripts of the version it started with, and pushed edits become visible to runs that start after the runtime adopts them as `main` ([Component versioning](../../../docs/components/VERSIONING.md)). When the location is bound directly without going through a version, the body and references are read with their current content each time, and changes to registered declarations or scripts are rejected when the run re-reads or executes them.

SKILL.md is limited to 64 KiB and other text files to 256 KiB. Only regular UTF-8 files without NUL are read. Absolute paths, `..`, and symlinks that leave the package are rejected. A symlink for the package itself is resolved to its source of truth at composition time, and replacing it is rejected. Failures are returned as ToolResults and are advisory by default. A successful retry clears the failure for the same name/path.

The default root is `skills/` of the running version's file view. An optional `SKILLS_DIR` replaces the source-of-truth root, and multiple roots are never searched. A version includes documents, tool declarations, scripts, references, and assets of the same commit. The always-on service picks up a change when it adopts the push to the component repository's `main`.

## Verification

The behavior of agents and tools that use skills is out of scope for pytest. After a change, make the following pass.

```bash
uv run pytest
uv run ruff check src tests
uv run mypy src
```

When distribution changes, also confirm that the real wheel includes the files and that they resolve from a different cwd.
