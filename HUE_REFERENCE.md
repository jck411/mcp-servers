# Hue Lights — Reference

## Bridge

| Key | Value |
|-----|-------|
| Model | Hue Bridge Pro (BSB003) |
| IP | 192.168.1.4 (env: `HUE_BRIDGE_IP`) |
| Bridge ID | C42996FFFECA0C01 |
| Firmware | v2071193000 |
| API Version | 1.75.0 |
| Zigbee Channel | 25 |
| Lights | 106 total (105 reachable) |

## MCP Server

| Key | Value |
|-----|-------|
| File | `servers/hue.py` |
| Auth helper | `shared/hue_auth.py` |
| Port | 9015 (LXC 117) |
| Tools | 18 (`hue_` prefix) |
| API key env | `HUE_KEY` |
| SSL | `verify=False` (self-signed cert) |

The server is a thin MCP facade. Read-only bridge calls live in
`shared/hue_queries.py`, state-changing calls in `shared/hue_commands.py`, and
indexed history tools in `shared/hue_log_tools.py`.

## Event History

`hue-event-collector.service` runs separately from the MCP server on LXC 117.
It consumes the Hue v2 SSE stream and stores normalized events in
`/var/lib/hue-events/hue.sqlite3` using SQLite WAL mode. A separate process
means MCP restarts do not create collection gaps.

The database contains:

| Table | Purpose |
|---|---|
| `events` | One normalized row per bridge event, including the raw event entry and light before/after state |
| `current_state` | Latest resource state for restart/reconnect reconciliation; replaces periodic full snapshots |
| `health` | Collector lifecycle, connection, reconciliation, and error records |
| `commands` | Intent and outcome for state-changing calls made through this MCP server |

The collector reconciles live light/group state on startup, after reconnects,
and every five minutes. Only differences become history rows. Events, health,
and command audits use 30-day retention; current state is retained. The Hue
event stream does not identify changes made by the Hue app, Alexa, or other
controllers, so only the `commands` table provides positive attribution to MCP
requests.

History tools:

| Tool | Purpose |
|---|---|
| `hue_log_recent` | Query activity, changes, raw events, health, commands, or current state |
| `hue_log_activity` | Query normalized activity, optionally by category |
| `hue_log_search` | Indexed time-bounded text search |
| `hue_log_status` | Database size, row counts, date bounds, and recent collector health |

Operational checks:

```bash
systemctl status hue-event-collector.service mcp-server@hue.service
journalctl -u hue-event-collector.service -n 100 --no-pager
```

The one-time migration command imports legacy `hue_activity_*`,
`hue_changes_*`, and `hue_health_*` JSONL files idempotently. Legacy raw-event
files duplicate the raw entry already stored with each activity row, while
periodic snapshots are superseded by `current_state` and reconciliation.

```bash
HUE_DB_PATH=/var/lib/hue-events/hue.sqlite3 \
  .venv/bin/python -m shared.hue_import /path/to/legacy-jsonl
```

## CLIP v2 Endpoints

```
Base URL:  https://192.168.1.4/clip/v2/resource
Auth:      hue-application-key: <HUE_KEY>  (header)

GET  /light                  → all lights + state
GET  /room                   → rooms with child device refs
GET  /scene                  → all scenes
GET  /grouped_light          → room/zone aggregate state
GET  /device                 → all physical devices
GET  /motion                 → motion sensor states
GET  /button                 → dimmer/button last-event
GET  /device_power           → battery levels
GET  /behavior_instance      → automations
GET  /bridge                 → firmware, bridge_id
GET  /zigbee_connectivity    → channel + per-device connectivity

PUT  /light/{id}             → set state (on/brightness/color/effect/identify)
PUT  /grouped_light/{id}     → set entire room atomically
PUT  /scene/{id}             → recall: {"recall": {"action": "active"}}
PUT  /behavior_instance/{id} → toggle: {"enabled": true/false}

POST https://192.168.1.4/api → register user (v1 only — no v2 equivalent)
                               body: {"devicetype": "appname#instancename"}
```

## Light State Payload (PUT /light/{id})

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
All fields optional — include only what you want to change.
Color temp range: 153 (6500K cool) – 500 (2000K warm).

## Color Map (CIE xy)

| Name | x | y |
|------|---|---|
| red | 0.6750 | 0.3220 |
| green | 0.4091 | 0.5180 |
| blue | 0.1670 | 0.0400 |
| yellow | 0.4432 | 0.5154 |
| orange | 0.5562 | 0.4084 |
| purple | 0.2485 | 0.0917 |
| pink | 0.3944 | 0.1990 |
| cyan | 0.1510 | 0.3430 |
| white | 0.3127 | 0.3290 |
| warm white | 0.4596 | 0.4105 |
| cool white | 0.3174 | 0.3207 |
| candle | 0.5119 | 0.4147 |
| sunset | 0.5267 | 0.4133 |
| lavender | 0.2932 | 0.1737 |
| coral | 0.5052 | 0.3558 |
| teal | 0.1700 | 0.3400 |

Hex colors (`#RRGGBB`) are also accepted — converted via sRGB→XYZ→xy in `shared/hue_auth.py`.
