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
| Port | 9015 (LXC 110) |
| Tools | 14 (`hue_` prefix) |
| API key env | `HUE_KEY` |
| SSL | `verify=False` (self-signed cert) |

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
