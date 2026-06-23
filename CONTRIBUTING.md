# Contributing
Git work for this project is tied to Jira issue tracking. Use Jira project key
`SCRUM`, GitHub for version control, and the GitHub for Jira app for automatic
branch, commit, and pull request linking.
## Quick Reference
| Item | Pattern | Example |
| --- | --- | --- |
| Branch | `<type>/<JIRA-KEY>-<short-description>` | `feature/SCRUM-16-backfill-audit` |
| Commit | `<JIRA-KEY> <type>: <imperative description>` | `SCRUM-24 fix: correct OHLC bounds validation` |
| Pull request | `<JIRA-KEY>: <description>` | `SCRUM-16: Add historical backfill audit` |
| No known ticket | `<type>/<short-description>` (no key) | `chore/update-readme-typo` |
## When There Is No Jira Ticket
The Jira key is only required when the work is tied to a real, existing ticket
that has actually been given to you for this task.
**Never invent or guess a Jira key.** If no ticket number has been provided,
or you are unsure which ticket applies, omit the key entirely rather than
making one up. A fabricated key (e.g. writing `SCRUM-16` because it looks
plausible, when the real ticket is unknown or does not exist) is worse than no
key at all: GitHub for Jira will silently attach the commit, branch, or PR to
whatever real ticket that key belongs to, polluting its Development panel with
unrelated work.
Rules:
- If you know the real ticket key, use it.
- If you do not know the ticket key, ask, or check the board, before
  committing.
- If the work genuinely has no associated ticket (a quick typo fix, a
  dependency bump, a one-line doc correction), commit without a Jira key.
  Use the plain pattern: `<type>/<short-description>` for the branch, and
  `<type>: <imperative description>` for the commit, with no key prefix.
- Do not default to the most recently seen ticket number, the lowest unused
  number, or any other guess. Absence of a known key means no key.
Not every commit needs a ticket reference. Small documentation fixes,
dependency updates, and tiny one-off fixes do not warrant the overhead of
creating or referencing a ticket just to satisfy this convention.
## Branch Naming Convention
Pattern:
```text
<type>/<JIRA-KEY>-<short-description>
```
Allowed branch types:
- `feature/`
- `bugfix/`
- `hotfix/`
- `refactor/`
- `chore/`
- `docs/`
- `test/`
Rules:
- Start every branch with one of the allowed types.
- Include the Jira key, such as `SCRUM-16`, only if you actually know it (see
  "When There Is No Jira Ticket" above).
- Use lowercase words and hyphens in the description.
- Do not use underscores, spaces, personal names, or temporary labels.
Good examples:
- `feature/SCRUM-16-backfill-audit`
- `bugfix/SCRUM-24-ohlc-null-rows`
- `feature/SCRUM-31-admin-dashboard-filters`
- `refactor/SCRUM-42-backtesting-engine-runner`
- `docs/SCRUM-48-data-validation-rules`
- `chore/update-readme-typo` (no ticket, and that's fine)
Bad examples to avoid:
- `fix-stuff`
- `johns-branch`
- `temp`
- `feature/SCRUM-16_backfill_audit`
- `bugfix/SCRUM 24 ohlc null rows`
- `feature/SCRUM-99-something` (a key that was guessed or made up)
## Commit Message Convention
Pattern:
```text
<JIRA-KEY> <type>: <imperative description>
```
Allowed commit types:
- `feat:`
- `fix:`
- `refactor:`
- `perf:`
- `style:`
- `chore:`
- `test:`
- `docs:`
Rules:
- Start with the Jira key only if you actually know it. If there is no known
  ticket, drop the key and start directly with the type, e.g.
  `chore: fix typo in README`.
- Use one of the allowed commit types.
- Write the subject in imperative mood, as if completing the sentence
  "This commit will ...".
- Keep the subject line under about 50 characters where possible.
Use:
- `SCRUM-16 feat: add historical backfill audit script`
- `SCRUM-24 fix: correct OHLC bounds validation for 2023 data`
- `SCRUM-35 test: add backend service smoke checks`
- `SCRUM-44 docs: document portfolio analytics inputs`
- `chore: bump pandas to 2.x` (no ticket, and that's fine)
Avoid:
- `SCRUM-16 fixed historical backfill audit`
- `SCRUM-24 fix validation stuff`
- `update files`
- `WIP`
- `SCRUM-16 chore: bump pandas` when SCRUM-16 has nothing to do with this
  change and was only added because it "looked right"
## Pull Request Convention
PR title pattern:
```text
<JIRA-KEY>: <description>
```
The Jira key in the PR title is what makes the pull request appear in the Jira
ticket's Development panel automatically through the GitHub for Jira app. Only
include it when the PR is genuinely tied to that ticket.
PR descriptions should briefly state:
- What changed.
- Why it changed.
- Any test or validation performed.
- A link back to the Jira ticket, if one applies.
Example PR title:
```text
SCRUM-16: Add historical backfill audit
```
Example Jira ticket link:
```text
https://kulatungakmnsb03.atlassian.net/browse/SCRUM-16
```
Example PR description:
```markdown
## Summary
- Adds an audit script for Data Pipeline backfill runs.
- Records validation failures for missing OHLC rows.
- Updates docs for the Data Validation workflow.
## Jira
https://kulatungakmnsb03.atlassian.net/browse/SCRUM-16
## Validation
- `uv run python -m unittest discover -s tests`
```
## Smart Commits
Smart Commits are optional and mainly for advanced use. They let a commit add a
comment or log time directly on the linked Jira ticket.
Syntax:
```text
<JIRA-KEY> #comment <text> #time <duration>
```
Example:
```text
SCRUM-24 #comment Fixed OHLC null-row handling #time 1h 30m
```
Use Smart Commits only when the commit message is still clear and useful in Git
history, and only when the Jira key is real. Normal commit messages are enough
for most work.
## Definition of Done
For this 3-person team, a ticket is done only after the Git and Jira flow is
complete:
```text
PR opened -> reviewed and approved by Nimesh -> CI checks passing -> merged
```
The person who merges the PR is responsible for moving the matching Jira ticket
to Done.
No ticket should be closed without going through this flow.
This applies across the project epics, including Data Pipeline, Data
Validation, Web App, Backend Services, Backtesting Engine, Admin Dashboard,
Paper-Trading Engine, Portfolio Analytics, AI/Analytics Service, and DB &
Infra.
