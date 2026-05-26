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
| web_search | 9016 | 110 |
| knowledge | 9017 | 110 |
| knowledge_api (REST, not MCP) | 9018 | 110 |
| knowledge_admin | 9019 | 110 |

Next available MCP port: **9020**. Retired ports (do not reuse): `9002`, `9003`, `9007`, `9012`. `9018` is the knowledge_api FastAPI REST service — it uses the dedicated `mcp-knowledge-api.service` unit and does not expose `/mcp`.

- **LXC 110** (knowledge): web_search, knowledge, knowledge_api, knowledge_admin
- **LXC 117** (private/home): calendar, gmail, gdrive, monarch, spotify, tv, hue — account credentials and home-control keys only exist here

### Knowledge Curation Queue

The Knowledge system stores an approval-gated curation queue in SQLite for
diagnostic cleanup: temporal fact cleanup, vector drift, and maintenance
findings that need human judgment. The chat-facing
`knowledge` MCP only reports the pending count in `knowledge_context_pack`;
review and cleanup tools live on `knowledge_admin`:

- `knowledge_curation_create`
- `knowledge_curation_list`
- `knowledge_curation_get`
- `knowledge_curation_question_packs`
- `knowledge_curation_question_pack_get`
- `knowledge_curation_pack_preview`
- `knowledge_curation_pack_apply`
- `knowledge_curation_snooze`
- `knowledge_curation_resolve`

Destructive actions require `confirmation` equal to the queue item id.

Knowledge search now infers temporal intent. Unqualified schedule/PTO-style
questions prefer current/upcoming facts, while past-tense or "last year" queries
include historical facts and archived domains.

## Related Repos

- [`jck411/Backend_FastAPI`](https://github.com/jck411/Backend_FastAPI) (LXC 111) — MCP client; discovers LXC 110 and LXC 117
- [`jck411/opencode-config`](https://github.com/jck411/opencode-config) (LXC 114) — OpenCode config; see that repo's `add-mcp-server.sh` to register servers
- [`jck411/PROXMOX`](https://github.com/jck411/PROXMOX) — host/LXC infrastructure

## Docs

- [Knowledge system](docs/KNOWLEDGE_SYSTEM.md) — how domains, facts, wiki, search, and curation work
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
reachability, fact/vector counts, BM25 doc count, and embedding model:

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

# Deploy Knowledge updates (reset tracked source to origin/main + sync + restart)
./deploy/deploy.sh
```

`setup-systemd.sh` installs the 3am ET backup/maintenance/wiki timer and the
noon ET wiki-only rebuild timer. Nightly maintenance runs the tracked
`servers.knowledge.maintenance` module from this repo.

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

Raw MCP ports are internal backend ports. LXC firewalls should allow only trusted clients such as chat-backend, OpenCode, LibreChat, the Proxmox host for Cloudflare tunnel ingress, and the admin laptop. Do not expose ports `9003–9019` through router port-forwarding. Keep `knowledge_admin` out of LibreChat; Codex reaches it through an SSH tunnel.

### Remote access (Cloudflare Tunnel)

Use a **Cloudflare Tunnel** — never port-forward `9003–9019` through your router. The tunnel gives Knowledge a public HTTPS endpoint without opening firewall holes.

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
  "inputs": [
    {
      "type": "promptString",
      "id": "mcp-knowledge-token",
      "description": "MCP Knowledge bearer token",
      "password": true
    }
  ],
  "servers": {
    "knowledge": {
      "type": "http",
      "url": "http://192.168.1.110:9017/mcp",
      "headers": {
        "Authorization": "Bearer ${input:mcp-knowledge-token}"
      }
    },
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

Use `https://mcp-knowledge-bearer.jackshome.com/mcp` with `Authorization: Bearer <MCP_KNOWLEDGE_BEARER_TOKEN>`; private/account/home-control servers stay on `http://192.168.1.117:<port>/mcp`.
