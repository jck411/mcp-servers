"""Standalone Google Drive MCP server.

Exposes file search, listing, download, create, delete, move, copy, rename,
folder creation, and permission inspection via MCP protocol.
Zero imports from Backend_FastAPI — fully standalone.

Run:
    python -m servers.gdrive --transport streamable-http --host 0.0.0.0 --port 9006
"""

from __future__ import annotations

import asyncio
import base64
import io
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from fastmcp import FastMCP

from shared.google_auth import DEFAULT_USER_EMAIL, get_drive_service

# Default port for HTTP transport
DEFAULT_HTTP_PORT = 9006

mcp = FastMCP("gdrive")

DRIVE_FIELDS_MINIMAL = (
    "files(id, name, mimeType, size, modifiedTime, webViewLink, iconLink)"
)
# Maximum file size in bytes for download operations (50MB)
MAX_CONTENT_BYTES = 50 * 1024 * 1024


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


def _normalize_parent_id(parent_id: Optional[str]) -> str:
    """Normalize parent folder ID, treating empty/None as root."""
    if not parent_id or not parent_id.strip():
        return "root"
    return parent_id.strip()


def _escape_query_term(value: str) -> str:
    """Escape special characters in Drive query terms."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _has_anyone_link_access(permissions: List[Dict[str, Any]]) -> bool:
    """Check if permissions include 'anyone with the link' access."""
    return any(
        perm.get("type") == "anyone"
        and perm.get("role") in {"reader", "writer", "commenter"}
        for perm in permissions
    )


def _build_drive_list_params(
    *,
    query: str,
    page_size: int,
    drive_id: Optional[str],
    include_items_from_all_drives: bool,
    corpora: Optional[str],
) -> Dict[str, object]:
    """Compose parameters for Drive files().list requests."""
    params: Dict[str, object] = {
        "q": query,
        "pageSize": max(page_size, 1),
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": include_items_from_all_drives,
        "fields": f"nextPageToken, {DRIVE_FIELDS_MINIMAL}",
        "orderBy": "modifiedTime desc",
    }
    if drive_id:
        params["driveId"] = drive_id
        params["corpora"] = corpora or "drive"
    elif corpora:
        params["corpora"] = corpora
    return params


async def _download_request_bytes(
    request: Any, max_size: Optional[int] = None
) -> bytes:
    """Stream a Drive media request into bytes using the resumable downloader."""
    if max_size is None:
        max_size = MAX_CONTENT_BYTES

    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)

    loop = asyncio.get_running_loop()
    done = False
    while not done:
        _, done = await loop.run_in_executor(None, downloader.next_chunk)
        current_size = buffer.tell()
        if current_size > max_size:
            raise ValueError(
                f"File size ({current_size} bytes) exceeds maximum allowed size "
                f"({max_size} bytes, ~{max_size // (1024 * 1024)}MB)"
            )

    return buffer.getvalue()


async def _extract_pdf_text(content_bytes: bytes) -> str:
    """Extract text from PDF bytes using kreuzberg."""
    try:
        import kreuzberg

        result = await kreuzberg.extract_bytes(content_bytes, mime_type="application/pdf")
        text = str(getattr(result, "content", "") or "").strip()
        if not text:
            # Try OCR fallback
            try:
                result_ocr = await kreuzberg.extract_bytes(
                    content_bytes,
                    mime_type="application/pdf",
                    force_ocr=True,
                )
                text = str(getattr(result_ocr, "content", "") or "").strip()
            except Exception:
                pass
        return text
    except ImportError:
        return ""
    except Exception:
        return ""


def _is_structured_drive_query(query: str) -> bool:
    """Detect if query is a structured Drive query vs plain text."""
    field_patterns = [
        r"\b(name|mimeType|fullText|modifiedTime|createdTime|trashed|starred)"
        r"\s*(=|!=|contains)\s*['\"]",
        r"['\"][^'\"]+['\"]\s+in\s+parents\b",
        r"\b(and|or)\s+(name|mimeType|fullText|trashed|starred)\s*(=|!=|contains)",
    ]
    for pattern in field_patterns:
        if re.search(pattern, query, re.IGNORECASE):
            return True
    return False


def _detect_file_type_query(query: str) -> Optional[str]:
    """Detect if query is asking for a specific file type and return MIME type filter."""
    query_lower = query.lower()

    type_mappings = [
        (
            [
                "image", "images", "photo", "photos", "picture", "pictures",
                "img", "png", "jpg", "jpeg", "gif",
            ],
            "mimeType contains 'image/'",
        ),
        (["pdf", "pdfs"], "mimeType = 'application/pdf'"),
        (
            ["document", "documents", "doc", "docs", "google doc", "google docs"],
            "mimeType = 'application/vnd.google-apps.document'",
        ),
        (
            [
                "spreadsheet", "spreadsheets", "sheet", "sheets",
                "google sheet", "google sheets",
            ],
            "mimeType = 'application/vnd.google-apps.spreadsheet'",
        ),
        (
            [
                "presentation", "presentations", "slide", "slides",
                "google slide", "google slides",
            ],
            "mimeType = 'application/vnd.google-apps.presentation'",
        ),
        (
            ["folder", "folders", "directory", "directories"],
            "mimeType = 'application/vnd.google-apps.folder'",
        ),
        (
            ["video", "videos", "movie", "movies", "mp4", "avi", "mov"],
            "mimeType contains 'video/'",
        ),
        (["audio", "sound", "music", "mp3", "wav"], "mimeType contains 'audio/'"),
        (["text file", "text files", "txt"], "mimeType = 'text/plain'"),
    ]

    for keywords, mime_filter in type_mappings:
        for keyword in keywords:
            pattern = r"\b" + re.escape(keyword) + r"\b"
            if re.search(pattern, query_lower):
                return mime_filter

    return None


async def _locate_child_folder(
    service: Any,
    *,
    parent_id: str,
    folder_name: str,
    drive_id: Optional[str],
    include_items_from_all_drives: bool,
    corpora: Optional[str],
    page_size: int = 10,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return the first folder named *folder_name* beneath *parent_id*."""
    escaped_name = _escape_query_term(folder_name.strip())
    query = (
        f"'{parent_id}' in parents and "
        f"name = '{escaped_name}' and "
        "mimeType = 'application/vnd.google-apps.folder' and trashed=false"
    )
    params = _build_drive_list_params(
        query=query,
        page_size=min(page_size, 100),
        drive_id=drive_id,
        include_items_from_all_drives=include_items_from_all_drives,
        corpora=corpora,
    )

    folders: List[Dict[str, Any]] = []
    page_token: Optional[str] = None

    while len(folders) < page_size:
        if page_token:
            params["pageToken"] = page_token
        try:
            results = await asyncio.to_thread(service.files().list(**params).execute)
        except Exception as exc:
            return None, (
                f"Error resolving folder '{folder_name}' under parent '{parent_id}': {exc}"
            )

        page_folders = [
            item
            for item in results.get("files", [])
            if item.get("mimeType") == "application/vnd.google-apps.folder"
        ]
        folders.extend(page_folders)

        page_token = results.get("nextPageToken")
        if not page_token or len(folders) >= page_size:
            break

    if not folders:
        return None, None

    warning: Optional[str] = None
    if len(folders) > 1:
        candidates = ", ".join(
            f"{item.get('name', '(unknown)')} (ID: {item.get('id', 'unknown')})"
            for item in folders[1:4]
        )
        warning = (
            f"Multiple folders named '{folder_name}' found; using the most recently "
            f"modified match (ID: {folders[0].get('id', 'unknown')})."
        )
        if candidates:
            warning += f" Other candidates: {candidates}"

    return folders[0], warning


async def _resolve_folder_reference(
    service: Any,
    *,
    folder_id: Optional[str],
    folder_name: Optional[str],
    folder_path: Optional[str],
    drive_id: Optional[str],
    include_items_from_all_drives: bool,
    corpora: Optional[str],
) -> Tuple[Optional[str], Optional[str], List[str]]:
    """Resolve user folder selection inputs to a concrete folder ID."""
    warnings: List[str] = []

    normalized_id = (folder_id or "").strip()
    normalized_path = (folder_path or "").strip().strip("/")
    normalized_name = (folder_name or "").strip()

    base_id = normalized_id if normalized_id else "root"
    base_label = "root" if base_id == "root" else base_id

    if normalized_path:
        parent_id = base_id
        label_parts: List[str] = [] if base_id == "root" else [base_label]
        for segment in [
            part.strip() for part in normalized_path.split("/") if part.strip()
        ]:
            located, note = await _locate_child_folder(
                service,
                parent_id=parent_id,
                folder_name=segment,
                drive_id=drive_id,
                include_items_from_all_drives=include_items_from_all_drives,
                corpora=corpora,
            )
            scope = "/".join(label_parts) if label_parts else base_label
            if located is None:
                missing_context = scope or (
                    "root" if parent_id == "root" else parent_id
                )
                return (
                    None,
                    f"Unable to find folder '{segment}' within '{missing_context}'.",
                    warnings,
                )
            if note:
                warnings.append(note)
            parent_id = located.get("id", parent_id)
            label_parts.append(located.get("name", segment))

        final_label = "/".join(label_parts) if label_parts else base_label
        return parent_id, final_label or normalized_path, warnings

    if (not normalized_id or normalized_id.lower() == "root") and normalized_name:
        located, note = await _locate_child_folder(
            service,
            parent_id=base_id,
            folder_name=normalized_name,
            drive_id=drive_id,
            include_items_from_all_drives=include_items_from_all_drives,
            corpora=corpora,
        )
        scope = "root" if base_id == "root" else base_label
        if located is None:
            return (
                None,
                f"No folder named '{normalized_name}' was found under '{scope}'.",
                warnings,
            )
        if note:
            warnings.append(note)
        label = located.get("name", normalized_name)
        if scope != "root":
            label = f"{scope}/{label}"
        return located.get("id"), label, warnings

    final_id = normalized_id or "root"
    final_label = "root" if final_id == "root" else final_id
    return final_id, final_label, warnings


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@mcp.tool("gdrive_search_files")
async def search_drive_files(
    query: str,
    user_email: str = DEFAULT_USER_EMAIL,
    page_size: int = 10,
    drive_id: Optional[str] = None,
    include_items_from_all_drives: bool = True,
    corpora: Optional[str] = None,
) -> str:
    """Search for files in Google Drive.

    Intelligently handles different query types:
    - File type queries (e.g., "image", "pdf", "spreadsheet") filter by MIME type
    - Structured queries (e.g., "name='report'") are passed through as-is
    - Text queries search in file names
    """
    service, error_msg = _get_drive_service_or_error(user_email)
    if error_msg:
        return error_msg
    assert service is not None

    is_structured = _is_structured_drive_query(query)
    escaped_query = _escape_query_term(query)

    if is_structured:
        final_query = query
    else:
        mime_filter = _detect_file_type_query(query)
        if mime_filter:
            query_lower = query.lower()
            search_terms = query_lower
            for keywords, _ in [
                (
                    [
                        "image", "images", "photo", "photos", "picture", "pictures",
                        "img", "png", "jpg", "jpeg", "gif",
                    ],
                    None,
                ),
                (["pdf", "pdfs"], None),
                (["document", "documents", "doc", "docs"], None),
                (["spreadsheet", "spreadsheets", "sheet", "sheets"], None),
                (["presentation", "presentations", "slide", "slides"], None),
                (["folder", "folders"], None),
                (["video", "videos", "movie", "movies"], None),
                (["audio", "sound", "music"], None),
            ]:
                for keyword in keywords:
                    pattern = r"\b" + re.escape(keyword) + r"\b"
                    search_terms = re.sub(pattern, "", search_terms)

            search_terms = re.sub(r"\b(latest|recent|new|old|my)\b", "", search_terms)
            search_terms = search_terms.strip()

            if search_terms:
                escaped_terms = _escape_query_term(search_terms)
                final_query = f"{mime_filter} and name contains '{escaped_terms}'"
            else:
                final_query = mime_filter
        else:
            final_query = f"name contains '{escaped_query}'"

    params = _build_drive_list_params(
        query=final_query,
        page_size=min(page_size, 100),
        drive_id=drive_id,
        include_items_from_all_drives=include_items_from_all_drives,
        corpora=corpora,
    )

    files: List[Dict[str, Any]] = []
    page_token: Optional[str] = None

    while len(files) < page_size:
        if page_token:
            params["pageToken"] = page_token
        try:
            results = await asyncio.to_thread(service.files().list(**params).execute)
        except Exception as exc:
            return f"Error searching Drive files: {exc}"

        files.extend(results.get("files", []))
        page_token = results.get("nextPageToken")
        if not page_token or len(files) >= page_size:
            break

    files = files[:page_size]

    if not files:
        return f"No files found for '{query}'."

    lines = [f"Found {len(files)} files for {user_email} matching '{query}':", ""]
    for item in files:
        size_text = f", Size: {item.get('size', 'N/A')}" if "size" in item else ""
        lines.append(
            f'- Name: "{item.get("name", "(unknown)")}" '
            f"(ID: {item.get('id', 'unknown')}, Type: {item.get('mimeType', 'unknown')}"
            f"{size_text}, Modified: {item.get('modifiedTime', 'N/A')}) "
            f"Link: {item.get('webViewLink', '#')}"
        )
    return "\n".join(lines)


@mcp.tool("gdrive_list_folder")
async def list_drive_items(
    folder_id: Optional[str] = "root",
    folder_name: Optional[str] = None,
    folder_path: Optional[str] = None,
    user_email: str = DEFAULT_USER_EMAIL,
    page_size: int = 100,
    drive_id: Optional[str] = None,
    include_items_from_all_drives: bool = True,
    corpora: Optional[str] = None,
) -> str:
    """List the contents of a Google Drive folder.

    Provide one of the following to identify the folder:
    - folder_id (defaults to "root")
    - folder_name for a direct child under the selected parent
    - folder_path like "Reports/2024" relative to the parent/root
    """
    service, error_msg = _get_drive_service_or_error(user_email)
    if error_msg:
        return error_msg
    assert service is not None

    resolved_id, display_label, warnings = await _resolve_folder_reference(
        service,
        folder_id=folder_id,
        folder_name=folder_name,
        folder_path=folder_path,
        drive_id=drive_id,
        include_items_from_all_drives=include_items_from_all_drives,
        corpora=corpora,
    )

    if resolved_id is None:
        detail_lines = [display_label or "Unable to resolve folder selection."]
        if warnings:
            detail_lines.extend(warnings)
        return "\n".join(detail_lines)

    query = f"'{resolved_id}' in parents and trashed=false"
    params = _build_drive_list_params(
        query=query,
        page_size=min(page_size, 100),
        drive_id=drive_id,
        include_items_from_all_drives=include_items_from_all_drives,
        corpora=corpora,
    )

    files: List[Dict[str, Any]] = []
    page_token: Optional[str] = None

    while len(files) < page_size:
        if page_token:
            params["pageToken"] = page_token
        try:
            results = await asyncio.to_thread(service.files().list(**params).execute)
        except Exception as exc:
            return f"Error listing Drive items: {exc}"

        files.extend(results.get("files", []))
        page_token = results.get("nextPageToken")
        if not page_token or len(files) >= page_size:
            break

    files = files[:page_size]

    if not files:
        response_lines = [f"No items found in folder '{display_label}'."]
        if warnings:
            response_lines.extend(warnings)
        return "\n".join(response_lines)

    lines = [
        f"Found {len(files)} items in folder '{display_label}' for {user_email}:",
        "",
    ]
    for item in files:
        size_text = f", Size: {item.get('size', 'N/A')}" if "size" in item else ""
        lines.append(
            f'- Name: "{item.get("name", "(unknown)")}" '
            f"(ID: {item.get('id', 'unknown')}, Type: {item.get('mimeType', 'unknown')}"
            f"{size_text}, Modified: {item.get('modifiedTime', 'N/A')}) "
            f"Link: {item.get('webViewLink', '#')}"
        )
    if warnings:
        lines.extend(["", *warnings])
    return "\n".join(lines)


@mcp.tool("gdrive_get_file_content")
async def get_drive_file_content(
    file_id: str,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Download and extract content from a Google Drive file.

    For Google Docs/Sheets/Slides, exports as text/CSV. For PDFs, attempts
    text extraction via kreuzberg (with OCR fallback). For other files,
    decodes as UTF-8 when possible.

    Args:
        file_id: Google Drive file ID (alphanumeric)
        user_email: User's email for authentication
    """
    service, error_msg = _get_drive_service_or_error(user_email)
    if error_msg:
        return error_msg
    assert service is not None

    try:
        metadata = await asyncio.to_thread(
            service.files()
            .get(
                fileId=file_id,
                fields="id, name, mimeType, webViewLink",
                supportsAllDrives=True,
            )
            .execute
        )
    except Exception as exc:
        return f"Error retrieving metadata for file {file_id}: {exc}"

    mime_type = metadata.get("mimeType", "")
    export_mappings = {
        "application/vnd.google-apps.document": "text/plain",
        "application/vnd.google-apps.spreadsheet": "text/csv",
        "application/vnd.google-apps.presentation": "text/plain",
    }
    export_mime = export_mappings.get(mime_type)

    request = (
        service.files().export_media(fileId=file_id, mimeType=export_mime)
        if export_mime
        else service.files().get_media(fileId=file_id)
    )

    try:
        content_bytes = await _download_request_bytes(request)
    except ValueError as exc:
        return f"File too large: {exc}"
    except Exception as exc:
        return f"Error downloading file content: {exc}"

    body_text: str
    extraction_note: Optional[str] = None

    if mime_type == "application/pdf":
        text = await _extract_pdf_text(content_bytes)
        if text:
            body_text = text
            extraction_note = "[Used kreuzberg PDF extraction]"
        else:
            try:
                body_text = content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                body_text = (
                    f"[Binary or unsupported text encoding for mimeType '{mime_type}' - "
                    f"{len(content_bytes)} bytes]"
                )
            extraction_note = "[PDF extraction unavailable or failed]"
    else:
        try:
            body_text = content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            body_text = (
                f"[Binary or unsupported text encoding for mimeType '{mime_type}' - "
                f"{len(content_bytes)} bytes]"
            )

    header = (
        f'File: "{metadata.get("name", "Unknown File")}" '
        f"(ID: {file_id}, Type: {mime_type})\n"
        f"Link: {metadata.get('webViewLink', '#')}\n"
    )
    if extraction_note:
        header += f"{extraction_note}\n"
    header += "\n--- CONTENT ---\n"

    return header + body_text


@mcp.tool("gdrive_create_file")
async def create_drive_file(
    file_name: str,
    user_email: str = DEFAULT_USER_EMAIL,
    content: Optional[str] = None,
    folder_id: str = "root",
    mime_type: str = "text/plain",
    file_url: Optional[str] = None,
) -> str:
    """Create a new file in Google Drive from text content or a URL."""
    if not content and not file_url:
        return "You must provide either 'content' or 'file_url'."

    service, error_msg = _get_drive_service_or_error(user_email)
    if error_msg:
        return error_msg
    assert service is not None

    normalized_folder_id = _normalize_parent_id(folder_id)

    data: bytes
    if file_url:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0)
            ) as client:
                head_resp = await client.head(file_url, follow_redirects=True)
                if head_resp.status_code == 200:
                    content_length = head_resp.headers.get("Content-Length")
                    if content_length and int(content_length) > MAX_CONTENT_BYTES:
                        return (
                            f"File at URL is too large ({int(content_length)} bytes). "
                            f"Maximum allowed size is {MAX_CONTENT_BYTES} bytes "
                            f"(~{MAX_CONTENT_BYTES // (1024 * 1024)}MB)."
                        )

                resp = await client.get(file_url, follow_redirects=True)
                resp.raise_for_status()
                data = await resp.aread()

                if len(data) > MAX_CONTENT_BYTES:
                    return (
                        f"File content from URL is too large ({len(data)} bytes). "
                        f"Maximum allowed size is {MAX_CONTENT_BYTES} bytes "
                        f"(~{MAX_CONTENT_BYTES // (1024 * 1024)}MB)."
                    )

                content_type = resp.headers.get("Content-Type")
                if content_type and content_type != "application/octet-stream":
                    mime_type = content_type
        except httpx.TimeoutException:
            return f"Request timed out while fetching file from URL '{file_url}'."
        except httpx.HTTPStatusError as exc:
            return f"HTTP error fetching file from URL '{file_url}': {exc.response.status_code}"
        except Exception as exc:
            return f"Failed to fetch file from URL '{file_url}': {exc}"
    else:
        data = (content or "").encode("utf-8")

    metadata = {
        "name": file_name,
        "parents": [normalized_folder_id],
        "mimeType": mime_type,
    }
    media_stream = io.BytesIO(data)
    media = MediaIoBaseUpload(media_stream, mimetype=mime_type, resumable=False)

    try:
        created = await asyncio.to_thread(
            service.files()
            .create(
                body=metadata,
                media_body=media,
                fields="id, name, webViewLink",
                supportsAllDrives=True,
            )
            .execute
        )
    except Exception as exc:
        return f"Error creating Drive file: {exc}"

    link = created.get("webViewLink", "N/A")
    return (
        f"Successfully created file '{created.get('name', file_name)}' "
        f"(ID: {created.get('id', 'unknown')}) in folder "
        f"'{normalized_folder_id}' for {user_email}. Link: {link}"
    )


@mcp.tool("gdrive_delete_file")
async def delete_drive_file(
    file_id: str,
    user_email: str = DEFAULT_USER_EMAIL,
    permanent: bool = False,
) -> str:
    """Delete or trash a Google Drive file."""
    service, error_msg = _get_drive_service_or_error(user_email)
    if error_msg:
        return error_msg
    assert service is not None

    try:
        metadata = await asyncio.to_thread(
            service.files()
            .get(fileId=file_id, fields="id, name, parents", supportsAllDrives=True)
            .execute
        )
    except Exception as exc:
        return f"Error retrieving Drive file {file_id}: {exc}"

    file_name = (
        metadata.get("name", "(unknown)") if isinstance(metadata, dict) else "(unknown)"
    )

    try:
        if permanent:
            await asyncio.to_thread(
                service.files().delete(fileId=file_id, supportsAllDrives=True).execute
            )
            return f"File '{file_name}' (ID: {file_id}) permanently deleted."

        trashed = await asyncio.to_thread(
            service.files()
            .update(
                fileId=file_id,
                body={"trashed": True},
                fields="id, name, trashed",
                supportsAllDrives=True,
            )
            .execute
        )
    except Exception as exc:
        return f"Error deleting Drive file {file_id}: {exc}"

    trashed_name = (
        trashed.get("name", file_name) if isinstance(trashed, dict) else file_name
    )
    return f"File '{trashed_name}' (ID: {file_id}) moved to trash."


@mcp.tool("gdrive_move_file")
async def move_drive_file(
    file_id: str,
    destination_folder_id: str,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Move a Google Drive file to a different folder."""
    service, error_msg = _get_drive_service_or_error(user_email)
    if error_msg:
        return error_msg
    assert service is not None

    try:
        metadata = await asyncio.to_thread(
            service.files()
            .get(fileId=file_id, fields="id, name, parents", supportsAllDrives=True)
            .execute
        )
    except Exception as exc:
        return f"Error retrieving Drive file {file_id}: {exc}"

    current_parents = metadata.get("parents", []) if isinstance(metadata, dict) else []
    remove_parents = ",".join(current_parents)

    update_kwargs: Dict[str, Any] = {
        "fileId": file_id,
        "addParents": destination_folder_id,
        "fields": "id, name, parents",
        "supportsAllDrives": True,
    }
    if remove_parents:
        update_kwargs["removeParents"] = remove_parents

    try:
        updated = await asyncio.to_thread(
            service.files().update(**update_kwargs).execute
        )
    except Exception as exc:
        return f"Error moving Drive file {file_id}: {exc}"

    new_name = (
        updated.get("name", metadata.get("name", "(unknown)"))
        if isinstance(updated, dict)
        else metadata.get("name", "(unknown)")
    )
    return f"File '{new_name}' (ID: {file_id}) moved to folder '{destination_folder_id}'."


@mcp.tool("gdrive_copy_file")
async def copy_drive_file(
    file_id: str,
    user_email: str = DEFAULT_USER_EMAIL,
    new_name: Optional[str] = None,
    destination_folder_id: Optional[str] = None,
) -> str:
    """Copy a Google Drive file, optionally renaming or moving the copy."""
    service, error_msg = _get_drive_service_or_error(user_email)
    if error_msg:
        return error_msg
    assert service is not None

    body: Dict[str, object] = {}
    if new_name:
        body["name"] = new_name
    if destination_folder_id:
        normalized_dest = _normalize_parent_id(destination_folder_id)
        body["parents"] = [normalized_dest]

    try:
        copied = await asyncio.to_thread(
            service.files()
            .copy(
                fileId=file_id,
                body=body,
                fields="id, name, webViewLink",
                supportsAllDrives=True,
            )
            .execute
        )
    except Exception as exc:
        return f"Error copying Drive file {file_id}: {exc}"

    copy_name = copied.get("name", "(unknown)")
    copy_id = copied.get("id", "(unknown)")
    link = copied.get("webViewLink", "N/A")
    return f"Created copy '{copy_name}' (ID: {copy_id}) from file {file_id}. Link: {link}"


@mcp.tool("gdrive_rename_file")
async def rename_drive_file(
    file_id: str,
    new_name: str,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Rename a Google Drive file."""
    if not new_name.strip():
        return "A non-empty new_name is required to rename a file."

    service, error_msg = _get_drive_service_or_error(user_email)
    if error_msg:
        return error_msg
    assert service is not None

    try:
        updated = await asyncio.to_thread(
            service.files()
            .update(
                fileId=file_id,
                body={"name": new_name},
                fields="id, name",
                supportsAllDrives=True,
            )
            .execute
        )
    except Exception as exc:
        return f"Error renaming Drive file {file_id}: {exc}"

    final_name = (
        updated.get("name", new_name) if isinstance(updated, dict) else new_name
    )
    return f"File {file_id} renamed to '{final_name}'."


@mcp.tool("gdrive_create_folder")
async def create_drive_folder(
    folder_name: str,
    user_email: str = DEFAULT_USER_EMAIL,
    parent_folder_id: str = "root",
) -> str:
    """Create a new folder in Google Drive."""
    if not folder_name.strip():
        return "A non-empty folder_name is required to create a folder."

    service, error_msg = _get_drive_service_or_error(user_email)
    if error_msg:
        return error_msg
    assert service is not None

    normalized_parent = _normalize_parent_id(parent_folder_id)

    body = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [normalized_parent],
    }

    try:
        created = await asyncio.to_thread(
            service.files()
            .create(
                body=body,
                fields="id, name, parents, webViewLink",
                supportsAllDrives=True,
            )
            .execute
        )
    except Exception as exc:
        return f"Error creating Drive folder '{folder_name}': {exc}"

    folder_id = created.get("id", "(unknown)")
    link = created.get("webViewLink", "N/A")
    return (
        f"Created folder '{created.get('name', folder_name)}' "
        f"(ID: {folder_id}) under parent '{normalized_parent}'. Link: {link}"
    )


@mcp.tool("gdrive_file_permissions")
async def get_drive_file_permissions(
    file_id: str,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Get detailed permission and sharing information for a Drive file."""
    service, error_msg = _get_drive_service_or_error(user_email)
    if error_msg:
        return error_msg
    assert service is not None

    try:
        metadata = await asyncio.to_thread(
            service.files()
            .get(
                fileId=file_id,
                fields=(
                    "id, name, mimeType, size, modifiedTime, owners, permissions, "
                    "webViewLink, webContentLink, shared, sharingUser, "
                    "viewersCanCopyContent"
                ),
                supportsAllDrives=True,
            )
            .execute
        )
    except Exception as exc:
        return f"Error retrieving permissions for file {file_id}: {exc}"

    lines = [
        f"File: {metadata.get('name', 'Unknown')}",
        f"ID: {file_id}",
        f"Type: {metadata.get('mimeType', 'Unknown')}",
        f"Size: {metadata.get('size', 'N/A')} bytes",
        f"Modified: {metadata.get('modifiedTime', 'N/A')}",
        "",
        "Sharing Status:",
        f"  Shared: {metadata.get('shared', False)}",
    ]
    sharing_user = metadata.get("sharingUser")
    if sharing_user:
        lines.append(
            f"  Shared by: {sharing_user.get('displayName', 'Unknown')} "
            f"({sharing_user.get('emailAddress', 'Unknown')})"
        )

    permissions = metadata.get("permissions", [])
    if permissions:
        lines.append(f"  Number of permissions: {len(permissions)}")
        lines.append("  Permissions:")
        for perm in permissions:
            perm_type = perm.get("type", "unknown")
            role = perm.get("role", "unknown")
            if perm_type == "anyone":
                lines.append(f"    - Anyone with the link ({role})")
            elif perm_type in {"user", "group"}:
                lines.append(
                    f"    - {perm_type.title()}: "
                    f"{perm.get('emailAddress', 'unknown')} ({role})"
                )
            elif perm_type == "domain":
                lines.append(f"    - Domain: {perm.get('domain', 'unknown')} ({role})")
            else:
                lines.append(f"    - {perm_type} ({role})")
    else:
        lines.append("  No additional permissions (private file)")

    lines.extend(
        ["", "URLs:", f"  View Link: {metadata.get('webViewLink', 'N/A')}"]
    )
    if metadata.get("webContentLink"):
        lines.append(f"  Direct Download Link: {metadata['webContentLink']}")

    has_public = _has_anyone_link_access(permissions)
    if has_public:
        lines.extend(["", "This file is shared with 'Anyone with the link'."])
    else:
        lines.extend(
            [
                "",
                "This file is NOT shared with 'Anyone with the link'.",
                "   To fix: Right-click the file in Google Drive -> Share -> "
                "Anyone with the link -> Viewer",
            ]
        )

    return "\n".join(lines)


@mcp.tool("gdrive_check_public_access")
async def check_drive_file_public_access(
    file_name: str,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Check whether a file (by name) has public 'anyone with the link' access."""
    service, error_msg = _get_drive_service_or_error(user_email)
    if error_msg:
        return error_msg
    assert service is not None

    escaped_name = _escape_query_term(file_name)
    query = f"name = '{escaped_name}'"
    params = {
        "q": query,
        "pageSize": 10,
        "fields": "files(id, name, mimeType, webViewLink, shared)",
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": True,
    }

    try:
        results = await asyncio.to_thread(service.files().list(**params).execute)
    except Exception as exc:
        return f"Error searching for file '{file_name}': {exc}"

    files = results.get("files", [])
    if not files:
        return f"No file found with name '{file_name}'."

    lines: List[str] = []
    if len(files) > 1:
        lines.append(f"Found {len(files)} files with name '{file_name}':")
        for item in files:
            lines.append(f"  - {item.get('name', '(unknown)')} (ID: {item.get('id', 'unknown')})")
        lines.extend(["", "Checking the first file...", ""])

    first = files[0]
    file_id = first.get("id")
    try:
        metadata = await asyncio.to_thread(
            service.files()
            .get(
                fileId=file_id,
                fields="id, name, mimeType, permissions, webViewLink, webContentLink, shared",
                supportsAllDrives=True,
            )
            .execute
        )
    except Exception as exc:
        return f"Error retrieving permissions for file '{file_id}': {exc}"

    permissions = metadata.get("permissions", [])
    has_public = _has_anyone_link_access(permissions)

    lines.extend(
        [
            f"File: {metadata.get('name', 'Unknown')}",
            f"ID: {metadata.get('id', 'unknown')}",
            f"Type: {metadata.get('mimeType', 'unknown')}",
            f"Shared: {metadata.get('shared', False)}",
            "",
        ]
    )

    if has_public:
        lines.extend(
            [
                "PUBLIC ACCESS ENABLED - This file is publicly shared.",
                f"Direct link: https://drive.google.com/uc?export=view&id={file_id}",
            ]
        )
    else:
        lines.extend(
            [
                "NO PUBLIC ACCESS - File is not publicly shared.",
                "Fix: Drive -> Share -> 'Anyone with the link' -> 'Viewer'.",
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
    """Run the Google Drive MCP server with the specified transport."""
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

    parser = argparse.ArgumentParser(description="Google Drive MCP Server")
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
    "search_drive_files",
    "list_drive_items",
    "get_drive_file_content",
    "create_drive_file",
    "delete_drive_file",
    "move_drive_file",
    "copy_drive_file",
    "rename_drive_file",
    "create_drive_folder",
    "get_drive_file_permissions",
    "check_drive_file_public_access",
]
