"""Google OAuth helper for standalone MCP servers.

Handles credential loading, token refresh, and service construction for
Google APIs (Calendar, Gmail, Drive, Tasks).  Tokens live in data/tokens/,
client secrets in credentials/.

Zero imports from Backend_FastAPI — fully standalone.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

import google.oauth2.credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Paths — resolved relative to the repo root
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS_PATH = REPO_ROOT / "credentials"
TOKEN_PATH = REPO_ROOT / "data" / "tokens"

os.makedirs(TOKEN_PATH, exist_ok=True)

# ---------------------------------------------------------------------------
# Default user (override via DEFAULT_USER_EMAIL env var)
# ---------------------------------------------------------------------------

DEFAULT_USER_EMAIL: str = os.environ.get("DEFAULT_USER_EMAIL", "jck411@gmail.com")

# ---------------------------------------------------------------------------
# OAuth scopes — superset used by all Google-dependent servers
# ---------------------------------------------------------------------------

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
TASKS_SCOPE = "https://www.googleapis.com/auth/tasks"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
GMAIL_LABELS_SCOPE = "https://www.googleapis.com/auth/gmail.labels"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"

SCOPES = [
    CALENDAR_SCOPE,
    TASKS_SCOPE,
    GMAIL_READONLY_SCOPE,
    GMAIL_MODIFY_SCOPE,
    GMAIL_SEND_SCOPE,
    GMAIL_COMPOSE_SCOPE,
    GMAIL_LABELS_SCOPE,
    DRIVE_SCOPE,
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_token_scopes(token_data: Dict[str, Any]) -> set[str]:
    """Extract OAuth scopes from a stored token payload."""
    scopes_field = token_data.get("scopes")
    if isinstance(scopes_field, list):
        return set(scopes_field)

    scope_field = token_data.get("scope")
    if isinstance(scope_field, str):
        return set(scope_field.split())

    return set()


def get_credentials_dir() -> Path:
    """Return the credentials directory, creating it if needed."""
    CREDENTIALS_PATH.mkdir(parents=True, exist_ok=True)
    return CREDENTIALS_PATH


def get_client_config() -> Dict[str, Any]:
    """Load the first client_secret_*.json from the credentials directory."""
    client_secrets = list(CREDENTIALS_PATH.glob("client_secret_*.json"))
    if not client_secrets:
        raise FileNotFoundError(
            "No client_secret file found in credentials directory. "
            "Please download it from Google Cloud Console."
        )
    with open(client_secrets[0], "r") as f:
        return json.load(f)


def get_token_path(user_email: str) -> Path:
    """Return the path for a user's stored token file."""
    filename = user_email.replace("@", "_at_").replace(".", "_") + ".json"
    return TOKEN_PATH / filename


def get_credentials(user_email: str) -> Optional[Any]:
    """Load and refresh stored credentials for *user_email*.

    Returns a Credentials object, or None if no valid token exists.
    """
    token_path = get_token_path(user_email)
    if not token_path.exists():
        return None

    with open(token_path, "r") as fh:
        token_data = json.load(fh)

    required_scopes = set(SCOPES)
    current_scopes = _extract_token_scopes(token_data)
    if not required_scopes.issubset(current_scopes):
        try:
            token_path.unlink()
        except OSError:
            pass
        return None

    creds = google.oauth2.credentials.Credentials.from_authorized_user_info(
        token_data, SCOPES
    )

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        store_credentials(user_email, creds)

    return creds if creds and not creds.expired else None


def store_credentials(user_email: str, credentials: Any) -> None:
    """Persist credentials to the token file."""
    token_path = get_token_path(user_email)
    with open(token_path, "w") as fh:
        fh.write(credentials.to_json())


# ---------------------------------------------------------------------------
# Service factories
# ---------------------------------------------------------------------------


def get_drive_service(user_email: str) -> Any:
    """Return an authenticated Google Drive v3 service."""
    credentials = get_credentials(user_email)
    if not credentials:
        raise ValueError(
            f"No valid credentials found for {user_email}. "
            "Click 'Connect Google Services' in Settings to authorize access."
        )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def get_calendar_service(user_email: str) -> Any:
    """Return an authenticated Google Calendar v3 service."""
    credentials = get_credentials(user_email)
    if not credentials:
        raise ValueError(
            f"No valid credentials found for {user_email}. "
            "Click 'Connect Google Services' in Settings to authorize access."
        )
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def get_gmail_service(user_email: str) -> Any:
    """Return an authenticated Gmail v1 service."""
    credentials = get_credentials(user_email)
    if not credentials:
        raise ValueError(
            f"No valid credentials found for {user_email}. "
            "Click 'Connect Google Services' in Settings to authorize access."
        )
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def get_tasks_service(user_email: str) -> Any:
    """Return an authenticated Google Tasks v1 service."""
    credentials = get_credentials(user_email)
    if not credentials:
        raise ValueError(
            f"No valid credentials found for {user_email}. "
            "Click 'Connect Google Services' in Settings to authorize access."
        )
    return build("tasks", "v1", credentials=credentials, cache_discovery=False)
