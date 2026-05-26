"""Knowledge Admin MCP server — operator/maintenance tools.

This server exposes administrative tools for the Knowledge system that are
NOT needed during normal conversation. These tools are used by maintenance
agents, nightly scripts, and operator workflows.

The conversational Knowledge tools live in knowledge_server.py (port 9017).
This admin server runs on port 9019 and should only be wired into
maintenance-specific presets in LibreChat.

Run:
    python -m servers.knowledge_admin --transport streamable-http --host 0.0.0.0 --port 9019
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from shared.logging_config import get_logger, logged_tool

log = get_logger("knowledge-admin")

# --- servers/knowledge/ package ---
from servers.knowledge.settings import KnowledgeSettings  # noqa: E402
from servers.knowledge.embeddings import BM25SparseEncoder, EmbeddingClient  # noqa: E402
from servers.knowledge.db import KnowledgeDB  # noqa: E402
from servers.knowledge.vectors import KnowledgeVectorStore  # noqa: E402

from servers.knowledge.wiki import WIKI_PAGE_STATUSES  # noqa: E402
from servers.knowledge.curation import (  # noqa: E402
    apply_curation_item,
    apply_curation_pack_resolution,
    build_curation_question_packs,
    create_curation_queue_item,
    get_curation_question_pack,
    preview_curation_pack_resolution,
)

DEFAULT_ADMIN_PORT = 9019


def _auth_provider() -> StaticTokenVerifier | None:
    token = os.environ.get("MCP_KNOWLEDGE_BEARER_TOKEN")
    if not token:
        return None
    return StaticTokenVerifier({token: {"client_id": "knowledge-admin", "scopes": []}})


mcp = FastMCP("knowledge-admin", auth=_auth_provider())

# ---------------------------------------------------------------------------
# Global State (shared init pattern with knowledge_server.py)
# ---------------------------------------------------------------------------

_settings: KnowledgeSettings | None = None
_embeddings: EmbeddingClient | None = None
_sparse_encoder: BM25SparseEncoder | None = None
_vectors: KnowledgeVectorStore | None = None
_db: KnowledgeDB | None = None
_ready = False


def _require_ready() -> (
    tuple[KnowledgeSettings, EmbeddingClient, BM25SparseEncoder, KnowledgeVectorStore, KnowledgeDB]
):
    if (
        not _ready
        or not _settings
        or not _embeddings
        or not _sparse_encoder
        or not _vectors
        or not _db
    ):
        raise RuntimeError("Knowledge admin subsystem not initialized")
    return _settings, _embeddings, _sparse_encoder, _vectors, _db


# ---------------------------------------------------------------------------
# MCP Tools — Domain Admin
# ---------------------------------------------------------------------------


@mcp.tool("knowledge_domain_archive")
@logged_tool(log)
async def knowledge_domain_archive(name: str) -> dict[str, Any]:
    """Archive a domain. Archived domains are excluded from searches by default.

    Does NOT delete data — the domain can still be searched explicitly.

    Args:
        name: Domain to archive.
    """
    _, _, _, _, db = _require_ready()

    archived = await db.domain_archive(name)
    if not archived:
        return {"success": False, "error": f"Domain '{name}' not found or already archived"}

    return {
        "success": True,
        "domain": name,
        "message": f"Domain '{name}' archived. Data preserved, excluded from default searches.",
    }


@mcp.tool("knowledge_domain_relate")
@logged_tool(log)
async def knowledge_domain_relate(
    name: str, related_domains: list[str]
) -> dict[str, Any]:
    """Update which domains are related to this one.

    Related domains are automatically included when searching this domain.

    Args:
        name: Domain to update.
        related_domains: Full list of related domain names (replaces existing).
    """
    _, _, _, _, db = _require_ready()

    if not await db.domain_exists(name):
        return {"success": False, "error": f"Domain '{name}' not found"}

    await db.domain_update_related(name, related_domains)
    return {"success": True, "domain": name, "related_domains": related_domains}




# ---------------------------------------------------------------------------
# MCP Tools — Wiki Admin
# ---------------------------------------------------------------------------


@mcp.tool("knowledge_wiki_set_status")
@logged_tool(log)
async def knowledge_wiki_set_status(
    slug: str,
    status: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """Promote a candidate page to active, or archive/reactivate a page."""
    _, _, _, _, db = _require_ready()
    clean_slug = slug.strip()
    clean_status = str(status or "").strip()
    if not clean_slug:
        return {"success": False, "error": "slug is required"}
    if clean_status not in WIKI_PAGE_STATUSES:
        return {"success": False, "error": "status must be candidate, active, or archived"}

    if not await db.wiki_set_status(clean_slug, clean_status):
        return {"success": False, "error": f"Wiki page '{clean_slug}' not found"}

    result: dict[str, Any] = {
        "success": True,
        "slug": clean_slug,
        "status": clean_status,
        "page": await db.wiki_get(clean_slug),
    }
    if notes:
        result["notes"] = notes
    return result


# ---------------------------------------------------------------------------
# MCP Tools — Curation Workflow
# ---------------------------------------------------------------------------


@mcp.tool("knowledge_curation_create")
@logged_tool(log)
async def knowledge_curation_create(
    actions: list[dict[str, Any]],
    notes: str,
    kind: str = "uncertain_fact",
    title: str | None = None,
    source_refs: list[dict[str, Any]] | None = None,
    risk: str = "medium",
    confidence: float = 0.0,
    item_id: str | None = None,
) -> dict[str, Any]:
    """Queue a proposed Knowledge change for human review. LAST RESORT ONLY.

    In normal chat, prefer storing facts directly or asking Jack. This tool
    exists for automated/batch processes that cannot ask Jack in the moment.
    If the curation queue has items, it means something upstream needs fixing.

    Do NOT use this for:
    - Facts Jack clearly stated (store them directly with as_of dating)
    - Things you're unsure about (ask Jack now — you're in a conversation)
    - High-confidence observations (confidence >= 0.8 means store it)

    Use this ONLY for:
    - Automated batch processing that finds contradictions with existing data
    - Maintenance scripts that detect structural issues needing human review

    Args:
        actions: Proposed actions. Each action must include "action" or "type".
        notes: Reviewer-facing context explaining why this is queued.
        kind: Queue item kind, e.g. "uncertain_fact" or "contradiction".
        title: Optional short title. Defaults to a compact version of notes.
        source_refs: Optional source/chat references.
        risk: low, medium, or high.
        confidence: Agent confidence from 0.0 to 1.0.
        item_id: Optional deterministic id for upsert/replace.
    """
    _, _, _, _, db = _require_ready()
    return await create_curation_queue_item(
        db=db,
        actions=actions,
        notes=notes,
        kind=kind,
        title=title,
        source_refs=source_refs,
        risk=risk,
        confidence=confidence,
        item_id=item_id,
    )


@mcp.tool("knowledge_curation_list")
@logged_tool(log)
async def knowledge_curation_list(
    status: str | None = "pending",
    kind: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List Knowledge curation queue items for operator review."""
    _, _, _, _, db = _require_ready()
    items = await db.curation_list(status=status, kind=kind, limit=limit)
    total_count = await db.curation_count(status=status, kind=kind)
    return {"success": True, "count": len(items), "total_count": total_count, "items": items}


@mcp.tool("knowledge_curation_get")
@logged_tool(log)
async def knowledge_curation_get(item_id: str) -> dict[str, Any]:
    """Get one curation queue item by id."""
    _, _, _, _, db = _require_ready()
    item = await db.curation_get(item_id)
    if not item:
        return {"success": False, "error": f"Curation item '{item_id}' not found"}
    return {"success": True, "item": item}


@mcp.tool("knowledge_curation_question_packs")
@logged_tool(log)
async def knowledge_curation_question_packs(
    limit: int = 10,
    kind: str | None = None,
    domain: str | None = None,
) -> dict[str, Any]:
    """Group pending curation rows into operator-friendly question packs."""
    _, _, _, _, db = _require_ready()
    packs = await build_curation_question_packs(db, limit=limit, kind=kind, domain=domain)
    return {"success": True, "count": len(packs), "packs": packs}


@mcp.tool("knowledge_curation_question_pack_get")
@logged_tool(log)
async def knowledge_curation_question_pack_get(pack_id: str) -> dict[str, Any]:
    """Get one curation question pack by id."""
    _, _, _, _, db = _require_ready()
    pack = await get_curation_question_pack(db, pack_id)
    if not pack:
        return {"success": False, "error": f"Curation question pack '{pack_id}' not found"}
    return {"success": True, "pack": pack}


@mcp.tool("knowledge_curation_pack_preview")
@logged_tool(log)
async def knowledge_curation_pack_preview(
    pack_id: str,
    answer: str,
    resolution_status: str = "applied",
) -> dict[str, Any]:
    """Preview resolving a curation question pack from Jack's answer.

    This does not write data. It returns affected rows, the proposed status
    change, and a durable resolution note that would be recorded on apply.
    """
    _, _, _, _, db = _require_ready()
    return await preview_curation_pack_resolution(
        db,
        pack_id=pack_id,
        answer=answer,
        resolution_status=resolution_status,
    )


@mcp.tool("knowledge_curation_pack_apply")
@logged_tool(log)
async def knowledge_curation_pack_apply(
    pack_id: str,
    answer: str,
    resolution_status: str = "applied",
    confirmed: bool = False,
) -> dict[str, Any]:
    """Resolve a non-destructive curation question pack after preview/confirmation.

    Destructive packs are blocked in this first version; verify and apply their
    individual queue items instead.
    """
    _, _, _, _, db = _require_ready()
    return await apply_curation_pack_resolution(
        db,
        pack_id=pack_id,
        answer=answer,
        resolution_status=resolution_status,
        confirmed=confirmed,
    )


@mcp.tool("knowledge_curation_snooze")
@logged_tool(log)
async def knowledge_curation_snooze(item_id: str) -> dict[str, Any]:
    """Snooze a curation queue item without applying or rejecting it."""
    _, _, _, _, db = _require_ready()
    updated = await db.curation_mark_status(item_id, "snoozed")
    if not updated:
        return {"success": False, "error": f"Curation item '{item_id}' not found"}
    return {"success": True, "item_id": item_id, "status": "snoozed"}


@mcp.tool("knowledge_curation_resolve")
@logged_tool(log)
async def knowledge_curation_resolve(
    item_id: str,
    action: str = "apply",
    confirmation: str | None = None,
) -> dict[str, Any]:
    """Resolve one curation item by applying or rejecting it."""
    if action not in ("apply", "reject"):
        return {"success": False, "error": "action must be 'apply' or 'reject'"}

    if action == "reject":
        _, _, _, _, db = _require_ready()
        updated = await db.curation_mark_status(item_id, "rejected")
        if not updated:
            return {"success": False, "error": f"Curation item '{item_id}' not found"}
        return {"success": True, "item_id": item_id, "status": "rejected"}

    settings, embeddings, sparse_encoder, vectors, db = _require_ready()
    return await apply_curation_item(
        item_id,
        confirmation=confirmation,
        settings=settings,
        embeddings=embeddings,
        sparse_encoder=sparse_encoder,
        vectors=vectors,
        db=db,
    )


# ---------------------------------------------------------------------------
# MCP Tools — Source File Management
# ---------------------------------------------------------------------------

from servers.knowledge.sources import (  # noqa: E402
    SourceManifest,
    convert_pdf_to_images,
    extract_text_from_sources,
    get_source_summary,
    scan_sources,
)


def _sources_root() -> Path:
    """Resolve the source files directory on LXC 110."""
    return Path(os.environ.get("KNOWLEDGE_SOURCES_ROOT", "/opt/mcp-servers/data/sources"))


def _manifest_path() -> Path:
    return _sources_root() / ".extracted" / "manifest.json"


@mcp.tool("knowledge_source_scan")
@logged_tool(log)
async def knowledge_source_scan() -> dict[str, Any]:
    """Scan the sources directory and catalog all files.

    Detects file types, classifies PDFs as text or image-based, and builds
    a processing manifest. Does NOT extract text — use knowledge_source_extract
    for that.
    """
    root = _sources_root()
    manifest = scan_sources(root)
    manifest.save(_manifest_path())
    return {
        "success": True,
        "summary": get_source_summary(manifest),
        "files": [
            {"path": f.path, "category": f.category.value, "status": f.status.value,
             "size_bytes": f.size_bytes}
            for f in manifest.files.values()
        ],
    }


@mcp.tool("knowledge_source_list")
@logged_tool(log)
async def knowledge_source_list(
    status: str | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    """List source files from the last scan.

    Args:
        status: Filter by processing status (unprocessed, text_extracted,
                needs_vision, processed, error).
        category: Filter by file category (text, text_pdf, image_pdf, image,
                  structured).
    """
    manifest = SourceManifest.load(_manifest_path())
    if not manifest.files:
        return {"success": True, "count": 0, "files": [],
                "hint": "Run knowledge_source_scan first"}

    files = list(manifest.files.values())
    if status:
        files = [f for f in files if f.status.value == status]
    if category:
        files = [f for f in files if f.category.value == category]

    return {
        "success": True,
        "count": len(files),
        "summary": get_source_summary(manifest),
        "files": [
            {"path": f.path, "category": f.category.value, "status": f.status.value,
             "size_bytes": f.size_bytes, "page_count": f.page_count,
             "text_length": len(f.text_content) if f.text_content else 0}
            for f in files
        ],
    }


@mcp.tool("knowledge_source_extract")
@logged_tool(log)
async def knowledge_source_extract(
    paths: list[str] | None = None,
) -> dict[str, Any]:
    """Extract text from source files.

    For text files and text-PDFs: extracts text content.
    For image-PDFs and images: marks them as needs_vision.

    Args:
        paths: Specific file paths to extract. If omitted, extracts all
               unprocessed files.
    """
    root = _sources_root()
    manifest = SourceManifest.load(_manifest_path())
    if not manifest.files:
        return {"success": False, "error": "No source files scanned. Run knowledge_source_scan first."}

    manifest = extract_text_from_sources(root, manifest, paths=paths)
    manifest.save(_manifest_path())

    summary = get_source_summary(manifest)
    return {
        "success": True,
        "summary": summary,
        "files": [
            {"path": f.path, "status": f.status.value, "category": f.category.value,
             "text_length": len(f.text_content) if f.text_content else 0,
             "error": f.error}
            for f in manifest.files.values()
            if paths is None or f.path in paths
        ],
    }


@mcp.tool("knowledge_source_convert_pdf")
@logged_tool(log)
async def knowledge_source_convert_pdf(
    path: str,
    dpi: int = 200,
) -> dict[str, Any]:
    """Convert an image-based PDF to JPEG images for vision processing.

    The images are saved in .extracted/images/ on LXC 110. Use these images
    with a vision-capable model to extract structured facts.

    Args:
        path: Relative path of the PDF file.
        dpi: Resolution for the conversion. Default 200.
    """
    root = _sources_root()
    images = convert_pdf_to_images(root, path, dpi=dpi)

    if not images:
        return {"success": False, "error": f"Failed to convert {path} to images"}

    return {
        "success": True,
        "path": path,
        "page_count": len(images),
        "images": [str(img) for img in images],
        "hint": "Use these images with a vision-capable model to extract facts",
    }


@mcp.tool("knowledge_source_read")
@logged_tool(log)
async def knowledge_source_read(
    path: str,
    max_chars: int = 50000,
) -> dict[str, Any]:
    """Read the extracted text content of a source file.

    Args:
        path: Relative path of the source file.
        max_chars: Maximum characters to return. Default 50000.
    """
    manifest = SourceManifest.load(_manifest_path())

    if path not in manifest.files:
        return {"success": False, "error": f"File '{path}' not in manifest. Run knowledge_source_scan first."}

    source = manifest.files[path]

    if source.text_content:
        return {
            "success": True,
            "path": path,
            "category": source.category.value,
            "status": source.status.value,
            "content": source.text_content[:max_chars],
            "total_chars": len(source.text_content),
            "truncated": len(source.text_content) > max_chars,
        }

    if source.status.value == "needs_vision":
        return {
            "success": True,
            "path": path,
            "category": source.category.value,
            "status": "needs_vision",
            "content": None,
            "hint": "This file needs vision model processing. Use knowledge_source_convert_pdf to get images.",
        }

    return {
        "success": True,
        "path": path,
        "category": source.category.value,
        "status": source.status.value,
        "content": None,
        "hint": "Run knowledge_source_extract to extract text first.",
    }


# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------


async def _startup() -> None:
    global _settings, _embeddings, _sparse_encoder, _vectors, _db, _ready

    try:
        _settings = KnowledgeSettings()  # type: ignore[call-arg]
    except Exception as exc:
        log.error("disabled config_error=%r", exc)
        return

    _embeddings = EmbeddingClient(_settings)
    _sparse_encoder = BM25SparseEncoder()
    _vectors = KnowledgeVectorStore(_settings)
    _db = KnowledgeDB(_settings.db_path)

    try:
        await _vectors.ensure_collection()
    except Exception as exc:
        log.error("disabled qdrant_unreachable=%r", exc)
        return

    await _db.initialize()

    _ready = True
    log.info("admin initialization complete")


async def _shutdown() -> None:
    if _embeddings:
        await _embeddings.close()
    if _vectors:
        await _vectors.close()
    if _db:
        await _db.close()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def run(
    transport: str = "stdio",
    host: str = "0.0.0.0",
    port: int = DEFAULT_ADMIN_PORT,
) -> None:
    """Run the Knowledge Admin MCP server."""
    mcp.auth = _auth_provider()
    if transport == "streamable-http" and mcp.auth is None:
        raise RuntimeError("MCP_KNOWLEDGE_BEARER_TOKEN is required for streamable-http")

    asyncio.get_event_loop().run_until_complete(_startup())

    try:
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
    finally:
        asyncio.get_event_loop().run_until_complete(_shutdown())


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Knowledge Admin MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_ADMIN_PORT)
    args = parser.parse_args()
    run(args.transport, args.host, args.port)


if __name__ == "__main__":
    main()


__all__ = ["mcp", "run", "main", "DEFAULT_ADMIN_PORT"]
