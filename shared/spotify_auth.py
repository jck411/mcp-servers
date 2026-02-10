"""Spotify OAuth helper for the Spotify MCP server.

Handles Spotify OAuth token loading and refresh.
Tokens are stored in the credentials/ directory.

TODO: Extract from Backend_FastAPI when migrating the Spotify server.
"""

from __future__ import annotations

from pathlib import Path

# Default credentials directory (relative to repo root)
DEFAULT_CREDENTIALS_DIR = Path(__file__).resolve().parent.parent / "credentials"


def get_credentials_dir() -> Path:
    """Return the credentials directory, creating it if needed."""
    DEFAULT_CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_CREDENTIALS_DIR


# Placeholder — will be implemented when spotify server is migrated
