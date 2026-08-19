# mcp-servers

This repo owns standalone MCP server source used by the account/home services
on LXC 117. Deployment orchestration lives in the NETWORK repository.

## Repo Rules

- Work on `main`; the LXC 117 deployment resets live tracked code to
  `origin/main`.
- Use `uv` for Python dependency and test commands.
- Keep MCP tools explicit and unsurprising; avoid hidden side effects.
- Do not edit live `/opt/mcp-accounts` as source of truth except for emergency
  hotfixes.
- Commit the matching repo change immediately after any live hotfix.
- For normal verified changes, commit and push `main`, then use the
  `mcp-accounts` entry in `NETWORK/deploy/registry.yml`.

## Deploy Checks

- Run focused tests for changed behavior before deploy.
- Compile changed service files when touching runtime code.
- After deploy, check the changed `mcp-server@...` units and backend tool
  discovery when relevant.
- Before the final response, explicitly verify whether the change required
  commit, push, deploy, and live checks.
