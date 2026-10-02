# mcp-servers

Standalone MCP servers deployed to Proxmox LXCs with systemd.

The former Knowledge MCP/API/database stack has been retired. This repository
keeps only the active non-Knowledge MCP services.

## Layout

```text
servers/          MCP server modules
shared/           Shared helpers
deploy/           Systemd and deploy scripts
tests/            Focused service tests
```

## Current deployment: CT 117

Read-only verification on **October 2, 2026** established that this repository
owns the account/home service code on **CT117 (`mcp-accounts`)**, at
`/opt/mcp-accounts`, origin `https://github.com/jck411/mcp-servers.git`.
The live HEAD and fetched `origin/main` both identified
`c2fe6b0b04c19516081f0678b2ea6af914fff646` at inspection. This is a deployment
checkpoint, not a version pin for future changes.

| Server | Port | Active systemd unit |
|---|---:|---|
| `calendar` | 9004 | `mcp-server@calendar` |
| `gmail` | 9005 | `mcp-server@gmail` |
| `gdrive` | 9006 | `mcp-server@gdrive` |
| `monarch` | 9008 | `mcp-server@monarch` |
| `spotify` | 9010 | `mcp-server@spotify` |
| `tv` | 9013 | `mcp-server@tv` |
| `hue` | 9015 | `mcp-server@hue` |

Selected live process arguments verified `servers.<name>`, streamable HTTP,
`0.0.0.0` and each listed port; all seven listeners and units were present.
`hue-event-collector.service` was also active. The template and collector run as
`mcp:mcp` with working directory `/opt/mcp-accounts`; live units are in
`/etc/systemd/system/`. Service-active/listener evidence does not prove provider
authentication, tool discovery or household-account functionality; no MCP action
or private-content read was performed.

The live checkout is **not clean**: tracked `README.md` and `AGENTS.md` edits
remove retirement warnings; untracked `.bash_logout`, `.bashrc`, `.profile` and
`.local/` also exist. Preserve and reconcile these independently; they do not
re-authorize the retired Knowledge stack. No live file was changed by this review.
Private environment, provider authentication and runtime state remain on the
guest, outside Git; they are not replaceable from an upstream checkout alone.

## Legacy CT 110 target — not present

CT110 was absent from the live Proxmox inventory. The previous README's
`web_search`/9016 target and `deploy/deploy.sh` still refer to CT110 and
`/opt/mcp-servers`; **they are not the deployment procedure for CT117**.
Do not recreate CT110, rename CT117 or repoint this script merely to make those
labels agree. Source ownership is now verified; a reviewed CT117 deployment
procedure remains to be implemented before the next service-code deployment.

## Local Setup

```bash
uv sync --extra all --extra dev
uv run pytest
```

## Deployment boundary and safe inspection

**Do not run `deploy/deploy.sh` for the current CT117 workload.** The inspected
script automatically commits/pushes local changes (unless `--no-push`), fetches
and hard-resets the CT110 checkout, rewrites per-service port files, kills port
holders, restarts units and refreshes backend discovery. `--no-push` does not
make it read-only. Its `--status` targets the absent CT110 too.

Use existing Proxmox access for read-only CT117 inspection, for example:

```bash
ssh proxmox 'pct exec 117 -- systemctl is-active mcp-server@calendar hue-event-collector'
ssh proxmox 'pct exec 117 -- git -C /opt/mcp-accounts status --short'
```

Before a separately approved deployment, inventory the installed template and
instance overrides without printing credentials, preserve private runtime state
and dirty work, and define an explicit service-to-port map and scoped restart /
rollback plan. Compare source revision, changed files, active units and selected
consumer discovery after deployment. Do not use a hard reset to resolve ownership.
The October 2 work corrected documentation only: no deploy, restart, push or
live checkout reconciliation was performed.

## Retired Knowledge Stack

Do not restore the old `servers/knowledge`, `servers/knowledge_admin`,
`servers/knowledge_api.py`, Knowledge systemd units, maintenance/wiki backup
jobs, SQLite database, or Knowledge Qdrant collections unless Jack asks for a
new replacement system.
