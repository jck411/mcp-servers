# mcp-servers

Standalone MCP servers deployed to Proxmox LXC (CT 110, 192.168.1.110) via systemd. Any MCP-compatible client can connect to these servers over HTTP.

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

## Port Assignments

| Server | Port |
|--------|------|
| calculator | 9003 |
| calendar | 9004 |
| gmail | 9005 |
| gdrive | 9006 |
| pdf | 9007 |
| monarch | 9008 |
| spotify | 9010 |
| tv | 9013 |
| hue | 9015 |
| web_search | 9016 |
| knowledge | 9017 |
| knowledge_api (REST, not MCP) | 9018 |

Next available MCP port: **9019**. Retired ports (do not reuse): `9002`, `9012`. `9018` is the knowledge_api FastAPI REST service — it's managed by the same systemd template but does not expose `/mcp`.

All servers deployed to Proxmox LXC (CT 110, 192.168.1.110) via systemd.

### Knowledge Curation Queue

The `knowledge` server owns a reviewed curation queue in SQLite for durable memory extraction, source consolidation, and temporal fact cleanup. Tools:

- `knowledge_curation_list`
- `knowledge_curation_get`
- `knowledge_curation_apply`
- `knowledge_curation_reject`
- `knowledge_curation_snooze`

Destructive actions require `confirmation` equal to the queue item id.

## Related Repos

- [`jck411/Backend_FastAPI`](https://github.com/jck411/Backend_FastAPI) (LXC 111) — MCP client; connect it to any server at `http://192.168.1.110:<port>/mcp`
- [`jck411/opencode-config`](https://github.com/jck411/opencode-config) (LXC 114) — OpenCode config; see that repo's `add-mcp-server.sh` to register servers
- [`jck411/PROXMOX`](https://github.com/jck411/PROXMOX) — host/LXC infrastructure

## Quick Start

```bash
# Install with uv
uv sync

# Run a single server (e.g., calculator)
python -m servers.calculator --transport streamable-http --host 0.0.0.0 --port 9003

# Run with extras for specific servers
uv sync --extra hue
python -m servers.hue --transport streamable-http --host 0.0.0.0 --port 9015
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

### 2. Test the server

Smoke-test any running server:

```bash
curl -s http://127.0.0.1:<port>/mcp \
  -X POST \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Or point any MCP client at `http://127.0.0.1:<port>/mcp`.

### 3. Iterate

Edit server code → watchfiles reloads automatically → retest. No deploy needed during development.

### 4. Deploy to Proxmox

Only after local testing passes — see [Deployment](#deployment-proxmox) below.

### Tests & Linting

```bash
uv sync --extra dev --extra all
pytest tests/ -v
ruff check servers/ shared/
```

## Observability

All servers use stdlib `logging` via `shared/logging_config.py`. Output goes
to stderr in this format:

```
2026-05-04T15:30:42-0600 INFO    knowledge: tool=knowledge_search status=ok duration_ms=212.4 success=True count=10
```

Set verbosity with the `LOG_LEVEL` env var (default `INFO`). On the LXC:

```bash
journalctl -u mcp-server@knowledge -f          # follow live
LOG_LEVEL=DEBUG  # set in /etc/default/mcp or systemd override for verbose
```

Every `@mcp.tool` on the `knowledge` server is wrapped with
`@logged_tool(log)` from [shared/logging_config.py](shared/logging_config.py),
so each call logs `tool=<name> status=ok|error duration_ms=N` plus a short
result summary. Apply the same decorator to other servers as needed.

The Knowledge REST API exposes `GET /api/health` returning Qdrant
reachability, source/chunk counts, BM25 doc count, and embedding model:

```bash
curl https://api-knowledge.jackshome.com/api/health
```

## Deployment (Proxmox)

Target: LXC CT 110 at `192.168.1.110` (Debian 13). Full guide: [deploy/PROXMOX_DEPLOY.md](deploy/PROXMOX_DEPLOY.md)

```bash
# On Proxmox LXC (192.168.1.110):
git clone https://github.com/jck411/mcp-servers.git /opt/mcp-servers
cd /opt/mcp-servers
uv sync --extra all

# .env is already symlinked on the LXC — no copy needed
sudo ./deploy/setup-systemd.sh

# Check status
./deploy/deploy.sh --status

# Deploy updates (reset tracked source to origin/master + sync + restart)
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

## Security

Servers bind `0.0.0.0` with **no built-in HTTP authentication**. Security is enforced at the network level.

### LAN access

Direct IP is safe as-is. Ensure your router/Proxmox firewall does **not** expose ports `9003–9018` to the internet — only your LAN subnet should reach them.

### Remote access (Cloudflare Tunnel)

Use a **Cloudflare Tunnel** — never port-forward `9003–9018` through your router. The tunnel gives each server a public HTTPS endpoint without opening firewall holes.

### Locking down the public tunnel (for ChatGPT, etc.)

Add a **Cloudflare Zero Trust Access policy** to the tunnel hostname — zero server code changes required:

1. Cloudflare Zero Trust → Access → Service Auth → **Create Service Token**
2. Attach an Access policy to the tunnel hostname requiring that token
3. Clients pass two headers with every request:
   ```
   CF-Access-Client-Id: <client-id>
   CF-Access-Client-Secret: <client-secret>
   ```
   For `mcp-knowledge.jackshome.com`, the generated values are stored in the
   master env as `MCP_KNOWLEDGE_CF_ACCESS_CLIENT_ID` and
   `MCP_KNOWLEDGE_CF_ACCESS_CLIENT_SECRET`.
4. All enforcement happens at the Cloudflare edge — the servers themselves are unchanged

For clients on your local network, skip Zero Trust and use the direct LAN URL.

## Client Integration

Servers speak the [MCP streamable-HTTP transport](https://spec.modelcontextprotocol.io). Any MCP client that supports HTTP can connect.

| Access | Base URL |
|--------|----------|
| LAN | `http://192.168.1.110:<port>/mcp` |
| Remote (Cloudflare Tunnel) | `https://<tunnel-hostname>/mcp` |

### VS Code Copilot

`.vscode/mcp.json` (or user-level `settings.json`):

```json
{
  "servers": {
    "calculator": { "type": "http", "url": "http://192.168.1.110:9003/mcp" },
    "spotify":    { "type": "http", "url": "http://192.168.1.110:9010/mcp" }
  }
}
```

### OpenCode

`~/.config/opencode/config.json`:

```json
{
  "mcp": {
    "calculator": { "type": "http", "url": "http://192.168.1.110:9003/mcp" }
  }
}
```

### LibreChat

`librechat.yaml`:

```yaml
mcpServers:
  calculator:
    url: http://192.168.1.110:9003/mcp
  spotify:
    url: http://192.168.1.110:9010/mcp
```

### ChatGPT

ChatGPT → Settings → Connected Apps → Add custom MCP server. Use the Cloudflare Tunnel URL:

```
https://<tunnel-hostname>/mcp
```

If the tunnel is protected by Zero Trust, add the `CF-Access-Client-Id` and `CF-Access-Client-Secret` headers in the custom action’s auth settings.

### Generic (any MCP client)

Point any MCP client at `http://192.168.1.110:<port>/mcp`. The server responds to all standard MCP methods (`tools/list`, `tools/call`, etc.).
