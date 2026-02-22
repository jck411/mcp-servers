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
from typing import Any
from uuid import uuid4

from fastmcp import FastMCP

from shared.embeddings import EmbeddingClient
from shared.memory_config import MemorySettings
from shared.memory_maintenance import start_maintenance
from shared.memory_repository import MemoryRepository
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





# ---------------------------------------------------------------------------
# Memory tools — profile-based factory
# ---------------------------------------------------------------------------

# Implementation functions that accept user_id


async def _remember_impl(
    user_id: str,
    content: str,
    category: str = "fact",
    tags: list[str] | None = None,
    importance: float = 0.5,
    session_id: str | None = None,
    pinned: bool = False,
    ttl_hours: int | None = None,
) -> dict[str, Any]:
    """Store a memory for a specific user profile."""
    embeddings, vectors, repo = _require_memory()

    memory_id = str(uuid4())
    embedding = await embeddings.embed(content)

    now = datetime.now(UTC)
    expires_at = (now + timedelta(hours=ttl_hours)).isoformat() if ttl_hours else None

    payload = {
        "user_id": user_id,
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
        user_id=user_id,
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
        "profile": user_id,
        "content_preview": content[:100] + ("\u2026" if len(content) > 100 else ""),
        "category": category,
        "pinned": pinned,
        "expires_at": expires_at,
        "message": f"Stored as {category}" + (f" (expires in {ttl_hours}h)" if ttl_hours else ""),
    }


async def _recall_impl(
    user_id: str,
    query: str,
    category: str | None = None,
    tags: list[str] | None = None,
    session_id: str | None = None,
    time_range_hours: int | None = None,
    limit: int = 10,
    min_similarity: float = 0.15,
) -> dict[str, Any]:
    """Search memories for a specific user profile."""
    embeddings, vectors, repo = _require_memory()

    query_embedding = await embeddings.embed(query)

    results = await vectors.search(
        query_embedding,
        user_id=user_id,
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
            "profile": user_id,
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
        "profile": user_id,
        "query": query,
        "memories": memories,
    }


async def _forget_impl(
    user_id: str,
    memory_id: str | None = None,
    session_id: str | None = None,
    category: str | None = None,
    older_than_hours: int | None = None,
    include_pinned: bool = False,
) -> dict[str, Any]:
    """Delete memories for a specific user profile."""
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
        "profile": user_id,
        "deleted_count": len(deleted_ids),
        "message": f"Deleted {len(deleted_ids)} memory/memories.",
    }


async def _memory_stats_impl(user_id: str) -> dict[str, Any]:
    """Return memory statistics for a specific user profile."""
    _, vectors, repo = _require_memory()

    db_stats = await repo.stats(user_id=user_id)
    vector_count = await vectors.count(user_id=user_id)

    return {
        "success": True,
        "profile": user_id,
        "vector_count": vector_count,
        **db_stats,
    }


def _register_profile_tools(profile: str) -> None:
    """Register memory tools for a specific profile.

    Creates: remember_{profile}, recall_{profile}, forget_{profile},
             reflect_{profile}, memory_stats_{profile}
    """
    # Use closures to capture the profile for each tool

    @mcp.tool(
        f"remember_{profile}",
        description=(
            f"Store a fact, preference, instruction, or summary in {profile}'s "
            "long-term memory for later retrieval. Use when the user states "
            "something worth remembering, corrects you, or expresses a preference."
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
        _profile: str = profile,
    ) -> dict[str, Any]:
        return await _remember_impl(
            user_id=_profile,
            content=content,
            category=category,
            tags=tags,
            importance=importance,
            session_id=session_id,
            pinned=pinned,
            ttl_hours=ttl_hours,
        )

    @mcp.tool(
        f"recall_{profile}",
        description=(
            f"Search {profile}'s long-term memory using natural language. Returns "
            "the most semantically relevant stored memories ranked by similarity."
        ),
    )
    async def recall(
        query: str,
        category: str | None = None,
        tags: list[str] | None = None,
        session_id: str | None = None,
        time_range_hours: int | None = None,
        limit: int = 10,
        min_similarity: float = 0.15,
        _profile: str = profile,
    ) -> dict[str, Any]:
        return await _recall_impl(
            user_id=_profile,
            query=query,
            category=category,
            tags=tags,
            session_id=session_id,
            time_range_hours=time_range_hours,
            limit=limit,
            min_similarity=min_similarity,
        )

    @mcp.tool(
        f"forget_{profile}",
        description=(
            f"Delete memories from {profile}'s memory by ID, session, category, "
            "or age. Pinned memories are protected unless include_pinned is True."
        ),
    )
    async def forget(
        memory_id: str | None = None,
        session_id: str | None = None,
        category: str | None = None,
        older_than_hours: int | None = None,
        include_pinned: bool = False,
        _profile: str = profile,
    ) -> dict[str, Any]:
        return await _forget_impl(
            user_id=_profile,
            memory_id=memory_id,
            session_id=session_id,
            category=category,
            older_than_hours=older_than_hours,
            include_pinned=include_pinned,
        )

    @mcp.tool(
        f"reflect_{profile}",
        description=(
            f"Store a high-level summary of a conversation in {profile}'s memory. "
            "Creates a pinned 'episode' memory that persists across sessions."
        ),
    )
    async def reflect(
        session_id: str,
        summary: str,
        _profile: str = profile,
    ) -> dict[str, Any]:
        result = await _remember_impl(
            user_id=_profile,
            content=summary,
            category="episode",
            importance=0.8,
            session_id=session_id,
            pinned=True,
        )
        return {
            "success": True,
            "profile": _profile,
            "memory_id": result["memory_id"],
            "message": "Session summary stored as episode memory (pinned).",
        }

    @mcp.tool(
        f"memory_stats_{profile}",
        description=f"Show statistics about {profile}'s stored memories.",
    )
    async def memory_stats(_profile: str = profile) -> dict[str, Any]:
        return await _memory_stats_impl(user_id=_profile)


def _register_all_profile_tools() -> None:
    """Register memory tools for all configured profiles."""
    try:
        settings = MemorySettings()  # type: ignore[call-arg]
        profiles = settings.memory_profiles
    except Exception:
        profiles = ["default"]

    for profile in profiles:
        _register_profile_tools(profile)
        print(f"[HOUSEKEEPING] Registered memory tools for profile: {profile}", file=sys.stderr)


# Register tools at module load time (before server starts)
_register_all_profile_tools()


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
]
