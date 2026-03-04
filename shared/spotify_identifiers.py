"""Spotify identifier parsing utilities.

Normalizes Spotify URIs, URLs, and IDs to consistent formats.
"""

from __future__ import annotations

import re

# Base-62 Spotify ID: 22 alphanumeric characters
_SPOTIFY_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")


def _validate_spotify_id(raw_id: str, kind: str) -> str:
    """Validate a Spotify ID looks structurally correct.

    Raises:
        ValueError: If the ID doesn't match the expected format.
    """
    if not _SPOTIFY_ID_RE.match(raw_id):
        raise ValueError(
            f"Invalid Spotify {kind} ID: {raw_id!r}. "
            "Expected a 22-character alphanumeric string."
        )
    return raw_id


def normalize_track_uri(value: str) -> str:
    """Convert track URL/ID/URI to spotify:track:xxx format.

    Args:
        value: Track identifier in any format:
            - URL: "https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6"
            - URI: "spotify:track:6rqhFgbbKwnb9MLmUQDhG6"
            - ID: "6rqhFgbbKwnb9MLmUQDhG6"

    Returns:
        Normalized URI in format "spotify:track:{id}"
    """
    if value.startswith("https://open.spotify.com/track/"):
        track_id = value.split("/")[-1].split("?")[0]
        _validate_spotify_id(track_id, "track")
        return f"spotify:track:{track_id}"
    elif value.startswith("spotify:track:"):
        _validate_spotify_id(value.split(":")[-1], "track")
        return value
    else:
        _validate_spotify_id(value, "track")
        return f"spotify:track:{value}"


def normalize_playlist_id(value: str) -> str:
    """Extract playlist ID from URI/URL/ID.

    Args:
        value: Playlist identifier in any format:
            - URL: "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
            - URI: "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"
            - ID: "37i9dQZF1DXcBWIGoYBM5M"

    Returns:
        Playlist ID (without spotify:playlist: prefix)
    """
    if value.startswith("spotify:playlist:"):
        pid = value.split(":")[-1]
        _validate_spotify_id(pid, "playlist")
        return pid
    elif "open.spotify.com/playlist/" in value:
        pid = value.split("/")[-1].split("?")[0]
        _validate_spotify_id(pid, "playlist")
        return pid
    else:
        _validate_spotify_id(value, "playlist")
        return value


def normalize_context_uri(value: str) -> tuple[str, str]:
    """Convert context URL/URI to normalized URI and extract type.

    Args:
        value: Context identifier (playlist, album, or artist) in any format

    Returns:
        Tuple of (normalized_uri, context_type)
        where context_type is one of: "playlist", "album", "artist"

    Raises:
        ValueError: If the context type cannot be determined
    """
    if "open.spotify.com/playlist/" in value:
        playlist_id = value.split("/")[-1].split("?")[0]
        _validate_spotify_id(playlist_id, "playlist")
        return f"spotify:playlist:{playlist_id}", "playlist"
    elif "open.spotify.com/album/" in value:
        album_id = value.split("/")[-1].split("?")[0]
        _validate_spotify_id(album_id, "album")
        return f"spotify:album:{album_id}", "album"
    elif "open.spotify.com/artist/" in value:
        artist_id = value.split("/")[-1].split("?")[0]
        _validate_spotify_id(artist_id, "artist")
        return f"spotify:artist:{artist_id}", "artist"
    elif value.startswith("spotify:playlist:"):
        _validate_spotify_id(value.split(":")[-1], "playlist")
        return value, "playlist"
    elif value.startswith("spotify:album:"):
        _validate_spotify_id(value.split(":")[-1], "album")
        return value, "album"
    elif value.startswith("spotify:artist:"):
        _validate_spotify_id(value.split(":")[-1], "artist")
        return value, "artist"
    else:
        raise ValueError(
            "Invalid context URI. Must be a playlist, album, or artist URI/URL."
        )
