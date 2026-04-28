# mcp-servers

Standalone MCP servers deployed to Proxmox via systemd. Zero imports from Backend\_FastAPI.

The backend (`jck411/Backend_FastAPI`) is a pure MCP client — it connects to these servers over HTTP, discovers tools via the MCP protocol, and routes tool calls from the LLM.

## Architecture

```
mcp-servers (this repo — deployed to Proxmox)
├── servers/          # One file per MCP server
├── shared/           # Auth helpers, utilities
├── deploy/           # Systemd templates + deploy script
├── credentials/      # Symlink to shared credential store
└── tests/
```

Each server:
- Is a standalone Python module using [FastMCP](https://github.com/jlowin/fastmcp)
- Runs via: `python -m servers.<name> --transport streamable-http --host 0.0.0.0 --port <PORT>`
- Self-describes via the MCP protocol (`list_tools()`)
- Has zero imports from Backend\_FastAPI

## Port Assignments

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

Next available port: **9018**. Port `9012` is retired (was `kiosk_clock_tools`).

All servers deployed to Proxmox LXC (CT 110, 192.168.1.110) via systemd.

### Knowledge Curation Queue

The `knowledge` server owns a reviewed curation queue in SQLite. LibreChat and
maintenance jobs can draft queue items for durable memory extraction, source
consolidation, temporal fact cleanup, and pending maintenance review. The MCP
tools are:

- `knowledge_curation_list`
- `knowledge_curation_get`
- `knowledge_curation_apply`
- `knowledge_curation_reject`
- `knowledge_curation_snooze`

Destructive actions require `confirmation` equal to the queue item id.

## Related Repos

- [`jck411/Backend_FastAPI`](https://github.com/jck411/Backend_FastAPI) (LXC 111) — auto-discovers servers on ports 9001–9017.
- [`jck411/opencode-config`](https://github.com/jck411/opencode-config) (LXC 114) — register a new server in OpenCode after deploying it here.
- [`jck411/PROXMOX`](https://github.com/jck411/PROXMOX) — host/LXC infrastructure. See [`docs/infrastructure-map.md`](https://github.com/jck411/PROXMOX/blob/master/docs/infrastructure-map.md).

## Quick Start

```bash
# Install with uv
uv sync

# Run a single server (e.g., calculator)
python -m servers.calculator --transport streamable-http --host 0.0.0.0 --port 9003

# Run with extras for specific servers
uv sync --extra playwright
python -m servers.playwright --transport streamable-http --host 0.0.0.0 --port 9011
```

## Local Development

Development happens locally — edit, run, and test servers on your machine, then deploy to Proxmox when done.

### 1. Start an MCP server

```bash
cd /path/to/mcp-servers

# Launch one or more servers
./dev.sh spotify
./dev.sh spotify calculator    # multiple at once
./dev.sh --list                # show all servers + ports
```

Or manually:

```bash
uv sync --extra spotify
python -m servers.spotify --transport streamable-http --host 127.0.0.1 --port 9010
```

### 2. Connect from the backend

Start the Backend_FastAPI locally (`./startdev.sh`), open the frontend, and go to **Settings → Server status**. Enter the server URL and click **Connect**:

```
http://127.0.0.1:9010/mcp
```

The backend discovers tools via MCP protocol and makes them available to the LLM immediately.

### 3. Iterate

Edit server code → restart (or let watchfiles reload) → tools update on next refresh. No deploy needed during development.

### 4. Deploy to Proxmox

Only after local testing passes — see [Deployment](#deployment-proxmox) below.

### Tests & Linting

```bash
uv sync --extra dev --extra all
pytest tests/ -v
ruff check servers/ shared/
```

## Deployment (Proxmox)

Target: LXC CT 110 at `192.168.1.110` (Debian 13). Full guide: [deploy/PROXMOX_DEPLOY.md](deploy/PROXMOX_DEPLOY.md)

```bash
# On Proxmox LXC (192.168.1.110):
git clone https://github.com/jck411/mcp-servers.git /opt/mcp-servers
cd /opt/mcp-servers
uv sync --extra all

# Copy shared env and install systemd units
cp .env.example .env
sudo ./deploy/setup-systemd.sh

# Check status
./deploy/deploy.sh --status

# Deploy updates (pull + sync + restart)
./deploy/deploy.sh
```

### Managing Services

```bash
# Status
systemctl list-units 'mcp-server@*' --no-pager

# Logs
journalctl -u mcp-server@calculator -f

# Restart one server
sudo systemctl restart mcp-server@calculator

# Deploy specific server
./deploy/deploy.sh calculator
```

## Connecting from Backend

```bash
# Tell Backend_FastAPI to connect to a running server
curl -X POST http://localhost:8000/api/mcp/servers/connect \
  -H 'Content-Type: application/json' \
  -d '{"url": "http://192.168.1.110:9003/mcp"}'
```
