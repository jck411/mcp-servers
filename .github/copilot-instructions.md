# Copilot Instructions — mcp-servers

Standalone MCP servers deployed to Proxmox LXC (CT 110, 192.168.1.110) via systemd.

## Related Repos

| Repo | Location | Purpose |
|------|----------|--------|
| Backend_FastAPI | LXC 111 (192.168.1.111:8000) | Chat backend, auto-discovers these servers |
| opencode-config | LXC 114 (192.168.1.114:4096) | OpenCode config; needs explicit `add-mcp-server.sh` after a new server ships |
| PROXMOX | Host (192.168.1.11) | Infrastructure, LXC definitions |

- Backend auto-discovers servers on ports 9001–9017 via `/api/mcp/servers/refresh`
- OpenCode does NOT auto-discover — use the `opencode-config` repo's `scripts/add-mcp-server.sh`
- Knowledge server uses Qdrant at 192.168.1.110:6333 for vector search
- Memory backup logs written by Backend_FastAPI, not MCP servers

## Local Development Workflow

- Edit servers directly in this repo — it is the source of truth for all MCP server code
- **Use `./dev.sh`** to launch servers locally with auto-reload (watchfiles watches `servers/` and `shared/`)
  - `./dev.sh` — interactive menu to pick servers
  - `./dev.sh spotify` — launch a single server
  - `./dev.sh spotify calculator` — launch multiple servers
  - `./dev.sh --list` — show available servers and ports
- Servers run on `127.0.0.1` at their assigned port with hot reload — code changes take effect automatically
- **After adding/removing tools:** the backend must re-fetch the tool list. Tell the user to click **Refresh** in Settings → Server status, or run:
  ```
  curl -sk -X POST https://127.0.0.1:8000/api/mcp/servers/refresh -H "Content-Type: application/json" -H "Accept: application/json"
  ```
- Smoke-test tools via curl: `curl -s http://127.0.0.1:<port>/mcp -X POST -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'`
- Only deploy to LXC once local testing passes — never iterate on the LXC directly
- The backend runs locally on HTTPS at `https://127.0.0.1:8000` (use `curl -sk` to skip cert verification)

### Adding/modifying tools on an existing server

When adding new tools to an existing server during local dev:
1. Edit the server file and add the tool (add to `__all__` too)
2. Verify import: `python -c "from servers.<name> import <new_func>; print('OK')"`
3. `dev.sh` auto-reloads — no restart needed
4. **Refresh the backend** so it picks up the new tool list (UI refresh button or curl)
5. Verify tool appears: check the refresh response for the new tool name
6. Commit and push only after confirming the tool works end-to-end

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
- If `dev.sh` is running, it auto-reloads. Otherwise start it: `./dev.sh <name>`
- **Refresh the backend** to pick up the new server's tools:
  ```
  curl -sk -X POST https://127.0.0.1:8000/api/mcp/servers/refresh -H "Content-Type: application/json" -H "Accept: application/json"
  ```
- Confirm the server appears with `connected: true` and expected tool count before proceeding

### 5. Commit and push

- `git add -A && git commit -m "<descriptive message>" && git push origin master`

### 6. Deploy to LXC

Use the deploy script (handles SSH detection, fallback to PVE console commands, and backend refresh):
```
./deploy/deploy.sh <name>
```

If direct SSH to 192.168.1.110 is unavailable (common), the script prints `pct exec` commands to paste into the Proxmox host console. Alternatively, deploy manually via PVE:
```
sshpass -p '97919791' ssh root@192.168.1.11 "pct exec 110 -- bash -c 'cd /opt/mcp-servers && git pull --ff-only && uv sync --extra all'"
sshpass -p '97919791' ssh root@192.168.1.11 "pct exec 110 -- bash /opt/mcp-servers/deploy/setup-systemd.sh <name>"
```

If the server needs credential files (e.g., Google OAuth), copy via the PVE host:
```
sshpass -p '97919791' scp <local_cred_files> root@192.168.1.11:/tmp/
sshpass -p '97919791' ssh root@192.168.1.11 "pct push 110 /tmp/<file> /opt/mcp-servers/credentials/<file>"
sshpass -p '97919791' ssh root@192.168.1.11 "pct exec 110 -- chown -R mcp:mcp /opt/mcp-servers/credentials/ /opt/mcp-servers/data/"
```

### 7. Trigger backend discovery

The Backend_FastAPI auto-discovers servers on ports 9001–9017 when refreshed:
```
curl -sk -X POST https://127.0.0.1:8000/api/mcp/servers/refresh -H "Content-Type: application/json" -H "Accept: application/json"
```
Verify the server appears with `connected: true` and correct tool count in the response.

### 8. Register with OpenCode (if the user wants OpenCode to see it)

OpenCode does NOT auto-discover. From the **opencode-config** repo:
```
cd ~/REPOS/opencode-config
./scripts/add-mcp-server.sh <name> <port>
./scripts/sync-to-lxc.sh
git commit -am "Add <name>" && git push
```
If the server needs an auth token, pass `--auth-env <VAR>` and add `<VAR>=<value>` to that repo's `.env` before syncing.

### 9. Confirm done

Report: server name, port, tool count, Backend connected status, OpenCode visibility. One-liner, no summary doc.

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

Note: Port 9012 was previously used by `kiosk_clock_tools` (removed). Do not reuse it.

Next available: 9018

## Credential Sources

When porting a server that needs credentials from Backend_FastAPI:
- Google OAuth client secret: `/home/jack/REPOS/Backend_FastAPI/credentials/client_secret_pihome123.json`
- Google OAuth token: `/home/jack/REPOS/Backend_FastAPI/data/tokens/jck411_at_gmail_com.json`
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
- **Direct SSH to 192.168.1.110 does NOT work** — always go via PVE host (192.168.1.11) using `pct exec 110 -- bash -c '...'`
- SSH to PVE host: `sshpass -p '97919791' ssh root@192.168.1.11`
- Service user: `mcp` — all runtime files owned by `mcp:mcp`
- Package manager: `uv` — never pip
- Systemd template: `mcp-server@.service`
- A `post-merge` git hook on LXC 110 auto-runs `uv sync --extra all` after every `git pull`
- `setup-systemd.sh` also runs `uv sync --extra all` before starting services
- NEVER commit credentials, tokens, or `.env` files
