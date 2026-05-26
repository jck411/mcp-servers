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

import contextlib
import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from servers.knowledge_source_files import resolve_source_path, sanitize_source_filename
from shared.logging_config import get_logger, logged_tool

log = get_logger("knowledge")

# --- servers/knowledge/ package ---
from servers.knowledge.embeddings import BM25SparseEncoder, EmbeddingClient  # noqa: E402
from servers.knowledge.settings import DEFAULT_HTTP_PORT, KnowledgeSettings  # noqa: E402


def _auth_provider() -> StaticTokenVerifier | None:
    token = os.environ.get("MCP_KNOWLEDGE_BEARER_TOKEN")
    if not token:
        return None
    return StaticTokenVerifier({token: {"client_id": "knowledge", "scopes": []}})


mcp = FastMCP("knowledge", auth=_auth_provider())

from servers.knowledge.cross_domain import enrich_context  # noqa: E402
from servers.knowledge.db import KnowledgeDB  # noqa: E402
from servers.knowledge.extraction import chunk_text, compute_text_hash  # noqa: E402
from servers.knowledge.ingestion import (  # noqa: E402
    _ingest_file_at_path,
    _validate_text_ingest_inputs,
)
from servers.knowledge.search import search_knowledge  # noqa: E402
from servers.knowledge.vectors import KnowledgeVectorStore  # noqa: E402
from servers.knowledge.wiki import (  # noqa: E402
    WIKI_PAGE_KINDS,
    WIKI_PAGE_LIST_STATUSES,
    preview_wiki_rebuild,
    rebuild_wiki,
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


# ---------------------------------------------------------------------------
# MCP Tools — Context Pack (high-level facade)
# ---------------------------------------------------------------------------


@mcp.tool("knowledge_context_pack")
@logged_tool(log)
async def knowledge_context_pack(
    query: str | None = None,
    question: str | None = None,
    q: str | None = None,
    temporal_intent: str | None = None,
    max_results: int = 8,
    max_items: int | None = None,
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
        question: Backward-compatible alias for query.
        q: Short alias for query.
        temporal_intent: Optional hint — auto, all, current_upcoming, or
            historical. Defaults to auto (inferred from the query).
        max_results: Maximum search results to include (default 8).
        max_items: Backward-compatible alias for max_results.

    Returns:
        A context package with facts, search results, wiki pages, and
        suggestions for whether additional tools (web, calendar, etc.)
        might be useful.
    """
    clean_query = (query or question or q or "").strip()
    if not clean_query:
        return {
            "success": False,
            "error": "query is required; call knowledge_context_pack(query=<user question>)",
        }
    query = clean_query
    if max_items is not None:
        max_results = max_items

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

    # 5. Cross-domain enrichment — detect signals in query + results and
    #    automatically fetch context from related domains (schedule,
    #    people, finances, tasks, etc.) so the model gets a complete
    #    picture without needing to make additional tool calls.
    cross_domain: dict[str, Any] = {}
    try:
        cross_domain = await enrich_context(
            query,
            primary_facts=facts,
            primary_results=results,
            searched_domains=search_result.get("searched_domains", []),
            embeddings=embeddings_client,
            sparse_encoder=sparse_encoder,
            vectors=vectors,
            db=db,
        )
    except Exception:
        log.warning("cross_domain_enrichment_failed", exc_info=True)

    # 6. Build augmentation suggestions
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

    # Finance hints
    finance_words = {"budget", "spending", "cost", "price", "afford",
                     "expense", "bill", "payment", "subscription", "balance"}
    if any(w in query_lower for w in finance_words):
        suggestions.append("finance: check Monarch for budget/spending details")

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

    # Merge cross-domain suggestions
    suggestions.extend(cross_domain.pop("cross_domain_suggestions", []))

    response = {
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

    # Attach cross-domain enrichment if any signals were detected
    if cross_domain:
        response["cross_domain_context"] = cross_domain

    return response


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


@mcp.tool("knowledge_wiki_rebuild")
@logged_tool(log)
async def knowledge_wiki_rebuild(
    domain: str | None = None,
    entity_slug: str | None = None,
    force_full: bool = False,
    dry_run: bool = False,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Estimate or run a wiki rebuild.

    Args:
        domain: Optional domain scope.
        entity_slug: Optional targeted page slug in '<domain>/<slug>' form.
        force_full: Rebuild from all eligible inputs/pages instead of only
            changes since the last wiki run. Leave false unless Jack asks for
            a full rebuild.
        dry_run: Return the scope, changed entities, page counts, and token
            estimate without writing.
        confirmed: Required for writes unless dry_run is true.
    """
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
