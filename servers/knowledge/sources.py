"""Source file utility functions for the Knowledge service.

Extracted from knowledge_server.py during Phase 3 modularization.
Contains source download, delete, rename operations.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from servers.knowledge.db import KnowledgeDB
from servers.knowledge.settings import KnowledgeSettings
from servers.knowledge.vectors import KnowledgeVectorStore
from servers.knowledge_source_files import (
    resolve_source_path,
    sanitize_source_filename,
    source_chunk_export_bytes,
    source_media_type,
    source_relative_path,
)


def compute_file_hash(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


async def source_download_bytes(
    settings: KnowledgeSettings,
    db: KnowledgeDB,
    source_id: str,
    vectors: KnowledgeVectorStore | None = None,
) -> dict[str, Any]:
    """Return original source bytes for a stored source."""
    source = await db.source_get(source_id)
    if not source:
        return {"success": False, "error": f"Source '{source_id}' not found"}

    filename = sanitize_source_filename(str(source.get("filename") or f"{source_id}.bin"))
    source_path = resolve_source_path(settings.knowledge_path, source)
    if source_path:
        data = source_path.read_bytes()
        media_type = source.get("media_type") or source_media_type(filename)
        generated = False
    elif vectors:
        export = await source_chunk_export_bytes(vectors, source)
        if not export:
            return {
                "success": False,
                "error": f"Stored source file for '{source_id}' was not found",
            }
        filename, data = export
        media_type = "text/markdown"
        generated = True
    else:
        return {
            "success": False,
            "error": f"Stored source file for '{source_id}' was not found",
        }

    return {
        "success": True,
        "source_id": source_id,
        "filename": filename,
        "domain": source.get("domain"),
        "media_type": media_type,
        "size_bytes": len(data),
        "generated": generated,
        "data": data,
    }


async def delete_source_record(
    settings: KnowledgeSettings,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
    source_id: str,
    delete_file: bool = True,
) -> dict[str, Any]:
    """Delete one source row, its vector chunks, and optionally its stored file."""
    source = await db.source_get(source_id)
    if not source:
        return {"success": False, "error": f"Source '{source_id}' not found"}

    await vectors.delete_by_source(source_id)
    deleted_files: list[str] = []
    preserved_files: list[str] = []
    if delete_file:
        candidate = resolve_source_path(settings.knowledge_path, source)
        if candidate:
            rel_path = source_relative_path(settings.knowledge_path, candidate)
            references = await db.sources_referencing_file(
                stored_paths=[rel_path, str(candidate)],
                domain=source.get("domain"),
                filename=source.get("filename"),
                exclude_source_id=source_id,
            )
            if references:
                preserved_files.append(rel_path)
            else:
                candidate.unlink()
                deleted_files.append(rel_path)

    deleted = await db.source_remove(source_id)
    return {
        "success": deleted,
        "deleted": deleted,
        "source": source,
        "deleted_files": deleted_files,
        "preserved_files": preserved_files,
    }


async def delete_sources_for_overwrite(
    settings: KnowledgeSettings,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
    domain: str,
    filename: str,
) -> list[dict[str, Any]]:
    """Remove existing source rows/chunks for a domain filename before replacement."""
    deleted: list[dict[str, Any]] = []
    for source in await db.sources_list_by_domain_filename(domain, filename):
        result = await delete_source_record(
            settings,
            vectors,
            db,
            str(source["id"]),
            delete_file=True,
        )
        if result.get("success"):
            deleted.append(result)
    return deleted


async def rename_source_record(
    settings: KnowledgeSettings,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
    source_id: str,
    filename: str,
) -> dict[str, Any]:
    """Rename a source for display/search and rename raw bytes when present."""
    source = await db.source_get(source_id)
    if not source:
        return {"success": False, "error": f"Source '{source_id}' not found"}

    clean_filename = sanitize_source_filename(filename)
    if not clean_filename:
        return {"success": False, "error": "filename is required"}

    old_path = resolve_source_path(settings.knowledge_path, source)
    renamed_file = False
    preserved_files: list[str] = []
    stored_path = source.get("stored_path")
    if old_path and old_path.exists() and old_path.is_file():
        new_path = old_path.with_name(clean_filename)
        rel_path = source_relative_path(settings.knowledge_path, old_path)
        if new_path != old_path:
            references = await db.sources_referencing_file(
                stored_paths=[rel_path, str(old_path)],
                domain=source.get("domain"),
                filename=source.get("filename"),
                exclude_source_id=source_id,
            )
            if references:
                preserved_files.append(rel_path)
                stored_path = rel_path
            else:
                if new_path.exists():
                    return {"success": False, "error": f"File already exists: {new_path.name}"}
                old_path.rename(new_path)
                renamed_file = True
                stored_path = source_relative_path(settings.knowledge_path, new_path)
        else:
            stored_path = rel_path

    await db.source_rename(source_id, clean_filename, stored_path)
    await vectors.update_source_name(source_id, clean_filename)
    updated = await db.source_get(source_id)
    return {
        "success": True,
        "source_id": source_id,
        "old_filename": source.get("filename"),
        "new_filename": clean_filename,
        "renamed_file": renamed_file,
        "preserved_files": preserved_files,
        "source": updated,
    }


