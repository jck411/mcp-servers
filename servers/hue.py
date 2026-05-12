"""Standalone Hue Lights MCP server.

Controls Philips Hue lights, rooms, scenes, and sensors via CLIP v2 API.

Run:
    python -m servers.hue --transport streamable-http --host 0.0.0.0 --port 9015
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastmcp import FastMCP

from shared.hue_auth import (
    BASE_URL,
    HUE_API_KEY,
    build_light_state,
    hue_request,
    parse_color,
    resolve_light,
    resolve_room,
    resolve_scene,
)

DEFAULT_HTTP_PORT = 9015
HUE_LOG_DIR = Path(os.environ.get("HUE_LOG_DIR", "/var/log/hue"))
HUE_LOG_TIMEZONE = os.environ.get("HUE_TIMEZONE", "America/New_York")
LOG_PREFIXES = {
    "activity": "hue_activity",
    "changes": "hue_changes",
    "events": "hue_events",
    "health": "hue_health",
    "snapshots": "hue_snapshots",
}

mcp = FastMCP("hue")


def _check_key() -> str | None:
    """Return error string if HUE_KEY is not configured."""
    if not HUE_API_KEY:
        return "HUE_KEY environment variable is not set. Run hue_register to get a key."
    return None


def _log_timezone() -> Any:
    try:
        return ZoneInfo(HUE_LOG_TIMEZONE)
    except ZoneInfoNotFoundError:
        return datetime.now().astimezone().tzinfo


def _log_date() -> str:
    return datetime.now(_log_timezone()).date().isoformat()


def _parse_log_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_log_timezone())
    return dt


def _log_paths(kind: str, date: str | None = None) -> list[Path]:
    prefix = LOG_PREFIXES.get(kind)
    if not prefix:
        return []
    if date:
        return [HUE_LOG_DIR / f"{prefix}_{date}.jsonl"]
    return sorted(HUE_LOG_DIR.glob(f"{prefix}_*.jsonl"))


def _read_log_records(kind: str, date: str | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in _log_paths(kind, date):
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict):
                        records.append(record)
        except OSError:
            continue
    return records


def _state_text(state: dict[str, Any]) -> str:
    parts: list[str] = []
    on = state.get("on")
    if on is True:
        parts.append("ON")
    elif on is False:
        parts.append("off")
    brightness = state.get("brightness")
    if brightness is not None:
        parts.append(f"{brightness:.0f}%")
    mirek = state.get("mirek")
    if mirek is not None:
        parts.append(f"mirek={mirek}")
    if "color" in state:
        parts.append("color")
    return " ".join(parts) if parts else "state unknown"


def _format_change(record: dict[str, Any]) -> str:
    ts = record.get("ts", "?")
    name = record.get("name") or record.get("rid", "?")
    changed = ", ".join(record.get("changed") or [])
    before = _state_text(record.get("before") or {})
    after = _state_text(record.get("after") or {})
    return f"{ts}  {name}  {changed or 'change'}  {before} -> {after}"


def _format_activity(record: dict[str, Any]) -> str:
    ts = record.get("ts", "?")
    category = record.get("category") or record.get("resource_type") or "event"
    name = record.get("name") or record.get("rid") or "?"
    summary = record.get("summary")
    if summary:
        return f"{ts}  [{category}]  {summary}"
    action = record.get("action") or record.get("event_type") or "event"
    value = record.get("value")
    suffix = f" -> {value}" if value is not None else ""
    return f"{ts}  [{category}]  {name}: {action}{suffix}"


def _format_health(record: dict[str, Any]) -> str:
    parts = [record.get("ts", "?"), record.get("kind", "?")]
    if record.get("error"):
        parts.append(str(record["error"]))
    if record.get("resources") is not None:
        parts.append(f"resources={record['resources']}")
    if record.get("counts") is not None:
        parts.append(f"counts={record['counts']}")
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# Tool 1 — hue_list_lights
# ---------------------------------------------------------------------------


@mcp.tool("hue_list_lights")
async def list_lights(room: str | None = None) -> str:
    """List all Hue lights with their current status. Optionally filter by room name."""
    if err := _check_key():
        return err

    lights_data = await hue_request("GET", "/clip/v2/resource/light")
    lights = lights_data.get("data", [])

    # If room filter provided, resolve device IDs in that room
    if room:
        room_data, room_err = await resolve_room(room)
        if room_err:
            return room_err
        room_device_ids = {
            child["rid"]
            for child in room_data.get("children", [])
            if child.get("rtype") == "device"
        }
        lights = [
            light
            for light in lights
            if light.get("owner", {}).get("rid") in room_device_ids
        ]

    if not lights:
        return "No lights found."

    lines = []
    for light in sorted(
        lights,
        key=lambda light_item: light_item.get("metadata", {}).get("name", ""),
    ):
        name = light.get("metadata", {}).get("name", "Unknown")
        on_state = light.get("on", {}).get("on", False)
        status = "ON" if on_state else "off"
        dimming = light.get("dimming", {})
        bri = dimming.get("brightness")
        bri_str = f"  {bri:.0f}%" if bri is not None else ""
        color_temp = light.get("color_temperature", {})
        mirek = color_temp.get("mirek")
        mirek_str = f"  {round(1_000_000 / mirek)}K" if mirek else ""
        light_id = light.get("id", "")[:8]
        lines.append(f"{name:<35} {status:<4}{bri_str:<8}{mirek_str:<10}  [{light_id}...]")

    header = f"{'Name':<35} {'State':<4}  {'Bri':<7}  {'Temp':<9}  ID"
    return header + "\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 2 — hue_list_rooms
# ---------------------------------------------------------------------------


@mcp.tool("hue_list_rooms")
async def list_rooms() -> str:
    """List all rooms and zones with their lights and on/off state."""
    if err := _check_key():
        return err

    rooms_data = await hue_request("GET", "/clip/v2/resource/room")
    lights_data = await hue_request("GET", "/clip/v2/resource/light")
    grouped_data = await hue_request("GET", "/clip/v2/resource/grouped_light")

    rooms = rooms_data.get("data", [])
    lights = lights_data.get("data", [])
    grouped = {g["id"]: g for g in grouped_data.get("data", [])}

    # Build device → light name map
    device_to_light: dict[str, str] = {}
    for light in lights:
        owner_id = light.get("owner", {}).get("rid")
        if owner_id:
            device_to_light[owner_id] = light.get("metadata", {}).get("name", "?")

    lines = []
    for room in sorted(rooms, key=lambda r: r.get("metadata", {}).get("name", "")):
        name = room.get("metadata", {}).get("name", "Unknown")

        # Find grouped_light service id for this room
        gl_id = next(
            (s["rid"] for s in room.get("services", []) if s.get("rtype") == "grouped_light"),
            None,
        )
        gl_on = grouped.get(gl_id, {}).get("on", {}).get("on")
        state = "ON" if gl_on else ("off" if gl_on is False else "?")

        child_device_ids = [
            c["rid"] for c in room.get("children", []) if c.get("rtype") == "device"
        ]
        child_names = sorted(device_to_light.get(d, f"[{d[:8]}]") for d in child_device_ids)

        room_id = room.get("id", "")[:8]
        lines.append(f"\n{name} [{state}]  (id: {room_id}...)")
        for cn in child_names:
            lines.append(f"    • {cn}")

    return "\n".join(lines).strip() if lines else "No rooms found."


# ---------------------------------------------------------------------------
# Tool 3 — hue_list_scenes
# ---------------------------------------------------------------------------


@mcp.tool("hue_list_scenes")
async def list_scenes(room: str | None = None) -> str:
    """List available scenes, optionally filtered by room name."""
    if err := _check_key():
        return err

    scenes_data = await hue_request("GET", "/clip/v2/resource/scene")
    scenes = scenes_data.get("data", [])

    if room:
        room_data, room_err = await resolve_room(room)
        if room_err:
            return room_err
        room_id = room_data.get("id")
        scenes = [s for s in scenes if s.get("group", {}).get("rid") == room_id]

    if not scenes:
        return "No scenes found."

    # Build room id → name map
    rooms_data = await hue_request("GET", "/clip/v2/resource/room")
    room_map = {r["id"]: r.get("metadata", {}).get("name", "?") for r in rooms_data.get("data", [])}

    lines = []
    for scene in sorted(scenes, key=lambda s: (
        room_map.get(s.get("group", {}).get("rid"), ""),
        s.get("metadata", {}).get("name", ""),
    )):
        scene_name = scene.get("metadata", {}).get("name", "Unknown")
        group_id = scene.get("group", {}).get("rid", "")
        room_name = room_map.get(group_id, "Unknown room")
        scene_id = scene.get("id", "")[:8]
        lines.append(f"{room_name:<25} {scene_name:<30}  [{scene_id}...]")

    header = f"{'Room':<25} {'Scene':<30}  ID"
    return header + "\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 4 — hue_list_devices
# ---------------------------------------------------------------------------


@mcp.tool("hue_list_devices")
async def list_devices() -> str:
    """List all Hue devices: lights, sensors, dimmers, and the bridge."""
    if err := _check_key():
        return err

    devices_data = await hue_request("GET", "/clip/v2/resource/device")
    devices = devices_data.get("data", [])

    if not devices:
        return "No devices found."

    lines = []
    for device in sorted(devices, key=lambda d: d.get("metadata", {}).get("name", "")):
        name = device.get("metadata", {}).get("name", "Unknown")
        product = device.get("product_data", {})
        model = product.get("model_id", "?")
        product_name = product.get("product_name", "?")
        device_id = device.get("id", "")[:8]
        lines.append(f"{name:<35} {product_name:<30} {model:<15}  [{device_id}...]")

    header = f"{'Name':<35} {'Product':<30} {'Model':<15}  ID"
    return header + "\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 5 — hue_set_light
# ---------------------------------------------------------------------------


@mcp.tool("hue_set_light")
async def set_light(
    light: str,
    on: bool | None = None,
    brightness: int | None = None,
    color: str | None = None,
    color_temp: int | None = None,
    transition_ms: int | None = None,
) -> str:
    """Control a single light by name or UUID.

    Args:
        light:         Light name (fuzzy match) or UUID.
        on:            True to turn on, False to turn off.
        brightness:    Brightness 1–100 percent.
        color:         CSS color name (e.g. 'red', 'warm white') or '#RRGGBB' hex.
        color_temp:    Color temperature in Kelvin (2000–6500).
        transition_ms: Transition duration in milliseconds.
    """
    if err := _check_key():
        return err

    light_data, err = await resolve_light(light)
    if err:
        return err

    light_id = light_data["id"]
    light_name = light_data.get("metadata", {}).get("name", light)

    if color and not parse_color(color):
        return f"Unknown color '{color}'. Use a CSS color name or #RRGGBB hex."

    payload = build_light_state(on, brightness, color, color_temp, transition_ms)

    if not payload:
        return f"No changes specified for '{light_name}'."

    await hue_request("PUT", f"/clip/v2/resource/light/{light_id}", json=payload)
    parts = []
    if on is not None:
        parts.append("on" if on else "off")
    if brightness is not None:
        parts.append(f"{brightness}% brightness")
    if color is not None:
        parts.append(f"color={color}")
    if color_temp is not None:
        parts.append(f"{color_temp}K")
    return f"Set '{light_name}': {', '.join(parts)}."


# ---------------------------------------------------------------------------
# Tool 6 — hue_set_room
# ---------------------------------------------------------------------------


@mcp.tool("hue_set_room")
async def set_room(
    room: str,
    on: bool | None = None,
    brightness: int | None = None,
    color: str | None = None,
    color_temp: int | None = None,
    transition_ms: int | None = None,
) -> str:
    """Control all lights in a room at once.

    Args:
        room:          Room name (fuzzy match) or UUID.
        on:            True to turn on, False to turn off.
        brightness:    Brightness 1–100 percent.
        color:         CSS color name or '#RRGGBB' hex.
        color_temp:    Color temperature in Kelvin (2000–6500).
        transition_ms: Transition duration in milliseconds.
    """
    if err := _check_key():
        return err

    room_data, err = await resolve_room(room)
    if err:
        return err

    room_name = room_data.get("metadata", {}).get("name", room)

    # Find the grouped_light service id for this room
    gl_id = next(
        (s["rid"] for s in room_data.get("services", []) if s.get("rtype") == "grouped_light"),
        None,
    )
    if not gl_id:
        return f"Room '{room_name}' has no grouped_light service."

    if color and not parse_color(color):
        return f"Unknown color '{color}'. Use a CSS color name or #RRGGBB hex."

    payload = build_light_state(on, brightness, color, color_temp, transition_ms)
    if not payload:
        return f"No changes specified for room '{room_name}'."

    await hue_request("PUT", f"/clip/v2/resource/grouped_light/{gl_id}", json=payload)
    parts = []
    if on is not None:
        parts.append("on" if on else "off")
    if brightness is not None:
        parts.append(f"{brightness}% brightness")
    if color is not None:
        parts.append(f"color={color}")
    if color_temp is not None:
        parts.append(f"{color_temp}K")
    return f"Set room '{room_name}': {', '.join(parts)}."


# ---------------------------------------------------------------------------
# Tool 7 — hue_activate_scene
# ---------------------------------------------------------------------------


@mcp.tool("hue_activate_scene")
async def activate_scene(scene: str, room: str | None = None) -> str:
    """Activate a Hue scene by name or UUID.

    Args:
        scene: Scene name (fuzzy match) or UUID.
        room:  Optional room name to narrow the search when scene names are ambiguous.
    """
    if err := _check_key():
        return err

    scene_data, err = await resolve_scene(scene, room)
    if err:
        return err

    scene_id = scene_data["id"]
    scene_name = scene_data.get("metadata", {}).get("name", scene)

    await hue_request(
        "PUT",
        f"/clip/v2/resource/scene/{scene_id}",
        json={"recall": {"action": "active"}},
    )
    return f"Activated scene '{scene_name}'."


# ---------------------------------------------------------------------------
# Tool 8 — hue_sensor_status
# ---------------------------------------------------------------------------


@mcp.tool("hue_sensor_status")
async def sensor_status() -> str:
    """Get status of all motion sensors, dimmers, and buttons, including battery levels."""
    if err := _check_key():
        return err

    motion_data = await hue_request("GET", "/clip/v2/resource/motion")
    power_data = await hue_request("GET", "/clip/v2/resource/device_power")
    button_data = await hue_request("GET", "/clip/v2/resource/button")

    # Build device_id → battery% map
    battery_map: dict[str, int] = {}
    for dp in power_data.get("data", []):
        owner_id = dp.get("owner", {}).get("rid")
        pct = dp.get("power_state", {}).get("battery_level")
        if owner_id and pct is not None:
            battery_map[owner_id] = pct

    lines = ["=== Motion Sensors ==="]
    for sensor in motion_data.get("data", []):
        owner_id = sensor.get("owner", {}).get("rid", "")
        name = sensor.get("metadata", {}).get("name", "Unknown")
        detected = sensor.get("motion", {}).get("motion", False)
        enabled = sensor.get("enabled", True)
        battery = battery_map.get(owner_id)
        batt_str = f"  🔋{battery}%" if battery is not None else ""
        state_str = "motion detected" if detected else "clear"
        ena_str = "" if enabled else "  [disabled]"
        lines.append(f"  {name:<30} {state_str:<18}{batt_str}{ena_str}")

    lines.append("\n=== Buttons / Dimmers ===")
    for btn in button_data.get("data", []):
        owner_id = btn.get("owner", {}).get("rid", "")
        name = btn.get("metadata", {}).get("name", "Unknown")
        last_event = btn.get("button", {}).get("last_event", "none")
        battery = battery_map.get(owner_id)
        batt_str = f"  🔋{battery}%" if battery is not None else ""
        lines.append(f"  {name:<30} last: {last_event:<20}{batt_str}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 9 — hue_list_automations
# ---------------------------------------------------------------------------


@mcp.tool("hue_list_automations")
async def list_automations() -> str:
    """List all bridge automations (behavior instances) with enabled/disabled status."""
    if err := _check_key():
        return err

    data = await hue_request("GET", "/clip/v2/resource/behavior_instance")
    automations = data.get("data", [])

    if not automations:
        return "No automations found."

    lines = []
    for auto in sorted(automations, key=lambda a: a.get("metadata", {}).get("name", "")):
        name = auto.get("metadata", {}).get("name", "Unnamed")
        enabled = auto.get("enabled", False)
        auto_id = auto.get("id", "")[:8]
        status = "enabled" if enabled else "disabled"
        script_id = auto.get("script_id", "")
        lines.append(f"{name:<40} {status:<10}  script={script_id[:8]}...  [{auto_id}...]")

    header = f"{'Name':<40} {'Status':<10}  Script ID         ID"
    return header + "\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 10 — hue_toggle_automation
# ---------------------------------------------------------------------------


@mcp.tool("hue_toggle_automation")
async def toggle_automation(automation: str, enabled: bool) -> str:
    """Enable or disable a bridge automation by name or UUID.

    Args:
        automation: Automation name (fuzzy match) or UUID.
        enabled:    True to enable, False to disable.
    """
    if err := _check_key():
        return err

    data = await hue_request("GET", "/clip/v2/resource/behavior_instance")
    automations = data.get("data", [])

    # Exact UUID match first
    target = next((a for a in automations if a.get("id") == automation), None)

    if not target:
        needle = automation.lower()
        matches = [
            a for a in automations
            if needle in a.get("metadata", {}).get("name", "").lower()
        ]
        if len(matches) == 1:
            target = matches[0]
        elif len(matches) > 1:
            names = ", ".join(m["metadata"]["name"] for m in matches)
            return f"Ambiguous automation name '{automation}' — matched: {names}"
        else:
            return f"Automation not found: '{automation}'"

    auto_id = target["id"]
    auto_name = target.get("metadata", {}).get("name", automation)
    await hue_request(
        "PUT",
        f"/clip/v2/resource/behavior_instance/{auto_id}",
        json={"enabled": enabled},
    )
    state = "enabled" if enabled else "disabled"
    return f"Automation '{auto_name}' {state}."


# ---------------------------------------------------------------------------
# Tool 11 — hue_bridge_info
# ---------------------------------------------------------------------------


@mcp.tool("hue_bridge_info")
async def bridge_info() -> str:
    """Get Hue Bridge system info: firmware, Zigbee channel, network, and connected services."""
    if err := _check_key():
        return err

    bridge_data = await hue_request("GET", "/clip/v2/resource/bridge")
    zigbee_data = await hue_request("GET", "/clip/v2/resource/zigbee_connectivity")

    bridges = bridge_data.get("data", [])
    if not bridges:
        return "No bridge info returned."

    bridge = bridges[0]
    bridge_id = bridge.get("bridge_id", "?")
    time_zone = bridge.get("time_zone", {}).get("time_zone", "?")

    lines = [
        f"Bridge ID:    {bridge_id}",
        f"Time zone:    {time_zone}",
    ]

    # Zigbee info
    zigbee_list = zigbee_data.get("data", [])
    channels = {z.get("channel", {}).get("value") for z in zigbee_list if z.get("channel")}
    if channels:
        lines.append(f"Zigbee ch:    {', '.join(str(c) for c in sorted(channels))}")
    lines.append(f"Zigbee devs:  {len(zigbee_list)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 12 — hue_register
# ---------------------------------------------------------------------------


@mcp.tool("hue_register")
async def register(app_name: str = "mcp-hue", instance_name: str = "server") -> str:
    """Register a new API user on the Hue Bridge.

    Press the physical link button on the bridge before calling this tool.
    The returned key should be set as the HUE_KEY environment variable.

    Args:
        app_name:      Application name (default: 'mcp-hue').
        instance_name: Instance/device name (default: 'server').
    """
    # Uses v1 registration endpoint — no v2 equivalent exists
    import httpx

    url = f"https://{BASE_URL.split('://')[-1]}/api"
    payload = {
        "devicetype": f"{app_name}#{instance_name}",
        "generateclientkey": True,
    }
    async with httpx.AsyncClient(verify=False) as client:
        response = await client.post(url, json=payload, timeout=10.0)
        response.raise_for_status()
        result = response.json()

    if isinstance(result, list) and result:
        item = result[0]
        if "success" in item:
            username = item["success"].get("username", "?")
            client_key = item["success"].get("clientkey", "")
            lines = [
                "Registration successful!",
                f"HUE_KEY={username}",
            ]
            if client_key:
                lines.append(f"Client key: {client_key}")
            lines.append("\nSet this as your HUE_KEY environment variable.")
            return "\n".join(lines)
        if "error" in item:
            err_type = item["error"].get("type")
            desc = item["error"].get("description", "Unknown error")
            if err_type == 101:
                return (
                    "Link button not pressed. Press the button on the bridge and try "
                    "again within 30 seconds."
                )
            return f"Registration failed: {desc}"

    return f"Unexpected response: {result}"


# ---------------------------------------------------------------------------
# Tool 13 — hue_all_off
# ---------------------------------------------------------------------------


@mcp.tool("hue_all_off")
async def all_off() -> str:
    """Turn off every light in the house."""
    if err := _check_key():
        return err

    gl_data = await hue_request("GET", "/clip/v2/resource/grouped_light")
    groups = gl_data.get("data", [])

    if not groups:
        return "No grouped lights found."

    errors = []
    for group in groups:
        gl_id = group.get("id")
        if not gl_id:
            continue
        try:
            await hue_request(
                "PUT",
                f"/clip/v2/resource/grouped_light/{gl_id}",
                json={"on": {"on": False}},
            )
        except Exception as exc:
            errors.append(f"{gl_id[:8]}: {exc}")

    if errors:
        return (
            f"Turned off {len(groups) - len(errors)}/{len(groups)} groups. "
            f"Errors: {'; '.join(errors)}"
        )
    return f"Turned off all lights ({len(groups)} groups)."


# ---------------------------------------------------------------------------
# Tool 14 — hue_identify
# ---------------------------------------------------------------------------


@mcp.tool("hue_identify")
async def identify(light: str) -> str:
    """Flash a light to physically identify it (breathe effect for ~5 seconds).

    Args:
        light: Light name (fuzzy match) or UUID.
    """
    if err := _check_key():
        return err

    light_data, err = await resolve_light(light)
    if err:
        return err

    light_id = light_data["id"]
    light_name = light_data.get("metadata", {}).get("name", light)

    await hue_request(
        "PUT",
        f"/clip/v2/resource/light/{light_id}",
        json={"identify": {"action": "identify"}},
    )
    return f"Identifying '{light_name}' — it will flash for ~5 seconds."


# ---------------------------------------------------------------------------
# Tool 15 — hue_log_recent
# ---------------------------------------------------------------------------


async def _log_recent_impl(
    query: str | None = None,
    light: str | None = None,
    kind: str = "activity",
    category: str | None = None,
    minutes: int = 60,
    limit: int = 50,
    date: str | None = None,
) -> str:
    """Shared implementation for the Hue log tools."""
    if kind not in LOG_PREFIXES:
        return f"Unknown kind '{kind}'. Use one of: {', '.join(LOG_PREFIXES)}."
    if limit < 1:
        return "limit must be at least 1."
    limit = min(limit, 200)

    if not HUE_LOG_DIR.exists():
        return f"Hue log directory not found: {HUE_LOG_DIR}"

    records = _read_log_records(kind, date)
    if not records:
        target = date or _log_date()
        return f"No Hue {kind} logs found for {target} in {HUE_LOG_DIR}."

    cutoff: datetime | None = None
    if date is None:
        cutoff = datetime.now(_log_timezone()) - timedelta(minutes=max(1, minutes))

    effective_query = query if query is not None else light
    needle = effective_query.lower() if effective_query else None
    category_filter = category.lower() if category else None
    filtered: list[dict[str, Any]] = []
    for record in records:
        ts = _parse_log_ts(record.get("ts"))
        if cutoff and (not ts or ts < cutoff):
            continue
        if category_filter and (record.get("category") or "").lower() != category_filter:
            continue
        if needle and needle not in json.dumps(record, ensure_ascii=False).lower():
            continue
        filtered.append(record)

    filtered.sort(key=lambda item: item.get("ts", ""))
    filtered = filtered[-limit:]
    if not filtered:
        scope = f"date {date}" if date else f"last {minutes} minutes"
        filters = []
        if effective_query:
            filters.append(f"matching '{effective_query}'")
        if category:
            filters.append(f"category={category}")
        suffix = f" ({', '.join(filters)})" if filters else ""
        return f"No Hue {kind} records found for {scope}{suffix}."

    lines = [f"Hue {kind} ({len(filtered)} shown, newest last):"]
    for record in filtered:
        if kind == "activity":
            lines.append(_format_activity(record))
        elif kind == "changes":
            lines.append(_format_change(record))
        elif kind == "health":
            lines.append(_format_health(record))
        else:
            ts = record.get("ts", "?")
            record_name = (
                record.get("name")
                or record.get("kind")
                or record.get("resource_type")
                or "record"
            )
            lines.append(
                f"{ts}  {record_name}  {json.dumps(record, ensure_ascii=False)[:500]}"
            )
    return "\n".join(lines)


@mcp.tool("hue_log_recent")
async def log_recent(
    query: str | None = None,
    light: str | None = None,
    kind: str = "activity",
    category: str | None = None,
    minutes: int = 60,
    limit: int = 50,
    date: str | None = None,
) -> str:
    """Show recent Hue activity from the local rolling logs.

    Args:
        query:    Optional case-insensitive text filter over the record.
        light:    Backward-compatible alias for query.
        kind:     One of activity, changes, events, health, snapshots.
        category: Optional activity category filter, e.g. light, input, sensor,
                  automation, scene, device, metadata. Used with kind=activity.
        minutes:  Look back this many minutes. Ignored when date is provided.
        limit:    Maximum rows to return.
        date:     Optional log date as YYYY-MM-DD; scans that whole local date.
    """
    return await _log_recent_impl(
        query=query,
        light=light,
        kind=kind,
        category=category,
        minutes=minutes,
        limit=limit,
        date=date,
    )


# ---------------------------------------------------------------------------
# Tool 16 — hue_log_status
# ---------------------------------------------------------------------------


@mcp.tool("hue_log_status")
async def log_status() -> str:
    """Summarize Hue logger health, files, and latest snapshot counts."""
    if not HUE_LOG_DIR.exists():
        return f"Hue log directory not found: {HUE_LOG_DIR}"

    lines = [f"Hue log directory: {HUE_LOG_DIR}"]
    for kind, prefix in LOG_PREFIXES.items():
        paths = sorted(HUE_LOG_DIR.glob(f"{prefix}_*.jsonl"))
        if not paths:
            lines.append(f"{kind:<9} no files")
            continue
        newest = paths[-1]
        size_kb = newest.stat().st_size / 1024
        lines.append(f"{kind:<9} {len(paths)} file(s), latest {newest.name} ({size_kb:.1f} KB)")

    health = _read_log_records("health")
    if health:
        lines.append("\nRecent health:")
        for record in health[-8:]:
            lines.append("  " + _format_health(record))

    snapshots = _read_log_records("snapshots")
    if snapshots:
        latest = snapshots[-1]
        lines.append("\nLatest snapshot:")
        lines.append(f"  {latest.get('ts', '?')} counts={latest.get('counts', {})}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 17 — hue_log_activity
# ---------------------------------------------------------------------------


@mcp.tool("hue_log_activity")
async def log_activity(
    category: str | None = None,
    query: str | None = None,
    minutes: int = 180,
    limit: int = 80,
    date: str | None = None,
) -> str:
    """Show normalized Hue activity, including buttons, automations, scenes, sensors, and lights.

    Args:
        category: Optional category filter: light, input, sensor, automation,
                  scene, device, metadata.
        query:    Optional case-insensitive text filter over the activity row.
        minutes:  Look back this many minutes. Ignored when date is provided.
        limit:    Maximum rows to return.
        date:     Optional log date as YYYY-MM-DD; scans that whole local date.
    """
    return await _log_recent_impl(
        query=query,
        kind="activity",
        category=category,
        minutes=minutes,
        limit=limit,
        date=date,
    )


# ---------------------------------------------------------------------------
# Tool 18 — hue_log_search
# ---------------------------------------------------------------------------


@mcp.tool("hue_log_search")
async def log_search(
    query: str,
    kind: str = "changes",
    date: str | None = None,
    limit: int = 50,
) -> str:
    """Search Hue JSONL logs by plain text.

    Args:
        query: Text to search for, case-insensitive.
        kind:  One of activity, changes, events, health, snapshots.
        date:  Optional log date as YYYY-MM-DD.
        limit: Maximum matching rows to return.
    """
    if kind not in LOG_PREFIXES:
        return f"Unknown kind '{kind}'. Use one of: {', '.join(LOG_PREFIXES)}."
    if not query.strip():
        return "query is required."
    limit = min(max(limit, 1), 100)
    needle = query.lower()

    matches: list[dict[str, Any]] = []
    for record in _read_log_records(kind, date):
        if needle in json.dumps(record, ensure_ascii=False).lower():
            matches.append(record)

    matches = matches[-limit:]
    if not matches:
        return f"No matches for '{query}' in Hue {kind} logs."

    lines = [f"Hue {kind} matches for '{query}' ({len(matches)} shown):"]
    for record in matches:
        if kind == "activity":
            lines.append(_format_activity(record))
        elif kind == "changes":
            lines.append(_format_change(record))
        elif kind == "health":
            lines.append(_format_health(record))
        else:
            ts = record.get("ts", "?")
            record_name = (
                record.get("name")
                or record.get("kind")
                or record.get("resource_type")
                or "record"
            )
            lines.append(
                f"{ts}  {record_name}  {json.dumps(record, ensure_ascii=False)[:500]}"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Server entrypoints
# ---------------------------------------------------------------------------


def run(
    transport: str = "stdio",
    host: str = "0.0.0.0",
    port: int = DEFAULT_HTTP_PORT,
) -> None:  # pragma: no cover - integration entrypoint
    """Run the MCP server with the specified transport."""
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


def main() -> None:  # pragma: no cover - CLI helper
    parser = argparse.ArgumentParser(description="Hue Lights MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport protocol to use",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind HTTP server to",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help="Port for HTTP server",
    )
    args = parser.parse_args()
    run(args.transport, args.host, args.port)


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "mcp",
    "run",
    "main",
    "DEFAULT_HTTP_PORT",
    # List tools
    "list_lights",
    "list_rooms",
    "list_scenes",
    "list_devices",
    # Control tools
    "set_light",
    "set_room",
    "activate_scene",
    # Sensor / info tools
    "sensor_status",
    "list_automations",
    "toggle_automation",
    "bridge_info",
    # Utility tools
    "register",
    "all_off",
    "identify",
    # Log tools
    "log_recent",
    "log_status",
    "log_activity",
    "log_search",
]
