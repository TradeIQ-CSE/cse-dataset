# Contributing

Git work for this project is tracked in Linear. Use the Linear project for issue
tracking, GitHub for version control, and the Linear–GitHub integration for
automatic branch, PR, and issue linking.

## Quick Reference

| Item | Pattern | Example |
| --- | --- | --- |
| Branch | `<type>/<LINEAR-ID>-<short-description>` | `feature/NIM-16-backfill-audit` |
| Commit | `<type>: <imperative description>` | `fix: correct OHLC bounds validation` |
| Pull request | `<LINEAR-ID>: <description>` | `NIM-16: Add historical backfill audit` |
| No known ticket | `<type>/<short-description>` (no key) | `chore/update-readme-typo` |

## How Linear Tracks Work

Linear links branches and PRs to issues automatically via the branch name. As
long as the Linear issue ID (e.g. `NIM-5`) appears in the branch name, Linear
attaches the branch and any PR opened from it to that issue — no special commit
message syntax is required.

When you start work from a Linear issue, copy the suggested branch name Linear
generates (e.g. `kulatungakmnsb03/nim-5-write-project-proposal`) or create your
own branch following the convention below.

## When There Is No Linear Ticket

The Linear ID is only required when the work is tied to a real, existing ticket.

**Never invent or guess an ID.** If no issue number has been provided, omit the
key entirely rather than making one up.

Rules:
- If you know the real issue ID, use it.
- If you do not know the ID, check the Linear board before committing.
- If the work genuinely has no associated ticket (a quick typo fix, a dependency
  bump, a one-line doc correction), commit without an ID. Use the plain pattern:
  `<type>/<short-description>` for the branch and `<type>: <description>` for
  the commit.
- Do not default to the most recently seen ID or any other guess.

## Branch Naming Convention

Pattern:
```text
<type>/<LINEAR-ID>-<short-description>
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
- Include the Linear ID (e.g. `NIM-16`) only if you actually know it.
- Use lowercase words and hyphens in the description.
- Do not use underscores, spaces, personal names, or temporary labels.

Good examples:
- `feature/NIM-16-backfill-audit`
- `bugfix/NIM-24-ohlc-null-rows`
- `refactor/NIM-42-backtesting-engine-runner`
- `docs/NIM-48-data-validation-rules`
- `chore/update-readme-typo` (no ticket, and that's fine)

Bad examples to avoid:
- `fix-stuff`
- `johns-branch`
- `temp`
- `feature/NIM-16_backfill_audit`
- `bugfix/NIM 24 ohlc null rows`

## Commit Message Convention

Pattern:
```text
<type>: <imperative description>
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
- Use one of the allowed commit types.
- Write the subject in imperative mood, as if completing "This commit will ...".
- Keep the subject line under about 50 characters where possible.
- No Linear ID needed in the commit message — the branch name handles linking.

Use:
- `feat: add historical backfill audit script`
- `fix: correct OHLC bounds validation for 2023 data`
- `test: add backend service smoke checks`
- `docs: document portfolio analytics inputs`
- `chore: bump pandas to 2.x`

Avoid:
- `fixed historical backfill audit`
- `fix validation stuff`
- `update files`
- `WIP`

## Pull Request Convention

PR title pattern:
```text
<LINEAR-ID>: <description>
```

Including the Linear ID in the PR title ensures the PR appears on the Linear
issue automatically. Only include it when the PR is genuinely tied to that issue.

PR descriptions should briefly state:
- What changed.
- Why it changed.
- Any test or validation performed.
- A link back to the Linear issue, if one applies.

Example PR title:
```text
NIM-16: Add historical backfill audit
```

Example Linear issue link:
```text
https://linear.app/nimeshk-personal/issue/NIM-16
```

Example PR description:
```markdown
## Summary
- Adds an audit script for Data Pipeline backfill runs.
- Records validation failures for missing OHLC rows.
- Updates docs for the Data Validation workflow.

## Linear
https://linear.app/nimeshk-personal/issue/NIM-16

## Validation
- `uv run python -m unittest discover -s tests`
```

## Branch Naming, Commits & PRs

This section reflects the conventions as confirmed via the Linear API. Use **TIQ** as the team
key throughout (not NIM or any other prefix).

### Branch naming

Linear generates a branch name for every issue. Always use it verbatim.

Format: `username/identifier-title`

```text
nimeshk03/tiq-23-backfill-ohlcv-gaps
```

- Copy the exact string from the Linear issue using **Copy git branch name**
  (Ctrl+Shift+. on Windows/Linux, Cmd+Shift+. on Mac) rather than typing it by hand. Small
  deviations silently break the Linear↔GitHub link — no error is shown.
- If a piece of work has no Linear issue yet, create the issue first — don't branch ahead of it.

### Commit messages

Follow [Conventional Commits](https://www.conventionalcommits.org/), consistent with existing
repo history:

```text
<type>: <short description>
```

Types in use: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`

Example from this repo:

```text
feat: add OHLCV coverage reporting
```

To link a commit directly to a Linear issue (optional — the branch-name link already covers
auto-linking):

- `Fixes TIQ-23` — use only when the commit fully resolves the issue; this auto-closes it on
  merge to main.
- `Part of TIQ-23` — use for incremental work that does not fully resolve the issue.

### Pull request titles

PR titles also follow Conventional Commits, because squash-merge uses the PR title as the final
commit message on main. Include the Linear issue ID for extra context:

```text
feat: add OHLCV coverage reporting (TIQ-23)
```

## Definition of Done

A ticket is done only after the full Git and Linear flow is complete:

```text
PR opened -> reviewed and approved by Nimesh -> CI checks passing -> merged
```

The person who merges the PR is responsible for moving the matching Linear issue
to Done.

No ticket should be closed without going through this flow.

This applies across the project epics, including Data Pipeline, Data Validation,
Web App, Backend Services, Backtesting Engine, Admin Dashboard, Paper-Trading
Engine, Portfolio Analytics, AI/Analytics Service, and DB & Infra.
