"""Raw source-file helpers for the Knowledge service.

The Knowledge service has two different representations of a document:
the original uploaded bytes on disk, and searchable extracted chunks in
Qdrant. This module keeps the raw-file path, media type, and fallback export
logic in one place so REST and MCP downloads behave the same way.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Protocol


class ChunkReader(Protocol):
    """Minimal vector-store interface needed to export chunks for one source."""

    async def chunks_by_source(self, source_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        """Return chunk payloads for a source ordered by chunk index."""


def sanitize_source_filename(filename: str | None) -> str:
    """Return a safe display/storage filename with path components removed."""
    clean = Path(filename or "upload").name
    return clean or "upload"


def source_media_type(filename: str | None) -> str:
    """Infer a source file media type from its filename."""
    media_type, _ = mimetypes.guess_type(filename or "")
    return media_type or "application/octet-stream"


def source_relative_path(knowledge_path: Path, path: Path) -> str:
    """Return a portable path relative to the Knowledge storage root."""
    try:
        return str(path.relative_to(knowledge_path))
    except ValueError:
        return str(path)


def resolve_source_path(knowledge_path: Path, source: dict[str, Any]) -> Path | None:
    """Find the raw bytes for a source using metadata, then legacy layout.

    New rows record ``stored_path``. Older rows only have ``domain`` and
    ``filename``, so the legacy fallback checks
    ``<knowledge_path>/<domain>/<filename>``.
    """
    stored_path = source.get("stored_path")
    if stored_path:
        path = Path(str(stored_path))
        if not path.is_absolute():
            path = knowledge_path / path
        if path.exists() and path.is_file():
            return path

    filename = source.get("filename")
    domain = source.get("domain")
    if not filename or not domain:
        return None

    legacy_path = knowledge_path / str(domain) / sanitize_source_filename(str(filename))
    if legacy_path.exists() and legacy_path.is_file():
        return legacy_path
    return None


def source_export_filename(filename: str | None, source_id: str) -> str:
    """Build the Markdown filename used when only extracted chunks remain."""
    stem = Path(str(filename or source_id)).stem or source_id
    return f"{stem}.md"


async def source_chunk_export_bytes(
    vectors: ChunkReader,
    source: dict[str, Any],
) -> tuple[str, bytes] | None:
    """Export stored chunks as Markdown when the original bytes are missing."""
    chunks = await vectors.chunks_by_source(str(source["id"]))
    contents = [str(chunk.get("content") or "").strip() for chunk in chunks]
    contents = [content for content in contents if content]
    if not contents:
        return None

    filename = source_export_filename(source.get("filename"), str(source["id"]))
    title = str(source.get("filename") or source["id"])
    body = "\n\n---\n\n".join(contents)
    text = (
        f"# {title}\n\n"
        "Exported from stored Knowledge text because original source bytes "
        "are not available on disk.\n\n"
        f"Domain: {source.get('domain')}\n"
        f"Source ID: {source.get('id')}\n\n"
        "---\n\n"
        f"{body}\n"
    )
    return filename, text.encode()
