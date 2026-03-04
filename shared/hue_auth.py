"""Hue Bridge CLIP v2 auth helper for the standalone MCP server.

Provides async HTTP client, name resolution (fuzzy matching),
color parsing, and light state payload construction.

Zero imports from Backend_FastAPI — fully standalone.
"""

from __future__ import annotations

import os

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HUE_BRIDGE_IP: str = os.environ.get("HUE_BRIDGE_IP", "192.168.1.4")
HUE_API_KEY: str = os.environ.get("HUE_KEY", "")
BASE_URL: str = f"https://{HUE_BRIDGE_IP}"
CLIP_V2: str = f"{BASE_URL}/clip/v2/resource"

# ---------------------------------------------------------------------------
# Async HTTP client
# ---------------------------------------------------------------------------


def _get_headers() -> dict[str, str]:
    return {"hue-application-key": HUE_API_KEY}


async def hue_request(method: str, path: str, json: dict | None = None) -> dict:
    """Make authenticated request to Hue Bridge CLIP v2 API.

    Args:
        method: HTTP method (GET, PUT, POST, DELETE).
        path:   URL path appended to BASE_URL (e.g. '/clip/v2/resource/light').
        json:   Optional JSON body for PUT/POST requests.

    Returns:
        Parsed JSON response dict.
    """
    url = f"{BASE_URL}{path}"
    async with httpx.AsyncClient(verify=False) as client:
        response = await client.request(
            method,
            url,
            headers=_get_headers(),
            json=json,
            timeout=10.0,
        )
        response.raise_for_status()
        return response.json()


# ---------------------------------------------------------------------------
# Name resolution helpers
# ---------------------------------------------------------------------------


async def resolve_light(name_or_id: str) -> tuple[dict, str | None]:
    """Resolve a light by name (fuzzy) or UUID.

    Returns (light_data, error_or_None). On failure light_data is {}.
    Tries exact UUID match first, then case-insensitive substring on metadata.name.
    """
    data = await hue_request("GET", "/clip/v2/resource/light")
    lights = data.get("data", [])

    # Exact UUID match
    for light in lights:
        if light.get("id") == name_or_id:
            return light, None

    # Case-insensitive substring match on metadata.name
    needle = name_or_id.lower()
    matches = [l for l in lights if needle in l.get("metadata", {}).get("name", "").lower()]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        names = ", ".join(m["metadata"]["name"] for m in matches)
        return {}, f"Ambiguous light name '{name_or_id}' — matched: {names}"
    return {}, f"Light not found: '{name_or_id}'"


async def resolve_room(name_or_id: str) -> tuple[dict, str | None]:
    """Resolve a room by name (fuzzy) or UUID.

    Returns (room_data, error_or_None). On failure room_data is {}.
    Tries exact UUID match first, then case-insensitive substring on metadata.name.
    """
    data = await hue_request("GET", "/clip/v2/resource/room")
    rooms = data.get("data", [])

    # Exact UUID match
    for room in rooms:
        if room.get("id") == name_or_id:
            return room, None

    # Case-insensitive substring match on metadata.name
    needle = name_or_id.lower()
    matches = [r for r in rooms if needle in r.get("metadata", {}).get("name", "").lower()]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        names = ", ".join(m["metadata"]["name"] for m in matches)
        return {}, f"Ambiguous room name '{name_or_id}' — matched: {names}"
    return {}, f"Room not found: '{name_or_id}'"


async def resolve_scene(
    name_or_id: str, room_name: str | None = None
) -> tuple[dict, str | None]:
    """Resolve a scene by name (fuzzy) or UUID, optionally filtered by room.

    Returns (scene_data, error_or_None). On failure scene_data is {}.
    """
    data = await hue_request("GET", "/clip/v2/resource/scene")
    scenes = data.get("data", [])

    # Exact UUID match (ignores room filter — UUID is authoritative)
    for scene in scenes:
        if scene.get("id") == name_or_id:
            return scene, None

    # Filter by room UUID if provided
    candidates = scenes
    if room_name:
        room_data, room_err = await resolve_room(room_name)
        if room_err:
            return {}, f"Could not resolve room '{room_name}': {room_err}"
        room_id = room_data.get("id")
        candidates = [s for s in scenes if s.get("group", {}).get("rid") == room_id]

    needle = name_or_id.lower()
    matches = [s for s in candidates if needle in s.get("metadata", {}).get("name", "").lower()]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        names = ", ".join(m["metadata"]["name"] for m in matches)
        return {}, f"Ambiguous scene name '{name_or_id}' — matched: {names}"
    return {}, f"Scene not found: '{name_or_id}'"


# ---------------------------------------------------------------------------
# Color mapping (CSS color names → CIE xy)
# ---------------------------------------------------------------------------

# Hue uses CIE xy color space. Pre-computed from sRGB primaries.
COLOR_MAP: dict[str, dict] = {
    "red":         {"x": 0.6750, "y": 0.3220},
    "green":       {"x": 0.2151, "y": 0.7106},
    "blue":        {"x": 0.1670, "y": 0.0400},
    "white":       {"x": 0.3127, "y": 0.3290},
    "warm white":  {"x": 0.4500, "y": 0.4100},
    "cool white":  {"x": 0.2900, "y": 0.2900},
    "yellow":      {"x": 0.4323, "y": 0.5007},
    "orange":      {"x": 0.5614, "y": 0.4156},
    "purple":      {"x": 0.2725, "y": 0.1096},
    "pink":        {"x": 0.3904, "y": 0.2493},
    "cyan":        {"x": 0.1700, "y": 0.3400},
    "magenta":     {"x": 0.3827, "y": 0.1591},
    "lavender":    {"x": 0.3030, "y": 0.2210},
    "teal":        {"x": 0.1700, "y": 0.3934},
    "lime":        {"x": 0.3557, "y": 0.5668},
    "coral":       {"x": 0.5467, "y": 0.3267},
    "turquoise":   {"x": 0.1700, "y": 0.3561},
    "indigo":      {"x": 0.2206, "y": 0.1007},
    "violet":      {"x": 0.2725, "y": 0.1347},
    "amber":       {"x": 0.5290, "y": 0.4348},
    "gold":        {"x": 0.4859, "y": 0.4599},
}


def parse_color(color: str) -> dict | None:
    """Parse color string to CIE xy dict for Hue API.

    Accepts:
      1. CSS color name from COLOR_MAP (case-insensitive)
      2. '#RRGGBB' hex string
      3. Returns None if unrecognised
    """
    normalized = color.strip().lower()
    if normalized in COLOR_MAP:
        return COLOR_MAP[normalized]
    if normalized.startswith("#") and len(normalized) in (7, 4):
        try:
            return hex_to_xy(normalized)
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# Hex → CIE xy conversion
# ---------------------------------------------------------------------------


def hex_to_xy(hex_color: str) -> dict:
    """Convert #RRGGBB hex to CIE xy dict for Hue API.

    Uses sRGB → linear → XYZ → xy gamut conversion (Wide RGB D65).
    """
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        raise ValueError(f"Invalid hex color: {hex_color!r}")

    r_lin = int(h[0:2], 16) / 255.0
    g_lin = int(h[2:4], 16) / 255.0
    b_lin = int(h[4:6], 16) / 255.0

    # Gamma correction (sRGB → linear)
    def to_linear(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r = to_linear(r_lin)
    g = to_linear(g_lin)
    b = to_linear(b_lin)

    # Wide RGB D65 conversion
    X = r * 0.664511 + g * 0.154324 + b * 0.162028
    Y = r * 0.283881 + g * 0.668433 + b * 0.047685
    Z = r * 0.000088 + g * 0.072310 + b * 0.986039

    total = X + Y + Z
    if total == 0:
        return {"x": 0.3127, "y": 0.3290}  # D65 white point fallback

    return {"x": round(X / total, 4), "y": round(Y / total, 4)}


# ---------------------------------------------------------------------------
# Light state payload builder
# ---------------------------------------------------------------------------


def build_light_state(
    on: bool | None = None,
    brightness: int | None = None,
    color: str | None = None,
    color_temp: int | None = None,
    transition_ms: int | None = None,
) -> dict:
    """Build a Hue CLIP v2 light state payload from optional params.

    Args:
        on:            True/False to turn on or off.
        brightness:    0–100 percent (converted to Hue's 1–254 scale).
        color:         CSS color name or '#RRGGBB' hex string.
        color_temp:    Colour temperature in Kelvin (2000–6500 K).
        transition_ms: Transition duration in milliseconds.

    Returns:
        Dict ready to send as the JSON body of PUT /light/{id} or /grouped_light/{id}.
    """
    payload: dict = {}

    if on is not None:
        payload["on"] = {"on": on}

    if brightness is not None:
        # Clamp to 1–100 and map to Hue's 1–254 dimming scale
        pct = max(1, min(100, brightness))
        payload["dimming"] = {"brightness": pct}

    if color is not None:
        xy = parse_color(color)
        if xy:
            payload["color"] = {"xy": xy}

    if color_temp is not None:
        # Hue accepts color_temperature in mirek (1_000_000 / K)
        mirek = round(1_000_000 / max(1, color_temp))
        payload["color_temperature"] = {"mirek": mirek}

    if transition_ms is not None:
        payload["dynamics"] = {"duration": transition_ms}

    return payload
