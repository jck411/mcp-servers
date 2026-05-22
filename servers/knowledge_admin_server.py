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
from servers.knowledge.sources import delete_source_record, rename_source_record  # noqa: E402
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
# MCP Tools — Source Admin
# ---------------------------------------------------------------------------


@mcp.tool("knowledge_source_delete")
@logged_tool(log)
async def knowledge_source_delete(source_id: str, delete_file: bool = True) -> dict[str, Any]:
    """Delete one ingested source by source_id, including its vector chunks.

    Use knowledge_sources(domain) first to find the source_id. Set delete_file=false
    only when you want to remove it from search but keep the stored file.
    """
    settings, _, _, vectors, db = _require_ready()
    return await delete_source_record(settings, vectors, db, source_id, delete_file)


@mcp.tool("knowledge_source_rename")
@logged_tool(log)
async def knowledge_source_rename(source_id: str, filename: str) -> dict[str, Any]:
    """Rename one ingested source by source_id.

    Updates SQLite metadata and Qdrant source_name. For standard file uploads,
    also renames the stored raw file when it exists.
    """
    settings, _, _, vectors, db = _require_ready()
    return await rename_source_record(settings, vectors, db, source_id, filename)


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
    """Group pending curation rows into chat-friendly question packs.

    Use this when Jack wants to clean up curation from normal chat. Packs turn
    many raw rows into a few questions that can be answered and resolved in bulk.
    """
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
    """Preview resolving a curation question pack from Jack's chat answer.

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

    _settings.knowledge_path.mkdir(parents=True, exist_ok=True)
    log.info("knowledge_path=%s", _settings.knowledge_path)

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
