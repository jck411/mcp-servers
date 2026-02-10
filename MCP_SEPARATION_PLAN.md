# MCP Separation Plan

**Branch:** `separation`
**Created:** February 9, 2026
**Goal:** Decouple MCP servers from the chat application. Backend becomes a pure MCP client (consumer). MCP servers become standalone always-on services deployed to Proxmox.

**Status:** Stage 1 + Stage 1.5 complete. Stage 2 repo ready — deploy infrastructure in place. Ready for Stage 3 (physical deployment to Proxmox).

---

## Architecture: Before & After

### Before

```
Backend_FastAPI (single repo, single process tree)
├── FastAPI Backend (:8000)
│   ├── ChatOrchestrator — owns MCP lifecycle
│   ├── MCPToolAggregator — spawns/kills server processes
│   ├── MCPServerSettingsService — manages server configs
│   └── 12 MCP server modules in src/backend/mcp_servers/
├── MCP Servers (:9001-9012) — spawned as subprocesses, die on Ctrl+C
└── Frontends (Svelte, Kiosk, Voice, CLI)
```

- MCP servers live inside the backend package
- Backend spawns/kills server processes
- Server config contains frontend-specific settings (`client_enabled`)
- Everything dies when you close the laptop

### After

```
Backend_FastAPI (this repo — MCP client only)
├── FastAPI Backend (:8000)
│   ├── ChatOrchestrator — uses tools, doesn't manage servers
│   ├── MCPClientRegistry — connects to running servers, routes tool calls
│   ├── ClientToolPreferences — per-frontend tool filtering
│   └── MCP Settings API — add/remove/discover servers, manage preferences
└── Frontends (Svelte, Kiosk, Voice, CLI)

mcp-servers (new repo — deployed to Proxmox)
├── Standalone MCP servers (systemd services, always-on)
├── Each server self-describes via MCP protocol
└── Zero imports from Backend_FastAPI
```

- Backend is a pure MCP consumer (MCP Host in spec terms)
- MCP servers run on Proxmox (192.168.1.11), always-on via systemd
- Server config is just URLs — backend connects and discovers tools automatically
- Frontend preferences are separate from server config
- Adding a new server = paste a URL, toggle it on

---

## Data Model Changes

### Server Registry (`data/mcp_servers.json`)

**Before:**
```json
{
  "servers": [
    {
      "id": "calculator",
      "enabled": true,
      "module": "backend.mcp_servers.calculator_server",
      "http_port": 9003,
      "env": {},
      "client_enabled": { "svelte": true, "cli": true, "kiosk": false },
      "disabled_tools": [],
      "contexts": [],
      "tool_overrides": {}
    }
  ]
}
```

**After:**
```json
{
  "servers": [
    {
      "id": "calculator",
      "url": "http://192.168.1.110:9003/mcp",
      "enabled": true,
      "disabled_tools": []
    }
  ]
}
```

Removed: `module`, `command`, `cwd`, `env`, `http_port`, `client_enabled`, `contexts`, `tool_overrides`, `tool_prefix`.
Server identity, tools, and descriptions come from the MCP protocol itself (`list_tools()`).

### Client Preferences (`data/client_tool_preferences.json`) — NEW

```json
{
  "svelte": {
    "enabled_servers": ["calculator", "notes", "shell-control", "spotify"]
  },
  "kiosk": {
    "enabled_servers": ["calculator", "spotify"]
  },
  "voice": {
    "enabled_servers": ["calculator", "spotify"]
  },
  "cli": {
    "enabled_servers": ["calculator", "notes", "shell-control"]
  }
}
```

Each frontend manages its own list. A new server appears in the settings modal with toggle off by default. The user enables it for their frontend.

---

## API Changes

### Removed Endpoints

| Endpoint | Reason |
|----------|--------|
| `PUT /api/mcp/servers/` | Bulk replace — over-engineered, replaced by add/remove |
| `PATCH /api/mcp/servers/{id}/clients/{client_id}` | Client preferences are separate now |

### Modified Endpoints

| Endpoint | Before | After |
|----------|--------|-------|
| `GET /api/mcp/servers/` | Returns server configs + runtime status | Returns connected servers + tools (auto-discovered) |
| `PATCH /api/mcp/servers/{id}` | Patch server config (enabled, disabled_tools, etc.) | Toggle enabled, toggle individual tools |
| `POST /api/mcp/servers/refresh` | Re-list tools | Unchanged |

### New Endpoints

| Endpoint | Purpose |
|----------|---------|
| `POST /api/mcp/servers/connect` | Add a server by URL. Backend connects, discovers name/tools via MCP protocol. Body: `{"url": "http://host:port/mcp"}` |
| `DELETE /api/mcp/servers/{id}` | Remove a server from the registry |
| `POST /api/mcp/servers/discover` | Scan a network range for MCP servers. Body: `{"host": "192.168.1.110", "ports": [9001, 9002, ...]}` |
| `GET /api/mcp/preferences/{client_id}` | Get tool preferences for a frontend |
| `PUT /api/mcp/preferences/{client_id}` | Update preferences. Body: `{"enabled_servers": ["calculator", "notes"]}` |

### Chat Stream Change

Frontends already send `X-Client-ID` header. The backend uses it to filter tools:

```
POST /api/chat/stream
Headers: X-Client-ID: svelte

→ Backend looks up svelte's enabled_servers
→ Only sends tools from those servers to the LLM
→ LLM can only call tools the frontend has enabled
```

---

## Stage 1: Strip Backend to Pure MCP Client

**Goal:** Remove all server lifecycle management. Backend only connects to already-running servers.

### 1.1 Simplify `MCPServerConfig`

**File:** `src/backend/chat/mcp_registry.py`

Remove fields: `module`, `command`, `cwd`, `env`, `http_port`, `tool_prefix`, `contexts`, `tool_overrides`, `client_enabled`.

New model:
```python
class MCPServerConfig(BaseModel):
    id: str                           # Stable identifier (auto-set from server name if not provided)
    url: str                          # Full MCP endpoint URL (e.g. http://192.168.1.110:9003/mcp)
    enabled: bool = True              # Whether backend should connect
    disabled_tools: set[str] = set()  # Tools to hide from LLM
```

### 1.2 Simplify `MCPToolClient`

**File:** `src/backend/chat/mcp_client.py`

Delete:
- All process spawning code (`_run_lifecycle`, subprocess management)
- `_wait_for_port`, `_record_process_log`, `_format_process_log`, `_log_startup_output`
- `reconnect()` method
- Constructor params: `server_module`, `command`, `cwd`, `env`

Keep:
- `connect()` — HTTP/streamable-http only
- `close()`
- `call_tool(name, arguments)`
- `get_openai_tools()`
- `format_tool_result(result)`
- `refresh_tools()`

### 1.3 Simplify `MCPToolAggregator`

**File:** `src/backend/chat/mcp_registry.py`

Delete:
- `_spawn_server_process()`, `_terminate_process()`
- `_processes` dict, `managed_mode`, `base_env`, `default_cwd`
- The subprocess branch in `_launch_server()`
- `_requires_restart()` (no restarts — servers are external)
- `get_openai_tools_for_contexts()`, `get_openai_tools_by_qualified_names()`, `get_capability_digest()` (unused at runtime)
- Context digest system (`_context_digest_index`, `_global_digest`, `_rebuild_context_digest`, `_rank_tools_for_context`)
- Tool name qualification/prefixing (servers manage their own namespaces)

Keep:
- `discover_and_connect()` — scan ports, connect to running servers
- `connect_to_url(url)` — connect to a specific URL
- `apply_configs(configs)` — update which servers to track
- `refresh()` / `_refresh_locked()` — rebuild tool index
- `close()` — disconnect all clients
- `get_openai_tools()` — aggregated tool schemas
- `get_openai_tools_for_servers(server_ids)` — NEW: filtered by allowed servers
- `call_tool(name, arguments)` — route to correct server
- `format_tool_result(result)`
- `describe_servers()` — runtime status for API
- `tools` property

### 1.4 Simplify `ChatOrchestrator`

**File:** `src/backend/chat/orchestrator.py`

Delete:
- `_build_mcp_base_env()` helper function
- `base_env` and `default_cwd` from aggregator construction
- `managed_mode` from aggregator construction

Change:
- `process_stream()` → filter tools by client ID using preferences service
- Remove direct MCP management methods (move to management service)

### 1.5 Create `ClientToolPreferences` Service

**New file:** `src/backend/services/client_tool_preferences.py`

```python
class ClientToolPreferences:
    """Manage per-frontend tool server preferences."""

    def __init__(self, path: Path):
        self._path = path  # data/client_tool_preferences.json

    async def get_enabled_servers(self, client_id: str) -> list[str] | None:
        """Return server IDs enabled for this client, or None (= all)."""

    async def set_enabled_servers(self, client_id: str, server_ids: list[str]) -> None:
        """Set which servers this client can use."""
```

### 1.6 Create MCP Management Service

**New file:** `src/backend/services/mcp_management.py`

Extract MCP management out of ChatOrchestrator:
```python
class MCPManagementService:
    """Manage MCP server connections and registry."""

    def __init__(self, aggregator, settings_service):
        ...

    async def connect_server(self, url: str) -> MCPServerStatus:
        """Connect to a new MCP server by URL, discover its tools."""

    async def remove_server(self, server_id: str) -> None:
        """Disconnect and remove a server."""

    async def discover_servers(self, host: str, ports: list[int]) -> list[MCPServerStatus]:
        """Scan for MCP servers on a network host."""

    async def get_status(self) -> list[MCPServerStatus]:
        """Return all servers with connection status and tools."""

    async def toggle_server(self, server_id: str, enabled: bool) -> None:
        """Enable/disable a server."""

    async def toggle_tool(self, server_id: str, tool_name: str, enabled: bool) -> None:
        """Enable/disable a specific tool."""
```

### 1.7 Update Router

**File:** `src/backend/routers/mcp_servers.py`

- Rewrite to use `MCPManagementService` + `ClientToolPreferences`
- Add new endpoints (connect, delete, discover, preferences)
- Remove old endpoints (PUT bulk replace, PATCH client toggle)

### 1.8 Update `app.py` Wiring

- Create `MCPManagementService` at startup
- Create `ClientToolPreferences` at startup
- Inject into orchestrator and router
- Remove `BUILTIN_MCP_SERVER_DEFINITIONS`

### 1.9 Update Schemas

**File:** `src/backend/schemas/mcp_servers.py`

Simplify response models to match new data:
```python
class MCPServerStatus:
    id: str
    url: str
    enabled: bool
    connected: bool
    tools: list[MCPToolInfo]
    disabled_tools: list[str]

class MCPToolInfo:
    name: str
    description: str

class ClientPreferences:
    client_id: str
    enabled_servers: list[str]
```

### 1.10 Update Tests

- Update `test_mcp_registry.py` — remove process spawn tests, simplify config tests
- Update `test_mcp_client.py` — remove subprocess tests
- Update `test_mcp_server_settings.py` — adapt to new config model
- Update `test_chat_orchestrator.py` — remove MCP lifecycle tests
- Add `test_client_tool_preferences.py` — new service
- Add `test_mcp_management.py` — new service

### 1.11 Update Frontend (Svelte Only)

- Update `mcpServers.ts` store to use new API shape
- Update `McpServersModal.svelte` — add URL input field, use preferences API
- Update `client.ts` API methods

### Stage 1 Verification

```bash
# All tests pass
pytest tests/ -v

# Lint clean
ruff check src/backend/

# Start backend (no MCP servers needed — they're external)
uv run uvicorn backend.app:create_app --factory --host 0.0.0.0

# Verify health
curl http://localhost:8000/health

# Verify MCP settings API returns empty (no servers running)
curl http://localhost:8000/api/mcp/servers/

# Start a test MCP server manually, verify connect works
curl -X POST http://localhost:8000/api/mcp/servers/connect \
  -H 'Content-Type: application/json' \
  -d '{"url": "http://127.0.0.1:9003/mcp"}'
```

---

## Stage 1.5: Post-Review Cleanup

**Goal:** Fix issues found during the Stage 1 code review before moving on. Backend code is solid; these are mostly frontend leftovers and a few encapsulation fixes.

### 1.5.1 Update Frontend Types (`types.ts`)

**File:** `frontend/src/lib/api/types.ts`

Strip legacy fields from TypeScript interfaces to match the new backend schemas:

```typescript
// BEFORE (stale — has module, command, http_url, http_port, cwd, env, tool_prefix, client_enabled)
export interface McpServerStatus {
  id: string;
  enabled: boolean;
  connected: boolean;
  module?: string | null;         // ← remove
  command?: string[] | null;      // ← remove
  http_url?: string | null;       // ← remove
  http_port?: number | null;      // ← remove
  cwd?: string | null;            // ← remove
  env?: Record<string, string>;   // ← remove
  tool_prefix?: string | null;    // ← remove
  disabled_tools: string[];
  tool_count: number;
  tools: McpServerToolStatus[];
  client_enabled?: Record<string, boolean>;  // ← remove
}

// AFTER
export interface McpServerToolStatus {
  name: string;
  enabled: boolean;
}

export interface McpServerStatus {
  id: string;
  url: string;
  enabled: boolean;
  connected: boolean;
  tool_count: number;
  tools: McpServerToolStatus[];
  disabled_tools: string[];
}

export interface McpServerUpdatePayload {
  enabled?: boolean;
  disabled_tools?: string[];
}
```

Also:
- Remove `qualified_name` from `McpServerToolStatus`
- Remove `McpServerDefinition` interface (legacy, unused)
- Remove `McpServersCollectionPayload` interface (PUT bulk replace was removed)
- Clean up any imports/references to removed types

### 1.5.2 Fix Frontend Store — Remove Dead Endpoint Calls

**File:** `frontend/src/lib/stores/mcpServers.ts`

| Function | Problem | Fix |
|----------|---------|-----|
| `setClientEnabled()` | Calls `PATCH /api/mcp/servers/{id}/clients/{client_id}` — **endpoint removed** | Rewrite to call `PUT /api/mcp/preferences/{client_id}` instead |
| `setServerEnv()` | Accumulates `env` changes — `env` is no longer a server config field | Delete this function entirely |
| `setKioskEnabled()`, `setFrontendEnabled()`, `setCliEnabled()` | Wrappers around `setClientEnabled` | Keep as convenience wrappers, they'll work once `setClientEnabled` is fixed |

**File:** `frontend/src/lib/api/client.ts`

| Function | Problem | Fix |
|----------|---------|-----|
| `replaceMcpServers()` | Calls `PUT /api/mcp/servers/` — **endpoint removed** | Delete this function |
| `setMcpServerClientEnabled()` | Calls `PATCH /api/mcp/servers/{id}/clients/{client_id}` — **endpoint removed** | Replace with `updateClientPreferences(clientId, serverIds)` calling `PUT /api/mcp/preferences/{client_id}` |

Add new API functions:
```typescript
export async function fetchClientPreferences(clientId: string): Promise<ClientPreferences> {
  return requestJson<ClientPreferences>(resolveApiPath(`/api/mcp/preferences/${encodeURIComponent(clientId)}`));
}

export async function updateClientPreferences(
  clientId: string,
  enabledServers: string[],
): Promise<ClientPreferences> {
  return requestJson<ClientPreferences>(
    resolveApiPath(`/api/mcp/preferences/${encodeURIComponent(clientId)}`),
    { method: 'PUT', body: JSON.stringify({ enabled_servers: enabledServers }) },
  );
}

export async function connectMcpServer(url: string): Promise<McpServerStatus> {
  return requestJson<McpServerStatus>(resolveApiPath('/api/mcp/servers/connect'), {
    method: 'POST',
    body: JSON.stringify({ url }),
  });
}

export async function removeMcpServer(serverId: string): Promise<void> {
  await requestVoid(resolveApiPath(`/api/mcp/servers/${encodeURIComponent(serverId)}`), {
    method: 'DELETE',
  });
}
```

### 1.5.3 Fix Private Member Access in `MCPManagementService`

**File:** `src/backend/services/mcp_management.py`

`discover_servers()` calls `self._aggregator._is_server_running()` (private). Fix:

```python
# In MCPToolAggregator (mcp_registry.py) — make public:
async def is_server_running(self, host: str, port: int) -> bool:

# In MCPManagementService — use public method:
is_running = await self._aggregator.is_server_running(host, port)
```

### 1.5.4 Fix Private Member Access in Router

**File:** `src/backend/routers/mcp_servers.py`

`update_mcp_server()` calls `mgmt._aggregator.apply_configs()` directly. Fix by adding a method to `MCPManagementService`:

```python
# In MCPManagementService:
async def update_disabled_tools(self, server_id: str, disabled_tools: list[str]) -> None:
    """Update disabled_tools for a server and reload configs."""
    await self._settings.patch_server(server_id, disabled_tools=disabled_tools)
    configs = await self._settings.get_configs()
    await self._aggregator.apply_configs(configs)
```

Then in the router:
```python
if payload.disabled_tools is not None:
    await mgmt.update_disabled_tools(server_id, payload.disabled_tools)
```

### 1.5.5 Improve `connect_to_url` Server ID Derivation

**File:** `src/backend/chat/mcp_registry.py`

Currently when `server_id` is `None`, the fallback is:
```python
server_id = url.rstrip("/").rsplit("/", 1)[0].rsplit(":", 1)[-1]  # → "9003"
```

This produces port numbers as IDs. Improve by using the MCP server's self-reported name:

```python
async def connect_to_url(self, url: str, server_id: str | None = None) -> str:
    async with self._lock:
        # Connect first, then derive ID from server name if not provided
        client = MCPToolClient(url=url, server_id=server_id or url)
        await client.connect()

        if server_id is None:
            # Use the MCP server's self-reported name if available
            if client._session and hasattr(client._session, 'server_info'):
                info = client._session.server_info
                if info and hasattr(info, 'name') and info.name:
                    server_id = info.name.lower().replace(' ', '-')
            if server_id is None:
                # Fallback: extract host:port as ID
                from urllib.parse import urlparse
                parsed = urlparse(url)
                server_id = f"{parsed.hostname or 'unknown'}-{parsed.port or 0}"

        # ... rest of method
```

This gives IDs like `"calculator"` (from server name) or `"192.168.1.110-9003"` (fallback) instead of just `"9003"`.

### 1.5.6 Delete Backup File

Delete `src/backend/chat/mcp_registry.py.bak` — old version with all legacy fields, no longer needed.

### 1.5.7 Update `McpServersModal.svelte`

**File:** `frontend/src/lib/components/chat/McpServersModal.svelte`

- Remove UI for `env` editing (env is no longer configurable per-server)
- Replace `client_enabled` toggle pattern with preferences API calls
- Add "Add Server" URL input field + Connect button (calls `POST /api/mcp/servers/connect`)
- Add "Remove" button per server row (calls `DELETE /api/mcp/servers/{id}`)
- Show `url` field for each server (read-only)

### Stage 1.5 Verification

```bash
# Backend lint clean
uvx ruff check src/backend/

# Backend tests pass
pytest tests/test_mcp_registry.py tests/test_mcp_client.py tests/test_mcp_server_settings.py \
  tests/test_mcp_management.py tests/test_client_tool_preferences.py -v

# Frontend builds
cd frontend && npm run build

# Manual: open Svelte settings modal, verify:
# - Can see server list with URLs
# - Can toggle server/tool on/off
# - Can add a server by URL
# - Can remove a server
# - Per-frontend preferences work
# - No console errors hitting deleted endpoints
```

---

## Stage 2: New Repo for MCP Servers

**Goal:** `jck411/mcp-servers` — standalone MCP servers deployed to Proxmox.

### 2.1 Create Repo Structure

```
mcp-servers/
├── pyproject.toml              # uv-managed, fastmcp + per-server deps
├── README.md
├── servers/
│   ├── calculator.py           # Port 9003 — copy as-is (already standalone)
│   ├── shell_control.py        # Port 9001 — copy as-is (already standalone)
│   ├── playwright_server.py    # Port 9011 — copy as-is (already standalone)
│   └── ...                     # Rebuild others as needed
├── shared/
│   ├── google_auth.py          # OAuth helper (extracted from backend)
│   └── spotify_auth.py         # Spotify OAuth (extracted from backend)
├── deploy/
│   ├── mcp-server@.service     # Systemd unit template
│   └── deploy.sh               # Pull + install + restart services
└── credentials/                # Symlink to shared credential store
```

### 2.2 Server Rules

Every server in this repo must:
- **Zero imports from Backend_FastAPI** — fully standalone
- Run with: `python -m servers.calculator --transport streamable-http --port 9003`
- Self-describe via MCP protocol (server name, tool list, tool schemas)
- If it needs chat data → call backend REST API (`GET /api/sessions/...`)
- If it needs Google auth → use `shared/google_auth.py`
- Have its own dependencies declared in `pyproject.toml`

### 2.3 Migration Priority

| Server | Effort | Priority |
|--------|--------|----------|
| calculator | Copy as-is | Now |
| shell_control | Copy as-is | Now |
| playwright | Copy as-is | Now |
| notes | Extract Google auth | Soon |
| spotify | Extract Spotify auth | Soon |
| kiosk_clock_tools | Inline 1 utility | Soon |
| calendar | Own auth + own models | Later |
| gmail | Own auth + REST for attachments | Later |
| gdrive | Own auth + REST for attachments | Later |
| housekeeping | REST for DB access | Later |
| monarch | Own auth + REST for DB | Later |
| pdf | REST for DB | Later |

### 2.4 Keep Old Servers as Reference

The 12 existing servers in `src/backend/mcp_servers/` stay in Backend_FastAPI until each replacement is built and tested in the new repo. Then delete them from this repo.

---

## Stage 3: Deploy to Proxmox

**Goal:** Always-on MCP servers on Proxmox (192.168.1.11).

### 3.1 Proxmox Setup

- New LXC container: **CT 110** (Debian 13, matches existing containers)
- Static IP: **192.168.1.110** (in the .100-.199 container range)
- Container spec: 2 cores, 2 GB RAM, 8 GB rootfs, `nesting=1`, `onboot=1`
- Clone `mcp-servers` repo to `/opt/mcp-servers`
- Service user: `mcp` (non-root, runs servers)
- Install with `uv`: `uv sync --extra all`
- Configure credentials (Google OAuth tokens, Spotify, etc.)
- Full guide: `deploy/PROXMOX_DEPLOY.md`

### 3.2 Systemd Unit Template

Uses per-instance environment files for port assignment (systemd can't do `${VAR_%i}` composition):

```ini
# /etc/systemd/system/mcp-server@.service
[Unit]
Description=MCP Server — %i
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=mcp
Group=mcp
WorkingDirectory=/opt/mcp-servers
EnvironmentFile=/opt/mcp-servers/.env
EnvironmentFile=-/opt/mcp-servers/.env.%i
ExecStart=/opt/mcp-servers/.venv/bin/python -m servers.%i \
    --transport streamable-http \
    --host 0.0.0.0 \
    --port ${MCP_PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Per-instance files (created by `deploy/setup-systemd.sh`):
```
.env.calculator    → MCP_PORT=9003
.env.shell_control → MCP_PORT=9001
.env.playwright    → MCP_PORT=9011
```

### 3.3 Port Assignments

| Server | Port |
|--------|------|
| shell-control | 9001 |
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
| kiosk-clock-tools | 9012 |

### 3.4 Optional: Cloudflare Tunnel

Add MCP servers to existing Cloudflare Tunnel config (already running for Overseerr, Sonarr, etc.):
```
mcp-calculator.jackshome.com → http://192.168.1.110:9003
mcp-notes.jackshome.com → http://192.168.1.110:9009
```

Enables using your MCP servers from anywhere, not just home network.

### Stage 3 Verification

```bash
# On Proxmox (192.168.1.110): run the setup script
sudo /opt/mcp-servers/deploy/setup-systemd.sh

# Verify services running
systemctl list-units 'mcp-server@*' --no-pager
# Or use: ./deploy/deploy.sh --status

# From dev machine (192.168.1.19): verify network connectivity
curl -s http://192.168.1.110:9003/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'

# From backend: connect to remote server
curl -X POST http://localhost:8000/api/mcp/servers/connect \
  -H 'Content-Type: application/json' \
  -d '{"url": "http://192.168.1.110:9003/mcp"}'

# Verify tools discovered
curl http://localhost:8000/api/mcp/servers/

# Send a chat message that triggers a tool
# Open Svelte frontend → "What's 2 + 2?" → should use calculator tool
```

---

## Stage 4: Update Svelte Frontend

**Goal:** Dynamic MCP settings modal with "Add Server" capability.

### 4.1 Settings Modal Changes

- Replace hardcoded server list with dynamic list from `GET /api/mcp/servers/`
- Add "Add Server" input field: URL text box + Connect button
- Add "Remove Server" button per server row
- Add "Scan Network" button (calls `POST /api/mcp/servers/discover`)
- Per-server toggle uses preferences API (`PUT /api/mcp/preferences/{client_id}`)
- Per-tool toggle uses server API (`PATCH /api/mcp/servers/{id}`)
- New servers appear with toggle OFF by default

### 4.2 Chat Stream Integration

Ensure `X-Client-ID` header is sent with every chat request so backend filters tools appropriately.

---

## Files Affected Summary

### Delete (after Stage 2 migration)
- `src/backend/mcp_servers/*.py` (all 12 server modules)
- `src/backend/mcp_servers/__init__.py`

### Heavy Rewrite
- `src/backend/chat/mcp_client.py` — strip to HTTP-only client (~150 lines from ~500)
- `src/backend/chat/mcp_registry.py` — strip to pure aggregator (~400 lines from ~1200)
- `src/backend/routers/mcp_servers.py` — new API surface
- `src/backend/schemas/mcp_servers.py` — simplified models

### Moderate Changes
- `src/backend/chat/orchestrator.py` — remove MCP lifecycle, use preferences for filtering
- `src/backend/services/mcp_server_settings.py` — simplified config persistence
- `src/backend/app.py` — new service wiring
- `frontend/src/lib/stores/mcpServers.ts` — new API shape
- `frontend/src/lib/components/chat/McpServersModal.svelte` — add URL input
- `frontend/src/lib/api/client.ts` — updated API methods

### New Files
- `src/backend/services/client_tool_preferences.py`
- `src/backend/services/mcp_management.py`
- `data/client_tool_preferences.json`
- `tests/test_client_tool_preferences.py`
- `tests/test_mcp_management.py`

### Test Updates
- `tests/test_mcp_registry.py`
- `tests/test_mcp_client.py`
- `tests/test_mcp_server_settings.py`
- `tests/test_chat_orchestrator.py`
- `tests/test_chat_router.py`

---

## Decision Log

| Decision | Rationale |
|----------|-----------|
| Refactor in-place, not new repo for backend | MCP servers already separate processes; splitting backend creates circular deps |
| New repo for MCP servers | Servers must be standalone with zero backend imports; clean deployment to Proxmox |
| No `ToolExecutor` interface | MCP protocol IS the interface; existing `ToolExecutor` in `streaming/types.py` is sufficient |
| Separate server registry from client preferences | Servers shouldn't know about frontends; frontends shouldn't dictate server config |
| Dynamic discovery via `list_tools()` | MCP spec prescribes this; no hardcoded tool lists needed |
| Proxmox deployment with systemd | Always-on servers; auto-restart on failure; matches existing Proxmox service pattern |
| Keep old servers as reference during migration | Rebuild servers one at a time in new repo; delete from backend after each is verified |
| Svelte frontend only (for now) | CLI, Kiosk, Voice get preferences API support but no UI changes in this branch |
