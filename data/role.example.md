# Notification recipient profile

> Optional component template. The Host defaults do not consume this file; use it only with components that document this setting.

Copy this file to `data/role.md` (or point `ROLE_PATH` at your own file) and
describe the one person the `notify` summaries go to. The compare agent reads
the file at summarize time and uses it to judge relevance (an irrelevant change
is not notified), to weight the summary, and to pick the notify level. Editing
the file takes effect on the next run — no restart. An empty or missing file
disables filtering entirely (every real change notifies).

## Recipient

Backend engineer at Example Corp, working on the company's web product. Owns
the backend services, the API definitions, and the cloud infrastructure they
run on.

## Care about

- Backend services: example-org/example-api, example-org/example-worker
- API definitions, DB schema changes, deploys, infrastructure-as-code changes
- Production incidents, error spikes, anything that affects availability
- Backend PR state changes (merged, closed, review conclusions)

## Do NOT care about

- Frontend implementation details, UI/design discussions, marketing content
- Reaction flips, greetings, bot chatter, and other small talk
- Changes that only touch tooling or docs with no runtime effect
- Changes to my own reviewer assignment in a PR's `requested_reviewers`
  (login `your-github-login` added or removed). Being added means the
  dedicated review-request flow already reported it; being removed is the
  automatic consequence of my own review submission. Exclude my own entry
  silently — this exclusion is exact, not a judgment call. Other reviewers'
  additions and removals in the same diff are NOT covered by this rule: report
  them as usual, together with any other real changes.

## Priority

prod incidents > API/DB schema changes > backend PR state changes > general
backend discussion.
