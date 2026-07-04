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

## CT 110

| Server | Port | Unit |
|---|---:|---|
| `web_search` | 9016 | `mcp-server@web_search` |

Private/account/home-control services are managed separately on CT 117.

## Local Setup

```bash
uv sync --extra all --extra dev
uv run pytest
```

## Deploy

Remote deploy through the Proxmox tunnel:

```bash
./deploy/deploy.sh --tunnel web_search
```

Check live status:

```bash
./deploy/deploy.sh --tunnel --status
```

The deploy script commits and pushes local changes unless `--no-push` is
provided, resets `/opt/mcp-servers` to `origin/main`, restarts the requested
systemd units, and refreshes backend MCP discovery.

## Retired Knowledge Stack

Do not restore the old `servers/knowledge`, `servers/knowledge_admin`,
`servers/knowledge_api.py`, Knowledge systemd units, maintenance/wiki backup
jobs, SQLite database, or Knowledge Qdrant collections unless Jack asks for a
new replacement system.
