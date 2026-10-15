# Repo catalog

> Optional component template. The Host defaults do not consume this file; use it only with components that document this setting.

Copy this file to `data/repo-catalog.md` (or point `REPO_CATALOG_PATH` at your
own file). One line per repository: `<owner>/<name> — what it covers`. The
investigator agent picks repo(s) from this list when a question omits "repo";
edit freely, the next run reads the file fresh. A missing file means no
catalog: an omitted repo then ends the run with a one-line ask instead of a
guess.

- example-org/example-api — main API (Python, Flask): app API endpoints, business logic, data models
- example-org/example-worker — event processing (Python): queue consumers and scheduled jobs
- example-org/example-web — main user web app (TypeScript, Vue)
- example-org/example-infrastructure — IaC (Terraform, GitHub Actions): cloud resources and dev/stg/prod environments
