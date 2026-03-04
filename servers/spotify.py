"""Standalone Spotify MCP server.

Exposes Spotify playback control and library tools via MCP protocol.
Zero imports from Backend_FastAPI — fully standalone.

Run:
    python -m servers.spotify --transport streamable-http --host 0.0.0.0 --port 9010
"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Callable
from typing import Any

import spotipy.exceptions
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_HTTP_PORT = 9010
SPOTIFY_API_TIMEOUT = 25  # seconds per Spotify API call
BATCH_SIZE = 50  # Spotify API max items per request

mcp = FastMCP("spotify")

_AUTH_HINT = "Click 'Connect Spotify' in Settings to authorize this account."

# Exceptions considered transient / API-related (caught by tools)
_API_ERRORS = (spotipy.exceptions.SpotifyException, TimeoutError, ConnectionError)

# Pattern to detect liked-songs intent
_LIKED_RE = re.compile(
    r"\b(liked?\s+songs?|saved?\s+(songs?|tracks?|music)"
    r"|my\s+(likes?|favorites?|favourites?|saved|library)"
    r"|favorite\s+songs?|songs?\s+i.ve?\s+liked|shuffle\s+my\s+likes)\b",
    re.IGNORECASE,
)

_RESUME_WORDS = frozenset(
    {"resume", "unpause", "continue", "continue playing", "continue playback"}
)


async def _call(
    func: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """Execute a blocking Spotify API function with timeout protection."""
    return await asyncio.wait_for(
        asyncio.to_thread(func, *args, **kwargs),
        timeout=SPOTIFY_API_TIMEOUT,
    )


def _format_duration(ms: int) -> str:
    """Format milliseconds to 'm:ss'."""
    return f"{ms // 60000}:{(ms % 60000) // 1000:02d}"


def _get_client(user_email: str = DEFAULT_USER_EMAIL) -> tuple[Any, str | None]:
    """Return (spotify_client, None) or (None, error_message)."""
    try:
        return get_spotify_client(user_email), None
    except ValueError as exc:
        return None, f"Authentication error: {exc}. {_AUTH_HINT}"
    except (OSError, spotipy.exceptions.SpotifyException) as exc:
        return None, f"Error creating Spotify client: {exc}"


def _format_track_info(track: dict[str, Any]) -> str:
    """Format track information for display."""
    name = track.get("name", "Unknown")
    artists = ", ".join(
        artist.get("name", "Unknown") for artist in track.get("artists", [])
    )
    album = track.get("album", {}).get("name", "Unknown")
    url = track.get("external_urls", {}).get("spotify", "")
    uri = track.get("uri", "")
    duration = _format_duration(track.get("duration_ms", 0))

    return (
        f"Track: {name}\n"
        f"Artist: {artists}\n"
        f"Album: {album}\n"
        f"Duration: {duration}\n"
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
    sp, err = _get_client(user_email)
    if err:
        return err

    try:
        results = await _call(
            sp.search,
            q=query,
            type="track",
            limit=min(limit, BATCH_SIZE),
        )
    except _API_ERRORS as exc:
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
            "duration": _format_duration(track.get("duration_ms", 0)),
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
    sp, err = _get_client(user_email)
    if err:
        return err

    try:
        playback = await _call(sp.current_playback)
    except _API_ERRORS as exc:
        return f"Error getting current playback: {exc}"

    if not playback or not playback.get("item"):
        return "No track currently playing"

    track = playback["item"]
    is_playing = playback.get("is_playing", False)
    device = playback.get("device", {})
    shuffle = playback.get("shuffle_state", False)
    repeat = playback.get("repeat_state", "off")
    progress_ms = playback.get("progress_ms", 0)
    progress = _format_duration(progress_ms)

    # Resolve playback context (playlist, album, or artist)
    context_line = ""
    ctx = playback.get("context")
    if ctx:
        ctx_type = ctx.get("type", "")
        ctx_uri = ctx.get("uri", "")
        ctx_id = ctx_uri.split(":")[-1] if ctx_uri else ""
        if ctx_type == "playlist" and ctx_id:
            try:
                pl = await _call(
                    sp.playlist, ctx_id, fields="name,owner(display_name)"
                )
                owner = pl['owner']['display_name']
                context_line = f"Playing from playlist: {pl['name']} (by {owner})"
            except Exception:  # noqa: BLE001
                context_line = f"Playing from playlist: {ctx_uri}"
        elif ctx_type == "album" and ctx_id:
            try:
                alb = await _call(sp.album, ctx_id)
                alb_artists = ", ".join(a["name"] for a in alb.get("artists", []))
                context_line = f"Playing from album: {alb['name']} by {alb_artists}"
            except Exception:  # noqa: BLE001
                context_line = f"Playing from album: {ctx_uri}"
        elif ctx_type == "artist" and ctx_id:
            try:
                art = await _call(sp.artist, ctx_id)
                context_line = f"Playing from artist: {art['name']}"
            except Exception:  # noqa: BLE001
                context_line = f"Playing from artist: {ctx_uri}"

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
        f"Progress: {progress}",
    ]
    if context_line:
        lines.append(context_line)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Private play helpers (used by the unified spotify_play tool)
# ---------------------------------------------------------------------------


async def _play_track_uri(
    sp: Any, track_uri: str, device_id: str | None = None
) -> str:
    """Play a single track by URI."""
    track_uri = normalize_track_uri(track_uri)
    try:
        await _call(sp.start_playback, uris=[track_uri], device_id=device_id)
    except _API_ERRORS as exc:
        return f"Error playing track: {exc}. Make sure Spotify is open on a device."

    try:
        track_id = track_uri.split(":")[-1]
        track = await _call(sp.track, track_id)
        if track and isinstance(track, dict):
            name = track.get("name", "Unknown")
            artists = ", ".join(
                a.get("name", "Unknown") for a in track.get("artists", [])
            )
            return f"Now playing: {name} by {artists}"
    except Exception:  # noqa: BLE001
        pass
    return f"Started playback of {track_uri}"


async def _play_context_uri(
    sp: Any, context_uri: str, shuffle: bool, device_id: str | None = None
) -> str:
    """Play a playlist, album, or artist by URI."""
    try:
        context_uri, context_type = normalize_context_uri(context_uri)
    except ValueError as exc:
        return str(exc)

    try:
        await _call(sp.start_playback, context_uri=context_uri, device_id=device_id)
        if shuffle:
            await _call(sp.shuffle, True, device_id=device_id)
    except _API_ERRORS as exc:
        return f"Error playing {context_type}: {exc}. Make sure Spotify is open on a device."

    try:
        context_id = context_uri.split(":")[-1]
        if context_type == "playlist":
            info = await _call(sp.playlist, context_id, fields="name")
            name = info.get("name", "Unknown") if isinstance(info, dict) else "Unknown"
            return f"Now playing playlist: {name}"
        if context_type == "album":
            info = await _call(sp.album, context_id)
            if isinstance(info, dict):
                name = info.get("name", "Unknown")
                artists = ", ".join(
                    a.get("name", "Unknown") for a in info.get("artists", [])
                )
                return f"Now playing album: {name} by {artists}"
        if context_type == "artist":
            info = await _call(sp.artist, context_id)
            name = info.get("name", "Unknown") if isinstance(info, dict) else "Unknown"
            return f"Now playing artist: {name}"
    except Exception:  # noqa: BLE001
        pass
    return f"Started playback of {context_type}"


async def _play_liked_songs(
    sp: Any, limit: int, shuffle: bool, device_id: str | None = None
) -> str:
    """Fetch and play the user's Liked Songs library."""
    limit = max(1, min(limit, 800))
    all_uris: list[str] = []
    try:
        offset = 0
        while True:
            results = await _call(
                sp.current_user_saved_tracks, limit=BATCH_SIZE, offset=offset
            )
            items = results.get("items", [])
            if not items:
                break
            all_uris.extend(item["track"]["uri"] for item in items if item.get("track"))
            if not results.get("next"):
                break
            offset += BATCH_SIZE
    except _API_ERRORS as exc:
        return f"Error fetching liked songs: {exc}"

    if not all_uris:
        return "No liked songs found in your library."

    total = len(all_uris)
    play_uris = random.sample(all_uris, min(limit, total)) if shuffle else all_uris[:limit]

    try:
        await _call(sp.start_playback, uris=play_uris, device_id=device_id)
        if shuffle:
            await _call(sp.shuffle, True, device_id=device_id)
    except _API_ERRORS as exc:
        return f"Error starting playback: {exc}. Make sure Spotify is open on a device."

    return (
        f"Now playing liked songs: queued {len(play_uris)} tracks "
        f"(randomly selected from {total} total). Shuffle is {'on' if shuffle else 'off'}."
    )


async def _search_and_play(
    sp: Any, query: str, device_id: str | None = None
) -> str:
    """Search Spotify and play the top result."""
    try:
        results = await _call(sp.search, q=query, type="track", limit=1)
    except _API_ERRORS as exc:
        return f"Error searching Spotify: {exc}"

    tracks = results.get("tracks", {}).get("items", []) if results else []
    if not tracks:
        return f"No tracks found for '{query}'"

    track = tracks[0]
    track_uri = track.get("uri", "")
    if not track_uri:
        return f"No playable track found for '{query}'"

    try:
        await _call(sp.start_playback, uris=[track_uri], device_id=device_id)
    except _API_ERRORS as exc:
        return f"Error playing track: {exc}. Make sure Spotify is open on a device."

    name = track.get("name", "Unknown")
    artists = ", ".join(a.get("name", "Unknown") for a in track.get("artists", []))
    return f"Now playing: {name} by {artists}"


@mcp.tool("spotify_play")
@retry_on_rate_limit(max_retries=3)
async def play(
    query: str,
    shuffle: bool = True,
    limit: int = 50,
    user_email: str = DEFAULT_USER_EMAIL,
    device_id: str | None = None,
) -> str:
    """Play music on Spotify. This single tool handles ALL play and resume requests.

    Accepts any of the following as `query`:
    - Liked songs keywords ("liked songs", "my likes", "saved songs", etc.)
      → plays the user's Liked Songs library
    - Resume keywords ("resume", "unpause", "continue")
      → resumes paused playback
    - A Spotify URI (spotify:track:..., spotify:album:..., spotify:playlist:...,
      spotify:artist:...)
    - A Spotify URL (https://open.spotify.com/track/...)
    - A free-text search query (e.g. "bohemian rhapsody", "chill jazz")
      → searches Spotify and plays the top result

    Args:
        query: What to play — see above for accepted formats.
        shuffle: Enable shuffle mode (default True; applies to liked songs and contexts).
        limit: Number of liked songs to queue (default 50, max 800).
        user_email: User's email for authentication.
        device_id: Optional device ID to play on (default: active device).

    Returns:
        Confirmation message or error.
    """
    sp, err = _get_client(user_email)
    if err:
        return err

    _lower = query.strip().lower()

    # Route 1: Liked Songs
    if _LIKED_RE.search(_lower):
        return await _play_liked_songs(sp, limit, shuffle, device_id)

    # Route 2: Resume
    if _lower in _RESUME_WORDS:
        try:
            await _call(sp.start_playback, device_id=device_id)
        except _API_ERRORS as exc:
            return f"Error resuming playback: {exc}. Make sure Spotify is open on a device."
        return "Playback resumed"

    # Route 3: Spotify URI or URL → track or context
    _stripped = query.strip()
    if _stripped.startswith("spotify:") or "open.spotify.com/" in _stripped:
        if ":track:" in _stripped or "/track/" in _stripped:
            return await _play_track_uri(sp, _stripped, device_id)
        return await _play_context_uri(sp, _stripped, shuffle, device_id)

    # Route 4: Search and play
    return await _search_and_play(sp, query.strip(), device_id)


@mcp.tool("spotify_pause")
@retry_on_rate_limit(max_retries=3)
async def pause_playback(
    user_email: str = DEFAULT_USER_EMAIL,
    device_id: str | None = None,
) -> str:
    """Pause Spotify playback.

    Args:
        user_email: User's email for authentication
        device_id: Optional device ID to pause (default: active device)

    Returns:
        Confirmation message or error.
    """
    sp, err = _get_client(user_email)
    if err:
        return err

    try:
        await _call(sp.pause_playback, device_id=device_id)
    except _API_ERRORS as exc:
        return f"Error pausing playback: {exc}. Make sure Spotify is open and playing."

    return "Playback paused"


@mcp.tool("spotify_next_track")
@retry_on_rate_limit(max_retries=3)
async def next_track(
    user_email: str = DEFAULT_USER_EMAIL,
    device_id: str | None = None,
) -> str:
    """Skip to the next track in Spotify playback.

    Args:
        user_email: User's email for authentication
        device_id: Optional device ID (default: active device)

    Returns:
        Confirmation message or error.
    """
    sp, err = _get_client(user_email)
    if err:
        return err

    try:
        await _call(sp.next_track, device_id=device_id)
    except _API_ERRORS as exc:
        return (
            f"Error skipping to next track: {exc}. "
            "Make sure Spotify is open and playing."
        )

    return "Skipped to next track"


@mcp.tool("spotify_previous_track")
@retry_on_rate_limit(max_retries=3)
async def previous_track(
    user_email: str = DEFAULT_USER_EMAIL,
    device_id: str | None = None,
) -> str:
    """Go back to the previous track in Spotify playback.

    Args:
        user_email: User's email for authentication
        device_id: Optional device ID (default: active device)

    Returns:
        Confirmation message or error.
    """
    sp, err = _get_client(user_email)
    if err:
        return err

    try:
        await _call(sp.previous_track, device_id=device_id)
    except _API_ERRORS as exc:
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
    device_id: str | None = None,
) -> str:
    """Toggle shuffle mode for Spotify playback.

    Args:
        state: True to enable shuffle, False to disable
        user_email: User's email for authentication
        device_id: Optional device ID (default: active device)

    Returns:
        Confirmation message or error.
    """
    sp, err = _get_client(user_email)
    if err:
        return err

    try:
        await _call(sp.shuffle, state, device_id=device_id)
    except _API_ERRORS as exc:
        return f"Error setting shuffle: {exc}. Make sure Spotify is open and playing."

    return f"Shuffle {'enabled' if state else 'disabled'}"


@mcp.tool("spotify_repeat")
@retry_on_rate_limit(max_retries=3)
async def set_repeat(
    state: str,
    user_email: str = DEFAULT_USER_EMAIL,
    device_id: str | None = None,
) -> str:
    """Set repeat mode for Spotify playback.

    Args:
        state: Repeat mode - "track", "context", or "off"
        user_email: User's email for authentication
        device_id: Optional device ID (default: active device)

    Returns:
        Confirmation message or error.
    """
    sp, err = _get_client(user_email)
    if err:
        return err

    valid_states = {"track", "context", "off"}
    if state not in valid_states:
        valid_states_str = ", ".join(sorted(valid_states))
        return f"Invalid repeat state '{state}'. Must be one of: {valid_states_str}"

    try:
        await _call(sp.repeat, state, device_id=device_id)
    except _API_ERRORS as exc:
        return (
            f"Error setting repeat mode: {exc}. Make sure Spotify is open and playing."
        )

    return f"Repeat mode set to '{state}'"


@mcp.tool("spotify_seek_position")
@retry_on_rate_limit(max_retries=3)
async def seek_position(
    position_ms: int,
    user_email: str = DEFAULT_USER_EMAIL,
    device_id: str | None = None,
) -> str:
    """Seek to a specific position in the currently playing track.

    Args:
        position_ms: Position in milliseconds (e.g., 30000 for 30 seconds)
        user_email: User's email for authentication
        device_id: Optional device ID (default: active device)

    Returns:
        Confirmation message or error.
    """
    sp, err = _get_client(user_email)
    if err:
        return err

    position_ms = max(0, position_ms)

    try:
        await _call(
            sp.seek_track,
            position_ms,
            device_id=device_id,
        )
    except _API_ERRORS as exc:
        return (
            f"Error seeking to position: {exc}. Make sure Spotify is open and playing."
        )

    return f"Seeked to {_format_duration(position_ms)}"


@mcp.tool("spotify_get_user_playlists")
@retry_on_rate_limit(max_retries=3)
async def get_user_playlists(
    user_email: str = DEFAULT_USER_EMAIL,
) -> str | dict[str, Any]:
    """Get all of the user's Spotify playlists.

    Paginates through the full list so every playlist is returned.

    Args:
        user_email: User's email for authentication

    Returns:
        Either a formatted error message string or a JSON-serializable dict
        with playlist metadata.
    """
    sp, err = _get_client(user_email)
    if err:
        return err

    try:
        all_playlists: list[dict[str, Any]] = []
        results = await _call(
            sp.current_user_playlists,
            limit=BATCH_SIZE,
        )
        while results and isinstance(results, dict):
            all_playlists.extend(results.get("items", []))
            if results.get("next"):
                results = await _call(sp.next, results)
            else:
                break
    except _API_ERRORS as exc:
        return f"Error fetching playlists: {exc}"

    if not all_playlists:
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
        for playlist in all_playlists
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
) -> str | dict[str, Any]:
    """Get all tracks from a Spotify playlist.

    Paginates through the full playlist so every track is returned,
    regardless of playlist size.

    Args:
        playlist_id: Spotify playlist ID or URI
        user_email: User's email for authentication

    Returns:
        Either a formatted error message string or a JSON-serializable dict
        with track metadata for the playlist.
    """
    sp, err = _get_client(user_email)
    if err:
        return err

    playlist_id = normalize_playlist_id(playlist_id)

    try:
        playlist_info = await _call(
            sp.playlist,
            playlist_id,
            fields="name,tracks.total",
        )
        if not isinstance(playlist_info, dict):
            return "Error: Invalid playlist info response"

        playlist_name = playlist_info.get("name", "Unknown Playlist")
        total_tracks = playlist_info.get("tracks", {}).get("total", 0)

        all_items: list[dict[str, Any]] = []
        results = await _call(
            sp.playlist_items,
            playlist_id,
            limit=100,
        )
        while results and isinstance(results, dict):
            all_items.extend(results.get("items", []))
            if results.get("next"):
                results = await _call(sp.next, results)
            else:
                break
    except _API_ERRORS as exc:
        return f"Error fetching playlist tracks: {exc}"

    if not all_items:
        return f"No tracks found in playlist '{playlist_name}'"

    tracks_list: list[dict[str, Any]] = []
    for item in all_items:
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
                "duration": _format_duration(track.get("duration_ms", 0)),
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
    sp, err = _get_client(user_email)
    if err:
        return err

    try:
        user_info = await _call(sp.current_user)
        if not isinstance(user_info, dict):
            return "Error: Invalid user info response"

        user_id = user_info.get("id")
        if not user_id:
            return "Error: Could not get user ID"

        playlist = await _call(
            sp.user_playlist_create,
            user_id,
            name,
            public=public,
            description=description,
        )
    except _API_ERRORS as exc:
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
    sp, err = _get_client(user_email)
    if err:
        return err

    playlist_id = normalize_playlist_id(playlist_id)

    try:
        playlist_info = await _call(
            sp.playlist,
            playlist_id,
            fields="name,owner.id",
        )
        if not isinstance(playlist_info, dict):
            return "Error: Could not retrieve playlist info"

        playlist_name = playlist_info.get("name", "Unknown")
        owner_id = playlist_info.get("owner", {}).get("id", "")

        user_info = await _call(sp.current_user)
        current_user_id = (
            user_info.get("id", "") if isinstance(user_info, dict) else ""
        )
        if owner_id and current_user_id and owner_id != current_user_id:
            return (
                f"Cannot delete playlist '{playlist_name}' - "
                f"you don't own it (owner: {owner_id})"
            )
    except _API_ERRORS as exc:
        return f"Error checking playlist ownership: {exc}"

    try:
        await _call(sp.current_user_unfollow_playlist, playlist_id)
    except _API_ERRORS as exc:
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
    sp, err = _get_client(user_email)
    if err:
        return err

    playlist_id = normalize_playlist_id(playlist_id)

    if not track_uris:
        return "Error: No track URIs provided"

    normalized_uris = [normalize_track_uri(uri) for uri in track_uris]

    try:
        await _call(
            sp.playlist_add_items,
            playlist_id,
            normalized_uris,
        )
    except _API_ERRORS as exc:
        return f"Error adding tracks to playlist: {exc}"

    return f"Successfully added {len(normalized_uris)} track(s) to playlist"


@mcp.tool("spotify_add_to_queue")
@retry_on_rate_limit(max_retries=3)
async def add_to_queue(
    track_uri: str,
    user_email: str = DEFAULT_USER_EMAIL,
    device_id: str | None = None,
) -> str:
    """Add a track to the playback queue.

    Args:
        track_uri: Spotify track URI, URL, or ID
        user_email: User's email for authentication
        device_id: Optional device ID to add to queue on (default: active device)

    Returns:
        Confirmation message with track details, or an error.
    """
    sp, err = _get_client(user_email)
    if err:
        return err

    track_uri = normalize_track_uri(track_uri)

    try:
        await _call(
            sp.add_to_queue,
            track_uri,
            device_id=device_id,
        )
    except _API_ERRORS as exc:
        return (
            f"Error adding track to queue: {exc}. "
            "Make sure Spotify is open and playing."
        )

    try:
        track_id = track_uri.split(":")[-1]
        track = await _call(sp.track, track_id)
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
    sp, err = _get_client(user_email)
    if err:
        return err

    try:
        queue_data = await _call(sp.queue)
    except _API_ERRORS as exc:
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
            "duration": _format_duration(currently_playing.get("duration_ms", 0)),
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
                    "duration": _format_duration(track.get("duration_ms", 0)),
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
    sp, err = _get_client(user_email)
    if err:
        return err

    try:
        devices_data = await _call(sp.devices)
    except _API_ERRORS as exc:
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
    sp, err = _get_client(user_email)
    if err:
        return err

    try:
        await _call(
            sp.transfer_playback,
            device_id=device_id,
            force_play=play,
        )
    except _API_ERRORS as exc:
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
    sp, err = _get_client(user_email)
    if err:
        return err

    try:
        results = await _call(
            sp.current_user_recently_played,
            limit=min(limit, BATCH_SIZE),
        )
    except _API_ERRORS as exc:
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
                "duration": _format_duration(track.get("duration_ms", 0)),
                "uri": track.get("uri", ""),
                "played_at": played_at,
            }
        )

    return {
        "count": len(tracks_list),
        "tracks": tracks_list,
    }


@mcp.tool("spotify_check_saved_tracks")
@retry_on_rate_limit(max_retries=3)
async def check_saved_tracks(
    track_uris: list[str],
    user_email: str = DEFAULT_USER_EMAIL,
) -> str | dict[str, Any]:
    """Check if one or more tracks are in the user's saved (liked) library.

    This is the fast way to check — a single API call, no pagination.
    Use this instead of fetching the entire saved-tracks library.

    Args:
        track_uris: List of Spotify track URIs, URLs, or IDs to check (max 50)
        user_email: User's email for authentication

    Returns:
        A dict mapping each track URI to True (saved) or False (not saved).
    """
    sp, err = _get_client(user_email)
    if err:
        return err

    if not track_uris:
        return "Error: No track URIs provided"

    normalized = [normalize_track_uri(uri) for uri in track_uris[:50]]
    track_ids = [uri.split(":")[-1] for uri in normalized]

    try:
        results = await _call(
            sp.current_user_saved_tracks_contains, track_ids
        )
    except _API_ERRORS as exc:
        return f"Error checking saved tracks: {exc}"

    if not isinstance(results, list):
        return "Invalid response from Spotify API"

    return {
        "results": {
            uri: saved for uri, saved in zip(normalized, results, strict=True)
        },
    }


@mcp.tool("spotify_find_saved_by_artist")
@retry_on_rate_limit(max_retries=3)
async def find_saved_by_artist(
    artist_name: str,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str | dict[str, Any]:
    """Find all tracks by an artist that are saved in the user's library.

    USE THIS when the user asks things like:
    - "what tracks by <artist> do I have saved?"
    - "do I have any <artist> in my favorites?"
    - "which <artist> songs are in my library?"

    Comprehensive: resolves the artist, fetches ALL their albums (including
    compilations and 'appears on'), gets every track, then batch-checks which
    are saved. This catches deep cuts and compilation appearances that a
    simple search would miss.

    Args:
        artist_name: The name of the artist to look up.
        user_email: User's email for authentication

    Returns:
        Dict with all saved tracks by that artist.
    """
    sp, err = _get_client(user_email)
    if err:
        return err

    # Step 1: resolve artist name → Spotify artist ID
    try:
        search_result = await _call(
            sp.search, q=f"artist:{artist_name}", type="artist", limit=5
        )
    except _API_ERRORS as exc:
        return f"Error searching for artist: {exc}"

    artists_items = (
        search_result.get("artists", {}).get("items", []) if search_result else []
    )
    if not artists_items:
        return f"No artist found matching '{artist_name}'"

    # Pick best match (first result from Spotify's relevance ranking)
    artist = artists_items[0]
    artist_id = artist["id"]
    artist_display = artist.get("name", artist_name)

    # Step 2: get ALL albums (albums, singles, compilations, appears_on)
    all_albums: list[dict[str, Any]] = []
    try:
        results = await _call(
            sp.artist_albums,
            artist_id,
            include_groups="album,single,compilation,appears_on",
            limit=BATCH_SIZE,
        )
        while results and isinstance(results, dict):
            all_albums.extend(results.get("items", []))
            if results.get("next"):
                results = await _call(sp.next, results)
            else:
                break
    except _API_ERRORS as exc:
        return f"Error fetching artist albums: {exc}"

    if not all_albums:
        return f"No albums found for artist '{artist_display}'"

    # Step 3: get all track IDs from those albums
    all_tracks: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for album in all_albums:
        album_id = album.get("id")
        if not album_id:
            continue
        try:
            album_tracks = await _call(
                sp.album_tracks, album_id, limit=BATCH_SIZE
            )
            while album_tracks and isinstance(album_tracks, dict):
                for track in album_tracks.get("items", []):
                    tid = track.get("id")
                    if not tid or tid in seen_ids:
                        continue
                    # Only include tracks where the target artist is credited
                    track_artist_ids = {
                        a.get("id") for a in track.get("artists", [])
                    }
                    if artist_id in track_artist_ids:
                        seen_ids.add(tid)
                        track["_album_name"] = album.get("name", "Unknown")
                        all_tracks.append(track)
                if album_tracks.get("next"):
                    album_tracks = await _call(sp.next, album_tracks)
                else:
                    break
        except Exception:  # noqa: BLE001
            continue  # skip albums that error out

    if not all_tracks:
        return f"No tracks found for artist '{artist_display}'"

    # Step 4: batch-check saved status (50 at a time)
    track_ids = [t["id"] for t in all_tracks]
    saved_flags: list[bool] = []
    for i in range(0, len(track_ids), BATCH_SIZE):
        batch = track_ids[i : i + BATCH_SIZE]
        try:
            flags = await _call(
                sp.current_user_saved_tracks_contains, batch
            )
            saved_flags.extend(flags)
        except Exception:  # noqa: BLE001
            saved_flags.extend([False] * len(batch))

    saved_ids: set[str] = set()
    saved_tracks: list[dict[str, Any]] = []
    for track, is_saved in zip(all_tracks, saved_flags, strict=True):
        if is_saved:
            saved_ids.add(track["id"])
            saved_tracks.append({
                "name": track.get("name", "Unknown"),
                "artist": ", ".join(
                    a.get("name", "Unknown") for a in track.get("artists", [])
                ),
                "album": track.get("_album_name", "Unknown"),
                "duration": _format_duration(track.get("duration_ms", 0)),
                "uri": track.get("uri", ""),
            })

    # Step 5: scan liked songs for tracks by this artist that the album-based
    # search missed.  artist_albums(appears_on) is NOT exhaustive — Spotify
    # omits many compilations, remixes, and features.  Scanning liked songs
    # (~50 per request) is the only reliable way to catch everything.
    liked_extra = 0
    try:
        results = await _call(sp.current_user_saved_tracks, limit=BATCH_SIZE)
        while results and isinstance(results, dict):
            for item in results.get("items", []):
                track = item.get("track")
                if not track or not track.get("id"):
                    continue
                tid = track["id"]
                if tid in saved_ids:
                    continue  # already found via album path
                track_artist_ids = {
                    a.get("id") for a in track.get("artists", [])
                }
                if artist_id in track_artist_ids:
                    saved_ids.add(tid)
                    saved_tracks.append({
                        "name": track.get("name", "Unknown"),
                        "artist": ", ".join(
                            a.get("name", "Unknown")
                            for a in track.get("artists", [])
                        ),
                        "album": track.get("album", {}).get("name", "Unknown"),
                        "duration": _format_duration(track.get("duration_ms", 0)),
                        "uri": track.get("uri", ""),
                    })
                    liked_extra += 1
            if results.get("next"):
                results = await _call(sp.next, results)
            else:
                break
    except Exception:  # noqa: BLE001
        pass  # liked-songs scan is best-effort

    return {
        "artist": artist_display,
        "total_albums_checked": len(all_albums),
        "extra_from_liked_scan": liked_extra,
        "saved_count": len(saved_tracks),
        "saved_tracks": saved_tracks,
    }


@mcp.tool("spotify_get_artist_info")
@retry_on_rate_limit(max_retries=3)
async def get_artist_info(
    artist_name: str,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str | dict[str, Any]:
    """Get detailed information about an artist including top tracks and albums.

    Args:
        artist_name: The name of the artist to look up.
        user_email: User's email for authentication

    Returns:
        Dict with artist details, top tracks, and album list.
    """
    sp, err = _get_client(user_email)
    if err:
        return err

    try:
        search_result = await _call(
            sp.search, q=f"artist:{artist_name}", type="artist", limit=5
        )
    except _API_ERRORS as exc:
        return f"Error searching for artist: {exc}"

    artists_items = (
        search_result.get("artists", {}).get("items", []) if search_result else []
    )
    if not artists_items:
        return f"No artist found matching '{artist_name}'"

    artist = artists_items[0]
    artist_id = artist["id"]

    # Get top tracks
    try:
        top_result = await _call(sp.artist_top_tracks, artist_id)
        top_tracks = [
            {
                "name": t.get("name", "Unknown"),
                "album": t.get("album", {}).get("name", "Unknown"),
                "uri": t.get("uri", ""),
            }
            for t in top_result.get("tracks", [])[:10]
        ]
    except Exception:  # noqa: BLE001
        top_tracks = []

    # Get albums
    try:
        albums_result = await _call(
            sp.artist_albums, artist_id, include_groups="album,single", limit=BATCH_SIZE
        )
        albums = [
            {
                "name": a.get("name", "Unknown"),
                "type": a.get("album_type", "unknown"),
                "release_date": a.get("release_date", ""),
                "total_tracks": a.get("total_tracks", 0),
                "uri": a.get("uri", ""),
            }
            for a in albums_result.get("items", [])
        ]
    except Exception:  # noqa: BLE001
        albums = []

    return {
        "name": artist.get("name", "Unknown"),
        "genres": artist.get("genres", []),
        "popularity": artist.get("popularity", 0),
        "followers": artist.get("followers", {}).get("total", 0),
        "uri": artist.get("uri", ""),
        "url": artist.get("external_urls", {}).get("spotify", ""),
        "top_tracks": top_tracks,
        "albums": albums,
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
    "play",
    "pause_playback",
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
    "check_saved_tracks",
    "find_saved_by_artist",
    "get_artist_info",
]
