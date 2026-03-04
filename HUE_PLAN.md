# Hue Lights MCP Server — Implementation Plan

> **Purpose:** Multi-session implementation guide for a Philips Hue MCP server.
> Each phase is self-contained — start a fresh session, read this plan, execute the unchecked phase.
> Mark checkboxes as you complete each step.

---

## Overview

| Key | Value |
|-----|-------|
| **Server** | `servers/hue.py` |
| **Auth helper** | `shared/hue_auth.py` |
| **Port** | 9015 |
| **Protocol** | Hue CLIP v2 REST API (v1 deprecated) |
| **HTTP lib** | `httpx` (async, already in deps) |
| **Bridge IP** | 192.168.1.4 (env var `HUE_BRIDGE_IP`, default 192.168.1.4) |
| **API key** | env var `HUE_KEY` (never hardcoded) |
| **SSL** | `verify=False` (bridge uses self-signed cert) |
| **Tools** | 14 tools, all prefixed `hue_` |
| **LXC** | CT 110 at 192.168.1.110 |

---

## Phase 1: Create shared/hue_auth.py

> **Goal:** Auth helper + HTTP client + name resolution + color mapping.
> **Pattern:** Follow `shared/spotify_auth.py` structure.

### File: `mcp-servers/shared/hue_auth.py`

- [ ] Create `shared/hue_auth.py` with these components:

**Configuration:**
```python
import os
from pathlib import Path

HUE_BRIDGE_IP = os.environ.get("HUE_BRIDGE_IP", "192.168.1.4")
HUE_API_KEY = os.environ.get("HUE_KEY", "")
BASE_URL = f"https://{HUE_BRIDGE_IP}"
CLIP_V2 = f"{BASE_URL}/clip/v2/resource"
```

**Async HTTP client factory:**
```python
import httpx

def _get_headers() -> dict[str, str]:
    return {"hue-application-key": HUE_API_KEY}

async def hue_request(method: str, path: str, json: dict | None = None) -> dict:
    """Make authenticated request to Hue Bridge CLIP v2 API.
    path examples: "light", "room", "scene", "grouped_light"
    Full URL becomes: https://192.168.1.4/clip/v2/resource/{path}
    """
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.request(method, f"{CLIP_V2}/{path}", headers=_get_headers(), json=json)
        resp.raise_for_status()
        return resp.json()
```

**Name resolution helpers (fuzzy matching):**
```python
async def resolve_light(name_or_id: str) -> tuple[dict, str | None]:
    """Resolve a light by name (fuzzy) or UUID. Returns (light_data, error_or_None)."""
    # Fetch all lights, try exact UUID match first, then case-insensitive substring on metadata.name

async def resolve_room(name_or_id: str) -> tuple[dict, str | None]:
    """Resolve a room by name (fuzzy) or UUID. Returns (room_data, error_or_None)."""
    # Fetch all rooms, try exact UUID match first, then case-insensitive substring on metadata.name

async def resolve_scene(name_or_id: str, room_name: str | None = None) -> tuple[dict, str | None]:
    """Resolve a scene by name (fuzzy) or UUID, optionally filtered by room."""
```

**Color mapping (CSS color names → CIE xy coordinates):**
```python
# Hue uses CIE xy color space. Map common names to xy.
COLOR_MAP: dict[str, dict] = {
    "red":         {"x": 0.6750, "y": 0.3220},
    "green":       {"x": 0.4091, "y": 0.5180},
    "blue":        {"x": 0.1670, "y": 0.0400},
    "yellow":      {"x": 0.4432, "y": 0.5154},
    "orange":      {"x": 0.5562, "y": 0.4084},
    "purple":      {"x": 0.2485, "y": 0.0917},
    "pink":        {"x": 0.3944, "y": 0.1990},
    "cyan":        {"x": 0.1510, "y": 0.3430},
    "white":       {"x": 0.3127, "y": 0.3290},
    "warm white":  {"x": 0.4596, "y": 0.4105},
    "cool white":  {"x": 0.3174, "y": 0.3207},
    "daylight":    {"x": 0.3127, "y": 0.3290},
    "candle":      {"x": 0.5119, "y": 0.4147},
    "sunset":      {"x": 0.5267, "y": 0.4133},
    "ice":         {"x": 0.2428, "y": 0.2097},
    "lavender":    {"x": 0.2932, "y": 0.1737},
    "lime":        {"x": 0.3227, "y": 0.5520},
    "coral":       {"x": 0.5052, "y": 0.3558},
    "magenta":     {"x": 0.3833, "y": 0.1591},
    "teal":        {"x": 0.1700, "y": 0.3400},
    "gold":        {"x": 0.4859, "y": 0.4599},
}

def parse_color(color: str) -> dict | None:
    """Parse color string to CIE xy dict for Hue API.
    Accepts: CSS name ("red", "warm white") or hex ("#FF0000").
    Returns {"xy": {"x": ..., "y": ...}} or None if unrecognized.
    """
    # 1. Check COLOR_MAP (case-insensitive)
    # 2. Try hex → RGB → xy conversion via hex_to_xy()
    # 3. Return None
```

**Hex to xy conversion:**
```python
def hex_to_xy(hex_color: str) -> dict:
    """Convert #RRGGBB hex to CIE xy dict for Hue API. Uses sRGB→XYZ→xy gamut conversion."""
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i+2], 16) / 255.0 for i in (0, 2, 4))
    # Apply sRGB gamma correction
    r = ((r + 0.055) / 1.055) ** 2.4 if r > 0.04045 else r / 12.92
    g = ((g + 0.055) / 1.055) ** 2.4 if g > 0.04045 else g / 12.92
    b = ((b + 0.055) / 1.055) ** 2.4 if b > 0.04045 else b / 12.92
    # Wide RGB D65 to XYZ
    x = r * 0.664511 + g * 0.154324 + b * 0.162028
    y = r * 0.283881 + g * 0.668433 + b * 0.047685
    z = r * 0.000088 + g * 0.072310 + b * 0.986039
    total = x + y + z
    if total == 0:
        return {"x": 0.3127, "y": 0.3290}  # white point
    return {"x": round(x / total, 4), "y": round(y / total, 4)}
```

**Build state payload helper:**
```python
def build_light_state(
    on: bool | None = None,
    brightness: float | None = None,
    color: str | None = None,
    color_temp: int | None = None,
    effect: str | None = None,
    transition_ms: int | None = None,
) -> dict:
    """Build a Hue CLIP v2 light state payload from optional params."""
    payload: dict = {}
    if on is not None:
        payload["on"] = {"on": on}
    if brightness is not None:
        payload["dimming"] = {"brightness": max(0.0, min(100.0, brightness))}
    if color is not None:
        xy = parse_color(color)
        if xy:
            payload["color"] = xy
    if color_temp is not None:
        payload["color_temperature"] = {"mirek": max(153, min(500, color_temp))}
    if effect is not None:
        payload["effects"] = {"effect": effect}
    if transition_ms is not None:
        payload["dynamics"] = {"duration": transition_ms}
    return payload
```

### Verification
```bash
cd /home/jack/REPOS/mcp-servers
python -c "from shared.hue_auth import hue_request, resolve_light, resolve_room, build_light_state, parse_color, COLOR_MAP; print('OK')"
```

---

## Phase 2: Create servers/hue.py

> **Goal:** The main server file with all 14 tools.
> **Pattern:** Follow `servers/tv.py` for device control, `servers/calculator.py` for run/main pattern.

### File: `mcp-servers/servers/hue.py`

- [ ] Create `servers/hue.py`

**Boilerplate (top of file):**
```python
"""Standalone Hue Lights MCP server.

Controls Philips Hue lights, rooms, scenes, and sensors via CLIP v2 API.

Run:
    python -m servers.hue --transport streamable-http --host 0.0.0.0 --port 9015
"""

from __future__ import annotations

import argparse

from fastmcp import FastMCP

from shared.hue_auth import (
    HUE_API_KEY,
    build_light_state,
    hue_request,
    parse_color,
    resolve_light,
    resolve_room,
    resolve_scene,
)

DEFAULT_HTTP_PORT = 9015

mcp = FastMCP("hue")

def _check_key() -> str | None:
    """Return error string if HUE_KEY is not configured."""
    if not HUE_API_KEY:
        return "HUE_KEY environment variable not set. Set it to your Hue Bridge API key."
    return None
```

**Tool 1 — `hue_list_lights`:**
```python
@mcp.tool("hue_list_lights")
async def list_lights(room: str | None = None) -> str:
    """List all Hue lights with status. Optionally filter by room name."""
    # If room specified, resolve room → get its service references (type=light) → filter
    # For each light: name, on/off, brightness, color_temp or color_xy, reachable
    # Return formatted table string
```

**Tool 2 — `hue_list_rooms`:**
```python
@mcp.tool("hue_list_rooms")
async def list_rooms() -> str:
    """List all rooms/zones with their lights."""
    # GET /room → for each room: name, grouped_light id, list of child light names
```

**Tool 3 — `hue_list_scenes`:**
```python
@mcp.tool("hue_list_scenes")
async def list_scenes(room: str | None = None) -> str:
    """List available scenes, optionally filtered by room name."""
    # GET /scene → filter by room if specified → name, room, status
```

**Tool 4 — `hue_list_devices`:**
```python
@mcp.tool("hue_list_devices")
async def list_devices() -> str:
    """List all Hue devices: lights, sensors, dimmers, bridge."""
    # GET /device → for each: name, model, type, product_name
```

**Tool 5 — `hue_set_light`:**
```python
@mcp.tool("hue_set_light")
async def set_light(
    light: str,
    on: bool | None = None,
    brightness: float | None = None,
    color: str | None = None,
    color_temp: int | None = None,
    effect: str | None = None,
    transition_ms: int | None = None,
) -> str:
    """Control a single light by name or ID.

    Args:
        light: Light name (fuzzy match) or UUID
        on: Turn on (true) or off (false)
        brightness: 0-100 percent
        color: Color name ("red", "warm white") or hex ("#FF0000")
        color_temp: Color temperature in mirek (153=cool/6500K to 500=warm/2000K)
        effect: "breathe", "candle", "fire", "sparkle", "no_effect"
        transition_ms: Transition duration in milliseconds
    """
    # resolve_light(light) → build_light_state() → PUT /light/{id}
```

**Tool 6 — `hue_set_room`:**
```python
@mcp.tool("hue_set_room")
async def set_room(
    room: str,
    on: bool | None = None,
    brightness: float | None = None,
    color: str | None = None,
    color_temp: int | None = None,
    effect: str | None = None,
    transition_ms: int | None = None,
) -> str:
    """Control all lights in a room at once.

    Args: same as hue_set_light but 'room' is a room name (fuzzy match) or UUID.
    Uses the grouped_light endpoint for atomic room control.
    """
    # resolve_room(room) → get grouped_light service id → build_light_state() → PUT /grouped_light/{id}
```

**Tool 7 — `hue_activate_scene`:**
```python
@mcp.tool("hue_activate_scene")
async def activate_scene(scene: str, room: str | None = None) -> str:
    """Activate a Hue scene by name or UUID.

    Args:
        scene: Scene name (fuzzy match) or UUID
        room: Optional room name to disambiguate scenes with same name
    """
    # resolve_scene(scene, room) → PUT /scene/{id} with {"recall": {"action": "active"}}
```

**Tool 8 — `hue_sensor_status`:**
```python
@mcp.tool("hue_sensor_status")
async def sensor_status() -> str:
    """Get status of all motion sensors, dimmers, and buttons."""
    # GET /motion → motion detected true/false, last event
    # GET /button → last event type (short_release, long_release, etc.)
    # GET /device_power → battery levels
```

**Tool 9 — `hue_list_automations`:**
```python
@mcp.tool("hue_list_automations")
async def list_automations() -> str:
    """List all bridge automations (behavior instances) with enabled/disabled status."""
    # GET /behavior_instance → name, type, enabled, description of what it does
```

**Tool 10 — `hue_toggle_automation`:**
```python
@mcp.tool("hue_toggle_automation")
async def toggle_automation(automation: str, enabled: bool) -> str:
    """Enable or disable a bridge automation.

    Args:
        automation: Automation name (fuzzy match) or UUID
        enabled: true to enable, false to disable
    """
    # Find automation by name/id → PUT /behavior_instance/{id} with {"enabled": enabled}
```

**Tool 11 — `hue_bridge_info`:**
```python
@mcp.tool("hue_bridge_info")
async def bridge_info() -> str:
    """Get Hue Bridge system info: firmware, Zigbee channel, network, connected services."""
    # GET /bridge → firmware, bridge_id
    # GET /zigbee_connectivity → channel info
```

**Tool 12 — `hue_register`:**
```python
@mcp.tool("hue_register")
async def register(app_name: str = "mcp-hue", instance_name: str = "server") -> str:
    """Register a new API user on the Hue Bridge. Press the link button first!

    Returns the new API key on success, or instructions if link button wasn't pressed.
    """
    # POST https://{bridge_ip}/api with {"devicetype": f"{app_name}#{instance_name}"}
    # This uses the v1 registration endpoint (only exception to v2-only rule — no v2 equivalent)
```

**Tool 13 — `hue_all_off`:**
```python
@mcp.tool("hue_all_off")
async def all_off() -> str:
    """Turn off every light in the house."""
    # GET /grouped_light → PUT each group with {"on": {"on": false}}
```

**Tool 14 — `hue_identify`:**
```python
@mcp.tool("hue_identify")
async def identify(light: str) -> str:
    """Flash a light to physically identify it (breathe effect for ~5 seconds).

    Args:
        light: Light name (fuzzy match) or UUID
    """
    # resolve_light → PUT /light/{id} with {"identify": {"action": "identify"}}
    # CLIP v2 has a dedicated identify action separate from effects
```

**Boilerplate (bottom of file):**
```python
def run(
    transport: str = "stdio",
    host: str = "0.0.0.0",
    port: int = DEFAULT_HTTP_PORT,
) -> None:
    if transport == "streamable-http":
        mcp.run(
            transport="streamable-http",
            host=host,
            port=port,
            json_response=True,
            stateless_http=True,
            uvicorn_config={"access_log": False},
        )
    else:
        mcp.run(transport="stdio")


def main() -> None:
    parser = argparse.ArgumentParser(description="Hue Lights MCP Server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)
    args = parser.parse_args()
    run(args.transport, args.host, args.port)


if __name__ == "__main__":
    main()


__all__ = [
    "mcp", "run", "main", "DEFAULT_HTTP_PORT",
    "list_lights", "list_rooms", "list_scenes", "list_devices",
    "set_light", "set_room", "activate_scene",
    "sensor_status", "list_automations", "toggle_automation",
    "bridge_info", "register",
    "all_off", "identify",
]
```

### Verification
```bash
cd /home/jack/REPOS/mcp-servers
python -c "from servers.hue import mcp, DEFAULT_HTTP_PORT, run, main; print('OK')"
grep -rn "from backend\|import backend" servers/hue.py shared/hue_auth.py  # must be empty
```

---

## Phase 3: Update project config files

> **Goal:** Wire the new server into pyproject.toml, deploy scripts, and dev.sh.

### 3a. pyproject.toml

- [ ] Add `hue` optional dependency group (after the `tv = [...]` block):
```toml
hue = [
    "httpx>=0.27.0",
]
```

- [ ] Add `hue` to the `all` group:
```toml
all = [
    "mcp-servers[housekeeping,shell,playwright,google,gdrive,gmail,calendar,notes,spotify,monarch,pdf,rag,tv,hue]",
]
```

### 3b. deploy/setup-systemd.sh

- [ ] Add to PORT_MAP (after `[rag]=9014`):
```bash
    [hue]=9015
```

- [ ] Add `"hue"` to DEFAULT_SERVERS array

### 3c. deploy/deploy.sh

- [ ] Add `hue` to the ALL_SERVERS array:
```bash
ALL_SERVERS=(
    housekeeping calculator shell_control playwright spotify
    gdrive gmail calendar notes pdf monarch tv rag hue
)
```

### 3d. dev.sh

- [ ] Add to PORTS array (after `[rag]=9014`):
```bash
    [hue]=9015
```

### Verification
```bash
cd /home/jack/REPOS/mcp-servers
uv sync --extra hue
python -c "from servers.hue import mcp, DEFAULT_HTTP_PORT, run, main; print('OK')"
```

---

## Phase 4: Local testing

> **Goal:** Verify the server runs, returns all 14 tools, and communicates with the live bridge.
> **Prereq:** `HUE_KEY` must be set in shell environment (bridge is at 192.168.1.4).

- [ ] Set env var: `export HUE_KEY="ymgMrWiEVx4-SZCQPhr08y2a0ak42cCS2IosE2pN"`
- [ ] Start server: `./dev.sh hue` (should start on port 9015 with hot-reload)
- [ ] Smoke test — tools/list (must return 14 tools):
```bash
curl -s http://127.0.0.1:9015/mcp -X POST \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```
- [ ] Functional test — list lights (must return ~106 lights):
```bash
curl -s http://127.0.0.1:9015/mcp -X POST \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"hue_list_lights","arguments":{}}}'
```
- [ ] Functional test — bridge info (must return firmware v2071193000):
```bash
curl -s http://127.0.0.1:9015/mcp -X POST \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"hue_bridge_info","arguments":{}}}'
```
- [ ] Functional test — set a single light (Wine bar lamp is a safe test light):
```bash
curl -s http://127.0.0.1:9015/mcp -X POST \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"hue_set_light","arguments":{"light":"Wine bar lamp","on":true,"brightness":50}}}'
```
- [ ] Backend refresh (if backend is running locally):
```bash
curl -sk -X POST https://127.0.0.1:8000/api/mcp/servers/refresh \
  -H "Content-Type: application/json" -H "Accept: application/json"
# Verify hue appears with connected: true and 14 tools
```
- [ ] Fix any issues found during testing

---

## Phase 5: Deploy to LXC 110

> **Goal:** Deploy to production on Proxmox LXC.
> **Critical note:** SSH to 192.168.1.110 directly does NOT work (password auth denied).
> Always go via Proxmox host: `ssh root@192.168.1.11` then `pct exec 110 -- bash -c '...'`

- [ ] Commit and push:
```bash
cd /home/jack/REPOS/mcp-servers
git add -A && git commit -m "Add hue lights MCP server (port 9015)" && git push origin master
```

- [ ] Pull on LXC and install deps:
```bash
ssh root@192.168.1.11 "pct exec 110 -- bash -c 'cd /opt/mcp-servers && git pull --ff-only && uv sync --extra hue'"
```

- [ ] Install systemd service:
```bash
ssh root@192.168.1.11 "pct exec 110 -- bash -c 'bash /opt/mcp-servers/deploy/setup-systemd.sh hue'"
```

- [ ] Set env vars (setup-systemd.sh overwrites .env.hue with just MCP_PORT — re-add HUE_KEY after):
```bash
ssh root@192.168.1.11 "pct exec 110 -- bash -c 'printf \"MCP_PORT=9015\nHUE_KEY=ymgMrWiEVx4-SZCQPhr08y2a0ak42cCS2IosE2pN\nHUE_BRIDGE_IP=192.168.1.4\n\" > /opt/mcp-servers/.env.hue && chown mcp:mcp /opt/mcp-servers/.env.hue && systemctl restart mcp-server@hue'"
```

- [ ] Verify service running:
```bash
ssh root@192.168.1.11 "pct exec 110 -- bash -c 'systemctl status mcp-server@hue'"
```

- [ ] Backend discovery:
```bash
curl -sk -X POST https://127.0.0.1:8000/api/mcp/servers/refresh \
  -H "Content-Type: application/json" -H "Accept: application/json"
# Verify hue appears with connected: true and 14 tools
```

---

## Phase 6: Documentation updates

> **Goal:** Update README and copilot instructions to reflect port 9015.

- [ ] Update `mcp-servers/README.md` — add to Port Assignments table:
```
| hue | 9015 |
```

- [ ] Update `mcp-servers/.github/copilot-instructions.md` — add to Port Assignments table:
```
| hue | 9015 |
```
  And update "Next available: 9015" → "Next available: 9016"

---

## Reference Data

### Hue Bridge
| Key | Value |
|-----|-------|
| Model | Hue Bridge Pro (BSB003) |
| IP | 192.168.1.4 |
| MAC | C4:29:96:BA:0C:01 |
| Bridge ID | C42996FFFECA0C01 |
| Firmware | v2071193000 |
| API Version | 1.75.0 |
| Zigbee Channel | 25 |
| Lights | 106 total (105 reachable) |

### API Endpoints (CLIP v2)
```
Base URL:  https://192.168.1.4/clip/v2/resource
Auth:      hue-application-key: <HUE_KEY>  (request header)
SSL:       verify=False  (bridge uses self-signed cert)

GET  /light                    → all lights with current state
GET  /room                     → all rooms with children references
GET  /scene                    → all configured scenes
GET  /grouped_light            → room/zone aggregate light states
GET  /device                   → all physical devices (lights, sensors, dimmers, bridge)
GET  /motion                   → motion sensor states
GET  /button                   → button/dimmer last-event states
GET  /device_power             → battery percentage for battery-powered devices
GET  /behavior_instance        → automations (schedules, sensor-triggered rules)
GET  /bridge                   → bridge hardware info and firmware version
GET  /zigbee_connectivity      → Zigbee channel and connectivity per device

PUT  /light/{id}               → set light state (on/off/brightness/color/effect)
PUT  /grouped_light/{id}       → set entire room/zone state atomically
PUT  /scene/{id}               → recall a scene: {"recall": {"action": "active"}}
PUT  /behavior_instance/{id}   → enable/disable an automation: {"enabled": true/false}
PUT  /light/{id}               → identify (flash): {"identify": {"action": "identify"}}

POST https://192.168.1.4/api   → register new user (v1 only endpoint — no v2 equivalent)
                                 body: {"devicetype": "appname#instancename"}
```

### Light State Payload (CLIP v2 PUT /light/{id})
```json
{
    "on":                {"on": true},
    "dimming":           {"brightness": 75.0},
    "color":             {"xy": {"x": 0.6750, "y": 0.3220}},
    "color_temperature": {"mirek": 250},
    "effects":           {"effect": "breathe"},
    "dynamics":          {"duration": 500},
    "identify":          {"action": "identify"}
}
```
All fields are optional — include only what you want to change.

### Rooms in the house (from Lights_Inventory.md)
Bedroom, Living Room, Kitchen, Dining Room, Bathrooms, Hallways, Zoe's Room, Outdoors, and more.

### MCP Server Boilerplate Rules
- `from __future__ import annotations` at top
- `DEFAULT_HTTP_PORT` constant at module level
- `mcp = FastMCP("name")` singleton
- All tools decorated `@mcp.tool("prefix_action")`
- All tool functions are `async def`
- Return strings (formatted text), never dicts
- Error handling: catch exceptions, return error as string (don't raise)
- `run()` → `mcp.run(transport=..., host=..., port=..., json_response=True, stateless_http=True, uvicorn_config={"access_log": False})`
- `main()` with argparse (transport/host/port args)
- `__all__` list at bottom of file
