"""Standalone Spotify MCP server.

Exposes Spotify playback control and library tools via MCP protocol.
Zero imports from Backend_FastAPI — fully standalone.

Run:
    python -m servers.spotify --transport streamable-http --host 0.0.0.0 --port 9010
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastmcp import FastMCP

from shared.spotify_auth import (
    DEFAULT_USER_EMAIL,
    get_spotify_client,
    retry_on_rate_limit,
)
from shared.spotify_identifiers import (
    normalize_context_uri,
    normalize_playlist_id,
    normalize_track_uri,
)

# Default port for HTTP transport
DEFAULT_HTTP_PORT = 9010

mcp = FastMCP("spotify")


def _format_track_info(track: dict[str, Any]) -> str:
    """Format track information for display."""
    name = track.get("name", "Unknown")
    artists = ", ".join(
        artist.get("name", "Unknown") for artist in track.get("artists", [])
    )
    album = track.get("album", {}).get("name", "Unknown")
    url = track.get("external_urls", {}).get("spotify", "")
    uri = track.get("uri", "")
    duration_ms = track.get("duration_ms", 0)
    duration_min = duration_ms // 60000
    duration_sec = (duration_ms % 60000) // 1000

    return (
        f"Track: {name}\n"
        f"Artist: {artists}\n"
        f"Album: {album}\n"
        f"Duration: {duration_min}:{duration_sec:02d}\n"
        f"URI: {uri}\n"
        f"Link: {url}"
    )


@mcp.tool("spotify_search_tracks")
@retry_on_rate_limit(max_retries=3)
async def search_tracks(
    query: str,
    user_email: str = DEFAULT_USER_EMAIL,
    limit: int = 10,
) -> str | dict[str, Any]:
    """Search Spotify for tracks by query string.

    Searches track names, artist names, and album names. Returns track details
    including name, artist, album, duration, and Spotify URL.

    Args:
        query: Search terms (e.g., "bohemian rhapsody queen", "jazz piano")
        user_email: User's email for authentication
        limit: Maximum number of results to return (default: 10, max: 50)

    Returns:
        Either a formatted error message string or a JSON-serializable dict
        with the search results.
    """
    try:
        sp = get_spotify_client(user_email)
    except ValueError as exc:
        return (
            f"Authentication error: {exc}. "
            "Click 'Connect Spotify' in Settings to authorize this account."
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error creating Spotify client: {exc}"

    try:
        results = await asyncio.to_thread(
            sp.search,
            q=query,
            type="track",
            limit=min(limit, 50),
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error searching Spotify: {exc}"

    if not results or not isinstance(results, dict):
        return "Invalid response from Spotify API"

    tracks_data = results.get("tracks", {})
    if not isinstance(tracks_data, dict):
        return "Invalid tracks data from Spotify API"

    tracks = tracks_data.get("items", [])
    if not tracks:
        return f"No tracks found for query '{query}'"

    tracks_list: list[dict[str, Any]] = [
        {
            "name": track.get("name", "Unknown"),
            "artist": ", ".join(
                artist.get("name", "Unknown") for artist in track.get("artists", [])
            ),
            "album": track.get("album", {}).get("name", "Unknown"),
            "duration": (
                f"{track.get('duration_ms', 0) // 60000}:"
                f"{(track.get('duration_ms', 0) % 60000) // 1000:02d}"
            ),
            "uri": track.get("uri", ""),
            "url": track.get("external_urls", {}).get("spotify", ""),
        }
        for track in tracks
    ]

    return {
        "query": query,
        "count": len(tracks_list),
        "tracks": tracks_list,
    }


@mcp.tool("spotify_get_current_playback")
@retry_on_rate_limit(max_retries=3)
async def get_current_playback(
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Get information about the user's current Spotify playback.

    Returns details about what's currently playing, including track info,
    playback state (playing/paused), device, shuffle/repeat status, and progress.

    Args:
        user_email: User's email for authentication

    Returns:
        Formatted playback information or message if nothing is playing.
    """
    try:
        sp = get_spotify_client(user_email)
    except ValueError as exc:
        return (
            f"Authentication error: {exc}. "
            "Click 'Connect Spotify' in Settings to authorize this account."
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error creating Spotify client: {exc}"

    try:
        playback = await asyncio.to_thread(sp.current_playback)
    except Exception as exc:  # noqa: BLE001
        return f"Error getting current playback: {exc}"

    if not playback or not playback.get("item"):
        return "No track currently playing"

    track = playback["item"]
    is_playing = playback.get("is_playing", False)
    device = playback.get("device", {})
    shuffle = playback.get("shuffle_state", False)
    repeat = playback.get("repeat_state", "off")
    progress_ms = playback.get("progress_ms", 0)
    progress_min = progress_ms // 60000
    progress_sec = (progress_ms % 60000) // 1000

    lines = [
        "Current Playback:",
        "",
        _format_track_info(track),
        "",
        f"Status: {'Playing' if is_playing else 'Paused'}",
        f"Device: {device.get('name', 'Unknown')} ({device.get('type', 'Unknown')})",
        f"Volume: {device.get('volume_percent', 'N/A')}%",
        f"Shuffle: {'On' if shuffle else 'Off'}",
        f"Repeat: {repeat}",
        f"Progress: {progress_min}:{progress_sec:02d}",
    ]

    return "\n".join(lines)


@mcp.tool("spotify_play_track")
@retry_on_rate_limit(max_retries=3)
async def play_track(
    track_uri: str,
    user_email: str = DEFAULT_USER_EMAIL,
    device_id: Optional[str] = None,
) -> str:
    """Start playing a specific track on Spotify.

    Args:
        track_uri: Spotify track URI or URL
        user_email: User's email for authentication
        device_id: Optional device ID to play on (default: active device)

    Returns:
        Confirmation message or error.
    """
    try:
        sp = get_spotify_client(user_email)
    except ValueError as exc:
        return (
            f"Authentication error: {exc}. "
            "Click 'Connect Spotify' in Settings to authorize this account."
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error creating Spotify client: {exc}"

    track_uri = normalize_track_uri(track_uri)

    try:
        await asyncio.to_thread(
            sp.start_playback,
            uris=[track_uri],
            device_id=device_id,
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error playing track: {exc}. Make sure Spotify is open on a device."

    try:
        track_id = track_uri.split(":")[-1]
        track = await asyncio.to_thread(sp.track, track_id)
        if track and isinstance(track, dict):
            track_name = track.get("name", "Unknown")
            artists = ", ".join(
                artist.get("name", "Unknown") for artist in track.get("artists", [])
            )
            return f"Now playing: {track_name} by {artists}"
        return f"Started playback of {track_uri}"
    except Exception:  # noqa: BLE001
        return f"Started playback of {track_uri}"


@mcp.tool("spotify_pause")
@retry_on_rate_limit(max_retries=3)
async def pause_playback(
    user_email: str = DEFAULT_USER_EMAIL,
    device_id: Optional[str] = None,
) -> str:
    """Pause Spotify playback.

    Args:
        user_email: User's email for authentication
        device_id: Optional device ID to pause (default: active device)

    Returns:
        Confirmation message or error.
    """
    try:
        sp = get_spotify_client(user_email)
    except ValueError as exc:
        return (
            f"Authentication error: {exc}. "
            "Click 'Connect Spotify' in Settings to authorize this account."
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error creating Spotify client: {exc}"

    try:
        await asyncio.to_thread(sp.pause_playback, device_id=device_id)
    except Exception as exc:  # noqa: BLE001
        return f"Error pausing playback: {exc}. Make sure Spotify is open and playing."

    return "Playback paused"


@mcp.tool("spotify_play_context")
@retry_on_rate_limit(max_retries=3)
async def play_context(
    context_uri: str,
    user_email: str = DEFAULT_USER_EMAIL,
    device_id: Optional[str] = None,
) -> str:
    """Start playing a Spotify playlist, album, or artist.

    Use this to play entire collections of tracks, not individual tracks.
    For individual tracks, use spotify_play_track instead.

    Args:
        context_uri: Spotify URI or URL for playlist, album, or artist
        user_email: User's email for authentication
        device_id: Optional device ID to play on (default: active device)

    Returns:
        Confirmation message or error.
    """
    try:
        sp = get_spotify_client(user_email)
    except ValueError as exc:
        return (
            f"Authentication error: {exc}. "
            "Click 'Connect Spotify' in Settings to authorize this account."
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error creating Spotify client: {exc}"

    try:
        context_uri, context_type = normalize_context_uri(context_uri)
    except ValueError as exc:
        return str(exc)

    try:
        await asyncio.to_thread(
            sp.start_playback,
            context_uri=context_uri,
            device_id=device_id,
        )
    except Exception as exc:  # noqa: BLE001
        return (
            f"Error playing {context_type}: {exc}. "
            "Make sure Spotify is open on a device."
        )

    try:
        context_id = context_uri.split(":")[-1]
        if context_type == "playlist":
            info = await asyncio.to_thread(sp.playlist, context_id, fields="name")
            name = info.get("name", "Unknown") if isinstance(info, dict) else "Unknown"
            return f"Now playing playlist: {name}"
        if context_type == "album":
            info = await asyncio.to_thread(sp.album, context_id)
            if isinstance(info, dict):
                name = info.get("name", "Unknown")
                artists = ", ".join(
                    artist.get("name", "Unknown")
                    for artist in info.get("artists", [])
                )
                return f"Now playing album: {name} by {artists}"
        if context_type == "artist":
            info = await asyncio.to_thread(sp.artist, context_id)
            name = info.get("name", "Unknown") if isinstance(info, dict) else "Unknown"
            return f"Now playing artist: {name}"
        return f"Started playback of {context_type}"
    except Exception:  # noqa: BLE001
        return f"Started playback of {context_type}"


@mcp.tool("spotify_resume")
@retry_on_rate_limit(max_retries=3)
async def resume_playback(
    user_email: str = DEFAULT_USER_EMAIL,
    device_id: Optional[str] = None,
) -> str:
    """Resume paused Spotify playback.

    Args:
        user_email: User's email for authentication
        device_id: Optional device ID to resume on (default: active device)

    Returns:
        Confirmation message or error.
    """
    try:
        sp = get_spotify_client(user_email)
    except ValueError as exc:
        return (
            f"Authentication error: {exc}. "
            "Click 'Connect Spotify' in Settings to authorize this account."
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error creating Spotify client: {exc}"

    try:
        await asyncio.to_thread(sp.start_playback, device_id=device_id)
    except Exception as exc:  # noqa: BLE001
        return f"Error resuming playback: {exc}. Make sure Spotify is open on a device."

    return "Playback resumed"


@mcp.tool("spotify_next_track")
@retry_on_rate_limit(max_retries=3)
async def next_track(
    user_email: str = DEFAULT_USER_EMAIL,
    device_id: Optional[str] = None,
) -> str:
    """Skip to the next track in Spotify playback.

    Args:
        user_email: User's email for authentication
        device_id: Optional device ID (default: active device)

    Returns:
        Confirmation message or error.
    """
    try:
        sp = get_spotify_client(user_email)
    except ValueError as exc:
        return (
            f"Authentication error: {exc}. "
            "Click 'Connect Spotify' in Settings to authorize this account."
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error creating Spotify client: {exc}"

    try:
        await asyncio.to_thread(sp.next_track, device_id=device_id)
    except Exception as exc:  # noqa: BLE001
        return (
            f"Error skipping to next track: {exc}. "
            "Make sure Spotify is open and playing."
        )

    return "Skipped to next track"


@mcp.tool("spotify_previous_track")
@retry_on_rate_limit(max_retries=3)
async def previous_track(
    user_email: str = DEFAULT_USER_EMAIL,
    device_id: Optional[str] = None,
) -> str:
    """Go back to the previous track in Spotify playback.

    Args:
        user_email: User's email for authentication
        device_id: Optional device ID (default: active device)

    Returns:
        Confirmation message or error.
    """
    try:
        sp = get_spotify_client(user_email)
    except ValueError as exc:
        return (
            f"Authentication error: {exc}. "
            "Click 'Connect Spotify' in Settings to authorize this account."
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error creating Spotify client: {exc}"

    try:
        await asyncio.to_thread(sp.previous_track, device_id=device_id)
    except Exception as exc:  # noqa: BLE001
        return (
            f"Error going to previous track: {exc}. "
            "Make sure Spotify is open and playing."
        )

    return "Went back to previous track"


@mcp.tool("spotify_shuffle")
@retry_on_rate_limit(max_retries=3)
async def set_shuffle(
    state: bool,
    user_email: str = DEFAULT_USER_EMAIL,
    device_id: Optional[str] = None,
) -> str:
    """Toggle shuffle mode for Spotify playback.

    Args:
        state: True to enable shuffle, False to disable
        user_email: User's email for authentication
        device_id: Optional device ID (default: active device)

    Returns:
        Confirmation message or error.
    """
    try:
        sp = get_spotify_client(user_email)
    except ValueError as exc:
        return (
            f"Authentication error: {exc}. "
            "Click 'Connect Spotify' in Settings to authorize this account."
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error creating Spotify client: {exc}"

    try:
        await asyncio.to_thread(sp.shuffle, state, device_id=device_id)
    except Exception as exc:  # noqa: BLE001
        return f"Error setting shuffle: {exc}. Make sure Spotify is open and playing."

    return f"Shuffle {'enabled' if state else 'disabled'}"


@mcp.tool("spotify_repeat")
@retry_on_rate_limit(max_retries=3)
async def set_repeat(
    state: str,
    user_email: str = DEFAULT_USER_EMAIL,
    device_id: Optional[str] = None,
) -> str:
    """Set repeat mode for Spotify playback.

    Args:
        state: Repeat mode - "track", "context", or "off"
        user_email: User's email for authentication
        device_id: Optional device ID (default: active device)

    Returns:
        Confirmation message or error.
    """
    try:
        sp = get_spotify_client(user_email)
    except ValueError as exc:
        return (
            f"Authentication error: {exc}. "
            "Click 'Connect Spotify' in Settings to authorize this account."
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error creating Spotify client: {exc}"

    valid_states = {"track", "context", "off"}
    if state not in valid_states:
        valid_states_str = ", ".join(sorted(valid_states))
        return f"Invalid repeat state '{state}'. Must be one of: {valid_states_str}"

    try:
        await asyncio.to_thread(sp.repeat, state, device_id=device_id)
    except Exception as exc:  # noqa: BLE001
        return (
            f"Error setting repeat mode: {exc}. Make sure Spotify is open and playing."
        )

    return f"Repeat mode set to '{state}'"


@mcp.tool("spotify_seek_position")
@retry_on_rate_limit(max_retries=3)
async def seek_position(
    position_ms: int,
    user_email: str = DEFAULT_USER_EMAIL,
    device_id: Optional[str] = None,
) -> str:
    """Seek to a specific position in the currently playing track.

    Args:
        position_ms: Position in milliseconds (e.g., 30000 for 30 seconds)
        user_email: User's email for authentication
        device_id: Optional device ID (default: active device)

    Returns:
        Confirmation message or error.
    """
    try:
        sp = get_spotify_client(user_email)
    except ValueError as exc:
        return (
            f"Authentication error: {exc}. "
            "Click 'Connect Spotify' in Settings to authorize this account."
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error creating Spotify client: {exc}"

    position_ms = max(0, position_ms)
    position_min = position_ms // 60000
    position_sec = (position_ms % 60000) // 1000

    try:
        await asyncio.to_thread(
            sp.seek_track,
            position_ms,
            device_id=device_id,
        )
    except Exception as exc:  # noqa: BLE001
        return (
            f"Error seeking to position: {exc}. Make sure Spotify is open and playing."
        )

    return f"Seeked to {position_min}:{position_sec:02d}"


@mcp.tool("spotify_get_user_playlists")
@retry_on_rate_limit(max_retries=3)
async def get_user_playlists(
    user_email: str = DEFAULT_USER_EMAIL,
    limit: int = 50,
) -> str | dict[str, Any]:
    """Get a list of the user's Spotify playlists.

    Args:
        user_email: User's email for authentication
        limit: Maximum number of playlists to return (default: 50, max: 50)

    Returns:
        Either a formatted error message string or a JSON-serializable dict
        with playlist metadata.
    """
    try:
        sp = get_spotify_client(user_email)
    except ValueError as exc:
        return (
            f"Authentication error: {exc}. "
            "Click 'Connect Spotify' in Settings to authorize this account."
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error creating Spotify client: {exc}"

    try:
        results = await asyncio.to_thread(
            sp.current_user_playlists,
            limit=min(limit, 50),
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error fetching playlists: {exc}"

    if not results or not isinstance(results, dict):
        return "Invalid response from Spotify API"

    playlists = results.get("items", [])
    if not playlists:
        return "No playlists found"

    playlists_list: list[dict[str, Any]] = [
        {
            "name": playlist.get("name", "Unknown"),
            "owner": playlist.get("owner", {}).get("display_name", "Unknown"),
            "tracks": playlist.get("tracks", {}).get("total", 0),
            "public": bool(playlist.get("public", False)),
            "id": playlist.get("id", ""),
            "uri": playlist.get("uri", ""),
            "url": playlist.get("external_urls", {}).get("spotify", ""),
        }
        for playlist in playlists
    ]

    return {
        "count": len(playlists_list),
        "playlists": playlists_list,
    }


@mcp.tool("spotify_get_playlist_tracks")
@retry_on_rate_limit(max_retries=3)
async def get_playlist_tracks(
    playlist_id: str,
    user_email: str = DEFAULT_USER_EMAIL,
    limit: int = 50,
) -> str | dict[str, Any]:
    """Get tracks from a Spotify playlist.

    Args:
        playlist_id: Spotify playlist ID or URI
        user_email: User's email for authentication
        limit: Maximum number of tracks to return (default: 50, max: 100)

    Returns:
        Either a formatted error message string or a JSON-serializable dict
        with track metadata for the playlist.
    """
    try:
        sp = get_spotify_client(user_email)
    except ValueError as exc:
        return (
            f"Authentication error: {exc}. "
            "Click 'Connect Spotify' in Settings to authorize this account."
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error creating Spotify client: {exc}"

    playlist_id = normalize_playlist_id(playlist_id)

    try:
        playlist_info = await asyncio.to_thread(
            sp.playlist,
            playlist_id,
            fields="name,tracks.total",
        )
        if not isinstance(playlist_info, dict):
            return "Error: Invalid playlist info response"

        playlist_name = playlist_info.get("name", "Unknown Playlist")
        total_tracks = playlist_info.get("tracks", {}).get("total", 0)

        results = await asyncio.to_thread(
            sp.playlist_tracks,
            playlist_id,
            limit=min(limit, 100),
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error fetching playlist tracks: {exc}"

    if not results or not isinstance(results, dict):
        return "Invalid response from Spotify API"

    items = results.get("items", [])
    if not items:
        return f"No tracks found in playlist '{playlist_name}'"

    tracks_list: list[dict[str, Any]] = []
    for item in items:
        track = item.get("track")
        if not track:
            continue
        tracks_list.append(
            {
                "name": track.get("name", "Unknown"),
                "artist": ", ".join(
                    artist.get("name", "Unknown")
                    for artist in track.get("artists", [])
                ),
                "album": track.get("album", {}).get("name", "Unknown"),
                "duration": (
                    f"{track.get('duration_ms', 0) // 60000}:"
                    f"{(track.get('duration_ms', 0) % 60000) // 1000:02d}"
                ),
                "uri": track.get("uri", ""),
                "added_by": item.get("added_by", {}).get("id", "Unknown"),
            }
        )

    return {
        "playlist_name": playlist_name,
        "total_tracks": total_tracks,
        "showing": len(tracks_list),
        "tracks": tracks_list,
    }


@mcp.tool("spotify_create_playlist")
@retry_on_rate_limit(max_retries=3)
async def create_playlist(
    name: str,
    description: str = "",
    public: bool = False,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Create a new Spotify playlist for the user.

    Args:
        name: Name of the new playlist
        description: Optional description for the playlist
        public: Whether the playlist should be public (default: False)
        user_email: User's email for authentication

    Returns:
        Confirmation message with playlist details and URL.
    """
    try:
        sp = get_spotify_client(user_email)
    except ValueError as exc:
        return (
            f"Authentication error: {exc}. "
            "Click 'Connect Spotify' in Settings to authorize this account."
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error creating Spotify client: {exc}"

    try:
        user_info = await asyncio.to_thread(sp.current_user)
        if not isinstance(user_info, dict):
            return "Error: Invalid user info response"

        user_id = user_info.get("id")
        if not user_id:
            return "Error: Could not get user ID"

        playlist = await asyncio.to_thread(
            sp.user_playlist_create,
            user_id,
            name,
            public=public,
            description=description,
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error creating playlist: {exc}"

    if not playlist or not isinstance(playlist, dict):
        return "Error creating playlist: Invalid response"

    playlist_name = playlist.get("name", name)
    playlist_id = playlist.get("id", "")
    playlist_url = playlist.get("external_urls", {}).get("spotify", "")
    playlist_uri = playlist.get("uri", "")

    return (
        f"Created playlist: {playlist_name}\n"
        f"ID: {playlist_id}\n"
        f"URI: {playlist_uri}\n"
        f"Public: {'Yes' if public else 'No'}\n"
        f"Link: {playlist_url}"
    )


@mcp.tool("spotify_delete_playlist")
@retry_on_rate_limit(max_retries=3)
async def delete_playlist(
    playlist_id: str,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Delete (unfollow) a Spotify playlist.

    If you own the playlist, it will be permanently deleted for all users.
    If you don't own it, it's only removed from your library.

    Args:
        playlist_id: Spotify playlist ID or URI
        user_email: User's email for authentication

    Returns:
        Confirmation message or error.
    """
    try:
        sp = get_spotify_client(user_email)
    except ValueError as exc:
        return (
            f"Authentication error: {exc}. "
            "Click 'Connect Spotify' in Settings to authorize this account."
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error creating Spotify client: {exc}"

    playlist_id = normalize_playlist_id(playlist_id)

    try:
        playlist_info = await asyncio.to_thread(
            sp.playlist,
            playlist_id,
            fields="name,owner.id",
        )
        if not isinstance(playlist_info, dict):
            return "Error: Could not retrieve playlist info"

        playlist_name = playlist_info.get("name", "Unknown")
        owner_id = playlist_info.get("owner", {}).get("id", "")

        user_info = await asyncio.to_thread(sp.current_user)
        current_user_id = (
            user_info.get("id", "") if isinstance(user_info, dict) else ""
        )
        if owner_id and current_user_id and owner_id != current_user_id:
            return (
                f"Cannot delete playlist '{playlist_name}' - "
                f"you don't own it (owner: {owner_id})"
            )
    except Exception as exc:  # noqa: BLE001
        return f"Error checking playlist ownership: {exc}"

    try:
        user_info = await asyncio.to_thread(sp.current_user)
        if not isinstance(user_info, dict):
            return "Error: Could not get user info"

        user_id = user_info.get("id")
        if not user_id:
            return "Error: Could not get user ID"

        await asyncio.to_thread(sp.user_playlist_unfollow, user_id, playlist_id)
    except Exception as exc:  # noqa: BLE001
        return f"Error deleting playlist: {exc}"

    return f"Successfully deleted playlist: {playlist_name}"


@mcp.tool("spotify_add_tracks_to_playlist")
@retry_on_rate_limit(max_retries=3)
async def add_tracks_to_playlist(
    playlist_id: str,
    track_uris: list[str],
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Add tracks to a Spotify playlist.

    Args:
        playlist_id: Spotify playlist ID or URI
        track_uris: List of track URIs, URLs, or IDs
        user_email: User's email for authentication

    Returns:
        Confirmation message with number of tracks added, or an error.
    """
    try:
        sp = get_spotify_client(user_email)
    except ValueError as exc:
        return (
            f"Authentication error: {exc}. "
            "Click 'Connect Spotify' in Settings to authorize this account."
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error creating Spotify client: {exc}"

    playlist_id = normalize_playlist_id(playlist_id)

    if not track_uris:
        return "Error: No track URIs provided"

    normalized_uris = [normalize_track_uri(uri) for uri in track_uris]

    try:
        await asyncio.to_thread(
            sp.playlist_add_items,
            playlist_id,
            normalized_uris,
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error adding tracks to playlist: {exc}"

    return f"Successfully added {len(normalized_uris)} track(s) to playlist"


@mcp.tool("spotify_add_to_queue")
@retry_on_rate_limit(max_retries=3)
async def add_to_queue(
    track_uri: str,
    user_email: str = DEFAULT_USER_EMAIL,
    device_id: Optional[str] = None,
) -> str:
    """Add a track to the playback queue.

    Args:
        track_uri: Spotify track URI, URL, or ID
        user_email: User's email for authentication
        device_id: Optional device ID to add to queue on (default: active device)

    Returns:
        Confirmation message with track details, or an error.
    """
    try:
        sp = get_spotify_client(user_email)
    except ValueError as exc:
        return (
            f"Authentication error: {exc}. "
            "Click 'Connect Spotify' in Settings to authorize this account."
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error creating Spotify client: {exc}"

    track_uri = normalize_track_uri(track_uri)

    try:
        await asyncio.to_thread(
            sp.add_to_queue,
            track_uri,
            device_id=device_id,
        )
    except Exception as exc:  # noqa: BLE001
        return (
            f"Error adding track to queue: {exc}. "
            "Make sure Spotify is open and playing."
        )

    try:
        track_id = track_uri.split(":")[-1]
        track = await asyncio.to_thread(sp.track, track_id)
        if track and isinstance(track, dict):
            track_name = track.get("name", "Unknown")
            artists = ", ".join(
                artist.get("name", "Unknown") for artist in track.get("artists", [])
            )
            return f"Added to queue: {track_name} by {artists}"
        return f"Added track to queue: {track_uri}"
    except Exception:  # noqa: BLE001
        return f"Added track to queue: {track_uri}"


@mcp.tool("spotify_get_queue")
@retry_on_rate_limit(max_retries=3)
async def get_queue(
    user_email: str = DEFAULT_USER_EMAIL,
) -> str | dict[str, Any]:
    """Get the user's current playback queue (upcoming tracks).

    Args:
        user_email: User's email for authentication

    Returns:
        Either a human-readable string for empty states or a JSON-serializable
        dict with the currently playing track and upcoming queue.
    """
    try:
        sp = get_spotify_client(user_email)
    except ValueError as exc:
        return (
            f"Authentication error: {exc}. "
            "Click 'Connect Spotify' in Settings to authorize this account."
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error creating Spotify client: {exc}"

    try:
        queue_data = await asyncio.to_thread(sp.queue)
    except Exception as exc:  # noqa: BLE001
        return f"Error getting queue: {exc}. Make sure Spotify is open and playing."

    if not queue_data or not isinstance(queue_data, dict):
        return "Invalid response from Spotify API"

    currently_playing = queue_data.get("currently_playing")
    queue_items = queue_data.get("queue", [])

    if not currently_playing and not queue_items:
        return "Nothing currently playing and queue is empty"

    if not queue_items and currently_playing:
        return (
            "Currently playing:\n\n"
            + _format_track_info(currently_playing)
            + "\n\nQueue is empty - no tracks queued after current song"
        )

    result: dict[str, Any] = {}

    if currently_playing:
        result["currently_playing"] = {
            "name": currently_playing.get("name", "Unknown"),
            "artist": ", ".join(
                artist.get("name", "Unknown")
                for artist in currently_playing.get("artists", [])
            ),
            "album": currently_playing.get("album", {}).get("name", "Unknown"),
            "duration": (
                f"{currently_playing.get('duration_ms', 0) // 60000}:"
                f"{(currently_playing.get('duration_ms', 0) % 60000) // 1000:02d}"
            ),
            "uri": currently_playing.get("uri", ""),
        }

    if queue_items:
        queue_list: list[dict[str, Any]] = []
        for track in queue_items:
            if not track:
                continue
            queue_list.append(
                {
                    "name": track.get("name", "Unknown"),
                    "artist": ", ".join(
                        artist.get("name", "Unknown")
                        for artist in track.get("artists", [])
                    ),
                    "duration": (
                        f"{track.get('duration_ms', 0) // 60000}:"
                        f"{(track.get('duration_ms', 0) % 60000) // 1000:02d}"
                    ),
                    "uri": track.get("uri", ""),
                }
            )
        result["queue"] = queue_list
        result["queue_count"] = len(queue_list)

    return result


@mcp.tool("spotify_get_devices")
@retry_on_rate_limit(max_retries=3)
async def get_devices(
    user_email: str = DEFAULT_USER_EMAIL,
) -> str | dict[str, Any]:
    """Get a list of available Spotify playback devices for the user.

    Args:
        user_email: User's email for authentication

    Returns:
        Either a formatted error message string or a JSON-serializable dict
        with device information.
    """
    try:
        sp = get_spotify_client(user_email)
    except ValueError as exc:
        return (
            f"Authentication error: {exc}. "
            "Click 'Connect Spotify' in Settings to authorize this account."
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error creating Spotify client: {exc}"

    try:
        devices_data = await asyncio.to_thread(sp.devices)
    except Exception as exc:  # noqa: BLE001
        return f"Error fetching devices: {exc}"

    if not devices_data or not isinstance(devices_data, dict):
        return "Invalid response from Spotify API"

    devices = devices_data.get("devices", [])
    if not devices:
        return "No active Spotify devices found"

    device_list: list[dict[str, Any]] = [
        {
            "id": device.get("id", ""),
            "name": device.get("name", "Unknown"),
            "type": device.get("type", "Unknown"),
            "active": bool(device.get("is_active", False)),
            "volume_percent": device.get("volume_percent"),
        }
        for device in devices
    ]

    return {
        "count": len(device_list),
        "devices": device_list,
    }


@mcp.tool("spotify_transfer_playback")
@retry_on_rate_limit(max_retries=3)
async def transfer_playback(
    device_id: str,
    user_email: str = DEFAULT_USER_EMAIL,
    play: bool = True,
) -> str:
    """Transfer playback to a different Spotify device.

    Args:
        device_id: Target Spotify device ID
        user_email: User's email for authentication
        play: Whether to start playback on the new device (default: True)

    Returns:
        Confirmation message or error.
    """
    try:
        sp = get_spotify_client(user_email)
    except ValueError as exc:
        return (
            f"Authentication error: {exc}. "
            "Click 'Connect Spotify' in Settings to authorize this account."
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error creating Spotify client: {exc}"

    try:
        await asyncio.to_thread(
            sp.transfer_playback,
            device_id=device_id,
            force_play=play,
        )
    except Exception as exc:  # noqa: BLE001
        return (
            f"Error transferring playback: {exc}. "
            "Make sure the target device is active."
        )

    return "Playback transferred to selected device"


@mcp.tool("spotify_get_recently_played")
@retry_on_rate_limit(max_retries=3)
async def get_recently_played(
    user_email: str = DEFAULT_USER_EMAIL,
    limit: int = 20,
) -> str | dict[str, Any]:
    """Get the user's recently played tracks.

    Args:
        user_email: User's email for authentication
        limit: Maximum number of tracks to return (default: 20, max: 50)

    Returns:
        Either a formatted error message string or a JSON-serializable dict
        with recently played track metadata.
    """
    try:
        sp = get_spotify_client(user_email)
    except ValueError as exc:
        return (
            f"Authentication error: {exc}. "
            "Click 'Connect Spotify' in Settings to authorize this account."
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error creating Spotify client: {exc}"

    try:
        results = await asyncio.to_thread(
            sp.current_user_recently_played,
            limit=min(limit, 50),
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error fetching recently played tracks: {exc}"

    if not results or not isinstance(results, dict):
        return "Invalid response from Spotify API"

    items = results.get("items", [])
    if not items:
        return "No recently played tracks found"

    tracks_list: list[dict[str, Any]] = []
    for item in items:
        track = item.get("track")
        if not track:
            continue
        played_at = item.get("played_at")
        tracks_list.append(
            {
                "name": track.get("name", "Unknown"),
                "artist": ", ".join(
                    artist.get("name", "Unknown")
                    for artist in track.get("artists", [])
                ),
                "album": track.get("album", {}).get("name", "Unknown"),
                "duration": (
                    f"{track.get('duration_ms', 0) // 60000}:"
                    f"{(track.get('duration_ms', 0) % 60000) // 1000:02d}"
                ),
                "uri": track.get("uri", ""),
                "played_at": played_at,
            }
        )

    return {
        "count": len(tracks_list),
        "tracks": tracks_list,
    }


@mcp.tool("spotify_get_saved_tracks")
@retry_on_rate_limit(max_retries=3)
async def get_saved_tracks(
    user_email: str = DEFAULT_USER_EMAIL,
    limit: int = 50,
) -> str | dict[str, Any]:
    """Get the user's saved (liked) tracks.

    Args:
        user_email: User's email for authentication
        limit: Maximum number of tracks to return (default: 50, max: 50)

    Returns:
        Either a formatted error message string or a JSON-serializable dict
        with saved track metadata.
    """
    try:
        sp = get_spotify_client(user_email)
    except ValueError as exc:
        return (
            f"Authentication error: {exc}. "
            "Click 'Connect Spotify' in Settings to authorize this account."
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error creating Spotify client: {exc}"

    try:
        results = await asyncio.to_thread(
            sp.current_user_saved_tracks,
            limit=min(limit, 50),
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error fetching saved tracks: {exc}"

    if not results or not isinstance(results, dict):
        return "Invalid response from Spotify API"

    items = results.get("items", [])
    if not items:
        return "No saved tracks found"

    tracks_list: list[dict[str, Any]] = []
    for item in items:
        track = item.get("track")
        if not track:
            continue
        added_at = item.get("added_at")
        tracks_list.append(
            {
                "name": track.get("name", "Unknown"),
                "artist": ", ".join(
                    artist.get("name", "Unknown")
                    for artist in track.get("artists", [])
                ),
                "album": track.get("album", {}).get("name", "Unknown"),
                "duration": (
                    f"{track.get('duration_ms', 0) // 60000}:"
                    f"{(track.get('duration_ms', 0) % 60000) // 1000:02d}"
                ),
                "uri": track.get("uri", ""),
                "added_at": added_at,
            }
        )

    return {
        "count": len(tracks_list),
        "tracks": tracks_list,
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def run(
    transport: str = "stdio",
    host: str = "0.0.0.0",
    port: int = DEFAULT_HTTP_PORT,
) -> None:  # pragma: no cover - integration entrypoint
    """Run the Spotify MCP server with the specified transport."""
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
    import argparse

    parser = argparse.ArgumentParser(description="Spotify MCP Server")
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
    "search_tracks",
    "get_current_playback",
    "play_track",
    "play_context",
    "pause_playback",
    "resume_playback",
    "next_track",
    "previous_track",
    "set_shuffle",
    "set_repeat",
    "seek_position",
    "get_user_playlists",
    "get_playlist_tracks",
    "create_playlist",
    "delete_playlist",
    "add_tracks_to_playlist",
    "add_to_queue",
    "get_queue",
    "get_devices",
    "transfer_playback",
    "get_recently_played",
    "get_saved_tracks",
]
