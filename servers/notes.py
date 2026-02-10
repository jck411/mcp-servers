"""Standalone MCP server for NOTES folder integration via Google Drive.

Provides tools to interact with a NOTES folder stored in Google Drive,
including reading, creating, editing, searching, and organizing notes and tags.
Zero imports from Backend_FastAPI — fully standalone.

Run:
    python -m servers.notes --transport streamable-http --host 0.0.0.0 --port 9009
"""

from __future__ import annotations

import asyncio
import io
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastmcp import FastMCP
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from shared.google_auth import DEFAULT_USER_EMAIL, get_drive_service

# Default port for HTTP transport
DEFAULT_HTTP_PORT = 9009

mcp = FastMCP("notes")

# Default vault folder name in Google Drive
DEFAULT_VAULT_FOLDER_NAME = "NOTES"

_MAX_SEARCH_RESULTS = 50
_MAX_FILE_SIZE_BYTES = 1_000_000  # 1MB limit for reading files
_NOTE_PREVIEW_LENGTH = 500


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_drive_service_or_error(
    user_email: str,
) -> Tuple[Optional[Any], Optional[str]]:
    """Get Drive service or return error message."""
    try:
        service = get_drive_service(user_email)
        return service, None
    except ValueError as exc:
        return None, (
            f"Authentication error: {exc}. Click 'Connect Google Services' in Settings "
            "to authorize this account."
        )
    except Exception as exc:
        return None, f"Error creating Google Drive service: {exc}"


def _escape_query_term(value: str) -> str:
    """Escape special characters in Drive query terms."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


async def _find_vault_folder(
    service: Any,
    vault_name: str = DEFAULT_VAULT_FOLDER_NAME,
) -> Tuple[Optional[str], Optional[str]]:
    """Find the NOTES vault folder in Google Drive.

    Returns:
        (folder_id, None) on success
        (None, error_message) on failure
    """
    escaped_name = _escape_query_term(vault_name)
    query = (
        f"name = '{escaped_name}' and "
        "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )

    try:
        results = await asyncio.to_thread(
            service.files()
            .list(
                q=query,
                pageSize=10,
                fields="files(id, name)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute
        )
    except Exception as exc:
        return None, f"Error searching for vault folder: {exc}"

    folders = results.get("files", [])
    if not folders:
        return None, (
            f"NOTES vault folder '{vault_name}' not found in Google Drive. "
            "Make sure your vault is synced to Google Drive."
        )

    return folders[0]["id"], None


async def _resolve_note_path(
    service: Any,
    vault_id: str,
    note_path: str,
) -> Tuple[Optional[str], Optional[str], str]:
    """Resolve a note path to a file ID within the vault.

    Args:
        service: Google Drive service
        vault_id: ID of the vault folder
        note_path: Path like "folder/note" or "note.md"

    Returns:
        (file_id, None, filename) on success
        (None, error_message, filename) on failure
    """
    path = note_path.strip().strip("/")
    if not path.lower().endswith(".md"):
        path += ".md"

    parts = path.split("/")
    filename = parts[-1]
    folder_parts = parts[:-1]

    current_folder_id = vault_id
    for folder_name in folder_parts:
        escaped_name = _escape_query_term(folder_name)
        query = (
            f"'{current_folder_id}' in parents and "
            f"name = '{escaped_name}' and "
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )

        try:
            results = await asyncio.to_thread(
                service.files()
                .list(q=query, pageSize=1, fields="files(id)", supportsAllDrives=True)
                .execute
            )
        except Exception as exc:
            return None, f"Error navigating to folder '{folder_name}': {exc}", filename

        folders = results.get("files", [])
        if not folders:
            return None, f"Folder '{folder_name}' not found in path.", filename

        current_folder_id = folders[0]["id"]

    escaped_filename = _escape_query_term(filename)
    query = (
        f"'{current_folder_id}' in parents and "
        f"name = '{escaped_filename}' and trashed = false"
    )

    try:
        results = await asyncio.to_thread(
            service.files()
            .list(q=query, pageSize=1, fields="files(id, name)", supportsAllDrives=True)
            .execute
        )
    except Exception as exc:
        return None, f"Error finding note '{filename}': {exc}", filename

    files = results.get("files", [])
    if not files:
        return None, f"Note '{note_path}' not found.", filename

    return files[0]["id"], None, filename


async def _get_or_create_folder(
    service: Any,
    parent_id: str,
    folder_name: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Get existing folder or create it if it doesn't exist."""
    escaped_name = _escape_query_term(folder_name)
    query = (
        f"'{parent_id}' in parents and "
        f"name = '{escaped_name}' and "
        "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )

    try:
        results = await asyncio.to_thread(
            service.files()
            .list(q=query, pageSize=1, fields="files(id)", supportsAllDrives=True)
            .execute
        )
    except Exception as exc:
        return None, f"Error checking for folder '{folder_name}': {exc}"

    folders = results.get("files", [])
    if folders:
        return folders[0]["id"], None

    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }

    try:
        folder = await asyncio.to_thread(
            service.files()
            .create(body=metadata, fields="id", supportsAllDrives=True)
            .execute
        )
        return folder["id"], None
    except Exception as exc:
        return None, f"Error creating folder '{folder_name}': {exc}"


async def _download_file_content(
    service: Any, file_id: str
) -> Tuple[Optional[str], Optional[str]]:
    """Download file content as text."""
    try:
        request = service.files().get_media(fileId=file_id)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)

        done = False
        while not done:
            _, done = await asyncio.to_thread(downloader.next_chunk)

        content = buffer.getvalue().decode("utf-8")
        return content, None
    except Exception as exc:
        return None, f"Error downloading file content: {exc}"


def _extract_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Extract YAML frontmatter from note content."""
    frontmatter: Dict[str, Any] = {}
    body = content

    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            yaml_block = parts[1].strip()
            body = parts[2].strip()

            for line in yaml_block.split("\n"):
                if ":" in line:
                    key, _, value = line.partition(":")
                    key = key.strip()
                    value = value.strip()
                    if value.startswith("[") and value.endswith("]"):
                        value = [
                            v.strip().strip("'\"")
                            for v in value[1:-1].split(",")
                            if v.strip()
                        ]
                    frontmatter[key] = value

    return frontmatter, body


def _extract_tags(content: str) -> List[str]:
    """Extract all tags from note content."""
    tags: set[str] = set()

    frontmatter, body = _extract_frontmatter(content)
    fm_tags = frontmatter.get("tags", [])
    if isinstance(fm_tags, list):
        tags.update(fm_tags)
    elif isinstance(fm_tags, str):
        tags.add(fm_tags)

    code_block_pattern = re.compile(r"```[\s\S]*?```|`[^`]+`", re.MULTILINE)
    body_no_code = code_block_pattern.sub("", body)

    tag_pattern = re.compile(r"(?<!\S)#([a-zA-Z][a-zA-Z0-9_/-]*)")
    inline_tags = tag_pattern.findall(body_no_code)
    tags.update(inline_tags)

    return sorted(tags)


def _extract_links(content: str) -> Dict[str, List[str]]:
    """Extract internal and external links from note content."""
    links: Dict[str, List[str]] = {"internal": [], "external": []}

    wiki_link_pattern = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
    wiki_links = wiki_link_pattern.findall(content)
    links["internal"] = sorted(set(wiki_links))

    external_pattern = re.compile(r"\[([^\]]*)\]\((https?://[^\)]+)\)")
    external_links = [url for _, url in external_pattern.findall(content)]
    links["external"] = sorted(set(external_links))

    return links


async def _list_notes_in_folder(
    service: Any,
    folder_id: str,
    vault_id: str,
    prefix: str = "",
) -> List[Dict[str, Any]]:
    """Recursively list all markdown notes in a folder."""
    notes: List[Dict[str, Any]] = []

    query = f"'{folder_id}' in parents and trashed = false"
    page_token = None

    while True:
        params: Dict[str, Any] = {
            "q": query,
            "pageSize": 100,
            "fields": (
                "nextPageToken, "
                "files(id, name, mimeType, size, modifiedTime, createdTime)"
            ),
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        }
        if page_token:
            params["pageToken"] = page_token

        try:
            results = await asyncio.to_thread(service.files().list(**params).execute)
        except Exception:
            break

        for item in results.get("files", []):
            name = item.get("name", "")
            mime_type = item.get("mimeType", "")

            if mime_type == "application/vnd.google-apps.folder":
                if not name.startswith("."):
                    sub_prefix = f"{prefix}{name}/" if prefix else f"{name}/"
                    sub_notes = await _list_notes_in_folder(
                        service, item["id"], vault_id, sub_prefix
                    )
                    notes.extend(sub_notes)
            elif name.lower().endswith(".md"):
                path = f"{prefix}{name}" if prefix else name
                notes.append(
                    {
                        "id": item["id"],
                        "path": path,
                        "name": name[:-3],
                        "folder": prefix.rstrip("/") if prefix else "",
                        "size_bytes": int(item.get("size", 0)),
                        "modified_at": item.get("modifiedTime", ""),
                        "created_at": item.get("createdTime", ""),
                    }
                )

        page_token = results.get("nextPageToken")
        if not page_token:
            break

    return notes


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool("notes_read_note")
async def read_note(
    note_path: str,
    include_metadata: bool = True,
    vault_name: str = DEFAULT_VAULT_FOLDER_NAME,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Read the contents of a note from Google Drive.

    Args:
        note_path: Path to the note relative to vault root (e.g., "folder/note" or "note.md")
        include_metadata: Whether to include extracted metadata (tags, links, frontmatter)
        vault_name: Name of the NOTES vault folder in Google Drive
        user_email: User's email for authentication

    Returns:
        Note content with optional metadata
    """
    service, error = _get_drive_service_or_error(user_email)
    if error:
        return error
    assert service is not None

    vault_id, error = await _find_vault_folder(service, vault_name)
    if error:
        return error
    assert vault_id is not None

    file_id, error, _ = await _resolve_note_path(service, vault_id, note_path)
    if error:
        return error
    assert file_id is not None

    content, error = await _download_file_content(service, file_id)
    if error:
        return error
    assert content is not None

    if not include_metadata:
        return content

    frontmatter, body = _extract_frontmatter(content)
    tags = _extract_tags(content)
    links = _extract_links(content)

    lines = [f"# Note: {note_path}", ""]

    if frontmatter:
        lines.append("## Frontmatter")
        for key, value in frontmatter.items():
            lines.append(f"- {key}: {value}")
        lines.append("")

    if tags:
        lines.append(f"## Tags: {', '.join('#' + t for t in tags)}")
        lines.append("")

    if links["internal"] or links["external"]:
        lines.append("## Links")
        if links["internal"]:
            lines.append(f"Internal: {', '.join(links['internal'])}")
        if links["external"]:
            lines.append(f"External: {len(links['external'])} link(s)")
        lines.append("")

    lines.extend(["## Content", "---", content])

    return "\n".join(lines)


@mcp.tool("notes_create_note")
async def create_note(
    note_path: str,
    content: str,
    tags: Optional[List[str]] = None,
    overwrite: bool = False,
    vault_name: str = DEFAULT_VAULT_FOLDER_NAME,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Create a new note in Google Drive.

    Args:
        note_path: Path for the new note relative to vault root
        content: The content of the note (markdown)
        tags: Optional list of tags to add in frontmatter
        overwrite: Whether to overwrite if note already exists
        vault_name: Name of the NOTES vault folder in Google Drive
        user_email: User's email for authentication

    Returns:
        Status message
    """
    service, error = _get_drive_service_or_error(user_email)
    if error:
        return error
    assert service is not None

    vault_id, error = await _find_vault_folder(service, vault_name)
    if error:
        return error
    assert vault_id is not None

    path = note_path.strip().strip("/")
    if not path.lower().endswith(".md"):
        path += ".md"

    parts = path.split("/")
    filename = parts[-1]
    folder_parts = parts[:-1]

    file_id, _, _ = await _resolve_note_path(service, vault_id, note_path)
    if file_id and not overwrite:
        return f"Note '{note_path}' already exists. Set overwrite=True to replace."

    current_folder_id = vault_id
    for folder_name in folder_parts:
        folder_id, error = await _get_or_create_folder(
            service, current_folder_id, folder_name
        )
        if error:
            return error
        assert folder_id is not None
        current_folder_id = folder_id

    final_content = content
    if tags:
        frontmatter_tags = ", ".join(tags)
        frontmatter = (
            f"---\ntags: [{frontmatter_tags}]\n"
            f"created: {datetime.now(timezone.utc).isoformat()}\n---\n\n"
        )
        final_content = frontmatter + content

    media = MediaIoBaseUpload(
        io.BytesIO(final_content.encode("utf-8")),
        mimetype="text/markdown",
        resumable=False,
    )

    if file_id and overwrite:
        try:
            await asyncio.to_thread(
                service.files()
                .update(fileId=file_id, media_body=media, supportsAllDrives=True)
                .execute
            )
            return f"Note '{note_path}' updated successfully."
        except Exception as exc:
            return f"Error updating note: {exc}"
    else:
        metadata = {
            "name": filename,
            "parents": [current_folder_id],
            "mimeType": "text/markdown",
        }
        try:
            await asyncio.to_thread(
                service.files()
                .create(
                    body=metadata, media_body=media, fields="id", supportsAllDrives=True
                )
                .execute
            )
            return f"Note created successfully at '{note_path}'."
        except Exception as exc:
            return f"Error creating note: {exc}"


@mcp.tool("notes_edit_note")
async def edit_note(
    note_path: str,
    new_content: Optional[str] = None,
    append_content: Optional[str] = None,
    prepend_content: Optional[str] = None,
    vault_name: str = DEFAULT_VAULT_FOLDER_NAME,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Edit an existing note in Google Drive.

    Args:
        note_path: Path to the note relative to vault root
        new_content: Replace entire content (excluding frontmatter if preserved)
        append_content: Text to append at the end
        prepend_content: Text to prepend after frontmatter
        vault_name: Name of the NOTES vault folder in Google Drive
        user_email: User's email for authentication

    Returns:
        Status message
    """
    service, error = _get_drive_service_or_error(user_email)
    if error:
        return error
    assert service is not None

    vault_id, error = await _find_vault_folder(service, vault_name)
    if error:
        return error
    assert vault_id is not None

    file_id, error, _ = await _resolve_note_path(service, vault_id, note_path)
    if error:
        return error
    assert file_id is not None

    current_content, error = await _download_file_content(service, file_id)
    if error:
        return error
    assert current_content is not None

    frontmatter_block = ""
    body = current_content

    if current_content.startswith("---"):
        parts = current_content.split("---", 2)
        if len(parts) >= 3:
            frontmatter_block = f"---{parts[1]}---\n"
            body = parts[2].strip()

    if new_content is not None:
        final_body = new_content
    else:
        final_body = body
        if prepend_content:
            final_body = prepend_content + "\n\n" + final_body
        if append_content:
            final_body = final_body + "\n\n" + append_content

    final_content = frontmatter_block + final_body

    media = MediaIoBaseUpload(
        io.BytesIO(final_content.encode("utf-8")),
        mimetype="text/markdown",
        resumable=False,
    )

    try:
        await asyncio.to_thread(
            service.files()
            .update(fileId=file_id, media_body=media, supportsAllDrives=True)
            .execute
        )
        return f"Note '{note_path}' updated successfully."
    except Exception as exc:
        return f"Error updating note: {exc}"


@mcp.tool("notes_delete_note")
async def delete_note(
    note_path: str,
    confirm: bool = False,
    vault_name: str = DEFAULT_VAULT_FOLDER_NAME,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Delete a note (move to trash in Google Drive).

    Args:
        note_path: Path to the note relative to vault root
        confirm: Must be True to actually delete (safety check)
        vault_name: Name of the NOTES vault folder in Google Drive
        user_email: User's email for authentication

    Returns:
        Status message
    """
    if not confirm:
        return "Delete not confirmed. Set confirm=True to delete the note."

    service, error = _get_drive_service_or_error(user_email)
    if error:
        return error
    assert service is not None

    vault_id, error = await _find_vault_folder(service, vault_name)
    if error:
        return error
    assert vault_id is not None

    file_id, error, _ = await _resolve_note_path(service, vault_id, note_path)
    if error:
        return error
    assert file_id is not None

    try:
        await asyncio.to_thread(
            service.files()
            .update(fileId=file_id, body={"trashed": True}, supportsAllDrives=True)
            .execute
        )
        return f"Note '{note_path}' moved to trash."
    except Exception as exc:
        return f"Error deleting note: {exc}"


@mcp.tool("notes_delete_directory")
async def delete_directory(
    dir_path: str,
    confirm: bool = False,
    vault_name: str = DEFAULT_VAULT_FOLDER_NAME,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Delete a directory (folder) from the vault (moves to trash).

    Args:
        dir_path: Path to the directory relative to vault root
        confirm: Must be True to actually delete (safety check)
        vault_name: Name of the NOTES vault folder in Google Drive
        user_email: User's email for authentication

    Returns:
        Status message
    """
    if not confirm:
        return "Delete not confirmed. Set confirm=True to delete the directory."

    service, error = _get_drive_service_or_error(user_email)
    if error:
        return error
    assert service is not None

    vault_id, error = await _find_vault_folder(service, vault_name)
    if error:
        return error
    assert vault_id is not None

    path = dir_path.strip().strip("/")
    parts = path.split("/")

    current_folder_id = vault_id
    for folder_name in parts:
        escaped = _escape_query_term(folder_name)
        query = (
            f"'{current_folder_id}' in parents and "
            f"name = '{escaped}' and "
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )

        try:
            results = await asyncio.to_thread(
                service.files()
                .list(q=query, pageSize=1, fields="files(id)", supportsAllDrives=True)
                .execute
            )
        except Exception as exc:
            return f"Error finding directory '{folder_name}': {exc}"

        folders = results.get("files", [])
        if not folders:
            return f"Directory '{dir_path}' not found."

        current_folder_id = folders[0]["id"]

    try:
        await asyncio.to_thread(
            service.files()
            .update(
                fileId=current_folder_id,
                body={"trashed": True},
                supportsAllDrives=True,
            )
            .execute
        )
        return f"Directory '{dir_path}' moved to trash."
    except Exception as exc:
        return f"Error deleting directory: {exc}"


@mcp.tool("notes_move_note")
async def move_note(
    source_path: str,
    destination_path: str,
    overwrite: bool = False,
    vault_name: str = DEFAULT_VAULT_FOLDER_NAME,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Move or rename a note within Google Drive.

    Args:
        source_path: Current path to the note
        destination_path: New path for the note
        overwrite: Whether to overwrite if destination exists
        vault_name: Name of the NOTES vault folder in Google Drive
        user_email: User's email for authentication

    Returns:
        Status message
    """
    service, error = _get_drive_service_or_error(user_email)
    if error:
        return error
    assert service is not None

    vault_id, error = await _find_vault_folder(service, vault_name)
    if error:
        return error
    assert vault_id is not None

    source_id, error, _ = await _resolve_note_path(service, vault_id, source_path)
    if error:
        return f"Source: {error}"
    assert source_id is not None

    dest_id, _, _ = await _resolve_note_path(service, vault_id, destination_path)
    if dest_id and not overwrite:
        return (
            f"Destination '{destination_path}' already exists. "
            "Set overwrite=True to replace."
        )

    dest = destination_path.strip().strip("/")
    if not dest.lower().endswith(".md"):
        dest += ".md"

    dest_parts = dest.split("/")
    new_filename = dest_parts[-1]
    dest_folder_parts = dest_parts[:-1]

    dest_folder_id = vault_id
    for folder_name in dest_folder_parts:
        folder_id, error = await _get_or_create_folder(
            service, dest_folder_id, folder_name
        )
        if error:
            return error
        assert folder_id is not None
        dest_folder_id = folder_id

    try:
        file_meta = await asyncio.to_thread(
            service.files()
            .get(fileId=source_id, fields="parents", supportsAllDrives=True)
            .execute
        )
        current_parents = file_meta.get("parents", [])
    except Exception as exc:
        return f"Error getting file info: {exc}"

    try:
        await asyncio.to_thread(
            service.files()
            .update(
                fileId=source_id,
                body={"name": new_filename},
                addParents=dest_folder_id,
                removeParents=",".join(current_parents),
                supportsAllDrives=True,
            )
            .execute
        )

        if dest_id:
            await asyncio.to_thread(
                service.files()
                .update(fileId=dest_id, body={"trashed": True}, supportsAllDrives=True)
                .execute
            )

        return f"Note moved from '{source_path}' to '{destination_path}'."
    except Exception as exc:
        return f"Error moving note: {exc}"


@mcp.tool("notes_create_directory")
async def create_directory_tool(
    dir_path: str,
    vault_name: str = DEFAULT_VAULT_FOLDER_NAME,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Create a new directory (folder) in the vault.

    Args:
        dir_path: Path for the new directory relative to vault root
        vault_name: Name of the NOTES vault folder in Google Drive
        user_email: User's email for authentication

    Returns:
        Status message
    """
    service, error = _get_drive_service_or_error(user_email)
    if error:
        return error
    assert service is not None

    vault_id, error = await _find_vault_folder(service, vault_name)
    if error:
        return error
    assert vault_id is not None

    path = dir_path.strip().strip("/")
    parts = path.split("/")

    current_folder_id = vault_id
    for folder_name in parts:
        folder_id, error = await _get_or_create_folder(
            service, current_folder_id, folder_name
        )
        if error:
            return error
        assert folder_id is not None
        current_folder_id = folder_id

    return f"Directory '{dir_path}' created successfully."


@mcp.tool("notes_search_vault")
async def search_vault(
    query: str,
    search_type: Literal["content", "filename", "tag", "all"] = "all",
    folder: Optional[str] = None,
    limit: int = 20,
    vault_name: str = DEFAULT_VAULT_FOLDER_NAME,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Search notes in the NOTES vault.

    Args:
        query: Search query string
        search_type: Type of search - "content", "filename", "tag", or "all"
        folder: Optional folder to limit search to
        limit: Maximum number of results to return
        vault_name: Name of the NOTES vault folder in Google Drive
        user_email: User's email for authentication

    Returns:
        Formatted search results
    """
    service, error = _get_drive_service_or_error(user_email)
    if error:
        return error
    assert service is not None

    vault_id, error = await _find_vault_folder(service, vault_name)
    if error:
        return error
    assert vault_id is not None

    search_folder_id = vault_id
    if folder:
        folder_path = folder.strip().strip("/")
        for folder_name in folder_path.split("/"):
            escaped = _escape_query_term(folder_name)
            query_str = (
                f"'{search_folder_id}' in parents and "
                f"name = '{escaped}' and "
                "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
            )
            try:
                api_results = await asyncio.to_thread(
                    service.files()
                    .list(
                        q=query_str,
                        pageSize=1,
                        fields="files(id)",
                        supportsAllDrives=True,
                    )
                    .execute
                )
            except Exception as exc:
                return f"Error navigating to folder '{folder}': {exc}"

            found_folders = api_results.get("files", [])
            if not found_folders:
                return f"Folder '{folder}' not found."
            search_folder_id = found_folders[0]["id"]

    notes = await _list_notes_in_folder(service, search_folder_id, vault_id)

    query_lower = query.lower()
    matches: List[Dict[str, Any]] = []
    applied_limit = min(limit, _MAX_SEARCH_RESULTS)

    for note in notes:
        if len(matches) >= applied_limit:
            break

        match_type: List[str] = []

        if search_type in ("filename", "all"):
            if query_lower in note["name"].lower():
                match_type.append("filename")

        if search_type in ("content", "tag", "all") and not match_type:
            content, _ = await _download_file_content(service, note["id"])
            if content:
                if search_type in ("tag", "all"):
                    tags = _extract_tags(content)
                    if any(query_lower in tag.lower() for tag in tags):
                        match_type.append("tag")

                if search_type in ("content", "all") and "tag" not in match_type:
                    if query_lower in content.lower():
                        match_type.append("content")
                        idx = content.lower().find(query_lower)
                        start = max(0, idx - 100)
                        end = min(len(content), idx + len(query) + 100)
                        note["preview"] = "..." + content[start:end] + "..."

        if match_type:
            note["match_type"] = match_type
            matches.append(note)

    if not matches:
        return f"No notes found matching '{query}'."

    lines = [f"Found {len(matches)} notes matching '{query}':", ""]

    for idx, result in enumerate(matches, 1):
        lines.extend(
            [
                f"{idx}. {result['name']}",
                f"   Path: {result['path']}",
                f"   Match: {', '.join(result.get('match_type', []))}",
                f"   Modified: {result['modified_at']}",
            ]
        )
        if "preview" in result:
            preview = result["preview"][:_NOTE_PREVIEW_LENGTH]
            lines.append(f"   Preview: {preview}")
        lines.append("")

    return "\n".join(lines)


@mcp.tool("notes_list_notes")
async def list_notes(
    folder: Optional[str] = None,
    limit: int = 50,
    sort_by: Literal["name", "modified", "created"] = "modified",
    vault_name: str = DEFAULT_VAULT_FOLDER_NAME,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """List notes in the vault or a specific folder.

    Args:
        folder: Optional folder path to list (defaults to entire vault)
        limit: Maximum number of notes to return
        sort_by: Sort order - "name", "modified", or "created"
        vault_name: Name of the NOTES vault folder in Google Drive
        user_email: User's email for authentication

    Returns:
        Formatted list of notes
    """
    service, error = _get_drive_service_or_error(user_email)
    if error:
        return error
    assert service is not None

    vault_id, error = await _find_vault_folder(service, vault_name)
    if error:
        return error
    assert vault_id is not None

    search_folder_id = vault_id
    if folder:
        folder_path = folder.strip().strip("/")
        for folder_name in folder_path.split("/"):
            escaped = _escape_query_term(folder_name)
            query = (
                f"'{search_folder_id}' in parents and "
                f"name = '{escaped}' and "
                "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
            )
            try:
                results = await asyncio.to_thread(
                    service.files()
                    .list(
                        q=query, pageSize=1, fields="files(id)", supportsAllDrives=True
                    )
                    .execute
                )
            except Exception as exc:
                return f"Error navigating to folder '{folder}': {exc}"

            folders = results.get("files", [])
            if not folders:
                return f"Folder '{folder}' not found."
            search_folder_id = folders[0]["id"]

    notes = await _list_notes_in_folder(service, search_folder_id, vault_id)

    if sort_by == "name":
        notes.sort(key=lambda n: n["name"].lower())
    elif sort_by == "modified":
        notes.sort(key=lambda n: n["modified_at"], reverse=True)
    elif sort_by == "created":
        notes.sort(key=lambda n: n["created_at"], reverse=True)

    notes = notes[: min(limit, _MAX_SEARCH_RESULTS)]

    if not notes:
        location = f"folder '{folder}'" if folder else "vault"
        return f"No notes found in {location}."

    location = f"folder '{folder}'" if folder else "vault"
    lines = [f"Found {len(notes)} notes in {location}:", ""]

    for idx, note in enumerate(notes, 1):
        lines.extend(
            [
                f"{idx}. {note['name']}",
                f"   Path: {note['path']}",
                f"   Modified: {note['modified_at']}",
                f"   Size: {note['size_bytes']} bytes",
                "",
            ]
        )

    return "\n".join(lines)


@mcp.tool("notes_add_tags")
async def add_tags(
    note_path: str,
    tags: List[str],
    vault_name: str = DEFAULT_VAULT_FOLDER_NAME,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Add tags to a note's frontmatter.

    Args:
        note_path: Path to the note
        tags: List of tags to add (without # prefix)
        vault_name: Name of the NOTES vault folder in Google Drive
        user_email: User's email for authentication

    Returns:
        Status message
    """
    service, error = _get_drive_service_or_error(user_email)
    if error:
        return error
    assert service is not None

    vault_id, error = await _find_vault_folder(service, vault_name)
    if error:
        return error
    assert vault_id is not None

    file_id, error, _ = await _resolve_note_path(service, vault_id, note_path)
    if error:
        return error
    assert file_id is not None

    content, error = await _download_file_content(service, file_id)
    if error:
        return error
    assert content is not None

    frontmatter, body = _extract_frontmatter(content)

    existing_tags = frontmatter.get("tags", [])
    if isinstance(existing_tags, str):
        existing_tags = [existing_tags] if existing_tags else []

    new_tags = [t.lstrip("#") for t in tags]
    combined_tags = list(set(existing_tags) | set(new_tags))
    combined_tags.sort()

    frontmatter["tags"] = combined_tags
    yaml_lines = ["---"]
    for key, value in frontmatter.items():
        if isinstance(value, list):
            yaml_lines.append(f"{key}: [{', '.join(value)}]")
        else:
            yaml_lines.append(f"{key}: {value}")
    yaml_lines.extend(["---", "", body])

    final_content = "\n".join(yaml_lines)

    media = MediaIoBaseUpload(
        io.BytesIO(final_content.encode("utf-8")),
        mimetype="text/markdown",
        resumable=False,
    )

    try:
        await asyncio.to_thread(
            service.files()
            .update(fileId=file_id, media_body=media, supportsAllDrives=True)
            .execute
        )
        return f"Added tags {tags} to '{note_path}'. Current tags: {combined_tags}"
    except Exception as exc:
        return f"Error updating note: {exc}"


@mcp.tool("notes_remove_tags")
async def remove_tags(
    note_path: str,
    tags: List[str],
    vault_name: str = DEFAULT_VAULT_FOLDER_NAME,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Remove tags from a note's frontmatter.

    Args:
        note_path: Path to the note
        tags: List of tags to remove (without # prefix)
        vault_name: Name of the NOTES vault folder in Google Drive
        user_email: User's email for authentication

    Returns:
        Status message
    """
    service, error = _get_drive_service_or_error(user_email)
    if error:
        return error
    assert service is not None

    vault_id, error = await _find_vault_folder(service, vault_name)
    if error:
        return error
    assert vault_id is not None

    file_id, error, _ = await _resolve_note_path(service, vault_id, note_path)
    if error:
        return error
    assert file_id is not None

    content, error = await _download_file_content(service, file_id)
    if error:
        return error
    assert content is not None

    frontmatter, body = _extract_frontmatter(content)

    existing_tags = frontmatter.get("tags", [])
    if isinstance(existing_tags, str):
        existing_tags = [existing_tags] if existing_tags else []

    tags_to_remove = {t.lstrip("#") for t in tags}
    remaining_tags = [t for t in existing_tags if t not in tags_to_remove]
    remaining_tags.sort()

    frontmatter["tags"] = remaining_tags
    yaml_lines = ["---"]
    for key, value in frontmatter.items():
        if isinstance(value, list):
            yaml_lines.append(f"{key}: [{', '.join(value)}]")
        else:
            yaml_lines.append(f"{key}: {value}")
    yaml_lines.extend(["---", "", body])

    final_content = "\n".join(yaml_lines)

    media = MediaIoBaseUpload(
        io.BytesIO(final_content.encode("utf-8")),
        mimetype="text/markdown",
        resumable=False,
    )

    try:
        await asyncio.to_thread(
            service.files()
            .update(fileId=file_id, media_body=media, supportsAllDrives=True)
            .execute
        )
        return (
            f"Removed tags {tags} from '{note_path}'. Remaining tags: {remaining_tags}"
        )
    except Exception as exc:
        return f"Error updating note: {exc}"


@mcp.tool("notes_list_tags")
async def list_tags(
    folder: Optional[str] = None,
    vault_name: str = DEFAULT_VAULT_FOLDER_NAME,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """List all tags used across notes in the vault.

    Args:
        folder: Optional folder to limit tag collection to
        vault_name: Name of the NOTES vault folder in Google Drive
        user_email: User's email for authentication

    Returns:
        Formatted list of tags with usage counts
    """
    service, error = _get_drive_service_or_error(user_email)
    if error:
        return error
    assert service is not None

    vault_id, error = await _find_vault_folder(service, vault_name)
    if error:
        return error
    assert vault_id is not None

    search_folder_id = vault_id
    if folder:
        folder_path = folder.strip().strip("/")
        for folder_name in folder_path.split("/"):
            escaped = _escape_query_term(folder_name)
            query = (
                f"'{search_folder_id}' in parents and "
                f"name = '{escaped}' and "
                "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
            )
            try:
                results = await asyncio.to_thread(
                    service.files()
                    .list(
                        q=query, pageSize=1, fields="files(id)", supportsAllDrives=True
                    )
                    .execute
                )
            except Exception as exc:
                return f"Error navigating to folder '{folder}': {exc}"

            folders = results.get("files", [])
            if not folders:
                return f"Folder '{folder}' not found."
            search_folder_id = folders[0]["id"]

    notes = await _list_notes_in_folder(service, search_folder_id, vault_id)
    tag_counts: Dict[str, int] = {}

    for note in notes:
        content, _ = await _download_file_content(service, note["id"])
        if content:
            tags = _extract_tags(content)
            for tag in tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

    if not tag_counts:
        return "No tags found in vault."

    sorted_tags = sorted(tag_counts.items(), key=lambda x: (-x[1], x[0]))

    lines = [f"Found {len(sorted_tags)} unique tags:", ""]
    for tag, count in sorted_tags:
        lines.append(f"  #{tag}: {count} note(s)")

    return "\n".join(lines)


@mcp.tool("notes_get_backlinks")
async def get_backlinks(
    note_path: str,
    vault_name: str = DEFAULT_VAULT_FOLDER_NAME,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Find all notes that link to the specified note.

    Args:
        note_path: Path to the note to find backlinks for
        vault_name: Name of the NOTES vault folder in Google Drive
        user_email: User's email for authentication

    Returns:
        List of notes that link to this note
    """
    service, error = _get_drive_service_or_error(user_email)
    if error:
        return error
    assert service is not None

    vault_id, error = await _find_vault_folder(service, vault_name)
    if error:
        return error
    assert vault_id is not None

    file_id, error, filename = await _resolve_note_path(service, vault_id, note_path)
    if error:
        return error
    assert file_id is not None

    target_name = filename[:-3] if filename.lower().endswith(".md") else filename

    notes = await _list_notes_in_folder(service, vault_id, vault_id)
    backlinks: List[Dict[str, Any]] = []

    for note in notes:
        if note["id"] == file_id:
            continue

        content, _ = await _download_file_content(service, note["id"])
        if content:
            links = _extract_links(content)
            for link in links["internal"]:
                link_name = link.split("/")[-1]
                if link_name.lower() == target_name.lower():
                    backlinks.append(note)
                    break

    if not backlinks:
        return f"No backlinks found for '{note_path}'."

    lines = [f"Found {len(backlinks)} backlinks to '{note_path}':", ""]
    for idx, bl in enumerate(backlinks, 1):
        lines.extend(
            [
                f"{idx}. {bl['name']}",
                f"   Path: {bl['path']}",
                "",
            ]
        )

    return "\n".join(lines)


@mcp.tool("notes_vault_info")
async def vault_info(
    vault_name: str = DEFAULT_VAULT_FOLDER_NAME,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Get information about the configured NOTES vault.

    Args:
        vault_name: Name of the NOTES vault folder in Google Drive
        user_email: User's email for authentication

    Returns:
        Vault path, statistics, and configuration status
    """
    service, error = _get_drive_service_or_error(user_email)
    if error:
        lines = [
            "# NOTES Vault Information",
            "",
            f"Vault Name: {vault_name}",
            "Status: ✗ Not Connected",
            "",
            f"Error: {error}",
            "",
            "To connect:",
            "1. Click 'Connect Google Services' in Settings",
            "2. Authorize access to Google Drive",
        ]
        return "\n".join(lines)

    assert service is not None

    vault_id, error = await _find_vault_folder(service, vault_name)

    lines = [
        "# NOTES Vault Information",
        "",
        f"Vault Name: {vault_name}",
    ]

    if error:
        lines.extend(
            [
                "Status: ✗ Not Found",
                "",
                f"Error: {error}",
                "",
                "Make sure your NOTES vault folder exists in Google Drive.",
            ]
        )
        return "\n".join(lines)

    assert vault_id is not None
    lines.append("Status: ✓ Connected")

    notes = await _list_notes_in_folder(service, vault_id, vault_id)
    folders: set[str] = set()
    total_size = 0

    for note in notes:
        if note["folder"]:
            folders.add(note["folder"])
        total_size += note["size_bytes"]

    lines.extend(
        [
            "",
            "## Statistics",
            f"- Total notes: {len(notes)}",
            f"- Folders: {len(folders)}",
            f"- Total size: {total_size / 1024 / 1024:.2f} MB",
            f"- Drive folder ID: {vault_id}",
        ]
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def run(
    transport: str = "stdio",
    host: str = "0.0.0.0",
    port: int = DEFAULT_HTTP_PORT,
) -> None:  # pragma: no cover - integration entrypoint
    """Run the Notes MCP server with the specified transport."""
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

    parser = argparse.ArgumentParser(description="Notes MCP Server")
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
    "read_note",
    "create_note",
    "edit_note",
    "delete_note",
    "delete_directory",
    "move_note",
    "create_directory_tool",
    "search_vault",
    "list_notes",
    "add_tags",
    "remove_tags",
    "list_tags",
    "get_backlinks",
    "vault_info",
]
