# Copilot Instructions — mcp-servers

Standalone MCP servers deployed to Proxmox LXC (CT 110, 192.168.1.110) via systemd.

## Related Repos

- [`jck411/PROXMOX`](https://github.com/jck411/PROXMOX) — host/LXC infrastructure (CT 110, 192.168.1.110)

Knowledge server uses Qdrant at `192.168.1.110:6333` for vector search.

## Local Development Workflow

- Edit servers directly in this repo — it is the source of truth for all MCP server code
- **Use `./dev.sh`** to launch servers locally with auto-reload (watchfiles watches `servers/` and `shared/`)
  - `./dev.sh` — interactive menu to pick servers
  - `./dev.sh spotify` — launch a single server
  - `./dev.sh spotify calculator` — launch multiple servers
  - `./dev.sh --list` — show available servers and ports
- Servers run on `127.0.0.1` at their assigned port with hot reload — code changes take effect automatically
- Smoke-test tools via curl: `curl -s http://127.0.0.1:<port>/mcp -X POST -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'`
- Only deploy to LXC once local testing passes — never iterate on the LXC directly

### Adding/modifying tools on an existing server

When adding new tools to an existing server during local dev:
1. Edit the server file and add the tool (add to `__all__` too)
2. Verify import: `python -c "from servers.<name> import <new_func>; print('OK')"`
3. `dev.sh` auto-reloads — no restart needed
4. Smoke-test: `curl -s http://127.0.0.1:<port>/mcp -X POST -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'`
5. Commit and push only after confirming the tool works end-to-end

## Architecture

- Each server is a single file in `servers/<name>.py` using FastMCP
- Shared helpers live in `shared/` (auth modules, normalizers, etc.)
- Zero imports from other projects — servers may only import from `shared/` and third-party packages
- `mcp = FastMCP("<name>")` — every server exposes `run()`, `main()`, `DEFAULT_HTTP_PORT`
- All tool names prefixed: `@mcp.tool("<name>_do_thing")`

## Adding a New Server — Do All Steps, No Questions

When the user asks to add/port a server, execute every step below without asking for confirmation. Read the source file, adapt it to be standalone, and deploy end-to-end.

### 1. Create the server file

- Create `servers/<name>.py` following the pattern in `servers/spotify.py` or `servers/calculator.py`
- Replace all Backend_FastAPI imports with standalone equivalents from `shared/`
- If `shared/` is missing a needed helper, implement it there first
- Remove any tools that depend on Backend_FastAPI services that can't be made standalone (e.g., AttachmentService)
- Keep `DEFAULT_HTTP_PORT` matching the port in PORT_MAP below

### 2. Update pyproject.toml

- Add `[project.optional-dependencies]` entry for the server if it needs extra packages
- Add the new extra to the `all` group

### 3. Update deploy scripts and instructions

- Add port to `PORT_MAP` in `deploy/setup-systemd.sh` (pick next available)
- Add server name to `DEFAULT_SERVERS` in `deploy/setup-systemd.sh` and `ALL_SERVERS` in `deploy/deploy.sh`
- Update the Port Assignments table in this file

### 4. Install and verify locally

- `uv sync --extra <name>` in the local venv
- `python -c "from servers.<name> import mcp, DEFAULT_HTTP_PORT, run, main"` — must succeed
- Verify no `from backend` or `import backend` in the source
- If `dev.sh` is running, it auto-reloads. Otherwise start it: `./dev.sh <name>`
- Smoke-test: `curl -s http://127.0.0.1:<port>/mcp -X POST -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'`
- Confirm the expected tools appear in the response before proceeding

### 5. Commit and push

- `git add -A && git commit -m "<descriptive message>" && git push origin master`

### 6. Deploy to LXC

Just run the deploy script — it auto-detects connectivity in order: direct SSH → Cloudflare tunnel → prints manual commands:
```
./deploy/deploy.sh <name>
```

The script tries `ssh proxmox-tunnel` (Cloudflare) automatically if direct SSH fails, so it works from anywhere without manual steps. Only falls back to printing `pct exec` commands if both SSH paths are unreachable.

If the server needs credential files (e.g., Google OAuth), copy via the tunnel:
```
ssh proxmox-tunnel 'pct exec 110 -- bash -c "mkdir -p /opt/mcp-servers/credentials"'
scp <local_cred_file> proxmox-tunnel:/tmp/
ssh proxmox-tunnel 'pct push 110 /tmp/<file> /opt/mcp-servers/credentials/<file> && pct exec 110 -- chown -R mcp:mcp /opt/mcp-servers/credentials/ /opt/mcp-servers/data/'
```

### 7. Confirm done

Report: server name, port, tool count reachable. One-liner, no summary doc.

---

## Process Improvement

If any step in this workflow is awkward, slow, or error-prone, propose a concrete edit to these instructions. Keep it short — one bullet per suggestion, no preamble.

## Port Assignments

Defined in `deploy/setup-systemd.sh` PORT_MAP. Never reuse a port.

| Server | Port |
|--------|------|
| shell_control | 9001 |
| calculator | 9003 |
| calendar | 9004 |
| gmail | 9005 |
| gdrive | 9006 |
| pdf | 9007 |
| monarch | 9008 |
| notes | 9009 |
| spotify | 9010 |
| playwright | 9011 |
| tv | 9013 |
| rag | 9014 |
| hue | 9015 |
| web_search | 9016 |
| knowledge | 9017 |
| knowledge_api | 9018 |

Retired ports — do not reuse: 9002, 9012

Next available: 9019

## Credential Sources

- Google OAuth client secret: `credentials/client_secret_pihome123.json`
- Google OAuth token: `data/tokens/jck411_at_gmail_com.json`
- Spotify creds: `credentials/` and `data/tokens/`
- All credential files are gitignored; secrets live in the universal symlinked `.env`

## Code Style

- Python ≥3.11; `from __future__ import annotations`
- Ruff: line-length 100, rules E/F/W/I/UP/B/SIM
- `async def` for all MCP tool functions
- Type hints on all signatures
- Imperative docstrings: "Search tracks" not "This function searches tracks"

## Deployment Target

- LXC CT 110 at 192.168.1.110, repo at `/opt/mcp-servers`
- **Direct SSH to 192.168.1.110 does NOT work** — use `ssh proxmox-tunnel` (Cloudflare) to reach PVE host, then `pct exec 110 -- bash -c '...'`
- `deploy.sh` auto-detects: tries direct SSH first, then `proxmox-tunnel`, then prints manual commands
- Service user: `mcp` — all runtime files owned by `mcp:mcp`
- Package manager: `uv` — never pip
- Systemd template: `mcp-server@.service`
- A `post-merge` git hook on LXC 110 auto-runs `uv sync --extra all` after every `git pull`
- `setup-systemd.sh` also runs `uv sync --extra all` before starting services
- NEVER commit credentials, tokens, or `.env` files
