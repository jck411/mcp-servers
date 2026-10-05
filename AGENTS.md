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
- Commit and push verified changes before authorized deployment.
  `deploy/deploy.sh` supports only explicit read-only CT117 dry-run/preflight;
  it has no apply mode. Follow README.md's scoped source-rollout gates before
  a separately authorized live deployment. Existing dirty/private guest state
  blocks rollout and must be preserved; bootstrap remains disabled.

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
