# Copilot Instructions — mcp-servers

Standalone MCP servers deployed to Proxmox LXC (CT 110, 192.168.1.110) via systemd template units.

## Architecture

- Each server is a single file in `servers/<name>.py` using FastMCP
- Shared helpers live in `shared/` — auth modules, identifier normalizers, etc.
- Zero imports from Backend_FastAPI — every server must be fully standalone
- Servers run via `python -m servers.<name> --transport streamable-http --host 0.0.0.0 --port <PORT>`
- The FastMCP instance must be named `mcp = FastMCP("<name>")`
- Every server exposes `run()` and `main()` entrypoints plus `DEFAULT_HTTP_PORT`

## Adding a New Server

- Create `servers/<name>.py` following the pattern in existing servers
- Prefix all tool names with the server name: `@mcp.tool("<name>_do_thing")`
- Add a `[project.optional-dependencies]` entry in `pyproject.toml` if the server needs extra packages
- Add the server to the `all` extra group
- Add port to `PORT_MAP` in `deploy/setup-systemd.sh`
- Add server name to `DEFAULT_SERVERS` in both `deploy/setup-systemd.sh` and `deploy/deploy.sh`
- Add `.env.spotify`-style port reference to `.env.example` comments

## Port Assignments

- Ports are assigned in `deploy/setup-systemd.sh` PORT_MAP (9001–9012+)
- Per-instance env files `.env.<name>` contain `MCP_PORT=<port>`
- Never reuse an existing port; pick the next available

## Deployment

- Target: Proxmox LXC CT 110 at 192.168.1.110, repo at `/opt/mcp-servers`
- SSH key auth configured for both `root` and `mcp` users
- Service user is `mcp`; all runtime files must be owned by `mcp:mcp`
- Package manager is `uv` — never use pip directly
- Use `uv sync --extra <name>` for selective installs; `--extra all` pulls heavy deps (torch)
- Systemd template: `mcp-server@.service` — enable with `systemctl enable --now mcp-server@<name>`
- Credentials go in `credentials/`, tokens in `data/tokens/` — both are gitignored
- After code push, deploy via SSH: `git pull`, `uv sync`, copy credentials, restart service

## Credentials & Secrets

- NEVER commit credentials, tokens, or `.env` files — `.gitignore` covers `credentials/*.json`, `data/`, `.env`, `.env.*`
- `.env.example` is the only tracked env file
- Auth helpers in `shared/` resolve paths relative to repo root via `Path(__file__).resolve().parent.parent`
- Credential files are SCP'd to the container separately from git

## Code Style

- Python ≥3.11; use `from __future__ import annotations`
- Ruff for linting: line-length 100, rules E/F/W/I/UP/B/SIM
- Use `async def` for all MCP tool functions
- Type hints on all function signatures
- Use imperative docstrings: "Search tracks" not "This function searches tracks"

## Scripts

- Use `#!/usr/bin/env bash` and `set -euo pipefail` for all shell scripts
- Deploy scripts live in `deploy/`

## Testing

- Tests in `tests/` using pytest with `asyncio_mode = "auto"`
- Install dev deps: `uv sync --extra dev`
