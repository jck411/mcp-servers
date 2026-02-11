# Copilot Instructions — mcp-servers

Standalone MCP servers deployed to Proxmox LXC (CT 110, 192.168.1.110) via systemd.

## Related Repos

| Repo | Location | Purpose |
|------|----------|--------|
| Backend_FastAPI | LXC 111 (192.168.1.111:8000) | Chat backend, auto-discovers these servers |
| PROXMOX | Host (192.168.1.11) | Infrastructure, LXC definitions |

- Backend auto-discovers servers on ports 9001–9015 via `/api/mcp/servers/refresh`
- Housekeeping server uses Qdrant at 192.168.1.110:6333 for vector search
- Memory backup logs written by Backend_FastAPI, not MCP servers

## Local Development Workflow

- Edit servers directly in this repo — it is the source of truth for all MCP server code
- Run locally to test: `python -m servers.<name> --transport streamable-http --host 127.0.0.1 --port <port>`
- Ctrl+C and rerun after edits (~1-second feedback loop)
- Auto-reload with watchfiles: `watchfiles "python -m servers.<name> --transport streamable-http --host 127.0.0.1 --port <port>" servers/ shared/`
- Smoke-test tools via curl: `curl -s http://127.0.0.1:<port>/mcp -X POST -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'`
- Only deploy to LXC once local testing passes — never iterate on the LXC directly

## Architecture

- Each server is a single file in `servers/<name>.py` using FastMCP
- Shared helpers live in `shared/` (auth modules, normalizers, etc.)
- Zero imports from Backend_FastAPI — every server must be fully standalone
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

### 3. Update deploy scripts

- Add port to `PORT_MAP` in `deploy/setup-systemd.sh` (pick next available)
- Add server name to `DEFAULT_SERVERS` in both `deploy/setup-systemd.sh` and `deploy/deploy.sh`

### 4. Install and verify locally

- `uv sync --extra <name>` in the local venv
- `python -c "from servers.<name> import mcp, DEFAULT_HTTP_PORT, run, main"` — must succeed
- Verify no `from backend` or `import backend` in the source

### 5. Commit and push

- `git add -A && git commit -m "<descriptive message>" && git push origin master`

### 6. Deploy to LXC

Run these commands in sequence (no questions, no pauses):
```
ssh root@192.168.1.110 "cd /opt/mcp-servers && git pull --ff-only && uv sync --extra <name>"
```

If the server needs credential files (e.g., Google OAuth):
```
scp <local_cred_files> root@192.168.1.110:/opt/mcp-servers/credentials/
scp <local_token_files> root@192.168.1.110:/opt/mcp-servers/data/tokens/
ssh root@192.168.1.110 "chown -R mcp:mcp /opt/mcp-servers/credentials/ /opt/mcp-servers/data/"
```

Start the systemd service:
```
ssh root@192.168.1.110 "bash /opt/mcp-servers/deploy/setup-systemd.sh <name>"
```

### 7. Trigger backend discovery

The Backend_FastAPI auto-discovers servers on ports 9001–9015 when refreshed:
```
curl -sk -X POST https://127.0.0.1:8000/api/mcp/servers/refresh -H "Content-Type: application/json" -H "Accept: application/json"
```
Verify the server appears with `connected: true` and correct tool count in the response.

### 8. Confirm done

Report: server name, port, tool count, connected status. One-liner, no summary doc.

## Port Assignments

Defined in `deploy/setup-systemd.sh` PORT_MAP. Never reuse a port.

| Server | Port |
|--------|------|
| shell_control | 9001 |
| housekeeping | 9002 |
| calculator | 9003 |
| calendar | 9004 |
| gmail | 9005 |
| gdrive | 9006 |
| pdf | 9007 |
| monarch | 9008 |
| notes | 9009 |
| spotify | 9010 |
| playwright | 9011 |
| kiosk_clock_tools | 9012 |

Next available: 9013

## Credential Sources

When porting a server that needs credentials from Backend_FastAPI:
- Google OAuth client secret: `/home/human/REPOS/Backend_FastAPI/credentials/client_secret_pihome123.json`
- Google OAuth token: `/home/human/REPOS/Backend_FastAPI/data/tokens/jck411_at_gmail_com.json`
- Spotify creds: already in this repo's `credentials/` and `data/tokens/`
- Copy to this repo locally AND scp to LXC — both are gitignored

## Code Style

- Python ≥3.11; `from __future__ import annotations`
- Ruff: line-length 100, rules E/F/W/I/UP/B/SIM
- `async def` for all MCP tool functions
- Type hints on all signatures
- Imperative docstrings: "Search tracks" not "This function searches tracks"

## Deployment Target

- LXC CT 110 at 192.168.1.110, repo at `/opt/mcp-servers`
- SSH key auth for `root` and `mcp` users
- Service user: `mcp` — all runtime files owned by `mcp:mcp`
- Package manager: `uv` — never pip
- Systemd template: `mcp-server@.service`
- NEVER commit credentials, tokens, or `.env` files
