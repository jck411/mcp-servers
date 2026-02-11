"""Standalone Monarch Money authentication helper.

Loads credentials from ``credentials/monarch_credentials.json`` relative to the
repo root.  The file should contain::

    {
        "email": "...",
        "password": "...",
        "mfa_secret": "..."   // optional
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

# Resolve project root: shared/ -> repo root
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CREDENTIALS_DIR = _PROJECT_ROOT / "credentials"
_MONARCH_CREDENTIALS_FILE = _CREDENTIALS_DIR / "monarch_credentials.json"


class MonarchCredentials(BaseModel):
    """Monarch Money login credentials."""

    email: str
    password: str
    mfa_secret: Optional[str] = None


def get_monarch_credentials() -> MonarchCredentials | None:
    """Load Monarch credentials from disk if they exist."""
    if not _MONARCH_CREDENTIALS_FILE.exists():
        return None
    try:
        with open(_MONARCH_CREDENTIALS_FILE) as f:
            data = json.load(f)
            return MonarchCredentials(**data)
    except Exception:
        return None


def get_session_file_path() -> Path:
    """Return the path for the Monarch session pickle file."""
    token_dir = _PROJECT_ROOT / "data" / "tokens"
    token_dir.mkdir(parents=True, exist_ok=True)
    return token_dir / "monarch_session.pickle"
