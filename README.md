# mcp-servers

Standalone MCP servers deployed to Proxmox LXCs via systemd. Knowledge services run on LXC 110 (192.168.1.110), while private/account/home-control servers (gmail, gdrive, calendar, monarch, spotify, tv, hue) are isolated on LXC 117 (192.168.1.117). Any MCP-compatible client can connect over HTTP when allowed by the local firewall.

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

| Server | Port | LXC |
|--------|------|-----|
| calendar | 9004 | 117 |
| gmail | 9005 | 117 |
| gdrive | 9006 | 117 |
| monarch | 9008 | 117 |
| spotify | 9010 | 117 |
| tv | 9013 | 117 |
| hue | 9015 | 117 |
| knowledge | 9017 | 110 |
| knowledge_api (REST, not MCP) | 9018 | 110 |

Next available MCP port: **9019**. Retired ports (do not reuse): `9002`, `9003`, `9007`, `9012`, `9016`. `9018` is the knowledge_api FastAPI REST service — it's managed by the same systemd template but does not expose `/mcp`.

- **LXC 110** (knowledge): knowledge, knowledge_api
- **LXC 117** (private/home): calendar, gmail, gdrive, monarch, spotify, tv, hue — account credentials and home-control keys only exist here

### Knowledge Curation Queue

The `knowledge` server owns an approval-gated curation queue in SQLite for durable memory extraction, source consolidation, and temporal fact cleanup. Tools:

- `knowledge_curation_list`
- `knowledge_curation_get`
- `knowledge_curation_apply`
- `knowledge_curation_reject`
- `knowledge_curation_snooze`

Destructive actions require `confirmation` equal to the queue item id.

## Related Repos

- [`jck411/Backend_FastAPI`](https://github.com/jck411/Backend_FastAPI) (LXC 111) — MCP client; discovers LXC 110 and LXC 117
- [`jck411/opencode-config`](https://github.com/jck411/opencode-config) (LXC 114) — OpenCode config; see that repo's `add-mcp-server.sh` to register servers
- [`jck411/PROXMOX`](https://github.com/jck411/PROXMOX) — host/LXC infrastructure

## Docs

- [Knowledge system](docs/KNOWLEDGE_SYSTEM.md) — how domains, sources, search, extraction, and curation work
- [Upload system](docs/UPLOAD_SYSTEM.md) — browser/API upload flow and troubleshooting
- [Deployment](docs/DEPLOYMENT.md) — deploy script, systemd, ports, and debugging

## Local Docs MCP (stdio)

`servers.docs` is a local-only MCP server for live `~/REPOS` context. It is not deployed to LXC.

Available tools:
- `docs_overview`
- `docs_read_file`
- `docs_write_file` (guarded text/doc writes in `~/REPOS`, modes: `replace` or `append`)
- `docs_search`
- `docs_env_manifest`

## Quick Start

```bash
# Install with uv
uv sync

# Run a single server (e.g., hue)
python -m servers.hue --transport streamable-http --host 0.0.0.0 --port 9015

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
./dev.sh spotify hue           # multiple at once
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
Knowledge adds the same `Authorization: Bearer <MCP_KNOWLEDGE_BEARER_TOKEN>` header when you test it locally.

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

Target: LXC CT 110 at `192.168.1.110` (Debian 13) for Knowledge services. LXC 117 at `192.168.1.117` runs the private/account/home-control services through the NETWORK deploy registry.

```bash
# On Proxmox LXC (192.168.1.110):
git clone https://github.com/jck411/mcp-servers.git /opt/mcp-servers
cd /opt/mcp-servers
uv sync --extra all

# .env is already symlinked on the LXC — no copy needed
sudo ./deploy/setup-systemd.sh

# Check Knowledge status
./deploy/deploy.sh --status

# Deploy Knowledge updates (reset tracked source to origin/master + sync + restart)
./deploy/deploy.sh
```

### Managing Services

```bash
# Status
systemctl list-units 'mcp-server@*' --no-pager

# Logs
journalctl -u mcp-server@knowledge -f

# Restart one server
sudo systemctl restart mcp-server@knowledge

# Deploy specific server
./deploy/deploy.sh knowledge
```

## Security

The Knowledge MCP endpoint uses a shared bearer token. Raw MCP ports stay network-restricted.

### LAN access

Raw MCP ports are internal backend ports. LXC firewalls should allow only trusted clients such as chat-backend, OpenCode, LibreChat, the Proxmox host for Cloudflare tunnel ingress, and the admin laptop. Do not expose ports `9003–9018` through router port-forwarding.

### Remote access (Cloudflare Tunnel)

Use a **Cloudflare Tunnel** — never port-forward `9003–9018` through your router. The tunnel gives Knowledge a public HTTPS endpoint without opening firewall holes.

### Authentication

Use `Authorization: Bearer <MCP_KNOWLEDGE_BEARER_TOKEN>` with `https://mcp-knowledge-bearer.jackshome.com/mcp`.
For Codex, start it from a shell that has sourced `~/REPOS/symlinked-env/.env` so `MCP_KNOWLEDGE_BEARER_TOKEN` is present at launch.

## Client Integration

Servers speak the [MCP streamable-HTTP transport](https://spec.modelcontextprotocol.io). Any MCP client that supports HTTP can connect with the bearer token.

| Access | Base URL |
|--------|----------|
| LAN (knowledge) | `http://192.168.1.110:<port>/mcp` |
| LAN (private/home) | `http://192.168.1.117:<port>/mcp` |
| Remote (knowledge) | `https://mcp-knowledge-bearer.jackshome.com/mcp` |

### Codex

`~/.codex/config.toml`:

```toml
[mcp_servers.jack-knowledge]
url = "https://mcp-knowledge-bearer.jackshome.com/mcp"
bearer_token_env_var = "MCP_KNOWLEDGE_BEARER_TOKEN"
```

### VS Code Copilot

`.vscode/mcp.json` (or user-level `settings.json`):

```json
{
  "servers": {
    "knowledge": { "type": "http", "url": "http://192.168.1.110:9017/mcp" },
    "spotify":   { "type": "http", "url": "http://192.168.1.117:9010/mcp" }
  }
}
```

### OpenCode

`~/.config/opencode/config.json`:

```json
{
  "mcp": {
    "knowledge": {
      "type": "http",
      "url": "http://192.168.1.110:9017/mcp",
      "headers": {
        "Authorization": "Bearer ${MCP_KNOWLEDGE_BEARER_TOKEN}"
      }
    }
  }
}
```

### LibreChat

`librechat.yaml`:

```yaml
mcpServers:
  knowledge:
    url: http://192.168.1.110:9017/mcp
  spotify:
    url: http://192.168.1.117:9010/mcp
```

When using MCP through LibreChat, the MCP request comes from the LibreChat
container (`192.168.1.115`). Your phone or laptop only needs to reach LibreChat;
it does not need direct firewall access to the raw MCP ports.

### ChatGPT

ChatGPT → Settings → Connected Apps → Add custom MCP server. Use the Knowledge endpoint:

```
https://mcp-knowledge-bearer.jackshome.com/mcp
```

Add `Authorization: Bearer <MCP_KNOWLEDGE_BEARER_TOKEN>` in the custom action’s auth settings.

### Generic (any MCP client)

Point any MCP client at the server's URL. Knowledge uses `https://mcp-knowledge-bearer.jackshome.com/mcp` and requires the bearer token; private/account/home-control servers use `http://192.168.1.117:<port>/mcp`. The server responds to all standard MCP methods (`tools/list`, `tools/call`, etc.).
