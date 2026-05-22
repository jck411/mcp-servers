"""Standalone MCP server for personal knowledge management.

Central knowledge base for life domains (health, finances, schedule, etc.)
with semantic search, structured facts, cross-domain queries, and file ingestion.

Domains are created on the fly. Each domain can declare related domains so
cross-domain queries automatically fan out. A special "core" domain holds
foundational personal profile facts that are implicitly included in queries.

Storage:
  - Qdrant (vector search): one collection, filtered by domain
  - SQLite (structured data): domains, facts, sources, ingest tracking

Directory structure for file ingestion:
    /opt/mcp-servers/knowledge/
    ├── health/          → lab reports, doctor summaries
    ├── finances/        → statements, budgets
    ├── schedule/        → routines, commitments
    └── gardening/       → research, plans

Run:
    python -m servers.knowledge --transport streamable-http --host 0.0.0.0 --port 9017
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from servers.knowledge_source_files import sanitize_source_filename
from shared.logging_config import get_logger, logged_tool

log = get_logger("knowledge")

# --- Extracted to servers/knowledge/ package ---
from servers.knowledge.settings import DEFAULT_HTTP_PORT, KnowledgeSettings  # noqa: E402
from servers.knowledge.embeddings import BM25SparseEncoder, EmbeddingClient  # noqa: E402


def _auth_provider() -> StaticTokenVerifier | None:
    token = os.environ.get("MCP_KNOWLEDGE_BEARER_TOKEN")
    if not token:
        return None
    return StaticTokenVerifier({token: {"client_id": "knowledge", "scopes": []}})


mcp = FastMCP("knowledge", auth=_auth_provider())



# --- Extracted to servers/knowledge/db.py (Phase 2) ---
from servers.knowledge.db import (  # noqa: E402
    SEARCH_STOPWORDS,
    KnowledgeDB,
    search_fact_keywords,
)




# --- Extracted to servers/knowledge/vectors.py (Phase 2) ---
from servers.knowledge.vectors import KnowledgeVectorStore  # noqa: E402



# --- Extracted to servers/knowledge/sources.py (Phase 3) ---
from servers.knowledge.sources import (  # noqa: E402
    compute_file_hash,
    delete_source_record,
    delete_sources_for_overwrite,
    rename_source_record,
    source_download_bytes,
)

# --- Extracted to servers/knowledge/extraction.py (Phase 3) ---
from servers.knowledge.extraction import (  # noqa: E402
    _is_likely_binary,
    chunk_text,
    compute_text_hash,
)


# --- Extracted to servers/knowledge/ingestion.py (Phase 3) ---
from servers.knowledge.ingestion import (  # noqa: E402
    _ingest_file_at_path,
    _validate_text_ingest_inputs,
    extract_source_facts_single_shot,
)

# ---------------------------------------------------------------------------
# Global State
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
        raise RuntimeError("Knowledge subsystem not initialized")
    return _settings, _embeddings, _sparse_encoder, _vectors, _db



# --- Extracted to servers/knowledge/wiki.py (Phase 3) ---
from servers.knowledge.wiki import (  # noqa: E402
    WIKI_PAGE_KINDS,
    WIKI_PAGE_LIST_STATUSES,
    WIKI_PAGE_STATUSES,
    _call_wiki_llm,
    preview_wiki_rebuild,
    rebuild_wiki,
)


# --- Extracted to servers/knowledge/curation.py (Phase 3) ---
from servers.knowledge.curation import (  # noqa: E402
    SUPPORTED_CURATION_ACTIONS,
    apply_curation_item,
    apply_curation_pack_resolution,
    build_curation_question_packs,
    create_curation_queue_item,
    curation_item_has_destructive_actions,
)


# --- Extracted to servers/knowledge/search.py (Phase 3) ---
from servers.knowledge.search import (  # noqa: E402
    EVIDENCE_QUERY_HINTS,
    FACT_QUERY_HINTS,
    SEARCH_TEMPORAL_INTENTS,
    TEMPORAL_TOPIC_HINTS,
    classify_search_route,
    classify_search_temporal_intent,
    filter_facts_for_required_terms,
    filter_facts_for_temporal_intent,
    resolve_search_domains,
    search_knowledge,
)

# ---------------------------------------------------------------------------
# MCP Tools — Domain Management
# ---------------------------------------------------------------------------


@mcp.tool("knowledge_domain_create")
@logged_tool(log)
async def knowledge_domain_create(
    name: str,
    description: str = "",
    related_domains: list[str] | None = None,
) -> dict[str, Any]:
    """Create a new knowledge domain.

    A domain is a topic area (health, finances, gardening, etc.).
    Related domains are automatically included when searching this domain.
    The 'core' domain is always included in searches implicitly.

    Args:
        name: Domain name (lowercase, no spaces — use underscores).
        description: What this domain covers.
        related_domains: Other domains to include when searching this one.
    """
    settings, _, _, _, db = _require_ready()

    # Sanitize name
    clean_name = re.sub(r"[^a-z0-9_]", "_", name.lower().strip())
    if not clean_name:
        return {"success": False, "error": "Invalid domain name"}

    created = await db.domain_create(clean_name, description, related_domains or [])
    if not created:
        return {"success": False, "error": f"Domain '{clean_name}' already exists"}

    # Create knowledge subdirectory
    domain_dir = settings.knowledge_path / clean_name
    domain_dir.mkdir(parents=True, exist_ok=True)

    return {
        "success": True,
        "domain": clean_name,
        "description": description,
        "related_domains": related_domains or [],
        "knowledge_path": str(domain_dir),
        "message": f"Domain '{clean_name}' created. Place files in {domain_dir} for ingestion.",
    }


@mcp.tool("knowledge_domain_list")
@logged_tool(log)
async def knowledge_domain_list() -> dict[str, Any]:
    """List all knowledge domains with their descriptions and related domains."""
    _, _, _, vectors, db = _require_ready()

    domains = await db.domain_list()
    for d in domains:
        d["chunk_count"] = await vectors.count_by_domain(d["name"])
        sources = await db.sources_list(d["name"])
        d["source_count"] = len(sources)
        facts = await db.facts_list(d["name"])
        d["fact_count"] = len(facts)

    return {"success": True, "count": len(domains), "domains": domains}


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
# MCP Tools — Facts (Structured Key-Value Knowledge)
# ---------------------------------------------------------------------------


@mcp.tool("knowledge_fact_set")
@logged_tool(log)
async def knowledge_fact_set(
    domain: str,
    key: str,
    value: str,
    source: str | None = None,
    confidence: float = 1.0,
    valid_from: str | None = None,
    valid_until: str | None = None,
    as_of: str | None = None,
    review_after: str | None = None,
    origin_type: str = "chat",
    origin_ref: str | None = None,
) -> dict[str, Any]:
    """Store a structured fact in a domain. Upserts — same key overwrites.

    Facts are for precise, retrievable information that semantic search
    would be unreliable for. Examples: "usda_zone" = "7b",
    "fasting_glucose_2026_03" = "95 mg/dL", "monthly_budget" = "5000".

    Args:
        domain: Domain this fact belongs to.
        key: Fact identifier (e.g. "usda_zone", "blood_type").
        value: The fact value.
        source: Where this fact came from (e.g. "lab report 2026-03-15").
        confidence: How confident (0.0 to 1.0). Default 1.0.
        valid_from: ISO date when this fact became true.
        valid_until: ISO date when this fact expires.
        as_of: ISO date/time when the source observed this claim.
        review_after: ISO date/time when this fact should be rechecked.
        origin_type: Provenance category. Defaults to "chat" for MCP writes.
        origin_ref: Provenance reference. Defaults to today's date for chat writes.
    """
    settings, embeddings, sparse_encoder, vectors, db = _require_ready()

    if not await db.domain_exists(domain):
        return {"success": False, "error": f"Domain '{domain}' not found. Create it first."}

    if origin_type == "chat" and not origin_ref:
        origin_ref = datetime.now(UTC).date().isoformat()

    fact_id = await db.fact_set(
        domain, key, value, source, confidence, valid_from, valid_until,
        as_of, review_after, origin_type, origin_ref,
    )

    # Embed fact as a derived vector for semantic search.
    try:
        fact_row = await db.fact_get(domain, key)
        if fact_row:
            await vectors.embed_fact(
                fact=fact_row, embeddings=embeddings, sparse_encoder=sparse_encoder,
            )
    except Exception:
        log.warning("fact_vector_embed_failed domain=%s key=%s", domain, key, exc_info=True)

    return {
        "success": True,
        "fact_id": fact_id,
        "domain": domain,
        "key": key,
        "value": value,
    }


@mcp.tool("knowledge_fact_delete")
@logged_tool(log)
async def knowledge_fact_delete(domain: str, key: str) -> dict[str, Any]:
    """Delete a specific fact from a domain.

    Args:
        domain: Domain the fact belongs to.
        key: The fact key to delete.
    """
    _, _, _, vectors, db = _require_ready()

    deleted = await db.fact_delete(domain, key)
    if not deleted:
        return {"success": False, "error": f"Fact '{key}' not found in domain '{domain}'"}

    # Clean up the derived Qdrant vector.
    try:
        await vectors.delete_fact_vector(domain, key)
    except Exception:
        log.warning("fact_vector_delete_failed domain=%s key=%s", domain, key, exc_info=True)

    return {"success": True, "domain": domain, "key": key, "message": "Fact deleted."}


@mcp.tool("knowledge_facts_list")
@logged_tool(log)
async def knowledge_facts_list(domain: str) -> dict[str, Any]:
    """List all structured facts in a domain.

    Args:
        domain: Domain to list facts for.
    """
    _, _, _, _, db = _require_ready()

    facts = await db.facts_list(domain)
    return {"success": True, "domain": domain, "count": len(facts), "facts": facts}


@mcp.tool("knowledge_facts_search")
@logged_tool(log)
async def knowledge_facts_search(
    query: str,
    domains: list[str] | None = None,
    keys: list[str] | None = None,
) -> dict[str, Any]:
    """Search structured facts across domains.

    Searches by key substring match. If no domains specified, searches all.

    Args:
        query: Not used for fact search — use keys param (kept for API consistency).
        domains: Domains to search. If omitted, searches all non-archived.
        keys: Key substrings to match (e.g. ["glucose", "budget"]).
    """
    _, _, _, _, db = _require_ready()

    if not domains:
        all_domains = await db.domain_list()
        domains = [d["name"] for d in all_domains if not d["archived"]]

    facts = await db.facts_search(domains, keys or [])
    return {"success": True, "count": len(facts), "facts": facts}


# ---------------------------------------------------------------------------
# MCP Tools — Ingestion
# ---------------------------------------------------------------------------


@mcp.tool("knowledge_ingest_text")
@logged_tool(log)
async def knowledge_ingest_text(
    domain: str,
    content: str,
    source_name: str = "manual",
    source_type: str = "note",
) -> dict[str, Any]:
    """Ingest free-form text into a domain's knowledge base.

    Text is chunked, embedded, and stored for semantic search.
    Use this for notes, summaries, research, doctor's advice, etc.

    Args:
        domain: Domain to ingest into.
        content: The text content to ingest.
        source_name: Label for this source (e.g. "Dr. Smith visit notes 2026-03").
        source_type: Type of source (note, summary, transcript, research, etc.).
    """
    settings, embeddings, sparse_encoder, vectors, db = _require_ready()

    if not await db.domain_exists(domain):
        return {"success": False, "error": f"Domain '{domain}' not found. Create it first."}

    validation_error = _validate_text_ingest_inputs(source_name, source_type)
    if validation_error:
        return {"success": False, "error": validation_error}

    content_hash = compute_text_hash(content)

    if await db.source_exists(content_hash, domain=domain):
        return {
            "success": True,
            "message": "Content already ingested (identical hash).",
            "chunks": 0,
        }

    # Chunk and embed
    chunks_text = chunk_text(content, settings.chunk_max_chars, settings.chunk_overlap)
    if not chunks_text:
        return {"success": False, "error": "No content to ingest"}

    sparse_encoder.fit_batch(chunks_text)
    sparse_vecs = [sparse_encoder.encode(t) for t in chunks_text]
    dense_vecs = await embeddings.embed_batch(chunks_text)

    source_id = str(uuid.uuid4())
    chunk_payloads = []
    for i, text in enumerate(chunks_text):
        chunk_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{source_id}_{i}"))
        chunk_payloads.append({
            "id": chunk_id,
            "domain": domain,
            "source_id": source_id,
            "source_type": source_type,
            "source_name": source_name,
            "chunk_index": i,
            "content": text,
            "ingested_at": datetime.now(UTC).isoformat(),
        })

    await vectors.upsert_chunks(chunk_payloads, dense_vecs, sparse_vecs)
    await db.source_add(source_id, domain, source_type, source_name, content_hash, len(chunks_text))

    return {
        "success": True,
        "source_id": source_id,
        "domain": domain,
        "source_name": source_name,
        "chunks": len(chunks_text),
        "message": f"Ingested {len(chunks_text)} chunks into '{domain}'.",
    }


@mcp.tool("knowledge_ingest_file")
@logged_tool(log)
async def knowledge_ingest_file(
    domain: str,
    filename: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Ingest file(s) from a domain's knowledge directory.

    Files are extracted (PDF, images via OCR, text, CSV), chunked, embedded,
    and stored for semantic search.

    The knowledge directory is: <knowledge_path>/<domain>/
    Place files there before calling this tool.

    Args:
        domain: Domain to ingest into (must exist, directory must have files).
        filename: Specific file to ingest. If omitted, ingests all new files.
        force: Re-ingest even if file hasn't changed.
    """
    settings, embeddings, sparse_encoder, vectors, db = _require_ready()

    if not await db.domain_exists(domain):
        return {"success": False, "error": f"Domain '{domain}' not found. Create it first."}

    domain_dir = settings.knowledge_path / domain
    if not domain_dir.exists():
        domain_dir.mkdir(parents=True, exist_ok=True)
        return {"success": False, "error": f"No files found. Place files in: {domain_dir}"}

    # Collect files to process
    if filename:
        safe_name = sanitize_source_filename(filename)
        target = domain_dir / safe_name
        if not target.is_relative_to(domain_dir):
            return {"success": False, "error": "Invalid filename"}
        if not target.exists():
            return {"success": False, "error": f"File not found: {target}"}
        files = [target]
    else:
        files = sorted(
            f for f in domain_dir.iterdir()
            if f.is_file() and not f.name.startswith(".")
        )

    if not files:
        return {"success": False, "error": f"No files found in {domain_dir}"}

    total_chunks = 0
    results = []
    for file_path in files:
        try:
            outcome = await _ingest_file_at_path(
                settings, embeddings, sparse_encoder, vectors, db,
                dest=file_path, domain=domain, force=force,
            )
            if outcome.get("ingested"):
                total_chunks += int(outcome.get("chunks_stored") or 0)
                results.append({
                    "file": file_path.name,
                    "status": "indexed",
                    "chunks": outcome.get("chunks_stored"),
                })
                log.info(
                    "indexed file=%s chunks=%s",
                    file_path.name, outcome.get("chunks_stored"),
                )
            else:
                results.append({
                    "file": file_path.name,
                    "status": "skipped",
                    "reason": outcome.get("reason", "unknown"),
                })

        except Exception as exc:
            results.append({"file": file_path.name, "status": "error", "error": str(exc)})
            log.exception("ingest_failed file=%s error=%r", file_path.name, exc)

    return {
        "success": True,
        "domain": domain,
        "total_chunks": total_chunks,
        "files": results,
    }


@mcp.tool("knowledge_upload_file_base64")
@logged_tool(log)
async def knowledge_upload_file_base64(
    domain: str,
    filename: str,
    content_base64: str,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Upload and ingest a file from base64 content supplied by the MCP client.

    Use this when the client can expose an attached file's bytes directly to
    tools.
    """
    settings, embeddings, sparse_encoder, vectors, db = _require_ready()

    if not await db.domain_exists(domain):
        return {"success": False, "error": f"Domain '{domain}' not found. Create it first."}

    clean_filename = sanitize_source_filename(filename)
    if not clean_filename:
        return {"success": False, "error": "Invalid filename"}

    try:
        data = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        return {"success": False, "error": f"Invalid base64 content: {exc}"}

    domain_dir = settings.knowledge_path / domain
    dest = (domain_dir / clean_filename).resolve()
    if not dest.is_relative_to(settings.knowledge_path.resolve()):
        return {"success": False, "error": "Invalid upload path"}
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not overwrite:
        return {
            "success": False,
            "error": (
                f"File '{clean_filename}' already exists in '{domain}'. "
                "Set overwrite=true to replace."
            ),
        }

    if overwrite:
        await delete_sources_for_overwrite(settings, vectors, db, domain, clean_filename)

    dest.write_bytes(data)

    return await _ingest_file_at_path(
        settings, embeddings, sparse_encoder, vectors, db,
        dest=dest, domain=domain, force=overwrite,
    )


# ---------------------------------------------------------------------------
# MCP Tools — Context Pack (high-level facade)
# ---------------------------------------------------------------------------


@mcp.tool("knowledge_context_pack")
@logged_tool(log)
async def knowledge_context_pack(
    query: str,
    temporal_intent: str | None = None,
    max_results: int = 8,
) -> dict[str, Any]:
    """Get Jack's personal context for any question. CALL THIS FIRST.

    This is your primary tool for ANY question that might relate to Jack's
    personal life, schedule, plans, preferences, health, work, relationships,
    finances, home, or anything he has told you before.

    Call this BEFORE web search, calendar, email, or any other tool. It
    combines semantic search, structured facts, and wiki pages into one
    compact context package.

    Only skip this tool if the question is purely about public knowledge
    with zero personal dimension (e.g., "what is the capital of France").

    Args:
        query: The user's question or topic to search for.
        temporal_intent: Optional hint — auto, all, current_upcoming, or
            historical. Defaults to auto (inferred from the query).
        max_results: Maximum search results to include (default 8).

    Returns:
        A context package with facts, search results, wiki pages, and
        suggestions for whether additional tools (web, calendar, etc.)
        might be useful.
    """
    settings, embeddings_client, sparse_encoder, vectors, db = _require_ready()

    # 1. Run the full Knowledge search (facts + chunks + wiki)
    search_result = await search_knowledge(
        embeddings=embeddings_client,
        sparse_encoder=sparse_encoder,
        vectors=vectors,
        db=db,
        query=query,
        limit=max_results,
        min_similarity=0.25,
        include_facts=True,
        max_chars=800,
        temporal_intent=temporal_intent,
    )

    # 2. Gather domain summary for context
    all_domains = await db.domain_list()
    active_domains = [
        {"name": d["name"], "description": d["description"]}
        for d in all_domains
        if not d["archived"]
    ]

    # 3. Check curation queue (lightweight)
    pending_count = 0
    with contextlib.suppress(Exception):
        pending_count = await db.curation_count(status="pending")

    # 4. Summarize what we found
    facts = search_result.get("facts", [])
    results = search_result.get("results", [])
    wiki_count = search_result.get("wiki_count", 0)
    chunk_count = search_result.get("chunk_count", 0)
    fact_count = len(facts)

    has_personal_context = fact_count > 0 or len(results) > 0

    # 5. Build augmentation suggestions
    suggestions: list[str] = []
    query_lower = query.lower()

    # Temporal/calendar hints
    temporal_words = {"tomorrow", "today", "tonight", "weekend", "next week",
                      "schedule", "calendar", "appointment", "meeting", "event"}
    if any(w in query_lower for w in temporal_words):
        suggestions.append("calendar: check calendar for schedule/event details")

    # Weather hints
    weather_words = {"weather", "rain", "temperature", "forecast", "outside",
                     "cold", "hot", "warm", "umbrella"}
    if any(w in query_lower for w in weather_words):
        suggestions.append("weather: get current/forecast weather data")

    # Email hints
    email_words = {"email", "mail", "inbox", "message", "sent", "reply"}
    if any(w in query_lower for w in email_words):
        suggestions.append("email: check Gmail for related messages")

    # Web supplement (only if personal context is thin)
    if not has_personal_context:
        suggestions.append(
            "web_search: no personal context found — web search may help "
            "if this is a public knowledge question"
        )
    elif any(w in query_lower for w in {"news", "latest", "current", "price",
                                         "stock", "market"}):
        suggestions.append(
            "web_search: personal context found but query may benefit from "
            "current public data"
        )

    return {
        "success": True,
        "query": query,
        "temporal_intent": search_result.get("temporal_intent", "auto"),
        "searched_domains": search_result.get("searched_domains", []),
        "has_personal_context": has_personal_context,
        # Core results
        "facts": facts,
        "fact_count": fact_count,
        "results": results,
        "result_count": len(results),
        "wiki_count": wiki_count,
        "chunk_count": chunk_count,
        # Domain awareness
        "available_domains": active_domains,
        # Diagnostics
        "curation_pending": pending_count,
        # Guidance for the model
        "augmentation_suggestions": suggestions,
    }


# ---------------------------------------------------------------------------
# MCP Tools — Search
# ---------------------------------------------------------------------------


@mcp.tool("knowledge_search")
@logged_tool(log)
async def knowledge_search(
    query: str,
    domain: str | None = None,
    domains: list[str] | None = None,
    limit: int = 10,
    min_similarity: float = 0.25,
    include_facts: bool = True,
    max_chars: int | None = None,
    temporal_intent: str | None = None,
) -> dict[str, Any]:
    """Search knowledge base using hybrid semantic + keyword search.

    If a single domain is given, automatically includes its related domains
    and the 'core' domain. If no domain is specified, searches everything.

    Args:
        query: What to search for.
        domain: Search this domain + its related domains + core.
        domains: Explicit list of domains to search (overrides auto-resolution).
        limit: Max results to return.
        min_similarity: Minimum similarity threshold (0.0 to 1.0).
        include_facts: Also search structured facts for relevant matches.
        max_chars: Optional cap on each result's content length to reduce
            context size. None (default) returns full chunk text.
        temporal_intent: Optional override: auto, all, current_upcoming, or
            historical. Auto infers from the query.
    """
    _, embeddings_client, sparse_encoder, vectors, db = _require_ready()
    return await search_knowledge(
        embeddings=embeddings_client,
        sparse_encoder=sparse_encoder,
        vectors=vectors,
        db=db,
        query=query,
        domain=domain,
        domains=domains,
        limit=limit,
        min_similarity=min_similarity,
        include_facts=include_facts,
        max_chars=max_chars,
        temporal_intent=temporal_intent,
    )


@mcp.tool("knowledge_sources")
@logged_tool(log)
async def knowledge_sources(domain: str) -> dict[str, Any]:
    """List all ingested sources in a domain.

    Each source includes a pre-signed `download_url` and a ready-to-paste
    `download_markdown` link. Display `download_markdown` verbatim when Jack
    asks to download/view a file. Links expire in 15 minutes.

    Args:
        domain: Domain to list sources for.
    """
    settings, _, _, _, db = _require_ready()

    sources = await db.sources_list(domain)
    base = settings.api_base.rstrip("/")
    for src in sources:
        sid = src.get("id") or src.get("source_id")
        if not sid:
            continue
        # Skip ingested-text/note rows that have no stored file to download.
        if not src.get("stored_path"):
            continue
        if not resolve_source_path(settings.knowledge_path, src):
            src["download_missing"] = True
            src["download_error"] = "stored source file is missing on disk"
            continue
        filename = src.get("filename") or sid
        try:
            token = await db.download_token_create(sid, 900)
        except Exception as exc:
            src["download_missing"] = True
            src["download_error"] = f"failed to mint download token: {exc}"
            continue
        url = f"{base}/api/download/{token['token']}"
        safe_label = str(filename).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
        src["download_url"] = url
        src["download_markdown"] = f"[{safe_label}]({url})"
        src["download_expires_at"] = token["expires_at"]
    return {"success": True, "domain": domain, "count": len(sources), "sources": sources}


@mcp.tool("knowledge_source_download_base64")
@logged_tool(log)
async def knowledge_source_download_base64(
    source_id: str,
) -> dict[str, Any]:
    """Download one stored source as base64 bytes for chat clients.

    Use knowledge_sources(domain) first to find the source_id.
    """
    settings, _, _, vectors, db = _require_ready()
    result = await source_download_bytes(settings, db, source_id, vectors)

    if not result.get("success"):
        return result

    data = result.pop("data")
    result["data_base64"] = base64.b64encode(data).decode()
    return result


@mcp.tool("knowledge_source_download_url")
@logged_tool(log)
async def knowledge_source_download_url(
    source_id: str,
    ttl_seconds: int = 900,
) -> dict[str, Any]:
    """Create a temporary clickable download URL for one stored source.

    Use knowledge_sources(domain) first to find the source_id. The URL can be
    opened without an Authorization header until it expires. The returned
    `markdown` field is a ready-to-paste link the agent should display verbatim.
    """
    settings, _, _, _, db = _require_ready()
    source = await db.source_get(source_id)
    if not source:
        return {"success": False, "error": f"Source '{source_id}' not found"}
    token = await db.download_token_create(source_id, ttl_seconds)
    base = settings.api_base.rstrip("/")
    url = f"{base}/api/download/{token['token']}"
    filename = source.get("filename") or source_id
    # Escape characters that would break a markdown link label.
    safe_label = str(filename).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    return {
        "success": True,
        "source_id": source_id,
        "filename": filename,
        "url": url,
        "markdown": f"[{safe_label}]({url})",
        "expires_at": token["expires_at"],
        "ttl_seconds": token["ttl_seconds"],
    }


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
# MCP Tools — Wiki Pages
# ---------------------------------------------------------------------------


@mcp.tool("knowledge_wiki_get")
@logged_tool(log)
async def knowledge_wiki_get(slug: str) -> dict[str, Any]:
    """Get one wiki page by slug, including frontmatter and sources."""
    _, _, _, _, db = _require_ready()
    clean_slug = slug.strip()
    if not clean_slug:
        return {"success": False, "error": "slug is required"}
    page = await db.wiki_get(clean_slug)
    if not page:
        return {"success": False, "error": f"Wiki page '{clean_slug}' not found"}
    return {"success": True, "page": page}


@mcp.tool("knowledge_wiki_list")
@logged_tool(log)
async def knowledge_wiki_list(
    domain: str | None = None,
    kind: str | None = None,
    status: str = "active",
    limit: int = 50,
) -> dict[str, Any]:
    """List wiki pages. status is active, candidate, archived, or all."""
    _, _, _, _, db = _require_ready()
    clean_status = str(status or "active").strip()
    if clean_status not in WIKI_PAGE_LIST_STATUSES:
        return {"success": False, "error": "status must be active, candidate, archived, or all"}

    clean_kind = kind.strip() if kind else None
    if clean_kind and clean_kind not in WIKI_PAGE_KINDS:
        return {"success": False, "error": "kind must be entity, concept, source_summary, or index"}

    pages = await db.wiki_list(
        domain=domain.strip() if domain else None,
        kind=clean_kind,
        status=clean_status,
        limit=limit,
    )
    return {"success": True, "count": len(pages), "pages": pages}


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


@mcp.tool("knowledge_wiki_rebuild")
@logged_tool(log)
async def knowledge_wiki_rebuild(
    domain: str | None = None,
    entity_slug: str | None = None,
    force_full: bool = False,
    dry_run: bool = False,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Estimate or run a wiki rebuild."""
    settings, embeddings, sparse_encoder, vectors, db = _require_ready()
    preview = await preview_wiki_rebuild(
        settings, db, domain=domain, entity_slug=entity_slug, force_full=force_full,
    )
    if dry_run or not preview.get("success"):
        return preview
    if not confirmed:
        scope = preview["scope"]
        target = scope["entity_slug"] or scope["domain"] or "full wiki"
        return {
            "success": False,
            "requires_confirmation": True,
            "writes_performed": False,
            "confirmation": (
                "Manual wiki rebuilds write pages and run rows. Ask Jack first unless he "
                "explicitly requested this rebuild, then call again with confirmed=true."
            ),
            "scope": scope,
            "target": target,
            "estimated_pages": preview["estimated_pages"],
            "estimated_entity_pages": preview["estimated_entity_pages"],
            "estimated_index_pages": preview["estimated_index_pages"],
            "token_estimate": preview["token_estimate"],
            "estimated_cost": preview["estimated_cost"],
            "latency_class": preview["latency_class"],
            "changed_entities": preview["changed_entities"],
        }
    return await rebuild_wiki(
        settings, embeddings, sparse_encoder, vectors, db,
        domain=domain, entity_slug=entity_slug, force_full=force_full,
    )


# ---------------------------------------------------------------------------
# MCP Tools — Curation Queue
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
    """List Knowledge curation queue items for review.

    The queue contains proposed conversation distillations, source consolidation
    candidates, temporal fact cleanups, and maintenance actions. Queue items are
    drafts until explicitly applied.

    Args:
        status: Filter by status. Default is "pending". Use null to list all.
        kind: Optional kind filter, e.g. "conversation_distill".
        limit: Maximum items to return (1-200).
    """
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


@mcp.tool("knowledge_curation_apply")
@logged_tool(log)
async def knowledge_curation_apply(
    item_id: str,
    confirmation: str | None = None,
) -> dict[str, Any]:
    """Apply a reviewed curation item.

    Destructive actions such as source deletion, fact deletion, or domain archive
    require confirmation equal to the queue item id.
    """
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


@mcp.tool("knowledge_curation_reject")
@logged_tool(log)
async def knowledge_curation_reject(item_id: str) -> dict[str, Any]:
    """Reject a curation queue item without applying any proposed actions."""
    _, _, _, _, db = _require_ready()
    updated = await db.curation_mark_status(item_id, "rejected")
    if not updated:
        return {"success": False, "error": f"Curation item '{item_id}' not found"}
    return {"success": True, "item_id": item_id, "status": "rejected"}


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

    # Warm up BM25 sparse encoder from existing chunks so hybrid search
    # has meaningful IDF scores on startup rather than a cold zero state.
    try:
        all_chunks = await _vectors.chunks_all()
        texts = [p["content"] for p in all_chunks if p.get("content")]
        if texts:
            _sparse_encoder.fit_batch(texts)
            log.info("bm25_warmup chunks=%d", len(texts))
    except Exception as exc:
        log.warning("bm25_warmup_skipped error=%r", exc)

    # Ensure 'core' domain exists
    await _db.domain_create(
        "core",
        "Foundational personal profile — always included in searches",
        [],
    )

    _ready = True
    log.info("initialization complete")


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
    port: int = DEFAULT_HTTP_PORT,
) -> None:
    """Run the Knowledge MCP server."""
    import asyncio

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

    parser = argparse.ArgumentParser(description="Knowledge MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)
    args = parser.parse_args()
    run(args.transport, args.host, args.port)


if __name__ == "__main__":
    main()


__all__ = ["mcp", "run", "main", "DEFAULT_HTTP_PORT"]
