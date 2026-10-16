# Re-applying the GSD setup to another project

1. Install once, globally: `npx @opengsd/gsd-core@latest --claude --global` (remove any old `get-shit-done-cc` copy first with `npx get-shit-done-cc@1.42.3 --uninstall --global` / `--local`; never keep both).
2. Copy `.planning/config.json` from this repo (budget profile, opus only for `gsd-planner`, research/plan-check/verifier/code-review off, gates off, `auto_prune_state` + `compact_content` on) and run `node ~/.claude/gsd-core/bin/gsd-tools.cjs validate health`.
3. Copy the "GSD usage", "MCP" and "Response style" sections of `CLAUDE.md`; rewrite the "Project" section for the new repo.
4. In `.claude/settings.local.json`, disable plugins and MCP servers the repo does not need (`enabledPlugins: {"<plugin>": false}`, `enabledMcpjsonServers: [...]`); here superpowers, github and context7 are off. Trim GSD's skill surface once, globally: `/gsd-surface disable research_ideate,ui,ai_eval,docs,ns_meta,milestone`.
5. Add `.planning/`, `.planning.bak-*/`, `.claude/` and `.mcp.json` to `.gitignore`, then `git rm -r --cached .planning/` if it was ever tracked.
