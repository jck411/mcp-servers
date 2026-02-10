"""Spotify OAuth helper for the standalone MCP server.

Handles Spotify OAuth token loading and refresh via spotipy.
Credentials: credentials/spotify_credentials.json
Tokens:      data/tokens/<email>_spotify.json

Zero imports from Backend_FastAPI — fully standalone.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from functools import wraps
from io import StringIO
from pathlib import Path
from typing import Any, Callable, TypeVar

import spotipy
from spotipy.oauth2 import SpotifyOAuth

# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

DEFAULT_USER_EMAIL = "jck411@gmail.com"

# Resolve paths relative to repo root  (shared/ -> repo root)
_REPO_ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS_PATH = _REPO_ROOT / "credentials"
TOKEN_PATH = _REPO_ROOT / "data" / "tokens"

# Create token directory if it doesn't exist
os.makedirs(TOKEN_PATH, exist_ok=True)

# All Spotify Web API user scopes
SCOPES = [
    # Images
    "ugc-image-upload",
    # Spotify Connect / Playback
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "app-remote-control",
    "streaming",
    # Playlists
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-public",
    "playlist-modify-private",
    # Follow
    "user-follow-modify",
    "user-follow-read",
    # Listening History
    "user-read-playback-position",
    "user-top-read",
    "user-read-recently-played",
    # Library
    "user-library-modify",
    "user-library-read",
    # User Profile
    "user-read-email",
    "user-read-private",
]

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Silence spotipy logging (pollutes MCP stdout otherwise)
# ---------------------------------------------------------------------------

SPOTIPY_LOGGER_NAMES = ("spotipy", "spotipy.client", "spotipy.oauth2")


def _silence_spotipy_logging() -> None:
    for logger_name in SPOTIPY_LOGGER_NAMES:
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.ERROR)
        logger.propagate = False
        if not logger.handlers:
            logger.addHandler(logging.NullHandler())


_silence_spotipy_logging()


# ---------------------------------------------------------------------------
# stdout/stderr suppression (spotipy prints OAuth prompts)
# ---------------------------------------------------------------------------


@contextmanager
def suppress_stdout_stderr():
    """Suppress stdout/stderr to keep MCP JSON-RPC clean."""
    old_stdout, old_stderr = sys.stdout, sys.stderr
    try:
        sys.stdout = StringIO()
        sys.stderr = StringIO()
        yield
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr


# ---------------------------------------------------------------------------
# Rate-limit retry decorator
# ---------------------------------------------------------------------------


def retry_on_rate_limit(
    max_retries: int = 3,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Retry on Spotify 429 with exponential backoff."""

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> T:
                for attempt in range(max_retries):
                    try:
                        return await func(*args, **kwargs)
                    except spotipy.exceptions.SpotifyException as e:
                        if e.http_status == 429 and attempt < max_retries - 1:
                            await asyncio.sleep(2**attempt)
                            continue
                        raise
                return await func(*args, **kwargs)

            return async_wrapper  # type: ignore
        else:

            @wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> T:
                for attempt in range(max_retries):
                    try:
                        return func(*args, **kwargs)
                    except spotipy.exceptions.SpotifyException as e:
                        if e.http_status == 429 and attempt < max_retries - 1:
                            time.sleep(2**attempt)
                            continue
                        raise
                return func(*args, **kwargs)

            return sync_wrapper  # type: ignore

    return decorator


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------


def get_spotify_config() -> dict[str, Any]:
    """Load client_id / client_secret / redirect_uri from credentials file."""
    config_path = CREDENTIALS_PATH / "spotify_credentials.json"
    if not config_path.exists():
        raise FileNotFoundError(
            f"Spotify credentials not found at {config_path}. "
            "Copy spotify_credentials.json into credentials/."
        )
    with open(config_path) as f:
        return json.load(f)


def get_token_path(user_email: str) -> Path:
    """Return the path for a user's cached Spotify token."""
    filename = user_email.replace("@", "_at_").replace(".", "_") + "_spotify.json"
    return TOKEN_PATH / filename


def get_credentials(user_email: str) -> dict[str, Any] | None:
    """Load stored token data; return None if missing/invalid."""
    token_path = get_token_path(user_email)
    if not token_path.exists():
        return None
    try:
        with open(token_path) as f:
            token_data = json.load(f)
        if "access_token" not in token_data:
            return None
        return token_data
    except (json.JSONDecodeError, OSError):
        return None


def store_credentials(user_email: str, token_info: dict[str, Any]) -> None:
    """Persist token data to disk."""
    token_path = get_token_path(user_email)
    with open(token_path, "w") as f:
        json.dump(token_info, f, indent=2)


# ---------------------------------------------------------------------------
# Spotify client factory
# ---------------------------------------------------------------------------


def get_spotify_client(user_email: str) -> spotipy.Spotify:
    """Return an authenticated Spotify client (auto-refreshes tokens)."""
    credentials = get_credentials(user_email)
    if not credentials:
        raise ValueError(
            f"No valid Spotify credentials found for {user_email}. "
            "Authorize via the backend's Spotify OAuth flow first."
        )

    config = get_spotify_config()

    with suppress_stdout_stderr():
        auth_manager = SpotifyOAuth(
            client_id=config["client_id"],
            client_secret=config["client_secret"],
            redirect_uri=config["redirect_uri"],
            scope=" ".join(SCOPES),
            cache_path=str(get_token_path(user_email)),
            open_browser=False,
            show_dialog=False,
        )
        token_info = auth_manager.validate_token(
            auth_manager.cache_handler.get_cached_token()
        )

    if not token_info:
        raise ValueError(
            "Stored Spotify credentials are expired or missing required scopes. "
            "Re-authorize via the backend's Spotify OAuth flow."
        )

    return spotipy.Spotify(auth_manager=auth_manager)
