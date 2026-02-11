# Memory MCP Server — Implementation Plan

**Purpose:** Standalone MCP server providing semantic long-term memory (store/recall/forget) for the AI assistant. Deployed on Proxmox as a systemd service alongside the other MCP servers.

**Embedding:** OpenRouter API (OpenAI text-embedding-3-small via `https://openrouter.ai/api/v1/embeddings`)
**Vector Store:** Qdrant (single binary, no Docker)
**Metadata:** SQLite (same pattern as existing servers)
**Transport:** streamable-http (same as other MCP servers)

---

## Architecture

```
Proxmox Host
├── memory-mcp-server (systemd, :9050)
│   ├── FastMCP tools: remember, recall, forget, reflect, memory_stats
│   ├── SQLite: data/memory.db (metadata, access tracking, TTL)
│   └── httpx → OpenRouter /embeddings endpoint
│
├── qdrant (systemd, :6333)
│   └── Collection: "memories" (1536-dim cosine)
│
├── calculator-server (systemd, :9003)
├── housekeeping-server (systemd, :9002)
├── ... other MCP servers
```

No Docker. Qdrant runs as a standalone binary via systemd, same as your other services.

---

## Project Structure

```
memory-mcp-server/
├── pyproject.toml
├── .env
├── .env.example
├── start.sh                     # Simple launcher
├── data/                        # Created at runtime
│   └── memory.db                # SQLite metadata
├── src/
│   └── memory_server/
│       ├── __init__.py
│       ├── __main__.py          # python -m memory_server
│       ├── config.py            # Pydantic settings
│       ├── server.py            # FastMCP + tool definitions
│       ├── embeddings.py        # OpenRouter embedding client
│       ├── vector_store.py      # Qdrant operations
│       ├── repository.py        # SQLite metadata store
│       └── maintenance.py       # TTL cleanup, importance decay
└── tests/
    ├── conftest.py
    ├── test_embeddings.py
    ├── test_vector_store.py
    ├── test_repository.py
    └── test_tools.py
```

---

## Phase 1: Scaffolding & Config

### pyproject.toml

```toml
[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

[project]
name = "memory-mcp-server"
version = "0.1.0"
description = "Semantic long-term memory MCP server"
requires-python = ">=3.11"
dependencies = [
    "fastmcp>=2.14.1",
    "httpx>=0.27.2",
    "qdrant-client>=1.9",
    "aiosqlite>=0.20.0",
    "pydantic-settings>=2.0",
]

[project.optional-dependencies]
dev = ["pytest", "pytest-anyio", "ruff"]

[tool.setuptools.package-dir]
"" = "src"
```

### .env.example

```bash
# OpenRouter (same key as your main backend)
OPENROUTER_API_KEY=sk-or-...

# Embedding model via OpenRouter
EMBEDDING_MODEL=openai/text-embedding-3-small
EMBEDDING_DIMENSIONS=1536

# Qdrant (running on same Proxmox host)
QDRANT_URL=http://127.0.0.1:6333
QDRANT_COLLECTION=memories

# Metadata database
MEMORY_DB_PATH=data/memory.db

# Server
MCP_HOST=0.0.0.0
MCP_PORT=9050
```

### config.py

```python
from pathlib import Path
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openrouter_api_key: SecretStr = Field(
        ..., validation_alias="OPENROUTER_API_KEY"
    )
    embedding_model: str = Field(
        default="openai/text-embedding-3-small",
        validation_alias="EMBEDDING_MODEL",
    )
    embedding_dimensions: int = Field(
        default=1536, validation_alias="EMBEDDING_DIMENSIONS"
    )

    qdrant_url: str = Field(
        default="http://127.0.0.1:6333", validation_alias="QDRANT_URL"
    )
    qdrant_collection: str = Field(
        default="memories", validation_alias="QDRANT_COLLECTION"
    )

    memory_db_path: Path = Field(
        default_factory=lambda: Path("data/memory.db"),
        validation_alias="MEMORY_DB_PATH",
    )

    mcp_host: str = Field(default="0.0.0.0", validation_alias="MCP_HOST")
    mcp_port: int = Field(default=9050, validation_alias="MCP_PORT")

    # Maintenance
    cleanup_interval_minutes: int = Field(default=15)
    decay_interval_hours: int = Field(default=24)
    min_importance_threshold: float = Field(default=0.1)
```

### __main__.py

```python
"""CLI entrypoint: python -m memory_server"""

from memory_server.server import run

if __name__ == "__main__":
    run()
```

---

## Phase 2: Embedding Client

### embeddings.py

```python
"""OpenRouter-based embedding client."""

from __future__ import annotations

import asyncio
from functools import lru_cache

import httpx

from memory_server.config import Settings


class EmbeddingClient:
    """Async client for generating embeddings via OpenRouter."""

    def __init__(self, settings: Settings) -> None:
        self._api_key = settings.openrouter_api_key.get_secret_value()
        self._model = settings.embedding_model
        self._dimensions = settings.embedding_dimensions
        self._url = "https://openrouter.ai/api/v1/embeddings"
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=15.0,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def embed(self, text: str) -> list[float]:
        """Embed a single text string. Returns a vector."""
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts in one API call (max ~100)."""
        client = await self._get_client()

        payload = {
            "model": self._model,
            "input": texts,
        }
        # Only include dimensions if the model supports it
        if "text-embedding-3" in self._model:
            payload["dimensions"] = self._dimensions

        # Retry with backoff
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await client.post(self._url, json=payload)
                response.raise_for_status()
                data = response.json()
                # Sort by index to guarantee order
                sorted_data = sorted(data["data"], key=lambda x: x["index"])
                return [item["embedding"] for item in sorted_data]
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)

        raise RuntimeError(f"Embedding failed after 3 attempts: {last_error}")

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
```

**Key points:**
- Uses the same OpenRouter API key you already have
- `openai/text-embedding-3-small` is $0.02 per 1M tokens — very cheap
- 1536 dimensions is a good balance of quality vs storage
- Batch support means you can embed multiple memories in one call
- 3 retries with exponential backoff

**OpenRouter embedding models available:**

| Model | Dimensions | Cost per 1M tokens | Notes |
|-------|-----------|-------------------|-------|
| `openai/text-embedding-3-small` | 1536 | $0.02 | Best value, recommended |
| `openai/text-embedding-3-large` | 3072 | $0.13 | Higher quality, 2x storage |
| `openai/text-embedding-ada-002` | 1536 | $0.10 | Legacy, no reason to use |

Stick with `text-embedding-3-small` unless you find recall quality insufficient.

---

## Phase 3: Vector Store Layer

### vector_store.py

```python
"""Qdrant vector store operations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    Range,
    ScoredPoint,
    VectorParams,
)

from memory_server.config import Settings


class VectorStore:
    """Manages the Qdrant memories collection."""

    def __init__(self, settings: Settings) -> None:
        self._client = AsyncQdrantClient(url=settings.qdrant_url)
        self._collection = settings.qdrant_collection
        self._dimensions = settings.embedding_dimensions

    async def ensure_collection(self) -> None:
        """Create collection and indexes if they don't exist."""
        collections = await self._client.get_collections()
        exists = any(
            c.name == self._collection for c in collections.collections
        )
        if not exists:
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(
                    size=self._dimensions,
                    distance=Distance.COSINE,
                ),
            )

        # Create payload indexes for filtering
        for field, schema in [
            ("user_id", PayloadSchemaType.KEYWORD),
            ("category", PayloadSchemaType.KEYWORD),
            ("tags", PayloadSchemaType.KEYWORD),
            ("session_id", PayloadSchemaType.KEYWORD),
            ("pinned", PayloadSchemaType.BOOL),
            ("importance", PayloadSchemaType.FLOAT),
            ("created_at", PayloadSchemaType.DATETIME),
            ("expires_at", PayloadSchemaType.DATETIME),
        ]:
            try:
                await self._client.create_payload_index(
                    collection_name=self._collection,
                    field_name=field,
                    field_schema=schema,
                )
            except Exception:
                pass  # Index already exists

    async def upsert(
        self,
        point_id: str,
        embedding: list[float],
        payload: dict[str, Any],
    ) -> None:
        """Store a memory vector with metadata."""
        await self._client.upsert(
            collection_name=self._collection,
            points=[
                PointStruct(
                    id=point_id,
                    vector=embedding,
                    payload=payload,
                )
            ],
        )

    async def search(
        self,
        query_embedding: list[float],
        *,
        user_id: str = "default",
        category: str | None = None,
        tags: list[str] | None = None,
        session_id: str | None = None,
        time_range_hours: int | None = None,
        limit: int = 10,
        min_score: float = 0.4,
    ) -> list[ScoredPoint]:
        """Semantic search with metadata filtering."""
        must_conditions = [
            FieldCondition(key="user_id", match=MatchValue(value=user_id)),
        ]

        # Exclude expired memories
        now_iso = datetime.now(timezone.utc).isoformat()
        must_not_conditions = [
            FieldCondition(
                key="expires_at",
                range=Range(lt=now_iso),
            ),
        ]

        if category:
            must_conditions.append(
                FieldCondition(key="category", match=MatchValue(value=category))
            )
        if session_id:
            must_conditions.append(
                FieldCondition(
                    key="session_id", match=MatchValue(value=session_id)
                )
            )
        if tags:
            for tag in tags:
                must_conditions.append(
                    FieldCondition(key="tags", match=MatchValue(value=tag))
                )
        if time_range_hours:
            cutoff = datetime.now(timezone.utc)
            cutoff = cutoff.replace(
                hour=cutoff.hour - time_range_hours
                if cutoff.hour >= time_range_hours
                else 0
            )
            from datetime import timedelta

            cutoff = datetime.now(timezone.utc) - timedelta(hours=time_range_hours)
            must_conditions.append(
                FieldCondition(
                    key="created_at",
                    range=Range(gte=cutoff.isoformat()),
                )
            )

        results = await self._client.query_points(
            collection_name=self._collection,
            query=query_embedding,
            query_filter=Filter(
                must=must_conditions,
                must_not=must_not_conditions,
            ),
            limit=limit,
            score_threshold=min_score,
            with_payload=True,
        )
        return results.points

    async def delete(self, point_ids: list[str]) -> None:
        """Delete specific memories by ID."""
        await self._client.delete(
            collection_name=self._collection,
            points_selector=point_ids,
        )

    async def delete_by_filter(self, filter_conditions: Filter) -> None:
        """Bulk delete by metadata filter."""
        await self._client.delete(
            collection_name=self._collection,
            points_selector=filter_conditions,
        )

    async def count(self, user_id: str = "default") -> int:
        """Count memories for a user."""
        result = await self._client.count(
            collection_name=self._collection,
            count_filter=Filter(
                must=[
                    FieldCondition(
                        key="user_id", match=MatchValue(value=user_id)
                    )
                ]
            ),
        )
        return result.count

    async def close(self) -> None:
        await self._client.close()
```

**Testing:** Qdrant client supports `AsyncQdrantClient(":memory:")` for tests — no running server needed.

---

## Phase 4: Metadata Repository

### repository.py

```python
"""SQLite metadata store for memory access tracking and TTL."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite


class MemoryRepository:
    """Tracks memory metadata, access patterns, and TTL."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_meta (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL DEFAULT 'default',
                session_id TEXT,
                category TEXT NOT NULL DEFAULT 'fact',
                content_preview TEXT,
                importance FLOAT DEFAULT 0.5,
                access_count INTEGER DEFAULT 0,
                pinned BOOLEAN DEFAULT 0,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                last_accessed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_meta_user
                ON memory_meta(user_id);
            CREATE INDEX IF NOT EXISTS idx_meta_session
                ON memory_meta(user_id, session_id);
            CREATE INDEX IF NOT EXISTS idx_meta_expires
                ON memory_meta(expires_at)
                WHERE expires_at IS NOT NULL;
            """
        )
        await self._conn.commit()

    async def insert(
        self,
        memory_id: str,
        user_id: str,
        category: str,
        content_preview: str,
        importance: float,
        pinned: bool,
        session_id: str | None = None,
        expires_at: str | None = None,
    ) -> None:
        assert self._conn is not None
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """
            INSERT INTO memory_meta
                (id, user_id, session_id, category, content_preview,
                 importance, pinned, created_at, expires_at, last_accessed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory_id, user_id, session_id, category,
                content_preview[:200], importance, pinned,
                now, expires_at, now,
            ),
        )
        await self._conn.commit()

    async def record_access(self, memory_ids: list[str]) -> None:
        """Increment access count and update last_accessed_at."""
        assert self._conn is not None
        now = datetime.now(timezone.utc).isoformat()
        for mid in memory_ids:
            await self._conn.execute(
                """
                UPDATE memory_meta
                SET access_count = access_count + 1,
                    last_accessed_at = ?
                WHERE id = ?
                """,
                (now, mid),
            )
        await self._conn.commit()

    async def delete(self, memory_ids: list[str]) -> int:
        assert self._conn is not None
        placeholders = ",".join("?" for _ in memory_ids)
        cursor = await self._conn.execute(
            f"DELETE FROM memory_meta WHERE id IN ({placeholders})",
            memory_ids,
        )
        await self._conn.commit()
        return cursor.rowcount

    async def delete_by_session(
        self, session_id: str, include_pinned: bool = False
    ) -> list[str]:
        """Delete memories for a session. Returns deleted IDs."""
        assert self._conn is not None
        query = "SELECT id FROM memory_meta WHERE session_id = ?"
        if not include_pinned:
            query += " AND pinned = 0"
        cursor = await self._conn.execute(query, (session_id,))
        rows = await cursor.fetchall()
        ids = [row["id"] for row in rows]
        if ids:
            await self.delete(ids)
        return ids

    async def get_expired(self) -> list[str]:
        """Return IDs of expired memories."""
        assert self._conn is not None
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self._conn.execute(
            "SELECT id FROM memory_meta WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        )
        rows = await cursor.fetchall()
        return [row["id"] for row in rows]

    async def get_stale(
        self, min_importance: float, max_access: int = 0
    ) -> list[str]:
        """Return IDs of low-importance, never-accessed memories."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT id FROM memory_meta
            WHERE pinned = 0
              AND importance < ?
              AND access_count <= ?
            """,
            (min_importance, max_access),
        )
        rows = await cursor.fetchall()
        return [row["id"] for row in rows]

    async def decay_importance(self, factor: float = 0.95, min_age_days: int = 7) -> int:
        """Reduce importance of old non-pinned memories."""
        assert self._conn is not None
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=min_age_days)).isoformat()
        cursor = await self._conn.execute(
            """
            UPDATE memory_meta
            SET importance = importance * ?
            WHERE pinned = 0 AND created_at < ?
            """,
            (factor, cutoff),
        )
        await self._conn.commit()
        return cursor.rowcount

    async def stats(self, user_id: str = "default") -> dict:
        """Return memory statistics."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN pinned = 1 THEN 1 ELSE 0 END) as pinned,
                SUM(access_count) as total_accesses,
                MIN(created_at) as oldest,
                MAX(created_at) as newest
            FROM memory_meta WHERE user_id = ?
            """,
            (user_id,),
        )
        row = await cursor.fetchone()

        # Category breakdown
        cat_cursor = await self._conn.execute(
            """
            SELECT category, COUNT(*) as count
            FROM memory_meta WHERE user_id = ?
            GROUP BY category
            """,
            (user_id,),
        )
        categories = {r["category"]: r["count"] for r in await cat_cursor.fetchall()}

        return {
            "total": row["total"],
            "pinned": row["pinned"],
            "total_accesses": row["total_accesses"],
            "oldest": row["oldest"],
            "newest": row["newest"],
            "by_category": categories,
        }

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
```

---

## Phase 5: MCP Tools (server.py)

```python
"""Memory MCP server — semantic long-term memory."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastmcp import FastMCP

from memory_server.config import Settings
from memory_server.embeddings import EmbeddingClient
from memory_server.repository import MemoryRepository
from memory_server.vector_store import VectorStore

mcp = FastMCP("memory")

# Initialized at startup
_settings: Settings | None = None
_embeddings: EmbeddingClient | None = None
_vectors: VectorStore | None = None
_repo: MemoryRepository | None = None


async def _startup() -> None:
    """Initialize all components."""
    global _settings, _embeddings, _vectors, _repo

    _settings = Settings()
    _embeddings = EmbeddingClient(_settings)
    _vectors = VectorStore(_settings)
    _repo = MemoryRepository(_settings.memory_db_path)

    await _vectors.ensure_collection()
    await _repo.initialize()

    print("[MEMORY] Server initialized", file=sys.stderr, flush=True)


async def _shutdown() -> None:
    """Clean up resources."""
    if _embeddings:
        await _embeddings.close()
    if _vectors:
        await _vectors.close()
    if _repo:
        await _repo.close()


# ── Tools ──────────────────────────────────────────────


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
    assert _embeddings and _vectors and _repo

    memory_id = str(uuid4())
    embedding = await _embeddings.embed(content)

    now = datetime.now(timezone.utc)
    expires_at = (
        (now + timedelta(hours=ttl_hours)).isoformat()
        if ttl_hours
        else None
    )

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

    await _vectors.upsert(memory_id, embedding, payload)
    await _repo.insert(
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
        "content_preview": content[:100] + ("…" if len(content) > 100 else ""),
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
        min_similarity: Minimum cosine similarity threshold (0.0–1.0).
    """
    assert _embeddings and _vectors and _repo

    query_embedding = await _embeddings.embed(query)

    results = await _vectors.search(
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

    # Track access
    accessed_ids = [str(r.id) for r in results]
    await _repo.record_access(accessed_ids)

    memories = []
    for result in results:
        payload = result.payload or {}
        memories.append({
            "memory_id": str(result.id),
            "content": payload.get("content", ""),
            "category": payload.get("category", "unknown"),
            "tags": payload.get("tags", []),
            "similarity": round(result.score, 4),
            "importance": payload.get("importance", 0.5),
            "created_at": payload.get("created_at"),
            "session_id": payload.get("session_id"),
            "pinned": payload.get("pinned", False),
        })

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
    assert _vectors and _repo

    if not any([memory_id, session_id, category, older_than_hours]):
        return {
            "success": False,
            "error": "Specify at least one filter (memory_id, session_id, category, or older_than_hours).",
        }

    deleted_ids: list[str] = []

    if memory_id:
        # Delete single memory
        await _vectors.delete([memory_id])
        await _repo.delete([memory_id])
        deleted_ids.append(memory_id)

    elif session_id:
        # Delete all for session
        ids = await _repo.delete_by_session(session_id, include_pinned=include_pinned)
        if ids:
            await _vectors.delete(ids)
        deleted_ids.extend(ids)

    # Additional filters can be combined with the above
    # (category, older_than_hours would need corresponding repo methods)

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
        summary: Your distillation of the conversation's key points.
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
        "message": f"Session summary stored as episode memory (pinned).",
    }


@mcp.tool(
    "memory_stats",
    description="Show statistics about stored memories: total count, categories, oldest/newest.",
)
async def memory_stats() -> dict[str, Any]:
    """Return memory store statistics."""
    assert _repo and _vectors

    db_stats = await _repo.stats()
    vector_count = await _vectors.count()

    return {
        "success": True,
        "vector_count": vector_count,
        **db_stats,
    }


# ── Server lifecycle ───────────────────────────────────


def run() -> None:
    """Run the memory MCP server."""
    settings = Settings()

    # Run startup in the event loop before serving
    import asyncio

    async def _init_and_run():
        await _startup()
        # Start maintenance tasks
        from memory_server.maintenance import start_maintenance
        start_maintenance(_repo, _vectors, settings)

    asyncio.get_event_loop().run_until_complete(_init_and_run())

    mcp.run(
        transport="streamable-http",
        host=settings.mcp_host,
        port=settings.mcp_port,
        json_response=True,
        stateless_http=True,
        uvicorn_config={"access_log": False},
    )
```

---

## Phase 6: Maintenance (maintenance.py)

```python
"""Background maintenance tasks: TTL cleanup, importance decay."""

from __future__ import annotations

import asyncio
import sys

from memory_server.config import Settings
from memory_server.repository import MemoryRepository
from memory_server.vector_store import VectorStore


async def _cleanup_loop(
    repo: MemoryRepository,
    vectors: VectorStore,
    interval_minutes: int,
    min_importance: float,
) -> None:
    """Periodically remove expired and stale memories."""
    while True:
        await asyncio.sleep(interval_minutes * 60)
        try:
            # Remove expired
            expired_ids = await repo.get_expired()
            if expired_ids:
                await vectors.delete(expired_ids)
                await repo.delete(expired_ids)
                print(
                    f"[MEMORY-MAINT] Cleaned {len(expired_ids)} expired memories",
                    file=sys.stderr, flush=True,
                )

            # Remove stale (low importance, never accessed)
            stale_ids = await repo.get_stale(min_importance)
            if stale_ids:
                await vectors.delete(stale_ids)
                await repo.delete(stale_ids)
                print(
                    f"[MEMORY-MAINT] Cleaned {len(stale_ids)} stale memories",
                    file=sys.stderr, flush=True,
                )
        except Exception as exc:
            print(
                f"[MEMORY-MAINT] Cleanup error: {exc}",
                file=sys.stderr, flush=True,
            )


async def _decay_loop(
    repo: MemoryRepository,
    interval_hours: int,
) -> None:
    """Periodically decay importance of old memories."""
    while True:
        await asyncio.sleep(interval_hours * 3600)
        try:
            count = await repo.decay_importance()
            if count:
                print(
                    f"[MEMORY-MAINT] Decayed importance for {count} memories",
                    file=sys.stderr, flush=True,
                )
        except Exception as exc:
            print(
                f"[MEMORY-MAINT] Decay error: {exc}",
                file=sys.stderr, flush=True,
            )


def start_maintenance(
    repo: MemoryRepository,
    vectors: VectorStore,
    settings: Settings,
) -> None:
    """Launch maintenance background tasks."""
    loop = asyncio.get_event_loop()
    loop.create_task(
        _cleanup_loop(
            repo, vectors,
            settings.cleanup_interval_minutes,
            settings.min_importance_threshold,
        )
    )
    loop.create_task(
        _decay_loop(repo, settings.decay_interval_hours)
    )
    print("[MEMORY-MAINT] Maintenance tasks started", file=sys.stderr, flush=True)
```

---

## Phase 7: Deployment on Proxmox

### Install Qdrant (standalone binary, no Docker)

```bash
# On Proxmox host (or LXC)
curl -L https://github.com/qdrant/qdrant/releases/latest/download/qdrant-x86_64-unknown-linux-gnu.tar.gz \
  | tar xz -C /opt/qdrant/

# Create data directory
mkdir -p /var/lib/qdrant/storage

# Create systemd service
cat > /etc/systemd/system/qdrant.service << 'EOF'
[Unit]
Description=Qdrant Vector Database
After=network.target

[Service]
Type=simple
ExecStart=/opt/qdrant/qdrant --storage-path /var/lib/qdrant/storage
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now qdrant

# Verify
curl http://127.0.0.1:6333/healthz
```

### Install Memory Server

```bash
# Clone your repo
cd /opt
git clone <your-repo-url> memory-mcp-server
cd memory-mcp-server

# Set up Python environment
uv venv
uv sync

# Configure
cp .env.example .env
# Edit .env — set your OPENROUTER_API_KEY

# Test it runs
uv run python -m memory_server

# Create systemd service
cat > /etc/systemd/system/memory-mcp-server.service << 'EOF'
[Unit]
Description=Memory MCP Server
After=network.target qdrant.service
Wants=qdrant.service

[Service]
Type=simple
WorkingDirectory=/opt/memory-mcp-server
ExecStart=/opt/memory-mcp-server/.venv/bin/python -m memory_server
Restart=always
RestartSec=5
EnvironmentFile=/opt/memory-mcp-server/.env

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now memory-mcp-server

# Verify
curl http://127.0.0.1:9050/mcp
```

### start.sh (for manual runs / development)

```bash
#!/bin/bash
set -e
cd "$(dirname "${BASH_SOURCE[0]}")"
uv run python -m memory_server
```

---

## Phase 8: Connect to Backend

Add to `data/mcp_servers.json` on your Backend_FastAPI:

```json
{
  "id": "memory",
  "url": "http://192.168.1.110:9050/mcp",
  "enabled": true,
  "disabled_tools": []
}
```

The tools (`remember`, `recall`, `forget`, `reflect`, `memory_stats`) appear automatically through MCP aggregation. No code changes needed in the backend.

### System Prompt Addition

Add to your system prompt so the LLM knows about the memory tools:

```
You have access to long-term memory that persists across conversations:
- Use `recall` to search your memory BEFORE answering questions about past
  interactions, user preferences, or previously discussed topics.
- Use `remember` to store important facts, user preferences, corrections,
  or instructions the user gives you.
- Use `reflect` at the end of meaningful conversations to save a summary.
- Use `forget` when the user explicitly asks you to forget something.
- Use `memory_stats` to check how many memories are stored.

Categories: fact, preference, summary, instruction, episode.
Memories marked as pinned persist permanently.
```

---

## Implementation Order

| Phase | What | Depends On |
|-------|------|------------|
| 1 | Scaffolding, config, pyproject.toml | Nothing |
| 2 | Embedding client (embeddings.py) | Phase 1 |
| 3 | Vector store (vector_store.py) | Phase 1 |
| 4 | Metadata repo (repository.py) | Phase 1 |
| 5 | MCP tools (server.py) | Phases 2 + 3 + 4 |
| 6 | Maintenance (maintenance.py) | Phases 3 + 4 |
| 7 | Deploy to Proxmox (systemd) | Phase 5 |
| 8 | Connect to Backend_FastAPI | Phase 7 |

Phases 2, 3, and 4 are independent — build them in any order or in parallel.
