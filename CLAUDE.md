# NarwalIntegration — Claude rules

## Project
- Home Assistant custom integration for Narwal vacuums; local WebSocket API on port 9002. Repo: sjmotew/NarwalIntegration.
- `narwal_client/` and `custom_components/narwal/narwal_client/` MUST stay identical (HACS needs the embedded copy).
- Use `py`, not `python`. Tests: `py -m pytest -q`. Use `gh` for GitHub; merge stacked PRs with `gh pr merge --merge` in order.
- Release discipline: CI green on the exact SHA → deploy to HA at 10.0.0.5 → verify live → tag. Never tag first.
- Tests cannot catch Hassfest manifest rules; check the Hassfest CI job, not just Tests.

## GSD usage
- Default to `/gsd-quick` for anything under ~3 files or a single bug. Use phases only for ROADMAP items.
- Research, plan-check, verifier and code-review are OFF in `.planning/config.json`. For risky work, re-enable for that phase only (`/gsd-plan-phase --research`, or set `workflow.verifier: true` then revert) or run `/gsd-verify-work` afterwards.
- `/clear` between discuss → plan → execute. GSD persists state in `.planning/`; the context window does not need to.
- Hand subagents file paths, not pasted contents. Never paste `.planning/` history into a prompt.
- `.planning/` is gitignored and untracked. Do not commit it.

## MCP
- The home-assistant MCP is for release verification only (entity counts, repairs, restart). Do not call it while coding.
- The github and context7 plugins are disabled for this repo in `.claude/settings.local.json`; use `gh`.

## Response style
- Answer first. No preamble, no restating the plan, no end-of-task recap.
- Show diffs or changed hunks, never full files, unless asked.
- One line per completed step. Tables only for 3+ parallel items.
- Do not re-read files already in context or re-run checks already green this session.
