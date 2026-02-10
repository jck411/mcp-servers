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

| Server | Port | Status |
|--------|------|--------|
| shell-control | 9001 | ✅ Ready |
| housekeeping | 9002 | 🔜 Later |
| calculator | 9003 | ✅ Ready |
| calendar | 9004 | 🔜 Later |
| gmail | 9005 | 🔜 Later |
| gdrive | 9006 | 🔜 Later |
| pdf | 9007 | 🔜 Later |
| monarch | 9008 | 🔜 Later |
| notes | 9009 | 🔜 Later |
| spotify | 9010 | 🔜 Later |
| playwright | 9011 | ✅ Ready |
| kiosk-clock-tools | 9012 | 🔜 Later |

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

## Development

```bash
# Install with dev deps
uv sync --extra dev --extra all

# Run tests
pytest tests/ -v

# Lint
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

### Port Assignments

Ports are set via per-instance env files (`.env.calculator`, `.env.shell_control`, etc.), created automatically by `setup-systemd.sh`:

| Server | Port | File |
|--------|------|------|
| shell_control | 9001 | `.env.shell_control` |
| calculator | 9003 | `.env.calculator` |
| playwright | 9011 | `.env.playwright` |

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
