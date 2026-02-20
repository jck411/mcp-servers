"""Standalone TV MCP server.

Controls LG WebOS TVs (living room, bedroom) and Google TV Streamers
via WebSocket (bscpylgtv), Wake-on-LAN, and Android TV Remote Protocol.

Run:
    python -m servers.tv --transport streamable-http --host 0.0.0.0 --port 9013
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Optional

from fastmcp import FastMCP

# Default port for HTTP transport
DEFAULT_HTTP_PORT = 9013

mcp = FastMCP("tv")

# ── Paths ──
CREDENTIALS_DIR = Path(__file__).parent.parent / "credentials"
DATA_DIR = Path(__file__).parent.parent / "data"

# LG TV pairing key storage
LG_KEY_DB = DATA_DIR / "lg_keys.sqlite"

# Android TV Remote certs (for Google TV Streamers)
REMOTE_CERTS_DIR = CREDENTIALS_DIR

# ── Device Registry ──
LG_DEVICES = {
    "living": {
        "name": "LG C5 77\" (Living Room)",
        "ip": "192.168.1.17",
        "mac": "30:34:DB:77:15:5A",
        "default_input": "HDMI_1",
    },
    "bedroom": {
        "name": "LG CX 55\" (Bedroom)",
        "ip": "192.168.1.22",
        "mac": "60:8D:26:70:4F:AA",
        "default_input": "HDMI_1",
    },
}

STREAMER_DEVICES = {
    "living": {
        "name": "Streamer-LivingRoom",
        "ip": "192.168.1.15",
        "mac": "3E:66:21:1B:AA:45",
    },
    "bedroom": {
        "name": "Streamer-Bedroom",
        "ip": "192.168.1.10",
        "mac": "9A:50:E8:28:2F:89",
    },
}

ROOM_ALIASES = {
    "lr": "living",
    "livingroom": "living",
    "live": "living",
    "br": "bedroom",
    "bed": "bedroom",
}

# Streaming app package names (fallback for apps without deep links)
APP_PACKAGES = {
    "netflix": "com.netflix.ninja",
    "plex": "com.plexapp.android",
    "youtube": "com.google.android.youtube.tv",
    "disney": "com.disney.disneyplus",
    "hulu": "com.hulu.livingroomplus",
    "prime": "com.amazon.amazonvideo.livingroom",
    "apple": "com.apple.atve.androidtv.appletv",
    "peacock": "com.peacocktv.peacockandroid",
    "spotify": "com.spotify.tv.android",
    "hbo": "com.wbd.stream",
    "pbs": "org.pbskids.video",
    "espn": "com.espn.score_center",
    "ytmusic": "com.google.android.youtube.tvmusic",
}

# Deep link URIs for apps that support them (preferred over package names)
APP_DEEP_LINKS = {
    "netflix": "https://www.netflix.com/title",
    "plex": "plex://",
    "youtube": "https://www.youtube.com",
    "disney": "https://www.disneyplus.com",
    "prime": "https://app.primevideo.com",
    "apple": "https://tv.apple.com",
    "spotify": "spotify://",
    "hbo": "https://play.hbomax.com",
    "ytmusic": "https://music.youtube.com",
}


def _resolve_room(room: str) -> str:
    """Normalize room aliases to canonical room name."""
    room = room.lower().replace("-", "").replace("_", "")
    return ROOM_ALIASES.get(room, room)


def _get_lg_device(room: str) -> dict | None:
    """Get LG TV device config for a room."""
    room = _resolve_room(room)
    return LG_DEVICES.get(room)


def _get_streamer_device(room: str) -> dict | None:
    """Get Google TV Streamer device config for a room."""
    room = _resolve_room(room)
    return STREAMER_DEVICES.get(room)


# ── LG TV Helpers ──


async def _get_lg_client(device: dict, timeout: int = 5):
    """Connect to LG TV and return WebOsClient."""
    from bscpylgtv import WebOsClient

    client = await WebOsClient.create(
        device["ip"],
        key_file_path=str(LG_KEY_DB),
        timeout_connect=timeout,
    )
    await client.connect()
    return client


# ── Android TV Remote Helpers ──


def _get_remote_cert_paths(room: str) -> tuple[str, str]:
    """Return (certfile, keyfile) paths for Android TV Remote."""
    room = _resolve_room(room)
    return (
        str(REMOTE_CERTS_DIR / f"remote-cert-{room}.pem"),
        str(REMOTE_CERTS_DIR / f"remote-key-{room}.pem"),
    )


async def _get_remote_client(room: str):
    """Connect to Google TV Streamer via Android TV Remote Protocol."""
    from androidtvremote2 import AndroidTVRemote, CannotConnect, InvalidAuth

    device = _get_streamer_device(room)
    if not device:
        raise ValueError(f"Unknown room: {room}")

    certfile, keyfile = _get_remote_cert_paths(room)
    remote = AndroidTVRemote(
        client_name="mcp-tv-control",
        certfile=certfile,
        keyfile=keyfile,
        host=device["ip"],
    )

    try:
        await remote.async_connect()
    except InvalidAuth:
        raise ValueError(f"Not paired with {device['name']}. Run pairing first.")
    except CannotConnect:
        raise ValueError(f"Cannot reach {device['name']} at {device['ip']}")

    return remote


def _normalize_key(key: str) -> str:
    """Normalize key name: DPAD_UP → KEYCODE_DPAD_UP."""
    k = key.upper()
    if not k.startswith("KEYCODE_"):
        k = f"KEYCODE_{k}"
    return k


# ===========================================================================
# LG TV Tools
# ===========================================================================


@mcp.tool("tv_lg_power_on")
async def lg_power_on(room: str = "living") -> str:
    """Wake up an LG TV using Wake-on-LAN and set default input.

    The TV will take 40-60 seconds to become fully responsive after WoL.

    Args:
        room: Room name (living, bedroom, lr, br)

    Returns:
        Status message.
    """
    from wakeonlan import send_magic_packet

    device = _get_lg_device(room)
    if not device:
        return f"Unknown room: {room}. Available: {', '.join(LG_DEVICES.keys())}"

    mac = device["mac"]
    default_input = device.get("default_input")

    # Send WoL twice for reliability
    send_magic_packet(mac)
    await asyncio.sleep(1)
    send_magic_packet(mac)

    result = f"{device['name']} → WoL sent to {mac}"

    # Wait and try to set input
    if default_input:
        await asyncio.sleep(10)
        for attempt in range(25):
            try:
                client = await _get_lg_client(device, timeout=3)
                await client.set_input(default_input)
                await client.disconnect()
                elapsed = 10 + (attempt + 1) * 2
                return f"{result}. Input set to {default_input} (ready after ~{elapsed}s)"
            except Exception:
                pass
            await asyncio.sleep(2)
        return f"{result}. TV may need more time to boot."

    return result


@mcp.tool("tv_lg_power_off")
async def lg_power_off(room: str = "living") -> str:
    """Power off (standby) an LG TV.

    Args:
        room: Room name (living, bedroom, lr, br)

    Returns:
        Status message.
    """
    device = _get_lg_device(room)
    if not device:
        return f"Unknown room: {room}. Available: {', '.join(LG_DEVICES.keys())}"

    try:
        client = await _get_lg_client(device)
        await client.power_off()
        await client.disconnect()
        return f"{device['name']} → powered off (standby)"
    except Exception as e:
        return f"Error powering off {device['name']}: {e}"


@mcp.tool("tv_lg_screen_off")
async def lg_screen_off(room: str = "living") -> str:
    """Turn off the screen while keeping audio playing on an LG TV.

    Useful for music playback without the screen draining power.

    Args:
        room: Room name (living, bedroom, lr, br)

    Returns:
        Status message.
    """
    device = _get_lg_device(room)
    if not device:
        return f"Unknown room: {room}. Available: {', '.join(LG_DEVICES.keys())}"

    try:
        client = await _get_lg_client(device)
        await client.turn_screen_off()
        await client.disconnect()
        return f"{device['name']} → screen off (audio continues)"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool("tv_lg_screen_on")
async def lg_screen_on(room: str = "living") -> str:
    """Turn the LG TV screen back on.

    Args:
        room: Room name (living, bedroom, lr, br)

    Returns:
        Status message.
    """
    device = _get_lg_device(room)
    if not device:
        return f"Unknown room: {room}. Available: {', '.join(LG_DEVICES.keys())}"

    try:
        client = await _get_lg_client(device)
        await client.turn_screen_on()
        await client.disconnect()
        return f"{device['name']} → screen on"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool("tv_lg_volume")
async def lg_volume(room: str = "living", level: Optional[int] = None) -> str:
    """Get or set the volume on an LG TV.

    Args:
        room: Room name (living, bedroom, lr, br)
        level: Volume level 0-100 (omit to get current volume)

    Returns:
        Current volume or confirmation of change.
    """
    device = _get_lg_device(room)
    if not device:
        return f"Unknown room: {room}. Available: {', '.join(LG_DEVICES.keys())}"

    try:
        client = await _get_lg_client(device)
        if level is None:
            vol = await client.get_volume()
            muted = await client.get_muted()
            await client.disconnect()
            return f"Volume: {vol}" + (" (muted)" if muted else "")
        else:
            await client.set_volume(int(level))
            await client.disconnect()
            return f"{device['name']} → volume {level}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool("tv_lg_mute")
async def lg_mute(room: str = "living", state: Optional[str] = None) -> str:
    """Toggle or set mute on an LG TV.

    Args:
        room: Room name (living, bedroom, lr, br)
        state: "on" to mute, "off" to unmute, omit to toggle

    Returns:
        Status message.
    """
    device = _get_lg_device(room)
    if not device:
        return f"Unknown room: {room}. Available: {', '.join(LG_DEVICES.keys())}"

    try:
        client = await _get_lg_client(device)
        if state is None:
            current = await client.get_muted()
            await client.set_mute(not current)
            await client.disconnect()
            return f"{device['name']} → {'unmuted' if current else 'muted'}"
        else:
            mute = state.lower() in ("on", "true", "1", "yes")
            await client.set_mute(mute)
            await client.disconnect()
            return f"{device['name']} → {'muted' if mute else 'unmuted'}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool("tv_lg_input")
async def lg_input(room: str = "living", input_id: Optional[str] = None) -> str:
    """Get or set the HDMI input on an LG TV.

    Args:
        room: Room name (living, bedroom, lr, br)
        input_id: HDMI input (HDMI_1, HDMI_2, HDMI_3, HDMI_4) or omit to get current

    Returns:
        Current input or confirmation of change.
    """
    device = _get_lg_device(room)
    if not device:
        return f"Unknown room: {room}. Available: {', '.join(LG_DEVICES.keys())}"

    try:
        client = await _get_lg_client(device)
        if input_id is None:
            current = await client.get_input()
            await client.disconnect()
            return f"Current input: {current}"
        else:
            await client.set_input(input_id)
            await client.disconnect()
            return f"{device['name']} → input {input_id}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool("tv_lg_status")
async def lg_status(room: str = "living") -> str:
    """Get comprehensive status of an LG TV.

    Returns power state, current app, input, volume, and audio status.

    Args:
        room: Room name (living, bedroom, lr, br)

    Returns:
        Formatted status information.
    """
    device = _get_lg_device(room)
    if not device:
        return f"Unknown room: {room}. Available: {', '.join(LG_DEVICES.keys())}"

    try:
        client = await _get_lg_client(device)
        power = await client.get_power_state()
        app = await client.get_current_app()
        vol = await client.get_volume()
        muted = await client.get_muted()
        inp = await client.get_input()
        sound = await client.get_audio_status()
        await client.disconnect()

        lines = [
            f"── {device['name']} ──",
            f"  Power:   {power}",
            f"  App:     {app}",
            f"  Input:   {inp}",
            f"  Volume:  {vol}" + (" (muted)" if muted else ""),
            f"  Audio:   {sound}",
        ]
        return "\n".join(lines)
    except Exception as e:
        return f"Error getting status (TV may be off): {e}"


@mcp.tool("tv_lg_apps")
async def lg_apps(room: str = "living") -> str:
    """List installed apps on an LG TV.

    Args:
        room: Room name (living, bedroom, lr, br)

    Returns:
        List of app IDs and names.
    """
    device = _get_lg_device(room)
    if not device:
        return f"Unknown room: {room}. Available: {', '.join(LG_DEVICES.keys())}"

    try:
        client = await _get_lg_client(device)
        apps = await client.get_apps()
        await client.disconnect()

        lines = [f"Apps on {device['name']}:"]
        for app in sorted(apps, key=lambda a: a.get("title", "")):
            lines.append(f"  {app.get('id', ''):40s} {app.get('title', '')}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool("tv_lg_launch")
async def lg_launch(app_id: str, room: str = "living") -> str:
    """Launch an app on an LG TV by its app ID.

    Use tv_lg_apps to see available app IDs.

    Args:
        app_id: The app ID to launch (e.g., 'netflix', 'com.webos.app.hdmi1')
        room: Room name (living, bedroom, lr, br)

    Returns:
        Confirmation message.
    """
    device = _get_lg_device(room)
    if not device:
        return f"Unknown room: {room}. Available: {', '.join(LG_DEVICES.keys())}"

    try:
        client = await _get_lg_client(device)
        await client.launch_app(app_id)
        await client.disconnect()
        return f"{device['name']} → launched {app_id}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool("tv_lg_button")
async def lg_button(button: str, room: str = "living") -> str:
    """Send a remote control button press to an LG TV.

    Args:
        button: Button name (HOME, BACK, UP, DOWN, LEFT, RIGHT, ENTER, EXIT,
                VOLUMEUP, VOLUMEDOWN, MUTE, CHANNELUP, CHANNELDOWN, PLAY,
                PAUSE, STOP, REWIND, FASTFORWARD)
        room: Room name (living, bedroom, lr, br)

    Returns:
        Confirmation message.
    """
    device = _get_lg_device(room)
    if not device:
        return f"Unknown room: {room}. Available: {', '.join(LG_DEVICES.keys())}"

    try:
        client = await _get_lg_client(device)
        await client.button(button.upper())
        await client.disconnect()
        return f"{device['name']} → button {button}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool("tv_lg_notify")
async def lg_notify(message: str, room: str = "living") -> str:
    """Display a toast notification on an LG TV screen.

    Args:
        message: The message to display
        room: Room name (living, bedroom, lr, br)

    Returns:
        Confirmation message.
    """
    device = _get_lg_device(room)
    if not device:
        return f"Unknown room: {room}. Available: {', '.join(LG_DEVICES.keys())}"

    try:
        client = await _get_lg_client(device)
        await client.send_message(message)
        await client.disconnect()
        return f"{device['name']} → notification sent"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool("tv_lg_reboot")
async def lg_reboot(room: str = "living") -> str:
    """Reboot an LG TV.

    Args:
        room: Room name (living, bedroom, lr, br)

    Returns:
        Confirmation message.
    """
    device = _get_lg_device(room)
    if not device:
        return f"Unknown room: {room}. Available: {', '.join(LG_DEVICES.keys())}"

    try:
        client = await _get_lg_client(device)
        await client.reboot()
        await client.disconnect()
        return f"{device['name']} → rebooting"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool("tv_lg_sound_output")
async def lg_sound_output(room: str = "living", output: Optional[str] = None) -> str:
    """Get or set the sound output on an LG TV.

    Args:
        room: Room name (living, bedroom, lr, br)
        output: Sound output (tv_speaker, external_arc, etc.) or omit to get current

    Returns:
        Current sound output or confirmation of change.
    """
    device = _get_lg_device(room)
    if not device:
        return f"Unknown room: {room}. Available: {', '.join(LG_DEVICES.keys())}"

    try:
        client = await _get_lg_client(device)
        if output is None:
            result = client.sound_output
            await client.disconnect()
            return f"Sound output: {result}"
        else:
            from bscpylgtv import endpoints as ep
            await client.request(ep.CHANGE_SOUND_OUTPUT, {"output": output})
            await client.disconnect()
            return f"{device['name']} → sound output {output}"
    except Exception as e:
        return f"Error: {e}"


# ===========================================================================
# Google TV Streamer Tools (via Android TV Remote Protocol)
# ===========================================================================


@mcp.tool("tv_streamer_key")
async def streamer_key(key: str, room: str = "living") -> str:
    """Send a key press to a Google TV Streamer.

    Common keys: DPAD_UP, DPAD_DOWN, DPAD_LEFT, DPAD_RIGHT, DPAD_CENTER,
    BACK, HOME, ENTER, POWER, SEARCH, MEDIA_PLAY_PAUSE, VOLUME_UP, VOLUME_DOWN

    Args:
        key: Key name (e.g., DPAD_CENTER, HOME, BACK)
        room: Room name (living, bedroom, lr, br)

    Returns:
        Confirmation message.
    """
    try:
        remote = await _get_remote_client(room)
        remote.send_key_command(_normalize_key(key))
        remote.disconnect()
        device = _get_streamer_device(room)
        return f"{device['name']} → key {key}"
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Error sending key: {e}"


@mcp.tool("tv_streamer_keys")
async def streamer_keys(keys: list[str], room: str = "living", delay: float = 0) -> str:
    """Send multiple key presses to a Google TV Streamer in sequence.

    Args:
        keys: List of key names (e.g., ["DPAD_DOWN", "DPAD_DOWN", "DPAD_CENTER"])
        room: Room name (living, bedroom, lr, br)
        delay: Delay in seconds between keys (default: 0)

    Returns:
        Confirmation message.
    """
    try:
        remote = await _get_remote_client(room)
        for i, key in enumerate(keys):
            remote.send_key_command(_normalize_key(key))
            if delay and i < len(keys) - 1:
                await asyncio.sleep(delay)
        remote.disconnect()
        device = _get_streamer_device(room)
        return f"{device['name']} → keys {' '.join(keys)}"
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Error sending keys: {e}"


@mcp.tool("tv_streamer_text")
async def streamer_text(text: str, room: str = "living") -> str:
    """Type text on a Google TV Streamer (for search fields).

    Args:
        text: Text to type
        room: Room name (living, bedroom, lr, br)

    Returns:
        Confirmation message.
    """
    try:
        remote = await _get_remote_client(room)
        remote.send_text(text)
        remote.disconnect()
        device = _get_streamer_device(room)
        return f"{device['name']} → typed '{text}'"
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Error typing text: {e}"


@mcp.tool("tv_streamer_app")
async def streamer_app(app_name: str, room: str = "living") -> str:
    """Launch a streaming app on the Google TV Streamer by name.

    Uses deep link URIs when available for faster, more reliable launching.
    For playing specific content, use tv_streamer_deep_link or the Spotify
    MCP server's spotify_play_track with the TV's Spotify Connect device_id.

    Supported apps: netflix, plex, youtube, disney, hulu, prime, apple,
    peacock, spotify, hbo, pbs, espn, ytmusic

    Args:
        app_name: App short name (e.g., 'netflix', 'plex', 'youtube')
        room: Room name (living, bedroom, lr, br)

    Returns:
        Confirmation message.
    """
    app_name_lower = app_name.lower()
    # Prefer deep link URI, fall back to package name
    launch_target = APP_DEEP_LINKS.get(app_name_lower) or APP_PACKAGES.get(app_name_lower)
    if not launch_target:
        return (
            f"Unknown app: {app_name}. "
            f"Available: {', '.join(sorted(APP_PACKAGES.keys()))}"
        )

    try:
        remote = await _get_remote_client(room)
        remote.send_launch_app_command(launch_target)
        remote.disconnect()
        device = _get_streamer_device(room)
        return f"{device['name']} → launched {app_name} ({launch_target})"
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Error launching app: {e}"


@mcp.tool("tv_streamer_deep_link")
async def streamer_deep_link(uri: str, room: str = "living") -> str:
    """Open a deep link URI on the Google TV Streamer.

    Send any URI that an installed app can handle. Examples:
      - spotify:track:6rqhFgbbKwnb9MLmUQDhG6  (play a Spotify track)
      - spotify:album:4oktVvRuO1In9B7Hz0xm0a  (play an album)
      - spotify:playlist:37i9dQZF1DXcBWIGoYBM5M  (play a playlist)
      - https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6
      - plex://  (open Plex)
      - https://www.netflix.com/title/80100172  (open specific Netflix title)
      - https://www.youtube.com/watch?v=dQw4w9WgXcQ  (play YouTube video)

    For Spotify: get track/album/playlist URIs from the spotify_search_tracks
    or spotify_get_user_playlists tools, then pass the URI here.

    Args:
        uri: Deep link URI (spotify:, plex://, https://, etc.)
        room: Room name (living, bedroom, lr, br)

    Returns:
        Confirmation message.
    """
    try:
        remote = await _get_remote_client(room)
        remote.send_launch_app_command(uri)
        remote.disconnect()
        device = _get_streamer_device(room)
        return f"{device['name']} → opened {uri}"
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Error opening deep link: {e}"


@mcp.tool("tv_streamer_status")
async def streamer_status(room: str = "living") -> str:
    """Get status of a Google TV Streamer.

    Returns power state, current app, volume info, and device model.

    Args:
        room: Room name (living, bedroom, lr, br)

    Returns:
        Formatted status information.
    """
    try:
        remote = await _get_remote_client(room)
        device = _get_streamer_device(room)

        is_on = remote.is_on
        lines = [f"── {device['name']} ──", f"  Power:  {'on' if is_on else 'off'}"]

        if is_on:
            lines.append(f"  App:    {remote.current_app}")
            vol = remote.volume_info
            if vol:
                muted = " (muted)" if vol.get("muted") else ""
                lines.append(f"  Volume: {vol.get('level', '?')}/{vol.get('max', '?')}{muted}")
            info = remote.device_info
            if info:
                lines.append(f"  Model:  {info.get('model', '?')}")

        remote.disconnect()
        return "\n".join(lines)
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Error getting status: {e}"


# ===========================================================================
# Convenience Tools (for common scenarios)
# ===========================================================================


@mcp.tool("tv_all_power_on")
async def all_power_on() -> str:
    """Power on all TVs and streamers in the house.

    Sends Wake-on-LAN to both LG TVs and waits for them to boot.

    Returns:
        Status of all devices.
    """
    from wakeonlan import send_magic_packet

    results = []

    for room, device in LG_DEVICES.items():
        send_magic_packet(device["mac"])
        results.append(f"{device['name']}: WoL sent")

    await asyncio.sleep(1)

    # Send second WoL
    for device in LG_DEVICES.values():
        send_magic_packet(device["mac"])

    results.append(
        "\nTVs will take 40-60 seconds to fully boot. "
        "Use tv_lg_status to check when ready."
    )
    return "\n".join(results)


@mcp.tool("tv_all_power_off")
async def all_power_off() -> str:
    """Power off all TVs in the house.

    Returns:
        Status of power off commands.
    """
    results = []

    for room, device in LG_DEVICES.items():
        try:
            client = await _get_lg_client(device)
            await client.power_off()
            await client.disconnect()
            results.append(f"{device['name']}: powered off")
        except Exception as e:
            results.append(f"{device['name']}: error - {e}")

    return "\n".join(results)


@mcp.tool("tv_list_devices")
async def list_devices() -> str:
    """List all available TV and streamer devices with their IPs.

    Returns:
        Table of all devices.
    """
    lines = ["LG TVs:"]
    for room, device in LG_DEVICES.items():
        lines.append(f"  {room:12s} {device['name']:30s} {device['ip']}")

    lines.append("\nGoogle TV Streamers:")
    for room, device in STREAMER_DEVICES.items():
        lines.append(f"  {room:12s} {device['name']:30s} {device['ip']}")

    lines.append("\nRoom aliases: lr=living, br=bedroom, bed=bedroom, live=living")
    return "\n".join(lines)


@mcp.tool("tv_play_pause")
async def play_pause(room: str = "living") -> str:
    """Toggle play/pause on the Google TV Streamer.

    Useful for pausing/resuming video playback.

    Args:
        room: Room name (living, bedroom, lr, br)

    Returns:
        Confirmation message.
    """
    return await streamer_key("MEDIA_PLAY_PAUSE", room)


@mcp.tool("tv_go_home")
async def go_home(room: str = "living") -> str:
    """Go to home screen on the Google TV Streamer.

    Args:
        room: Room name (living, bedroom, lr, br)

    Returns:
        Confirmation message.
    """
    return await streamer_key("HOME", room)


@mcp.tool("tv_go_back")
async def go_back(room: str = "living") -> str:
    """Go back on the Google TV Streamer.

    Args:
        room: Room name (living, bedroom, lr, br)

    Returns:
        Confirmation message.
    """
    return await streamer_key("BACK", room)


# ===========================================================================
# Entrypoint
# ===========================================================================


def run(
    transport: str = "stdio",
    host: str = "0.0.0.0",
    port: int = DEFAULT_HTTP_PORT,
) -> None:  # pragma: no cover
    """Run the TV MCP server with the specified transport."""
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


def main() -> None:  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description="TV MCP Server")
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
    # LG TV tools
    "lg_power_on",
    "lg_power_off",
    "lg_screen_off",
    "lg_screen_on",
    "lg_volume",
    "lg_mute",
    "lg_input",
    "lg_status",
    "lg_apps",
    "lg_launch",
    "lg_button",
    "lg_notify",
    "lg_reboot",
    "lg_sound_output",
    # Streamer tools
    "streamer_key",
    "streamer_keys",
    "streamer_text",
    "streamer_app",
    "streamer_deep_link",
    "streamer_status",
    # Convenience tools
    "all_power_on",
    "all_power_off",
    "list_devices",
    "play_pause",
    "go_home",
    "go_back",
]
