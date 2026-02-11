"""Standalone housekeeping MCP server.

Exposes utility tools (time, echo, long-term memory) via MCP protocol.
Zero imports from Backend_FastAPI — fully standalone.

Memory tools (remember, recall, forget, reflect, memory_stats) use Qdrant
for vector search and SQLite for metadata.  They require OPENROUTER_API_KEY
and a running Qdrant instance (defaults to http://127.0.0.1:6333).

Run:
    python -m servers.housekeeping --transport streamable-http --host 0.0.0.0 --port 9002
"""

from __future__ import annotations

import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from fastmcp import FastMCP

from shared.embeddings import EmbeddingClient
from shared.memory_config import MemorySettings
from shared.memory_maintenance import start_maintenance
from shared.memory_repository import MemoryRepository
from shared.time_context import (
    EASTERN_TIMEZONE,
    EASTERN_TIMEZONE_NAME,
    build_context_lines,
    create_time_snapshot,
    format_timezone_offset,
)
from shared.vector_store import VectorStore

# Default port for HTTP transport
DEFAULT_HTTP_PORT = 9002

mcp = FastMCP("housekeeping")

# ---------------------------------------------------------------------------
# Memory subsystem — initialised lazily at startup
# ---------------------------------------------------------------------------
_mem_settings: MemorySettings | None = None
_embeddings: EmbeddingClient | None = None
_vectors: VectorStore | None = None
_repo: MemoryRepository | None = None
_memory_ready = False


async def _memory_startup() -> None:
    """Initialise Qdrant, embeddings client, and SQLite repo."""
    global _mem_settings, _embeddings, _vectors, _repo, _memory_ready

    try:
        _mem_settings = MemorySettings()  # type: ignore[call-arg]
    except Exception as exc:
        print(
            f"[HOUSEKEEPING] Memory subsystem disabled — config error: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return

    _embeddings = EmbeddingClient(_mem_settings)
    _vectors = VectorStore(_mem_settings)
    _repo = MemoryRepository(_mem_settings.memory_db_path)

    try:
        await _vectors.ensure_collection()
    except Exception as exc:
        print(
            f"[HOUSEKEEPING] Memory subsystem disabled — Qdrant unreachable: {exc}",
            file=sys.stderr,
            flush=True,
        )
        _embeddings = _vectors = _repo = None  # type: ignore[assignment]
        return

    await _repo.initialize()

    start_maintenance(_repo, _vectors, _mem_settings)
    _memory_ready = True
    print("[HOUSEKEEPING] Memory subsystem initialised", file=sys.stderr, flush=True)


async def _memory_shutdown() -> None:
    """Release memory subsystem resources."""
    if _embeddings:
        await _embeddings.close()
    if _vectors:
        await _vectors.close()
    if _repo:
        await _repo.close()


def _require_memory() -> tuple[EmbeddingClient, VectorStore, MemoryRepository]:
    """Return memory components or raise a clear error."""
    if not _memory_ready or _embeddings is None or _vectors is None or _repo is None:
        raise RuntimeError(
            "Memory subsystem is not available. "
            "Check OPENROUTER_API_KEY and Qdrant connectivity."
        )
    return _embeddings, _vectors, _repo


@dataclass
class EchoResult:
    message: str
    uppercase: bool


@mcp.tool("test_echo")
async def test_echo(message: str, uppercase: bool = False) -> dict[str, Any]:
    """Return the message, optionally uppercased, for integration testing."""
    payload = message.upper() if uppercase else message
    return asdict(EchoResult(message=payload, uppercase=uppercase))


@mcp.tool(
    "current_time",
    description=(
        "Retrieve the current moment with precise Unix timestamps plus UTC and Eastern Time "
        "(ET/EDT) ISO formats. Use this whenever the conversation needs an up-to-date clock "
        "reference or time zone comparison."
    ),
)
async def current_time(format: Literal["iso", "unix"] = "iso") -> dict[str, Any]:
    """Return the current time with UTC and Eastern Time representations."""
    print(
        f"[HOUSEKEEPING-DEBUG] current_time called with format={format}",
        file=sys.stderr,
        flush=True,
    )

    snapshot = create_time_snapshot(EASTERN_TIMEZONE_NAME, fallback=EASTERN_TIMEZONE)
    eastern = snapshot.eastern

    if format == "iso":
        rendered = snapshot.iso_utc
    elif format == "unix":
        rendered = str(snapshot.unix_seconds)
    else:  # pragma: no cover - guarded by Literal
        raise ValueError(f"Unsupported format: {format}")

    offset = format_timezone_offset(eastern.utcoffset())
    context_lines = list(build_context_lines(snapshot))
    context_summary = "\n".join(context_lines)

    result = {
        "format": format,
        "value": rendered,
        "utc_iso": snapshot.iso_utc,
        "utc_unix": str(snapshot.unix_seconds),
        "utc_unix_precise": snapshot.unix_precise,
        "eastern_iso": eastern.isoformat(),
        "eastern_abbreviation": eastern.tzname(),
        "eastern_display": eastern.strftime("%a %b %d %Y %I:%M:%S %p %Z"),
        "eastern_offset": offset,
        "timezone": EASTERN_TIMEZONE_NAME,
        "context_lines": context_lines,
        "context_summary": context_summary,
    }

    print("[HOUSEKEEPING-DEBUG] current_time returning result", file=sys.stderr, flush=True)
    return result


# ---------------------------------------------------------------------------
# Memory tools
# ---------------------------------------------------------------------------


@mcp.tool(
    "remember",
    description=(
        "Store a fact, preference, instruction, or conversation summary in "
        "long-term memory for later retrieval. Use this when the user states "
        "something worth remembering across conversations, corrects you, or "
        "expresses a preference. Memories persist until explicitly forgotten "
        "or they expire."
    ),
)
async def remember(
    content: str,
    category: str = "fact",
    tags: list[str] | None = None,
    importance: float = 0.5,
    session_id: str | None = None,
    pinned: bool = False,
    ttl_hours: int | None = None,
) -> dict[str, Any]:
    """Store a memory with vector embedding for semantic retrieval.

    Args:
        content: The fact, preference, or summary to remember.
        category: One of: fact, preference, summary, instruction, episode.
        tags: Optional tags for filtering (e.g., ["weather", "home"]).
        importance: 0.0 to 1.0 — higher means harder to forget.
        session_id: Tie to a session, or omit for cross-session memory.
        pinned: If True, survives cleanup and session clears.
        ttl_hours: Auto-expire after this many hours. Omit for permanent.
    """
    embeddings, vectors, repo = _require_memory()

    memory_id = str(uuid4())
    embedding = await embeddings.embed(content)

    now = datetime.now(UTC)
    expires_at = (now + timedelta(hours=ttl_hours)).isoformat() if ttl_hours else None

    payload = {
        "user_id": "default",
        "content": content,
        "category": category,
        "tags": tags or [],
        "importance": importance,
        "session_id": session_id,
        "pinned": pinned,
        "created_at": now.isoformat(),
        "expires_at": expires_at,
    }

    await vectors.upsert(memory_id, embedding, payload)
    await repo.insert(
        memory_id=memory_id,
        user_id="default",
        category=category,
        content_preview=content[:200],
        importance=importance,
        pinned=pinned,
        session_id=session_id,
        expires_at=expires_at,
    )

    return {
        "success": True,
        "memory_id": memory_id,
        "content_preview": content[:100] + ("\u2026" if len(content) > 100 else ""),
        "category": category,
        "pinned": pinned,
        "expires_at": expires_at,
        "message": f"Stored as {category}" + (f" (expires in {ttl_hours}h)" if ttl_hours else ""),
    }


@mcp.tool(
    "recall",
    description=(
        "Search long-term memory using natural language. Returns the most "
        "semantically relevant stored memories ranked by similarity. Use this "
        "before answering questions about past interactions, user preferences, "
        "or anything the user might have told you previously."
    ),
)
async def recall(
    query: str,
    category: str | None = None,
    tags: list[str] | None = None,
    session_id: str | None = None,
    time_range_hours: int | None = None,
    limit: int = 10,
    min_similarity: float = 0.4,
) -> dict[str, Any]:
    """Semantic search over stored memories.

    Args:
        query: Natural language description of what to recall.
        category: Filter to a specific category.
        tags: Filter to memories with these tags.
        session_id: Limit to a specific session, or omit for all.
        time_range_hours: Only return memories from the last N hours.
        limit: Maximum results to return.
        min_similarity: Minimum cosine similarity threshold (0.0-1.0).
    """
    embeddings, vectors, repo = _require_memory()

    query_embedding = await embeddings.embed(query)

    results = await vectors.search(
        query_embedding,
        category=category,
        tags=tags,
        session_id=session_id,
        time_range_hours=time_range_hours,
        limit=limit,
        min_score=min_similarity,
    )

    if not results:
        return {
            "success": True,
            "count": 0,
            "memories": [],
            "message": "No matching memories found.",
        }

    accessed_ids = [str(r.id) for r in results]
    await repo.record_access(accessed_ids)

    memories = []
    for result in results:
        p = result.payload or {}
        memories.append(
            {
                "memory_id": str(result.id),
                "content": p.get("content", ""),
                "category": p.get("category", "unknown"),
                "tags": p.get("tags", []),
                "similarity": round(result.score, 4),
                "importance": p.get("importance", 0.5),
                "created_at": p.get("created_at"),
                "session_id": p.get("session_id"),
                "pinned": p.get("pinned", False),
            }
        )

    return {
        "success": True,
        "count": len(memories),
        "query": query,
        "memories": memories,
    }


@mcp.tool(
    "forget",
    description=(
        "Delete memories by ID, session, category, or age. Use when the user "
        "asks you to forget something, or to clean up outdated information. "
        "Pinned memories are protected unless include_pinned is True."
    ),
)
async def forget(
    memory_id: str | None = None,
    session_id: str | None = None,
    category: str | None = None,
    older_than_hours: int | None = None,
    include_pinned: bool = False,
) -> dict[str, Any]:
    """Remove memories by ID, session, category, or age.

    Args:
        memory_id: Delete a specific memory by its ID.
        session_id: Delete all memories for a session.
        category: Delete all memories in a category.
        older_than_hours: Delete memories older than N hours.
        include_pinned: Must be True to delete pinned memories.
    """
    _, vectors, repo = _require_memory()

    if not any([memory_id, session_id, category, older_than_hours]):
        return {
            "success": False,
            "error": (
                "Specify at least one filter "
                "(memory_id, session_id, category, or older_than_hours)."
            ),
        }

    deleted_ids: list[str] = []

    if memory_id:
        await vectors.delete([memory_id])
        await repo.delete([memory_id])
        deleted_ids.append(memory_id)

    if session_id:
        ids = await repo.delete_by_session(session_id, include_pinned=include_pinned)
        if ids:
            await vectors.delete(ids)
        deleted_ids.extend(ids)

    return {
        "success": True,
        "deleted_count": len(deleted_ids),
        "message": f"Deleted {len(deleted_ids)} memory/memories.",
    }


@mcp.tool(
    "reflect",
    description=(
        "Store a high-level summary of a conversation session. Call this at "
        "the end of a meaningful conversation to capture key takeaways as a "
        "single, high-importance memory. This creates an 'episode' memory "
        "that persists across sessions."
    ),
)
async def reflect(
    session_id: str,
    summary: str,
) -> dict[str, Any]:
    """Store an episode summary for a conversation.

    Args:
        session_id: The session being summarized.
        summary: Distillation of the conversation's key points.
    """
    result = await remember(
        content=summary,
        category="episode",
        importance=0.8,
        session_id=session_id,
        pinned=True,
    )
    return {
        "success": True,
        "memory_id": result["memory_id"],
        "message": "Session summary stored as episode memory (pinned).",
    }


@mcp.tool(
    "memory_stats",
    description="Show statistics about stored memories: total count, categories, oldest/newest.",
)
async def memory_stats() -> dict[str, Any]:
    """Return memory store statistics."""
    _, vectors, repo = _require_memory()

    db_stats = await repo.stats()
    vector_count = await vectors.count()

    return {
        "success": True,
        "vector_count": vector_count,
        **db_stats,
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def run(
    transport: str = "stdio",
    host: str = "0.0.0.0",
    port: int = DEFAULT_HTTP_PORT,
) -> None:  # pragma: no cover - integration entrypoint
    """Run the housekeeping MCP server with the specified transport."""
    import asyncio

    # Initialise memory subsystem before serving
    asyncio.get_event_loop().run_until_complete(_memory_startup())

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
        asyncio.get_event_loop().run_until_complete(_memory_shutdown())


def main() -> None:  # pragma: no cover - CLI helper
    import argparse

    parser = argparse.ArgumentParser(description="Housekeeping MCP Server")
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
    "test_echo",
    "current_time",
    "remember",
    "recall",
    "forget",
    "reflect",
    "memory_stats",
]
