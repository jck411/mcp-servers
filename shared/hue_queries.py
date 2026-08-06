"""Read-only Hue MCP tool implementations."""

from __future__ import annotations

import asyncio
from typing import Any

from shared.hue_auth import HUE_API_KEY, hue_request, resolve_room


def _check_key() -> str | None:
    if not HUE_API_KEY:
        return "HUE_KEY environment variable is not set. Run hue_register to get a key."
    return None


async def list_lights(room: str | None = None) -> str:
    """List all Hue lights with their current status. Optionally filter by room name."""
    if err := _check_key():
        return err
    lights = (await hue_request("GET", "/clip/v2/resource/light")).get("data", [])
    if room:
        room_data, room_err = await resolve_room(room)
        if room_err:
            return room_err
        device_ids = {
            child["rid"]
            for child in room_data.get("children", [])
            if child.get("rtype") == "device"
        }
        lights = [light for light in lights if light.get("owner", {}).get("rid") in device_ids]
    if not lights:
        return "No lights found."

    lines = []
    for light in sorted(lights, key=lambda item: item.get("metadata", {}).get("name", "")):
        name = light.get("metadata", {}).get("name", "Unknown")
        status = "ON" if light.get("on", {}).get("on", False) else "off"
        brightness = light.get("dimming", {}).get("brightness")
        brightness_text = f"  {brightness:.0f}%" if brightness is not None else ""
        mirek = light.get("color_temperature", {}).get("mirek")
        temperature_text = f"  {round(1_000_000 / mirek)}K" if mirek else ""
        lines.append(
            f"{name:<35} {status:<4}{brightness_text:<8}{temperature_text:<10}  "
            f"[{light.get('id', '')[:8]}...]"
        )
    header = f"{'Name':<35} {'State':<4}  {'Bri':<7}  {'Temp':<9}  ID"
    return header + "\n" + "\n".join(lines)


async def list_rooms() -> str:
    """List all rooms and zones with their lights and on/off state."""
    if err := _check_key():
        return err
    rooms_data, lights_data, grouped_data = await asyncio.gather(
        hue_request("GET", "/clip/v2/resource/room"),
        hue_request("GET", "/clip/v2/resource/light"),
        hue_request("GET", "/clip/v2/resource/grouped_light"),
    )
    grouped = {item["id"]: item for item in grouped_data.get("data", [])}
    device_to_light = {
        light.get("owner", {}).get("rid"): light.get("metadata", {}).get("name", "?")
        for light in lights_data.get("data", [])
        if light.get("owner", {}).get("rid")
    }
    lines = []
    for room in sorted(
        rooms_data.get("data", []), key=lambda item: item.get("metadata", {}).get("name", "")
    ):
        name = room.get("metadata", {}).get("name", "Unknown")
        grouped_id = next(
            (
                service["rid"]
                for service in room.get("services", [])
                if service.get("rtype") == "grouped_light"
            ),
            None,
        )
        on = grouped.get(grouped_id, {}).get("on", {}).get("on")
        state = "ON" if on else "off" if on is False else "?"
        device_ids = [
            child["rid"] for child in room.get("children", []) if child.get("rtype") == "device"
        ]
        lines.append(f"\n{name} [{state}]  (id: {room.get('id', '')[:8]}...)")
        lines.extend(
            f"    • {child_name}"
            for child_name in sorted(
                device_to_light.get(device_id, f"[{device_id[:8]}]") for device_id in device_ids
            )
        )
    return "\n".join(lines).strip() if lines else "No rooms found."


async def list_scenes(room: str | None = None) -> str:
    """List available scenes, optionally filtered by room name."""
    if err := _check_key():
        return err
    scenes = (await hue_request("GET", "/clip/v2/resource/scene")).get("data", [])
    if room:
        room_data, room_err = await resolve_room(room)
        if room_err:
            return room_err
        scenes = [
            scene for scene in scenes if scene.get("group", {}).get("rid") == room_data.get("id")
        ]
    if not scenes:
        return "No scenes found."
    rooms = (await hue_request("GET", "/clip/v2/resource/room")).get("data", [])
    room_map = {item["id"]: item.get("metadata", {}).get("name", "?") for item in rooms}
    lines = []
    for scene in sorted(
        scenes,
        key=lambda item: (
            room_map.get(item.get("group", {}).get("rid"), ""),
            item.get("metadata", {}).get("name", ""),
        ),
    ):
        scene_name = scene.get("metadata", {}).get("name", "Unknown")
        room_name = room_map.get(scene.get("group", {}).get("rid", ""), "Unknown room")
        lines.append(f"{room_name:<25} {scene_name:<30}  [{scene.get('id', '')[:8]}...]")
    return f"{'Room':<25} {'Scene':<30}  ID\n" + "\n".join(lines)


async def list_devices() -> str:
    """List all Hue devices: lights, sensors, dimmers, and the bridge."""
    if err := _check_key():
        return err
    devices = (await hue_request("GET", "/clip/v2/resource/device")).get("data", [])
    if not devices:
        return "No devices found."
    lines = []
    for device in sorted(devices, key=lambda item: item.get("metadata", {}).get("name", "")):
        product = device.get("product_data", {})
        lines.append(
            f"{device.get('metadata', {}).get('name', 'Unknown'):<35} "
            f"{product.get('product_name', '?'):<30} {product.get('model_id', '?'):<15}  "
            f"[{device.get('id', '')[:8]}...]"
        )
    return f"{'Name':<35} {'Product':<30} {'Model':<15}  ID\n" + "\n".join(lines)


async def sensor_status() -> str:
    """Get status of all motion sensors, dimmers, and buttons, including battery levels."""
    if err := _check_key():
        return err
    motion_data, power_data, button_data, device_data = await asyncio.gather(
        hue_request("GET", "/clip/v2/resource/motion"),
        hue_request("GET", "/clip/v2/resource/device_power"),
        hue_request("GET", "/clip/v2/resource/button"),
        hue_request("GET", "/clip/v2/resource/device"),
    )
    device_names = {
        item["id"]: item.get("metadata", {}).get("name")
        for item in device_data.get("data", [])
        if item.get("id") and item.get("metadata", {}).get("name")
    }
    battery_map = {
        item.get("owner", {}).get("rid"): item.get("power_state", {}).get("battery_level")
        for item in power_data.get("data", [])
        if item.get("owner", {}).get("rid")
        and item.get("power_state", {}).get("battery_level") is not None
    }
    lines = ["=== Motion Sensors ==="]
    sensors = sorted(
        motion_data.get("data", []),
        key=lambda item: device_names.get(item.get("owner", {}).get("rid", ""), ""),
    )
    for sensor in sensors:
        owner_id = sensor.get("owner", {}).get("rid", "")
        name = device_names.get(owner_id, f"[{owner_id[:8]}]" if owner_id else "Unknown")
        state = "motion detected" if sensor.get("motion", {}).get("motion", False) else "clear"
        battery = battery_map.get(owner_id)
        battery_text = f"  🔋{battery}%" if battery is not None else ""
        enabled_text = "" if sensor.get("enabled", True) else "  [disabled]"
        lines.append(f"  {name:<30} {state:<18}{battery_text}{enabled_text}")

    lines.append("\n=== Buttons / Dimmers ===")
    latest: dict[str, dict[str, Any]] = {}
    for button in button_data.get("data", []):
        owner_id = button.get("owner", {}).get("rid", "")
        if not owner_id:
            continue
        updated = button.get("button", {}).get("button_report", {}).get("updated", "")
        previous = (
            latest.get(owner_id, {}).get("button", {}).get("button_report", {}).get("updated", "")
        )
        if owner_id not in latest or updated > previous:
            latest[owner_id] = button
    for owner_id, button in sorted(latest.items(), key=lambda item: device_names.get(item[0], "")):
        name = device_names.get(owner_id, f"[{owner_id[:8]}]")
        last_event = button.get("button", {}).get("last_event", "none")
        control_id = button.get("metadata", {}).get("control_id")
        control_text = f" button {control_id}" if control_id is not None else ""
        battery = battery_map.get(owner_id)
        battery_text = f"  🔋{battery}%" if battery is not None else ""
        lines.append(f"  {name:<30} last: {last_event:<20}{control_text:<10}{battery_text}")
    return "\n".join(lines)


async def list_automations() -> str:
    """List all bridge automations (behavior instances) with enabled/disabled status."""
    if err := _check_key():
        return err
    automations = (await hue_request("GET", "/clip/v2/resource/behavior_instance")).get("data", [])
    if not automations:
        return "No automations found."
    lines = []
    for automation in sorted(
        automations, key=lambda item: item.get("metadata", {}).get("name", "")
    ):
        name = automation.get("metadata", {}).get("name", "Unnamed")
        status = "enabled" if automation.get("enabled", False) else "disabled"
        script_id = automation.get("script_id", "")
        lines.append(
            f"{name:<40} {status:<10}  script={script_id[:8]}...  "
            f"[{automation.get('id', '')[:8]}...]"
        )
    return f"{'Name':<40} {'Status':<10}  Script ID         ID\n" + "\n".join(lines)


async def bridge_info() -> str:
    """Get Hue Bridge system info: firmware, Zigbee channel, network, and connected services."""
    if err := _check_key():
        return err
    bridge_data, zigbee_data = await asyncio.gather(
        hue_request("GET", "/clip/v2/resource/bridge"),
        hue_request("GET", "/clip/v2/resource/zigbee_connectivity"),
    )
    bridges = bridge_data.get("data", [])
    if not bridges:
        return "No bridge info returned."
    bridge = bridges[0]
    lines = [
        f"Bridge ID:    {bridge.get('bridge_id', '?')}",
        f"Time zone:    {bridge.get('time_zone', {}).get('time_zone', '?')}",
    ]
    zigbee = zigbee_data.get("data", [])
    channels = {item.get("channel", {}).get("value") for item in zigbee if item.get("channel")}
    if channels:
        lines.append(f"Zigbee ch:    {', '.join(str(channel) for channel in sorted(channels))}")
    lines.append(f"Zigbee devs:  {len(zigbee)}")
    return "\n".join(lines)
