# mcp-servers

This repo owns standalone MCP servers and deploy scripts. The old Knowledge
MCP/API/database stack has been decommissioned and must not be recreated here
unless Jack explicitly asks for a new replacement system.

## Repo Rules

- Work on `main`; `deploy/deploy.sh` resets live tracked code to `origin/main`.
- Use `uv` for Python dependency and test commands.
- Keep MCP tools explicit and unsurprising; avoid hidden side effects.
- Do not edit live `/opt/mcp-servers` as source of truth except for emergency
  hotfixes.
- Commit the matching repo change immediately after any live hotfix.
- For normal verified changes to deployable service code, commit and push
  `main`, then run `deploy/deploy.sh --tunnel ...` when working remotely or
  `deploy/deploy.sh ...` on the LAN.

## Decommissioned Knowledge Stack

- Do not add back `servers/knowledge`, `servers/knowledge_admin`,
  `servers/knowledge_api.py`, Knowledge systemd units, wiki/maintenance jobs,
  Qdrant collection management, or Knowledge tests.
- CT 110 keeps only currently configured non-Knowledge MCP services.
- If a task mentions old Knowledge behavior, verify the target before writing
  code; this repository should not silently restore the retired stack.

## Deploy Checks

- Run focused tests for changed behavior before deploy.
- Compile changed service files when touching runtime code.
- After deploy, check the changed `mcp-server@...` units and backend tool
  discovery when relevant.
- Before the final response, explicitly verify whether the change required
  commit, push, deploy, and live checks.
