"""State-changing Hue MCP tool implementations with command auditing."""

from __future__ import annotations

import httpx

from shared.hue_audit import audited_hue_command
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


def _check_key() -> str | None:
    if not HUE_API_KEY:
        return "HUE_KEY environment variable is not set. Run hue_register to get a key."
    return None


@audited_hue_command("hue_set_light")
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
        light: Light name (fuzzy match) or UUID.
        on: True to turn on, False to turn off.
        brightness: Brightness 1–100 percent.
        color: CSS color name or '#RRGGBB' hex.
        color_temp: Color temperature in Kelvin (2000–6500).
        transition_ms: Transition duration in milliseconds.
    """
    if err := _check_key():
        return err
    light_data, err = await resolve_light(light)
    if err:
        return err
    light_name = light_data.get("metadata", {}).get("name", light)
    if color and not parse_color(color):
        return f"Unknown color '{color}'. Use a CSS color name or #RRGGBB hex."
    payload = build_light_state(on, brightness, color, color_temp, transition_ms)
    if not payload:
        return f"No changes specified for '{light_name}'."
    await hue_request("PUT", f"/clip/v2/resource/light/{light_data['id']}", json=payload)
    return f"Set '{light_name}': {', '.join(_state_parts(on, brightness, color, color_temp))}."


@audited_hue_command("hue_set_room")
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
        room: Room name (fuzzy match) or UUID.
        on: True to turn on, False to turn off.
        brightness: Brightness 1–100 percent.
        color: CSS color name or '#RRGGBB' hex.
        color_temp: Color temperature in Kelvin (2000–6500).
        transition_ms: Transition duration in milliseconds.
    """
    if err := _check_key():
        return err
    room_data, err = await resolve_room(room)
    if err:
        return err
    room_name = room_data.get("metadata", {}).get("name", room)
    grouped_id = next(
        (
            service["rid"]
            for service in room_data.get("services", [])
            if service.get("rtype") == "grouped_light"
        ),
        None,
    )
    if not grouped_id:
        return f"Room '{room_name}' has no grouped_light service."
    if color and not parse_color(color):
        return f"Unknown color '{color}'. Use a CSS color name or #RRGGBB hex."
    payload = build_light_state(on, brightness, color, color_temp, transition_ms)
    if not payload:
        return f"No changes specified for room '{room_name}'."
    await hue_request("PUT", f"/clip/v2/resource/grouped_light/{grouped_id}", json=payload)
    return f"Set room '{room_name}': {', '.join(_state_parts(on, brightness, color, color_temp))}."


def _state_parts(
    on: bool | None,
    brightness: int | None,
    color: str | None,
    color_temp: int | None,
) -> list[str]:
    parts: list[str] = []
    if on is not None:
        parts.append("on" if on else "off")
    if brightness is not None:
        parts.append(f"{brightness}% brightness")
    if color is not None:
        parts.append(f"color={color}")
    if color_temp is not None:
        parts.append(f"{color_temp}K")
    return parts


@audited_hue_command("hue_activate_scene")
async def activate_scene(scene: str, room: str | None = None) -> str:
    """Activate a Hue scene by name or UUID.

    Args:
        scene: Scene name (fuzzy match) or UUID.
        room: Optional room name to narrow ambiguous scenes.
    """
    if err := _check_key():
        return err
    scene_data, err = await resolve_scene(scene, room)
    if err:
        return err
    scene_name = scene_data.get("metadata", {}).get("name", scene)
    await hue_request(
        "PUT",
        f"/clip/v2/resource/scene/{scene_data['id']}",
        json={"recall": {"action": "active"}},
    )
    return f"Activated scene '{scene_name}'."


@audited_hue_command("hue_toggle_automation")
async def toggle_automation(automation: str, enabled: bool) -> str:
    """Enable or disable a bridge automation by name or UUID.

    Args:
        automation: Automation name (fuzzy match) or UUID.
        enabled: True to enable, False to disable.
    """
    if err := _check_key():
        return err
    automations = (await hue_request("GET", "/clip/v2/resource/behavior_instance")).get("data", [])
    target = next((item for item in automations if item.get("id") == automation), None)
    if not target:
        needle = automation.lower()
        matches = [
            item
            for item in automations
            if needle in item.get("metadata", {}).get("name", "").lower()
        ]
        if len(matches) == 1:
            target = matches[0]
        elif len(matches) > 1:
            names = ", ".join(item["metadata"]["name"] for item in matches)
            return f"Ambiguous automation name '{automation}' — matched: {names}"
        else:
            return f"Automation not found: '{automation}'"
    name = target.get("metadata", {}).get("name", automation)
    await hue_request(
        "PUT",
        f"/clip/v2/resource/behavior_instance/{target['id']}",
        json={"enabled": enabled},
    )
    return f"Automation '{name}' {'enabled' if enabled else 'disabled'}."


@audited_hue_command("hue_register", record_result=False)
async def register(app_name: str = "mcp-hue", instance_name: str = "server") -> str:
    """Register a new API user after pressing the physical Bridge link button.

    Args:
        app_name: Application name.
        instance_name: Application instance/device name.
    """
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
            lines = ["Registration successful!", f"HUE_KEY={username}"]
            if client_key:
                lines.append(f"Client key: {client_key}")
            lines.append("\nSet this as your HUE_KEY environment variable.")
            return "\n".join(lines)
        if "error" in item:
            error_type = item["error"].get("type")
            description = item["error"].get("description", "Unknown error")
            if error_type == 101:
                return (
                    "Link button not pressed. Press the button on the bridge and try "
                    "again within 30 seconds."
                )
            return f"Registration failed: {description}"
    return f"Unexpected response: {result}"


@audited_hue_command("hue_all_off")
async def all_off() -> str:
    """Turn off every light in the house."""
    if err := _check_key():
        return err
    groups = (await hue_request("GET", "/clip/v2/resource/grouped_light")).get("data", [])
    if not groups:
        return "No grouped lights found."
    errors = []
    for group in groups:
        grouped_id = group.get("id")
        if not grouped_id:
            continue
        try:
            await hue_request(
                "PUT",
                f"/clip/v2/resource/grouped_light/{grouped_id}",
                json={"on": {"on": False}},
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{grouped_id[:8]}: {exc}")
    if errors:
        return (
            f"Turned off {len(groups) - len(errors)}/{len(groups)} groups. "
            f"Errors: {'; '.join(errors)}"
        )
    return f"Turned off all lights ({len(groups)} groups)."


@audited_hue_command("hue_identify")
async def identify(light: str) -> str:
    """Flash a light to physically identify it for about five seconds.

    Args:
        light: Light name (fuzzy match) or UUID.
    """
    if err := _check_key():
        return err
    light_data, err = await resolve_light(light)
    if err:
        return err
    name = light_data.get("metadata", {}).get("name", light)
    await hue_request(
        "PUT",
        f"/clip/v2/resource/light/{light_data['id']}",
        json={"identify": {"action": "identify"}},
    )
    return f"Identifying '{name}' — it will flash for ~5 seconds."
