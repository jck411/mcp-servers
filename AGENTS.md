# mcp-servers

This repo is the source of truth for standalone MCP servers, the Knowledge REST
API, and deploy scripts. Knowledge services run on LXC 110; private/account/home
control services are deployed separately.

## Workflow

- Keep changes small, tested, and committed before deploy.
- Work on `master` for normal changes; `deploy/deploy.sh` resets live code to `origin/master`.
- Use `uv` for Python dependency and test commands.
- Prefer repo patterns over new abstractions.
- Remove obsolete code, scripts, ports, and docs after replacements are verified.
- Do not edit live `/opt/mcp-servers` as the source of truth except for emergency hotfixes; commit the same change here immediately after.

## Knowledge Deploy Checks

- Run focused tests for changed behavior before deploy.
- Compile changed service files when touching runtime code.
- After deploy, check `mcp-server@knowledge`, `mcp-knowledge-api.service`, and relevant timers.
- Back up `knowledge.db` before schema/data migrations, source deletion, or bulk reindexing.

## Style

- Python 3.11+ with type hints on tool functions.
- Keep MCP tools explicit and boring; avoid hidden side effects.
- Never commit credentials, `.env`, tokens, generated OAuth files, or raw personal data.
- Use Context7 for external library/API/framework details when needed; repo-local logic does not require external lookup.
