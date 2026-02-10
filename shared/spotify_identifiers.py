"""Spotify identifier parsing utilities.

Normalizes Spotify URIs, URLs, and IDs to consistent formats.
"""

from __future__ import annotations


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
        return f"spotify:track:{track_id}"
    elif value.startswith("spotify:track:"):
        return value
    else:
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
        return value.split(":")[-1]
    elif "open.spotify.com/playlist/" in value:
        return value.split("/")[-1].split("?")[0]
    else:
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
        return f"spotify:playlist:{playlist_id}", "playlist"
    elif "open.spotify.com/album/" in value:
        album_id = value.split("/")[-1].split("?")[0]
        return f"spotify:album:{album_id}", "album"
    elif "open.spotify.com/artist/" in value:
        artist_id = value.split("/")[-1].split("?")[0]
        return f"spotify:artist:{artist_id}", "artist"
    elif value.startswith("spotify:playlist:"):
        return value, "playlist"
    elif value.startswith("spotify:album:"):
        return value, "album"
    elif value.startswith("spotify:artist:"):
        return value, "artist"
    else:
        raise ValueError(
            "Invalid context URI. Must be a playlist, album, or artist URI/URL."
        )
