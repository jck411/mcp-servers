# mcp-servers

This repo owns standalone MCP servers and deploy scripts. The old Knowledge
MCP/API/database stack has been decommissioned and must not be recreated here
unless Jack explicitly asks for a new replacement system.

## Repo Rules

- Work on `main`; follow the shared workflow in `../PROXMOX/AGENTS.md`.
- Use `uv` for Python dependency and test commands.
- Keep MCP tools explicit and unsurprising; avoid hidden side effects.
- Use this repository as source of truth; see README.md for the verified live
  target and preserve its private state and uncommitted work.
- Commit the matching repo change immediately after any live hotfix.
- Commit and push verified changes before authorized deployment. The legacy
  `deploy/deploy.sh` is not a deployment procedure for the current workload;
  do not run it until its target and destructive behavior are reconciled through
  a reviewed, scoped deployment plan. See README.md for the boundary.

## Decommissioned Knowledge Stack

- Do not add back `servers/knowledge`, `servers/knowledge_admin`,
  `servers/knowledge_api.py`, Knowledge systemd units, wiki/maintenance jobs,
  Qdrant collection management, or Knowledge tests.
- Preserve only the verified current non-Knowledge workload; do not recreate
  retired guests or services to match historical deployment labels.
- If a task mentions old Knowledge behavior, verify the target before writing
  code; this repository should not silently restore the retired stack.

## Deploy Checks

- Run focused tests for changed behavior before deploy.
- Compile changed service files when touching runtime code.
- After deploy, check the changed `mcp-server@...` units and backend tool
  discovery when relevant.
- Before the final response, explicitly verify whether the change required
  commit, push, deploy, and live checks.
