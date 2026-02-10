"""Google OAuth helper for MCP servers that need Google API access.

Servers that need Google auth (calendar, gmail, gdrive, notes) use this
module to load and refresh OAuth tokens. Tokens are stored in the
credentials/ directory.

TODO: Extract from Backend_FastAPI when migrating Google-dependent servers.
"""

from __future__ import annotations

import json
from pathlib import Path

# Default credentials directory (relative to repo root)
DEFAULT_CREDENTIALS_DIR = Path(__file__).resolve().parent.parent / "credentials"


def get_credentials_dir() -> Path:
    """Return the credentials directory, creating it if needed."""
    DEFAULT_CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_CREDENTIALS_DIR


# Placeholder — will be implemented when calendar/gmail/gdrive/notes are migrated
