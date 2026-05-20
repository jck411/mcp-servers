# mcp-servers

This repo owns standalone MCP servers, the Knowledge REST API, and deploy
scripts. Shared workflow rules live in `/home/jack/REPOS/AGENTS.md`.

## Repo Rules

- Work on `main`; `deploy/deploy.sh` resets live tracked code to `origin/main`.
- Use `uv` for Python dependency/test commands.
- Keep MCP tools explicit and unsurprising; avoid hidden side effects.
- Do not edit live `/opt/mcp-servers` as source of truth except for emergency hotfixes.
- Commit the matching repo change immediately after any live hotfix.

## Knowledge Deploy Checks

- Run focused tests for changed behavior before deploy.
- Compile changed service files when touching runtime code.
- After deploy, check `mcp-server@knowledge`, `mcp-knowledge-api.service`, and relevant timers.
- Back up `knowledge.db` before schema/data migrations, source deletion, or bulk reindexing.
