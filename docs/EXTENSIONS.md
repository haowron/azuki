# Component ownership

This repository owns the Host: intake, queue delivery, execution, storage contracts, LLM adapters, optimization,
and HTTP/dashboard serving. It also ships a minimal default component version. A component repository owns its
application agents, tools, skills, harnesses, Sources, business rules, and evaluations.

The defaults are `dispatch`, `general`, and `optimizer`; they contain no Sources or harnesses. `general` uses the Host
base tools and leaves its result in the run records. Installing the Host does not install chat reply, review, development,
notification, or document-generation workflows. See the [default catalog](../src/trackeverything/agent/inventory.md).

## Developing components

Use [the agent guide](../src/trackeverything/agent/how-to-create.md), [tool guide](../src/trackeverything/tools/how-to-create.md),
and [skill guide](../src/trackeverything/skills/how-to-create.md) to build components against Host contracts.
[Versioning](components/VERSIONING.md) defines loading and compatibility; [Operations](OPERATIONS.md#component-repository)
defines checks and publication. Each component repository maintains its own `docs/README.md`, inventories, and evaluations.
Specifications for installed components must match that repository's version.

The following paths are documentation locations **inside a component repository that supplies these workflows**.
They are not files or bundled features of this Host. Host references to those workflows describe integration boundaries;
the component specifications own their business decisions.

## Conversations

A component dispatch decides how raw events become conversations, translations, notifications, and approval flows.
Its `docs/components/DISPATCH.md` owns event routing and application-specific internal payloads. Its `docs/components/DISCORD.md` and `SLACK.md` own delivery tools and conversation behavior.
The Host owns [Discord intake](components/DISCORD.md), [Slack intake](components/SLACK.md), and
[session/channel persistence](components/SESSION.md).

## Development and reviews

A component repository can define issue investigation, implementation, independent review, and PR-event processing in:

- `docs/components/ISSUE_WORKFLOW.md`
- `docs/components/IMPLEMENTATION.md`
- `docs/components/CODING_SDKS.md`
- `docs/components/PR_REVIEW.md`
- `docs/components/PR_WATCH.md`

The Host owns the [base tools](../src/trackeverything/tools/inventory.md), [session persistence](components/SESSION.md),
[execution records](LOGGING.md), and [HTTP serving of published pages](components/FASTAPI.md#public-pages-pr-reviews-documents-issue-investigation-reports).
Document creation, sanitization, publication, and notification are component responsibilities.

## Project documents

`docs/PROJECT_MEMORY.md` and `docs/components/WORK_DOCS.md` in the component repository own project materials,
conflicts, links, and the `docs_*` tools. `DOCS_DIR` is application data, separate from this repository's `docs/`.
The Host retains shared storage models and APIs for components; their application policies belong to the component.

The generic `data/role.example.md` and `data/repo-catalog.example.md` templates support components that read recipient
profiles and repository catalogs. Copy them to their local paths only when those components are installed; the default
agents do not use them. Generated-document evaluations belong with those components, including
`tests/browser/document_smoke.md`.

## Scheduled components

The Host [scheduler](components/SCHEDULING.md) publishes rows from `schedule` and `reservation`; it does not seed
application jobs. Only schedule agents declared by the installed dispatch.
Component `docs/components/SCHEDULING.md` owns reservation creation/consumption and application job examples;
`docs/components/SENTRY_AUTOFIX.md` owns Sentry selection, fixes, verification, and publication.
The [optimizer](components/OPTIMIZER.md) is Host-owned and has a bundled entry agent.

## Configuration

[Configuration](CONFIGURATION.md) documents Host settings, including compatibility settings exposed to components.
Each component repository documents which of those settings its version uses. Setting a component-specific variable
does not install the corresponding agent or tool. Keep installation-specific schedules, paths, and credentials in local
configuration and `AGENTS.local.md`.
