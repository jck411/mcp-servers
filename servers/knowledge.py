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
import base64
import binascii
import hashlib
import json
import re
import secrets
import sys
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
import httpx
from fastmcp import FastMCP
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Condition,
    Distance,
    FieldCondition,
    Filter,
    Fusion,
    FusionQuery,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    Prefetch,
    ScoredPoint,
    SparseIndexParams,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from servers.knowledge_source_files import (
    resolve_source_path,
    sanitize_source_filename,
    source_chunk_export_bytes,
    source_media_type,
    source_relative_path,
)

# Default port for HTTP transport
DEFAULT_HTTP_PORT = 9017

PROJECT_ROOT = Path(__file__).resolve().parent.parent

mcp = FastMCP("knowledge")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class KnowledgeSettings(BaseSettings):
    """Knowledge server configuration from environment variables."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Knowledge storage
    knowledge_path: Path = Field(
        default_factory=lambda: PROJECT_ROOT / "knowledge",
        validation_alias="KNOWLEDGE_PATH",
    )

    # OpenRouter embedding API
    openrouter_api_key: str = Field(..., validation_alias="OPENROUTER_API_KEY")
    embedding_model: str = Field(
        default="openai/text-embedding-3-small",
        validation_alias="EMBEDDING_MODEL",
    )
    embedding_dimensions: int = Field(default=1536, validation_alias="EMBEDDING_DIMENSIONS")

    # Qdrant vector store
    qdrant_url: str = Field(default="http://127.0.0.1:6333", validation_alias="QDRANT_URL")
    qdrant_collection: str = Field(
        default="knowledge", validation_alias="KNOWLEDGE_QDRANT_COLLECTION"
    )

    # SQLite database
    db_path: Path = Field(
        default_factory=lambda: PROJECT_ROOT / "data" / "knowledge.db",
        validation_alias="KNOWLEDGE_DB_PATH",
    )

    # Chunking
    chunk_max_chars: int = Field(default=1000, validation_alias="KNOWLEDGE_CHUNK_MAX_CHARS")
    chunk_overlap: int = Field(default=200, validation_alias="KNOWLEDGE_CHUNK_OVERLAP")

    # OCR for images and scanned PDFs
    ocr_enabled: bool = Field(default=True, validation_alias="KNOWLEDGE_OCR_ENABLED")
    ocr_language: str = Field(default="eng", validation_alias="KNOWLEDGE_OCR_LANGUAGE")

    # Vision LLM used for high-accuracy OCR (set to empty to disable and use tesseract).
    # Any OpenRouter vision-capable model id works, e.g.:
    #   google/gemini-2.0-flash-001  (cheap, fast, very good)
    #   anthropic/claude-3.5-sonnet   (best on dense docs/handwriting)
    #   openai/gpt-4o-mini            (cheap)
    vision_model: str = Field(
        default="google/gemini-2.0-flash-001",
        validation_alias="KNOWLEDGE_VISION_MODEL",
    )
    vision_max_pages: int = Field(default=20, validation_alias="KNOWLEDGE_VISION_MAX_PAGES")
    vision_dpi: int = Field(default=200, validation_alias="KNOWLEDGE_VISION_DPI")

    # Model for single-shot fact extraction via POST /api/sources/{id}/extract.
    # Must be a vision-capable model; Sonnet gives best accuracy on documents.
    extraction_model: str = Field(
        default="anthropic/claude-sonnet-4-5",
        validation_alias="KNOWLEDGE_EXTRACTION_MODEL",
    )

    # Public REST API base used when MCP tools generate clickable download URLs
    api_base: str = Field(
        default="https://api-knowledge.jackshome.com",
        validation_alias="API_BASE",
    )

# ---------------------------------------------------------------------------
# Embedding Client
# ---------------------------------------------------------------------------


class EmbeddingClient:
    """Generate text embeddings via OpenRouter API."""

    def __init__(self, settings: KnowledgeSettings) -> None:
        self._api_key = settings.openrouter_api_key
        self._model = settings.embedding_model
        self._dimensions = settings.embedding_dimensions
        self._url = "https://openrouter.ai/api/v1/embeddings"
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def embed(self, text: str) -> list[float]:
        """Embed a single text string."""
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts in one API call."""
        if not texts:
            return []
        client = await self._get_client()
        payload: dict = {"model": self._model, "input": texts}
        if "text-embedding-3" in self._model:
            payload["dimensions"] = self._dimensions

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await client.post(self._url, json=payload)
                response.raise_for_status()
                data = response.json()
                if "data" not in data:
                    err = data.get("error", data)
                    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    raise RuntimeError(f"Embedding API error: {msg}")
                sorted_data = sorted(data["data"], key=lambda x: x["index"])
                return [item["embedding"] for item in sorted_data]
            except (httpx.HTTPStatusError, httpx.TransportError, RuntimeError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2**attempt)

        raise RuntimeError(f"Embedding failed after 3 attempts: {last_error}")

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# ---------------------------------------------------------------------------
# BM25 Sparse Encoder
# ---------------------------------------------------------------------------


class BM25SparseEncoder:
    """BM25-based sparse vectors for hybrid search via feature hashing."""

    def __init__(self, vocab_size: int = 30000) -> None:
        self._vocab_size = vocab_size
        self._k1 = 1.5
        self._b = 0.75
        self._doc_count = 0
        self._doc_freqs: Counter[int] = Counter()
        self._avg_doc_len = 0.0
        self._total_doc_len = 0

    def _tokenize(self, text: str) -> list[str]:
        text = text.lower()
        tokens = re.findall(r"\b[a-z0-9]+\b", text)
        return [t for t in tokens if len(t) > 1]

    def _hash_token(self, token: str) -> int:
        h = hashlib.sha256(token.encode()).digest()
        return int.from_bytes(h[:4], "little") % self._vocab_size

    def fit_batch(self, texts: list[str]) -> None:
        for text in texts:
            tokens = self._tokenize(text)
            self._doc_count += 1
            self._total_doc_len += len(tokens)
            unique_indices = set(self._hash_token(t) for t in tokens)
            for idx in unique_indices:
                self._doc_freqs[idx] += 1
        if self._doc_count > 0:
            self._avg_doc_len = self._total_doc_len / self._doc_count

    def encode(self, text: str) -> tuple[list[int], list[float]]:
        tokens = self._tokenize(text)
        if not tokens:
            return [], []
        doc_len = len(tokens)
        term_freqs: Counter[int] = Counter()
        for token in tokens:
            term_freqs[self._hash_token(token)] += 1

        indices = []
        values = []
        for idx, tf in term_freqs.items():
            tf_score = (tf * (self._k1 + 1)) / (
                tf + self._k1 * (1 - self._b + self._b * doc_len / max(self._avg_doc_len, 1))
            )
            df = self._doc_freqs.get(idx, 0)
            idf = max(0.0, (self._doc_count - df + 0.5) / (df + 0.5))
            if idf > 0:
                idf = (idf + 1.0) ** 0.5
            score = tf_score * idf
            if score > 0:
                indices.append(idx)
                values.append(float(score))

        if indices:
            sorted_pairs = sorted(zip(indices, values, strict=True), key=lambda x: x[0])
            indices, values = zip(*sorted_pairs, strict=True)
            return list(indices), list(values)
        return [], []

    def encode_query(self, text: str) -> tuple[list[int], list[float]]:
        return self.encode(text)


# ---------------------------------------------------------------------------
# Knowledge Store (SQLite)
# ---------------------------------------------------------------------------


class KnowledgeDB:
    """SQLite store for domains, facts, and source tracking."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=10000")
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS domains (
                name TEXT PRIMARY KEY,
                description TEXT NOT NULL DEFAULT '',
                related_domains TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS facts (
                id TEXT PRIMARY KEY,
                domain TEXT NOT NULL REFERENCES domains(name),
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                source TEXT,
                confidence REAL NOT NULL DEFAULT 1.0,
                valid_from TEXT,
                valid_until TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_domain_key
                ON facts(domain, key);
            CREATE INDEX IF NOT EXISTS idx_facts_domain
                ON facts(domain);

            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY,
                domain TEXT NOT NULL REFERENCES domains(name),
                source_type TEXT NOT NULL,
                filename TEXT,
                content_hash TEXT,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                ingested_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sources_domain
                ON sources(domain);
            CREATE INDEX IF NOT EXISTS idx_sources_hash
                ON sources(content_hash);

            CREATE TABLE IF NOT EXISTS curation_items (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                risk TEXT NOT NULL DEFAULT 'medium',
                confidence REAL NOT NULL DEFAULT 0.0,
                title TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                source_refs TEXT NOT NULL DEFAULT '[]',
                proposed_actions TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                reviewed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_curation_status
                ON curation_items(status);
            CREATE INDEX IF NOT EXISTS idx_curation_kind
                ON curation_items(kind);

            CREATE TABLE IF NOT EXISTS download_tokens (
                token TEXT PRIMARY KEY,
                source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_download_tokens_source
                ON download_tokens(source_id);
            CREATE INDEX IF NOT EXISTS idx_download_tokens_expires
                ON download_tokens(expires_at);

        """)
        await self._ensure_source_metadata_columns()
        await self._conn.commit()

    async def _ensure_source_metadata_columns(self) -> None:
        """Add raw-file metadata columns to older Knowledge databases."""
        assert self._conn is not None
        cursor = await self._conn.execute("PRAGMA table_info(sources)")
        existing = {str(row["name"]) for row in await cursor.fetchall()}
        additions = {
            "stored_path": "TEXT",
            "media_type": "TEXT",
            "size_bytes": "INTEGER",
        }
        for column, declaration in additions.items():
            if column not in existing:
                await self._conn.execute(
                    f"ALTER TABLE sources ADD COLUMN {column} {declaration}"  # noqa: S608
                )

    # -- Domains --

    async def domain_create(
        self, name: str, description: str, related_domains: list[str]
    ) -> bool:
        """Create a domain. Returns False if it already exists."""
        assert self._conn is not None
        import json

        try:
            await self._conn.execute(
                "INSERT INTO domains (name, description, related_domains, created_at) "
                "VALUES (?, ?, ?, ?)",
                (name, description, json.dumps(related_domains), datetime.now(UTC).isoformat()),
            )
            await self._conn.commit()
            return True
        except aiosqlite.IntegrityError:
            await self._conn.rollback()
            return False

    async def domain_list(self) -> list[dict[str, Any]]:
        assert self._conn is not None
        import json

        cursor = await self._conn.execute(
            "SELECT name, description, related_domains, created_at, archived FROM domains"
        )
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            results.append({
                "name": row["name"],
                "description": row["description"],
                "related_domains": json.loads(row["related_domains"]),
                "created_at": row["created_at"],
                "archived": bool(row["archived"]),
            })
        return results

    async def domain_get(self, name: str) -> dict[str, Any] | None:
        assert self._conn is not None
        import json

        cursor = await self._conn.execute(
            "SELECT name, description, related_domains, created_at, archived "
            "FROM domains WHERE name = ?",
            (name,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "name": row["name"],
            "description": row["description"],
            "related_domains": json.loads(row["related_domains"]),
            "created_at": row["created_at"],
            "archived": bool(row["archived"]),
        }

    async def domain_update_related(self, name: str, related_domains: list[str]) -> bool:
        assert self._conn is not None
        import json

        cursor = await self._conn.execute(
            "UPDATE domains SET related_domains = ? WHERE name = ?",
            (json.dumps(related_domains), name),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def domain_archive(self, name: str) -> bool:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "UPDATE domains SET archived = 1 WHERE name = ? AND archived = 0",
            (name,),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def domain_exists(self, name: str) -> bool:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT 1 FROM domains WHERE name = ?", (name,)
        )
        return await cursor.fetchone() is not None

    # -- Facts --

    async def fact_set(
        self,
        domain: str,
        key: str,
        value: str,
        source: str | None = None,
        confidence: float = 1.0,
        valid_from: str | None = None,
        valid_until: str | None = None,
    ) -> str:
        """Set a fact. Upserts by (domain, key). Returns fact ID."""
        assert self._conn is not None
        now = datetime.now(UTC).isoformat()
        fact_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{domain}:{key}"))

        await self._conn.execute(
            """
            INSERT INTO facts (id, domain, key, value, source, confidence,
                               valid_from, valid_until, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(domain, key) DO UPDATE SET
                value = excluded.value,
                source = excluded.source,
                confidence = excluded.confidence,
                valid_from = excluded.valid_from,
                valid_until = excluded.valid_until,
                updated_at = excluded.updated_at
            """,
            (fact_id, domain, key, value, source, confidence,
             valid_from, valid_until, now, now),
        )
        await self._conn.commit()
        return fact_id

    async def fact_get(self, domain: str, key: str) -> dict[str, Any] | None:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM facts WHERE domain = ? AND key = ?", (domain, key)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return dict(row)

    async def fact_delete(self, domain: str, key: str) -> bool:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "DELETE FROM facts WHERE domain = ? AND key = ?", (domain, key)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def facts_list(self, domain: str) -> list[dict[str, Any]]:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT key, value, source, confidence, valid_from, valid_until, updated_at "
            "FROM facts WHERE domain = ? ORDER BY key",
            (domain,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def facts_search(self, domains: list[str], keys: list[str]) -> list[dict[str, Any]]:
        """Search facts across multiple domains by key substring match."""
        assert self._conn is not None
        placeholders_d = ",".join("?" for _ in domains)
        conditions = [f"domain IN ({placeholders_d})"]
        params: list[Any] = list(domains)

        if keys:
            key_conditions = []
            for k in keys:
                key_conditions.append("key LIKE ?")
                params.append(f"%{k}%")
            conditions.append(f"({' OR '.join(key_conditions)})")

        where = " AND ".join(conditions)
        cursor = await self._conn.execute(
            f"SELECT domain, key, value, source, confidence, valid_from, valid_until, updated_at "  # noqa: S608
            f"FROM facts WHERE {where} ORDER BY domain, key",
            params,
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # -- Sources --

    async def source_exists(self, content_hash: str) -> bool:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT 1 FROM sources WHERE content_hash = ?", (content_hash,)
        )
        return await cursor.fetchone() is not None

    async def source_get_by_hash(self, content_hash: str) -> dict[str, Any] | None:
        """Return the first existing source row matching this content hash, if any."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT id, domain, source_type, filename, content_hash, chunk_count,
                   ingested_at, stored_path, media_type, size_bytes
            FROM sources WHERE content_hash = ?
            ORDER BY ingested_at ASC LIMIT 1
            """,
            (content_hash,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def source_get_by_filename(self, domain: str, filename: str) -> dict[str, Any] | None:
        """Return the most-recent source row matching domain + filename, if any."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT id, domain, source_type, filename, content_hash, chunk_count,
                   ingested_at, stored_path, media_type, size_bytes
            FROM sources WHERE domain = ? AND filename = ?
            ORDER BY ingested_at DESC LIMIT 1
            """,
            (domain, filename),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def source_update_chunk_count(self, source_id: str, chunk_count: int) -> bool:
        """Update chunk_count for an existing source row."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "UPDATE sources SET chunk_count = ? WHERE id = ?",
            (chunk_count, source_id),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def source_update_storage(
        self,
        source_id: str,
        *,
        stored_path: str,
        media_type: str | None,
        size_bytes: int | None,
        domain: str | None = None,
    ) -> bool:
        """Backfill stored_path / media_type / size_bytes for an existing row."""
        assert self._conn is not None
        if domain is not None:
            cursor = await self._conn.execute(
                """
                UPDATE sources
                SET stored_path = ?, media_type = ?, size_bytes = ?, domain = ?
                WHERE id = ?
                """,
                (stored_path, media_type, size_bytes, domain, source_id),
            )
        else:
            cursor = await self._conn.execute(
                """
                UPDATE sources
                SET stored_path = ?, media_type = ?, size_bytes = ?
                WHERE id = ?
                """,
                (stored_path, media_type, size_bytes, source_id),
            )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def source_add(
        self,
        source_id: str,
        domain: str,
        source_type: str,
        filename: str | None,
        content_hash: str,
        chunk_count: int,
        stored_path: str | None = None,
        media_type: str | None = None,
        size_bytes: int | None = None,
    ) -> None:
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO sources
            (id, domain, source_type, filename, content_hash, chunk_count,
             ingested_at, stored_path, media_type, size_bytes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source_id, domain, source_type, filename, content_hash,
             chunk_count, datetime.now(UTC).isoformat(), stored_path, media_type, size_bytes),
        )
        await self._conn.commit()

    async def source_remove(self, source_id: str) -> bool:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "DELETE FROM sources WHERE id = ?", (source_id,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def source_get(self, source_id: str) -> dict[str, Any] | None:
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT s.id, s.domain, s.source_type, s.filename, s.content_hash,
                   s.chunk_count, s.ingested_at, s.stored_path, s.media_type,
                   s.size_bytes
            FROM sources s
            WHERE s.id = ?
            """,
            (source_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def source_get_by_domain_filename(
        self, domain: str, filename: str
    ) -> dict[str, Any] | None:
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT s.id, s.domain, s.source_type, s.filename, s.content_hash,
                   s.chunk_count, s.ingested_at, s.stored_path, s.media_type,
                   s.size_bytes
            FROM sources s
            WHERE s.domain = ? AND s.filename = ?
            ORDER BY s.ingested_at DESC
            LIMIT 1
            """,
            (domain, filename),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def source_rename(
        self,
        source_id: str,
        filename: str,
        stored_path: str | None = None,
    ) -> bool:
        assert self._conn is not None
        if stored_path is None:
            cursor = await self._conn.execute(
                "UPDATE sources SET filename = ? WHERE id = ?",
                (filename, source_id),
            )
        else:
            cursor = await self._conn.execute(
                "UPDATE sources SET filename = ?, stored_path = ? WHERE id = ?",
                (filename, stored_path, source_id),
            )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def sources_list(self, domain: str) -> list[dict[str, Any]]:
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT s.id, s.source_type, s.filename, s.content_hash,
                   s.chunk_count, s.ingested_at, s.stored_path, s.media_type,
                   s.size_bytes
            FROM sources s
            WHERE s.domain = ?
            ORDER BY s.ingested_at DESC
            """,
            (domain,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def sources_referencing_file(
        self,
        *,
        stored_paths: list[str],
        domain: str | None,
        filename: str | None,
        exclude_source_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return source rows that would resolve to the same raw file."""
        assert self._conn is not None
        conditions: list[str] = []
        params: list[Any] = []

        unique_paths = [path for path in dict.fromkeys(stored_paths) if path]
        if unique_paths:
            placeholders = ",".join("?" for _ in unique_paths)
            conditions.append(f"s.stored_path IN ({placeholders})")
            params.extend(unique_paths)

        if domain and filename:
            conditions.append("(s.stored_path IS NULL AND s.domain = ? AND s.filename = ?)")
            params.extend([domain, filename])

        if not conditions:
            return []

        where = f"({' OR '.join(conditions)})"
        if exclude_source_id:
            where += " AND s.id != ?"
            params.append(exclude_source_id)

        cursor = await self._conn.execute(
            f"""
            SELECT s.id, s.domain, s.source_type, s.filename, s.content_hash,
                   s.chunk_count, s.ingested_at, s.stored_path, s.media_type,
                   s.size_bytes
            FROM sources s
            WHERE {where}
            ORDER BY s.ingested_at DESC
            """,  # noqa: S608
            params,
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def download_token_create(self, source_id: str, ttl_seconds: int = 900) -> dict[str, Any]:
        assert self._conn is not None
        ttl = max(60, min(int(ttl_seconds or 900), 86400))
        now = datetime.now(UTC)
        expires_at = now.timestamp() + ttl
        token = secrets.token_urlsafe(32)
        await self._conn.execute(
            """
            INSERT INTO download_tokens (token, source_id, expires_at, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                token,
                source_id,
                datetime.fromtimestamp(expires_at, UTC).isoformat(),
                now.isoformat(),
            ),
        )
        await self._conn.commit()
        return {
            "token": token,
            "source_id": source_id,
            "expires_at": datetime.fromtimestamp(expires_at, UTC).isoformat(),
            "ttl_seconds": ttl,
        }

    async def download_token_get(self, token: str) -> dict[str, Any] | None:
        assert self._conn is not None
        now = datetime.now(UTC).isoformat()
        await self._conn.execute("DELETE FROM download_tokens WHERE expires_at < ?", (now,))
        cursor = await self._conn.execute(
            """
            SELECT token, source_id, expires_at, created_at
            FROM download_tokens
            WHERE token = ? AND expires_at >= ?
            """,
            (token, now),
        )
        row = await cursor.fetchone()
        await self._conn.commit()
        return dict(row) if row else None

    # -- Curation Queue --

    @staticmethod
    def _decode_curation_row(row: aiosqlite.Row) -> dict[str, Any]:
        item = dict(row)
        for key in ("source_refs", "proposed_actions"):
            try:
                item[key] = json.loads(item[key] or "[]")
            except json.JSONDecodeError:
                item[key] = []
        return item

    async def curation_upsert(
        self,
        *,
        kind: str,
        title: str,
        summary: str = "",
        source_refs: list[dict[str, Any]] | None = None,
        proposed_actions: list[dict[str, Any]] | None = None,
        risk: str = "medium",
        confidence: float = 0.0,
        item_id: str | None = None,
        status: str = "pending",
        created_at: str | None = None,
    ) -> str:
        """Create or replace a curation queue item."""
        assert self._conn is not None
        curation_id = item_id or str(uuid.uuid4())
        now = created_at or datetime.now(UTC).isoformat()
        await self._conn.execute(
            """
            INSERT INTO curation_items
                (id, kind, status, risk, confidence, title, summary, source_refs,
                 proposed_actions, created_at, reviewed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(id) DO UPDATE SET
                kind = excluded.kind,
                status = excluded.status,
                risk = excluded.risk,
                confidence = excluded.confidence,
                title = excluded.title,
                summary = excluded.summary,
                source_refs = excluded.source_refs,
                proposed_actions = excluded.proposed_actions,
                created_at = excluded.created_at,
                reviewed_at = CASE
                    WHEN excluded.status = 'pending' THEN NULL
                    ELSE curation_items.reviewed_at
                END
            """,
            (
                curation_id,
                kind,
                status,
                risk,
                confidence,
                title,
                summary,
                json.dumps(source_refs or []),
                json.dumps(proposed_actions or []),
                now,
            ),
        )
        await self._conn.commit()
        return curation_id

    async def curation_get(self, item_id: str) -> dict[str, Any] | None:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM curation_items WHERE id = ?",
            (item_id,),
        )
        row = await cursor.fetchone()
        return self._decode_curation_row(row) if row else None

    async def curation_list(
        self,
        *,
        status: str | None = "pending",
        kind: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        assert self._conn is not None
        limit = max(1, min(limit, 200))
        conditions = []
        params: list[Any] = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if kind:
            conditions.append("kind = ?")
            params.append(kind)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._conn.execute(
            f"SELECT * FROM curation_items {where} ORDER BY created_at DESC LIMIT ?",  # noqa: S608
            [*params, limit],
        )
        rows = await cursor.fetchall()
        return [self._decode_curation_row(row) for row in rows]

    async def curation_mark_status(self, item_id: str, status: str) -> bool:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "UPDATE curation_items SET status = ?, reviewed_at = ? WHERE id = ?",
            (status, datetime.now(UTC).isoformat(), item_id),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()


# ---------------------------------------------------------------------------
# Vector Store (Qdrant)
# ---------------------------------------------------------------------------


class KnowledgeVectorStore:
    """Qdrant operations for knowledge with hybrid search."""

    DENSE_VECTOR_NAME = "dense"
    SPARSE_VECTOR_NAME = "sparse"

    def __init__(self, settings: KnowledgeSettings) -> None:
        self._client = AsyncQdrantClient(url=settings.qdrant_url)
        self._collection = settings.qdrant_collection
        self._dimensions = settings.embedding_dimensions

    async def ensure_collection(self) -> None:
        collections = await self._client.get_collections()
        exists = any(c.name == self._collection for c in collections.collections)

        if not exists:
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config={
                    self.DENSE_VECTOR_NAME: VectorParams(
                        size=self._dimensions, distance=Distance.COSINE
                    ),
                },
                sparse_vectors_config={
                    self.SPARSE_VECTOR_NAME: SparseVectorParams(
                        index=SparseIndexParams(on_disk=False)
                    ),
                },
            )

        import contextlib

        indexes: list[tuple[str, PayloadSchemaType]] = [
            ("domain", PayloadSchemaType.KEYWORD),
            ("source_id", PayloadSchemaType.KEYWORD),
            ("source_type", PayloadSchemaType.KEYWORD),
            ("chunk_index", PayloadSchemaType.INTEGER),
        ]
        for field, schema in indexes:
            with contextlib.suppress(Exception):
                await self._client.create_payload_index(
                    collection_name=self._collection,
                    field_name=field,
                    field_schema=schema,
                )

    async def upsert_chunks(
        self,
        chunks: list[dict[str, Any]],
        embeddings: list[list[float]],
        sparse_vectors: list[tuple[list[int], list[float]]],
    ) -> None:
        points = []
        for chunk, embedding, (indices, values) in zip(
            chunks, embeddings, sparse_vectors, strict=True
        ):
            vector_data: dict[str, Any] = {self.DENSE_VECTOR_NAME: embedding}
            if indices and values:
                vector_data[self.SPARSE_VECTOR_NAME] = SparseVector(
                    indices=indices, values=values
                )
            points.append(PointStruct(id=chunk["id"], vector=vector_data, payload=chunk))
        await self._client.upsert(collection_name=self._collection, points=points)

    async def delete_by_source(self, source_id: str) -> None:
        await self._client.delete(
            collection_name=self._collection,
            points_selector=Filter(
                must=[FieldCondition(key="source_id", match=MatchValue(value=source_id))]
            ),
        )

    async def delete_by_domain(self, domain: str) -> None:
        await self._client.delete(
            collection_name=self._collection,
            points_selector=Filter(
                must=[FieldCondition(key="domain", match=MatchValue(value=domain))]
            ),
        )

    async def update_source_name(self, source_id: str, source_name: str) -> None:
        await self._client.set_payload(
            collection_name=self._collection,
            payload={"source_name": source_name},
            points=Filter(
                must=[FieldCondition(key="source_id", match=MatchValue(value=source_id))]
            ),
        )

    async def chunks_by_source(self, source_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        """Return stored chunk payloads for one source, ordered by chunk index."""
        points = []
        offset = None
        while True:
            batch, offset = await self._client.scroll(
                collection_name=self._collection,
                scroll_filter=Filter(
                    must=[FieldCondition(key="source_id", match=MatchValue(value=source_id))]
                ),
                limit=min(limit, 256),
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            points.extend(batch)
            if offset is None or len(points) >= limit:
                break

        payloads = [dict(point.payload or {}) for point in points]
        payloads.sort(key=lambda p: int(p.get("chunk_index") or 0))
        return payloads

    async def chunks_all(self, limit: int = 50_000) -> list[dict[str, Any]]:
        """Scroll all chunk payloads — used for BM25 warm-up on startup."""
        points = []
        offset = None
        while True:
            batch, offset = await self._client.scroll(
                collection_name=self._collection,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            points.extend(batch)
            if offset is None or len(points) >= limit:
                break
        return [dict(point.payload or {}) for point in points]

    async def search(
        self,
        query_embedding: list[float],
        sparse_query: tuple[list[int], list[float]] | None = None,
        domains: list[str] | None = None,
        limit: int = 10,
        min_score: float = 0.25,
    ) -> list[ScoredPoint]:
        """Hybrid search filtered by domain(s)."""
        must_conditions: list[Condition] = []
        if domains and len(domains) == 1:
            must_conditions.append(
                FieldCondition(key="domain", match=MatchValue(value=domains[0]))
            )

        query_filter = Filter(must=must_conditions) if must_conditions else None

        # Multi-domain filter uses should with min_count
        if domains and len(domains) > 1:
            should_conditions: list[Condition] = [
                FieldCondition(key="domain", match=MatchValue(value=d)) for d in domains
            ]
            query_filter = Filter(should=should_conditions, must=must_conditions or None)

        if sparse_query and sparse_query[0] and sparse_query[1]:
            indices, values = sparse_query
            prefetch_limit = max(limit * 4, 20)
            results = await self._client.query_points(
                collection_name=self._collection,
                prefetch=[
                    Prefetch(
                        query=query_embedding,
                        using=self.DENSE_VECTOR_NAME,
                        filter=query_filter,
                        limit=prefetch_limit,
                        score_threshold=min_score,
                    ),
                    Prefetch(
                        query=SparseVector(indices=indices, values=values),
                        using=self.SPARSE_VECTOR_NAME,
                        filter=query_filter,
                        limit=prefetch_limit,
                    ),
                ],
                query=FusionQuery(fusion=Fusion.RRF),
                limit=limit,
                with_payload=True,
            )
        else:
            results = await self._client.query_points(
                collection_name=self._collection,
                query=query_embedding,
                using=self.DENSE_VECTOR_NAME,
                query_filter=query_filter,
                limit=limit,
                score_threshold=min_score,
                with_payload=True,
            )
        return results.points

    async def count_by_domain(self, domain: str) -> int:
        result = await self._client.count(
            collection_name=self._collection,
            count_filter=Filter(
                must=[FieldCondition(key="domain", match=MatchValue(value=domain))]
            ),
        )
        return result.count

    async def close(self) -> None:
        await self._client.close()


# ---------------------------------------------------------------------------
# Document Processing
# ---------------------------------------------------------------------------


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
    stored_path = source.get("stored_path")
    if old_path and old_path.exists() and old_path.is_file():
        new_path = old_path.with_name(clean_filename)
        if new_path.exists() and new_path != old_path:
            return {"success": False, "error": f"File already exists: {new_path.name}"}
        if new_path != old_path:
            old_path.rename(new_path)
            renamed_file = True
            stored_path = source_relative_path(settings.knowledge_path, new_path)
        else:
            stored_path = source_relative_path(settings.knowledge_path, old_path)

    await db.source_rename(source_id, clean_filename, stored_path)
    await vectors.update_source_name(source_id, clean_filename)
    updated = await db.source_get(source_id)
    return {
        "success": True,
        "source_id": source_id,
        "old_filename": source.get("filename"),
        "new_filename": clean_filename,
        "renamed_file": renamed_file,
        "source": updated,
    }


def compute_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


IMAGE_EXTENSIONS = {
    ".avif", ".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp",
}

# Common binary file magic byte prefixes used to detect binary files with no extension.
_BINARY_MAGIC_PREFIXES: tuple[bytes, ...] = (
    b"\x89PNG",          # PNG
    b"\xff\xd8",        # JPEG
    b"GIF8",            # GIF
    b"BM",              # BMP
    b"RIFF",            # WebP / WAV
    b"\x49\x49\x2a\x00",  # TIFF little-endian
    b"\x4d\x4d\x00\x2a",  # TIFF big-endian
    b"PK\x03\x04",     # ZIP / DOCX / XLSX
    b"\x1f\x8b",       # GZIP
    b"\x7fELF",        # ELF binary
    b"ID3",            # MP3 ID3 tag
    b"\xff\xfb",       # MP3 frame sync
    b"\x4f\x67\x67\x53",  # OGG
)


def _is_likely_binary(raw: bytes) -> bool:
    """Return True when raw bytes look like a binary/non-text file."""
    head = raw[:16]
    for magic in _BINARY_MAGIC_PREFIXES:
        if head.startswith(magic):
            return True
    # ISO base media file format (HEIC, HEIF, MP4): 'ftyp' at bytes 4-8
    if len(raw) >= 8 and raw[4:8] == b"ftyp":
        return True
    # Null byte: almost never appears in UTF-8 text
    if b"\x00" in raw[:512]:
        return True
    # High ratio of control characters
    sample = raw[:512]
    control = sum(1 for b in sample if b < 0x09 or b in (0x0b, 0x0c) or 0x0E <= b <= 0x1F)
    return bool(sample) and control / len(sample) > 0.10
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".json", ".yaml", ".yml",
    ".html", ".htm", ".xml",
}


async def _run(
    cmd: list[str],
    stdin: bytes | None = None,
    timeout: float = 120.0,
) -> tuple[int, bytes, bytes]:
    """Run a subprocess and return (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(input=stdin), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, b"", b"timeout"
    return proc.returncode or 0, out, err


VISION_OCR_PROMPT = (
    "You are an OCR engine. Transcribe ALL text visible in this image VERBATIM. "
    "Preserve spelling, numbers, punctuation, and order. Include every label, field, "
    "barcode value, and stamp. For tables, output rows as plain text with columns "
    "separated by ' | '. Do not summarize, do not translate, do not redact, do not "
    "invent text that is not visible. Output only the transcribed text — no preamble "
    "or commentary. If the image contains no text, output exactly: [no text]"
)

IMAGE_DESCRIPTION_PROMPT = (
    "Describe this image in 2-4 sentences for a personal knowledge base. "
    "Cover the main subject, setting, notable objects or people (no names needed), "
    "colors, any visible text, and specific details that would help someone find this "
    "image when searching. Be concrete and factual. Output only the description."
)

EXTRACTION_SYSTEM_PROMPT = (
    "You are a document extraction engine for a personal knowledge base.\n"
    "Your job: read the provided document content and return a JSON object.\n\n"
    "Rules:\n"
    "- Extract every value you can see. Do not fabricate, guess, or paraphrase values.\n"
    "- Use stable snake_case keys with meaningful prefixes, e.g. w2_2025_box1_wages, "
    "passport_us_number, lab_ldl_2024_12.\n"
    "- For dates use ISO format: YYYY-MM-DD.\n"
    "- For currency include the number only (no $ sign): 94200.00\n"
    "- For images with no document structure (photos, pets, scenery): set 'caption' "
    "to a 2-3 sentence description, set 'facts' to {}.\n"
    "- For documents: set 'facts' to all extracted key/value pairs, set 'caption' to null.\n"
    "- Omit fields that are not legible or not present — do not set null or 'unknown'.\n"
    "- Output only valid JSON. No markdown fences, no commentary.\n"
    "Output format: {\"facts\": {\"key\": \"value\", ...}, \"caption\": null}"
)


async def _vision_ocr_bytes(
    image_bytes: bytes, media_type: str, settings: KnowledgeSettings
) -> str:
    """OCR an image via OpenRouter vision LLM. Returns text or empty on failure."""
    if not settings.vision_model or not settings.openrouter_api_key:
        return ""
    import base64 as _b64

    b64 = _b64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{media_type};base64,{b64}"
    payload = {
        "model": settings.vision_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_OCR_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
            text = (data["choices"][0]["message"]["content"] or "").strip()
            return "" if text == "[no text]" else text
    except Exception as exc:  # noqa: BLE001
        print(
            f"[knowledge] vision OCR failed ({settings.vision_model}): {exc}",
            file=sys.stderr,
        )
        return ""


async def _tesseract_image(path: Path, language: str) -> str:
    rc, out, _ = await _run(["tesseract", str(path), "-", "-l", language])
    return out.decode("utf-8", errors="replace") if rc == 0 else ""


_IMAGE_MEDIA = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".tif": "image/tiff", ".tiff": "image/tiff",
}


async def _ocr_image_file(path: Path, settings: KnowledgeSettings) -> str:
    """Vision LLM first, tesseract fallback."""
    if settings.vision_model and settings.openrouter_api_key:
        try:
            data = path.read_bytes()
            media = _IMAGE_MEDIA.get(path.suffix.lower(), "image/png")
            text = await _vision_ocr_bytes(data, media, settings)
            if text:
                return text
        except OSError:
            pass
    return await _tesseract_image(path, settings.ocr_language)


async def _extract_pdf_text(path: Path, settings: KnowledgeSettings) -> str:
    """pdftotext for native PDFs; rasterize + vision LLM for scans."""
    rc, out, _ = await _run(["pdftotext", "-layout", str(path), "-"])
    text = out.decode("utf-8", errors="replace").strip() if rc == 0 else ""
    if text:
        return text

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "page"
        rc, _, _ = await _run([
            "pdftoppm", "-r", str(settings.vision_dpi), "-png",
            str(path), str(prefix),
        ])
        if rc != 0:
            return ""
        page_files = sorted(Path(tmp).glob("page-*.png"))[: settings.vision_max_pages]
        pages: list[str] = []
        for img in page_files:
            pages.append(await _ocr_image_file(img, settings))
        return "\n\n".join(p for p in pages if p.strip())


# ---------------------------------------------------------------------------
# Pipeline-logging extraction functions
# Each returns (text_or_chunks, pipeline_steps) so callers can report exactly
# what ran, which model was called, whether it succeeded or fell back.
# ---------------------------------------------------------------------------


async def _vision_call(
    image_bytes: bytes,
    media_type: str,
    prompt: str,
    model: str,
    api_key: str,
    step_name: str,
) -> tuple[str, dict[str, Any]]:
    """Single OpenRouter vision LLM call. Returns (text, pipeline_step)."""
    step: dict[str, Any] = {
        "step": step_name,
        "model": model,
        "status": "failed",
        "tokens_in": 0,
        "tokens_out": 0,
        "note": "",
    }
    if not model or not api_key:
        step["status"] = "skipped"
        step["note"] = "no model or api_key configured"
        return "", step

    import base64 as _b64
    b64 = _b64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{media_type};base64,{b64}"
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
            text = (data["choices"][0]["message"]["content"] or "").strip()
            usage = data.get("usage") or {}
            step["tokens_in"] = usage.get("prompt_tokens", 0)
            step["tokens_out"] = usage.get("completion_tokens", 0)
            if text == "[no text]":
                text = ""
                step["status"] = "ok"
                step["note"] = "model reported: no text in image"
            else:
                step["status"] = "ok"
                step["note"] = f"{len(text)} chars"
            return text, step
    except Exception as exc:  # noqa: BLE001
        step["status"] = "failed"
        step["note"] = str(exc)
        print(f"[knowledge] _vision_call failed ({model}/{step_name}): {exc}", file=sys.stderr)
        return "", step


async def _describe_image_file(
    path: Path, settings: KnowledgeSettings
) -> tuple[str, list[dict[str, Any]]]:
    """Describe a photo/image using the vision model. Returns (description, steps)."""
    steps: list[dict[str, Any]] = []
    if not settings.vision_model or not settings.openrouter_api_key:
        steps.append({
            "step": "image_description", "model": None, "status": "skipped",
            "note": "KNOWLEDGE_VISION_MODEL not configured",
        })
        return "", steps
    try:
        data = path.read_bytes()
        media = _IMAGE_MEDIA.get(path.suffix.lower(), "image/png")
    except OSError as exc:
        steps.append({
            "step": "image_description", "model": settings.vision_model,
            "status": "failed", "note": f"read error: {exc}",
        })
        return "", steps
    text, step = await _vision_call(
        data, media, IMAGE_DESCRIPTION_PROMPT,
        settings.vision_model, settings.openrouter_api_key, "image_description",
    )
    steps.append(step)
    return text, steps


async def _ocr_image_file_with_log(
    path: Path, settings: KnowledgeSettings
) -> tuple[str, list[dict[str, Any]]]:
    """Vision LLM OCR, tesseract fallback. Returns (text, steps)."""
    steps: list[dict[str, Any]] = []
    if settings.vision_model and settings.openrouter_api_key:
        try:
            data = path.read_bytes()
            media = _IMAGE_MEDIA.get(path.suffix.lower(), "image/png")
            text, step = await _vision_call(
                data, media, VISION_OCR_PROMPT,
                settings.vision_model, settings.openrouter_api_key, "vision_ocr",
            )
            steps.append(step)
            if text:
                return text, steps
        except OSError as exc:
            steps.append({
                "step": "vision_ocr", "model": settings.vision_model,
                "status": "failed", "note": f"read error: {exc}",
            })
    # Tesseract fallback
    tess_step: dict[str, Any] = {"step": "tesseract", "model": "tesseract"}
    rc, out, _ = await _run(["tesseract", str(path), "-", "-l", settings.ocr_language])
    if rc == 0:
        text = out.decode("utf-8", errors="replace")
        tess_step["status"] = "ok"
        tess_step["note"] = f"{len(text)} chars (fallback)"
    else:
        text = ""
        tess_step["status"] = "failed"
        tess_step["note"] = "tesseract returned non-zero exit code"
    steps.append(tess_step)
    return text, steps


async def _extract_pdf_text_with_log(
    path: Path, settings: KnowledgeSettings
) -> tuple[str, list[dict[str, Any]]]:
    """pdftotext for native PDFs; rasterize + OCR for scans. Returns (text, steps)."""
    steps: list[dict[str, Any]] = []
    pdf_step: dict[str, Any] = {"step": "pdftotext", "model": None}
    rc, out, _ = await _run(["pdftotext", "-layout", str(path), "-"])
    text = out.decode("utf-8", errors="replace").strip() if rc == 0 else ""
    if text:
        pdf_step["status"] = "ok"
        pdf_step["note"] = f"{len(text)} chars (native PDF text)"
        steps.append(pdf_step)
        return text, steps
    pdf_step["status"] = "ok" if rc == 0 else "failed"
    pdf_step["note"] = "no embedded text — scanned PDF" if rc == 0 else "pdftotext failed"
    steps.append(pdf_step)

    import tempfile
    raster_step: dict[str, Any] = {"step": "rasterize", "model": None}
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "page"
        rc, _, _ = await _run([
            "pdftoppm", "-r", str(settings.vision_dpi), "-png", str(path), str(prefix),
        ])
        if rc != 0:
            raster_step["status"] = "failed"
            raster_step["note"] = "pdftoppm failed"
            steps.append(raster_step)
            return "", steps
        page_files = sorted(Path(tmp).glob("page-*.png"))[: settings.vision_max_pages]
        raster_step["status"] = "ok"
        raster_step["note"] = (
            f"{len(page_files)} page(s) rasterized at {settings.vision_dpi} dpi"
        )
        steps.append(raster_step)

        pages: list[str] = []
        for img in page_files:
            page_text, page_steps = await _ocr_image_file_with_log(img, settings)
            for s in page_steps:
                s["page"] = img.name
            steps.extend(page_steps)
            if page_text.strip():
                pages.append(page_text)

    text = "\n\n".join(p for p in pages if p.strip())
    if page_files and len(text) < 100:
        steps.append({
            "step": "confidence_check",
            "model": None,
            "status": "warn",
            "note": (
                f"low OCR output ({len(text)} chars across {len(page_files)} page(s)) "
                "— consider using Extract Facts with Sonnet for better accuracy"
            ),
        })
    return text, steps


async def _extract_and_chunk_with_log(
    path: Path, settings: KnowledgeSettings
) -> tuple[list[str], list[dict[str, Any]], str]:
    """Extract text and split into chunks with a full pipeline log.

    Returns (chunks, pipeline_steps, pipeline_type) where pipeline_type is one of:
    'image_description' | 'document_ocr' | 'text_read' | 'unsupported'
    """
    suffix = path.suffix.lower()
    steps: list[dict[str, Any]] = []
    text = ""

    if suffix == ".pdf":
        pipeline_type = "document_ocr"
        text, steps = await _extract_pdf_text_with_log(path, settings)
    elif suffix in IMAGE_EXTENSIONS and settings.ocr_enabled:
        # Photos/images: generate a semantic description, not verbatim OCR.
        # OCR is reserved for PDFs where text layout matters.
        pipeline_type = "image_description"
        text, steps = await _describe_image_file(path, settings)
    elif suffix in TEXT_EXTENSIONS or suffix == "":
        pipeline_type = "text_read"
        read_step: dict[str, Any] = {"step": "text_read", "model": None}
        try:
            raw = path.read_bytes()
            if suffix == "" and _is_likely_binary(raw):
                read_step["status"] = "skipped"
                read_step["note"] = "binary file with no extension — no text indexing"
                steps.append(read_step)
                return [], steps, "unsupported"
            text = raw.decode("utf-8", errors="replace")
            read_step["status"] = "ok"
            read_step["note"] = f"{len(text)} chars read"
        except OSError as exc:
            read_step["status"] = "failed"
            read_step["note"] = str(exc)
            text = ""
        steps.append(read_step)
    else:
        steps.append({
            "step": "classify", "model": None, "status": "skipped",
            "note": f"unsupported file type: {suffix}",
        })
        return [], steps, "unsupported"

    text = text.strip()
    if not text:
        if not any(s.get("status") in ("failed", "warn") for s in steps):
            steps.append({
                "step": "chunking", "model": None, "status": "skipped",
                "note": "no text extracted — nothing to chunk",
            })
        return [], steps, pipeline_type

    chunks = chunk_text(text, settings.chunk_max_chars, settings.chunk_overlap)
    steps.append({
        "step": "chunking", "model": None, "status": "ok",
        "note": f"{len(chunks)} chunk(s) from {len(text)} chars",
    })
    return chunks, steps, pipeline_type


async def extract_and_chunk(path: Path, settings: KnowledgeSettings) -> list[str]:
    """Extract text from a file and split into chunks.

    Pipeline: pdftotext for native PDFs (free), vision LLM via OpenRouter for
    scanned PDFs and images (accurate), tesseract as final fallback.
    """
    suffix = path.suffix.lower()
    text = ""

    if suffix == ".pdf":
        text = await _extract_pdf_text(path, settings)
    elif suffix in IMAGE_EXTENSIONS and settings.ocr_enabled:
        text = await _ocr_image_file(path, settings)
    elif suffix in TEXT_EXTENSIONS or suffix == "":
        try:
            raw = path.read_bytes()
            if suffix == "" and _is_likely_binary(raw):
                # Extensionless binary file (e.g. image uploaded without extension).
                # Store the bytes but skip text indexing; caption via knowledge_ingest_text.
                return []
            text = raw.decode("utf-8", errors="replace")
        except OSError:
            text = ""
    else:
        # Unknown binary type — store the file but skip indexing.
        return []

    text = text.strip()
    if not text:
        return []

    return chunk_text(text, settings.chunk_max_chars, settings.chunk_overlap)


def chunk_text(text: str, max_chars: int = 1000, overlap: int = 200) -> list[str]:
    """Chunk plain text into overlapping segments."""
    if len(text) <= max_chars:
        return [text]

    chunks = []
    current = ""
    for para in text.split("\n\n"):
        if len(current) + len(para) > max_chars:
            if current:
                chunks.append(current.strip())
            # Start new chunk with overlap from end of previous
            if chunks and overlap > 0:
                prev = chunks[-1]
                current = prev[-overlap:] + "\n\n" + para
            else:
                current = para
        else:
            current = current + "\n\n" + para if current else para
    if current:
        chunks.append(current.strip())

    return [c for c in chunks if c]


# ---------------------------------------------------------------------------
# Shared ingestion pipeline
# ---------------------------------------------------------------------------

# File extensions that imply binary/document uploads. These must never be
# accepted as a `source_name` for `knowledge_ingest_text` — that path stores
# only chunks (no `stored_path`, no raw bytes), so a `.pdf` source created via
# text ingest is silently a fake file. Real binary uploads must go through
# `knowledge_upload_file_base64` or `POST /api/upload/{domain}`.
_BINARY_NAME_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf", ".png", ".jpg", ".jpeg", ".heic", ".heif", ".tif", ".tiff",
    ".webp", ".bmp", ".gif", ".svg",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods",
    ".zip", ".tar", ".gz", ".7z", ".rar",
    ".mp3", ".m4a", ".wav", ".flac", ".ogg",
    ".mp4", ".mov", ".mkv", ".avi", ".webm",
    ".epub", ".mobi",
})

# `source_type` values that `knowledge_ingest_text` is allowed to record. This
# blocks an agent from labeling a text source as `identity_document`,
# `pdf`, etc., which previously hid text-only rows behind binary-looking types.
_TEXT_SOURCE_TYPE_ALLOWLIST: frozenset[str] = frozenset({
    "note", "summary", "transcript", "research", "caption",
    "markdown", "text", "manual", "chat", "memo",
})


def _validate_text_ingest_inputs(
    source_name: str,
    source_type: str,
) -> str | None:
    """Return an error message if text-ingest inputs look like a binary upload."""
    name_ext = Path(source_name).suffix.lower()
    if name_ext in _BINARY_NAME_EXTENSIONS:
        return (
            f"source_name '{source_name}' has a binary/document extension "
            f"({name_ext}). Use knowledge_upload_file_base64 (or "
            "POST /api/upload/{domain}) so the original bytes are stored. "
            "knowledge_ingest_text only stores extracted text chunks."
        )
    type_lower = source_type.lower().strip()
    if type_lower not in _TEXT_SOURCE_TYPE_ALLOWLIST:
        if type_lower.lstrip(".") in {ext.lstrip(".") for ext in _BINARY_NAME_EXTENSIONS}:
            return (
                f"source_type '{source_type}' looks like a file extension. "
                "Use knowledge_upload_file_base64 to upload the actual file."
            )
        allowed = ", ".join(sorted(_TEXT_SOURCE_TYPE_ALLOWLIST))
        return (
            f"source_type '{source_type}' is not allowed for text ingest. "
            f"Use one of: {allowed}."
        )
    return None


async def _ingest_file_at_path(
    settings: KnowledgeSettings,
    embeddings: EmbeddingClient,
    sparse_encoder: BM25SparseEncoder,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
    *,
    dest: Path,
    domain: str,
    force: bool = False,
) -> dict[str, Any]:
    """Hash, extract, embed, and persist one file already on disk under `dest`.

    Shared by `POST /api/upload/{domain}`, `knowledge_upload_file_base64`,
    and `knowledge_ingest_file`. Returns a result dict; never raises for
    "no content" / "already ingested" — those are normal outcomes.
    """
    file_hash = compute_file_hash(dest)
    rel_path = source_relative_path(settings.knowledge_path, dest)
    media_type = source_media_type(dest.name)
    size_bytes = dest.stat().st_size

    existing = await db.source_get_by_hash(file_hash)
    if existing and not force:
        existing_id = str(existing.get("id") or "")
        existing_path = existing.get("stored_path")
        if not existing_path:
            # Legacy text-only row — backfill the stored bytes onto the same source_id.
            await db.source_update_storage(
                existing_id,
                stored_path=rel_path,
                media_type=media_type,
                size_bytes=size_bytes,
                domain=domain,
            )
            return {
                "success": True,
                "file": dest.name,
                "domain": domain,
                "ingested": False,
                "source_id": existing_id,
                "stored_path": rel_path,
                "reason": "backfilled stored bytes onto existing source",
            }
        # Already have bytes for this hash — drop the freshly-written duplicate.
        try:
            if rel_path != existing_path and dest.exists():
                dest.unlink()
        except OSError:
            pass
        return {
            "success": True,
            "file": dest.name,
            "domain": domain,
            "ingested": False,
            "source_id": existing_id,
            "stored_path": existing_path,
            "reason": "already ingested with stored bytes",
        }

    chunks_text, pipeline_log, pipeline_type = await _extract_and_chunk_with_log(dest, settings)

    if not chunks_text:
        # No text extracted (e.g. photo with description model skipped/failed, or
        # unsupported binary). Register the source so bytes are downloadable.
        source_id = str(uuid.uuid4())
        source_type = dest.suffix.lstrip(".") or "file"
        await db.source_add(
            source_id, domain, source_type, dest.name,
            file_hash, 0, rel_path, media_type, size_bytes,
        )
        # Determine a helpful reason from the pipeline log
        failed = [s for s in pipeline_log if s.get("status") == "failed"]
        warn = [s for s in pipeline_log if s.get("status") == "warn"]
        if failed:
            reason = f"pipeline step '{failed[0]['step']}' failed: {failed[0].get('note', '')}"
        elif pipeline_type == "image_description":
            reason = "image stored — use Extract Facts to generate a searchable description"
        elif pipeline_type == "unsupported":
            reason = "unsupported file type — bytes stored only"
        else:
            reason = "no extractable text — bytes stored"
        return {
            "success": True,
            "file": dest.name,
            "domain": domain,
            "ingested": True,
            "source_id": source_id,
            "chunks_stored": 0,
            "stored_path": rel_path,
            "pipeline_type": pipeline_type,
            "pipeline": pipeline_log,
            "needs_extraction": pipeline_type in ("image_description", "document_ocr"),
            "reason": reason,
        }

    sparse_encoder.fit_batch(chunks_text)
    sparse_vecs = [sparse_encoder.encode(t) for t in chunks_text]
    dense_vecs = await embeddings.embed_batch(chunks_text)

    source_id = str(uuid.uuid4())
    source_type = dest.suffix.lstrip(".") or "file"
    now = datetime.now(UTC).isoformat()
    chunk_payloads = [
        {
            "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{source_id}_{i}")),
            "domain": domain,
            "source_id": source_id,
            "source_type": source_type,
            "source_name": dest.name,
            "chunk_index": i,
            "content": text,
            "ingested_at": now,
        }
        for i, text in enumerate(chunks_text)
    ]

    await vectors.upsert_chunks(chunk_payloads, dense_vecs, sparse_vecs)
    await db.source_add(
        source_id, domain, source_type, dest.name,
        file_hash, len(chunks_text), rel_path, media_type, size_bytes,
    )

    warn_steps = [s for s in pipeline_log if s.get("status") == "warn"]
    return {
        "success": True,
        "file": dest.name,
        "domain": domain,
        "ingested": True,
        "source_id": source_id,
        "chunks_stored": len(chunks_text),
        "stored_path": rel_path,
        "pipeline_type": pipeline_type,
        "pipeline": pipeline_log,
        "needs_extraction": bool(warn_steps),
    }


# ---------------------------------------------------------------------------
# Single-shot fact extraction (POST /api/sources/{id}/extract)
# ---------------------------------------------------------------------------


async def extract_source_facts_single_shot(
    settings: KnowledgeSettings,
    embeddings: EmbeddingClient,
    sparse_encoder: BM25SparseEncoder,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
    source_id: str,
    hint: str | None = None,
) -> dict[str, Any]:
    """Single-shot Sonnet extraction: one LLM call → structured facts + optional caption.

    For images and sources with no chunks: loads raw bytes directly.
    For text documents: uses existing Qdrant chunks (much cheaper — no image token cost).
    Uses Anthropic prompt caching on the system prompt when using Claude models.
    """
    source = await db.source_get(source_id)
    if not source:
        return {"success": False, "error": f"Source '{source_id}' not found"}

    if not settings.extraction_model:
        return {"success": False, "error": "KNOWLEDGE_EXTRACTION_MODEL not configured"}

    pipeline: list[dict[str, Any]] = []
    suffix = Path(str(source.get("filename") or "")).suffix.lower()
    is_image = suffix in IMAGE_EXTENSIONS
    chunk_count = int(source.get("chunk_count") or 0)
    domain = str(source.get("domain") or "")

    # --- Step 1: gather content ---
    user_content: str | list[dict[str, Any]]

    if is_image or chunk_count == 0:
        source_path = resolve_source_path(settings.knowledge_path, source)
        if not source_path:
            pipeline.append({
                "step": "load_source", "status": "failed",
                "note": "file not found on disk",
            })
            return {"success": False, "error": "Source file not found on disk", "pipeline": pipeline}
        try:
            image_bytes = source_path.read_bytes()
            image_media_type = _IMAGE_MEDIA.get(suffix, "image/png")
            pipeline.append({
                "step": "load_source", "status": "ok",
                "note": f"{len(image_bytes)} bytes read from disk",
            })
        except OSError as exc:
            pipeline.append({
                "step": "load_source", "status": "failed", "note": str(exc),
            })
            return {"success": False, "error": f"Could not read source file: {exc}", "pipeline": pipeline}

        import base64 as _b64
        b64 = _b64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{image_media_type};base64,{b64}"
        hint_text = f"\nDocument type hint: {hint}" if hint else ""
        _json_reminder = (
            "\n\nIMPORTANT: Respond with ONLY a JSON object. "
            'Format: {"facts": {"key": "value"}, "caption": null} — '
            "No markdown, no explanations, no code fences."
        )
        user_content = [
            {"type": "text", "text": f"Extract all information from this document.{hint_text}{_json_reminder}"},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
    else:
        chunks = await vectors.chunks_by_source(source_id)
        text_body = "\n\n".join(
            str(c.get("content") or "").strip() for c in chunks if c.get("content")
        )
        pipeline.append({
            "step": "load_chunks", "status": "ok",
            "note": f"{len(chunks)} chunks, {len(text_body)} chars total",
        })
        hint_text = f"\nDocument type hint: {hint}" if hint else ""
        _json_reminder = (
            "\n\nIMPORTANT: Respond with ONLY a JSON object. "
            'Format: {"facts": {"key": "value"}, "caption": null} — '
            "No markdown, no explanations, no code fences."
        )
        user_content = (
            f"Extract all information from this document.{hint_text}{_json_reminder}"
            f"\n\n---\n\n{text_body}"
        )

    # --- Step 2: call extraction model ---
    user_msg: dict[str, Any] = {
        "role": "user",
        "content": (
            user_content if isinstance(user_content, list)
            else [{"type": "text", "text": user_content}]
        ),
    }
    is_claude = "anthropic" in settings.extraction_model or "claude" in settings.extraction_model
    # For Claude: add an assistant prefill of '{"facts":' to force JSON output.
    # The model must continue from this prefix — it cannot produce markdown.
    # We prepend that prefix back when parsing the response.
    PREFILL = '{"facts":'
    messages: list[dict[str, Any]] = [user_msg]
    if is_claude:
        messages.append({"role": "assistant", "content": PREFILL})

    payload: dict[str, Any] = {
        "model": settings.extraction_model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": 4096,
    }

    # Anthropic prompt caching: wrap system prompt in a content block with
    # cache_control so repeated calls within 5 min read from cache at 90% discount.
    if is_claude:
        payload["system"] = [{
            "type": "text",
            "text": EXTRACTION_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }]
    else:
        payload["system"] = EXTRACTION_SYSTEM_PROMPT

    llm_step: dict[str, Any] = {
        "step": "extraction_llm",
        "model": settings.extraction_model,
        "status": "failed",
        "tokens_in": 0,
        "tokens_out": 0,
        "cache_read_tokens": 0,
        "note": "",
    }
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
    }
    raw_output = ""
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
            raw_output = (data["choices"][0]["message"]["content"] or "").strip()
            # When assistant prefill was used, the API returns only the completion
            # (everything after the prefill). Prepend the prefill so we have valid JSON.
            if is_claude and not raw_output.startswith("{"):
                raw_output = PREFILL + raw_output
            usage = data.get("usage") or {}
            llm_step["tokens_in"] = usage.get("prompt_tokens", 0)
            llm_step["tokens_out"] = usage.get("completion_tokens", 0)
            llm_step["cache_read_tokens"] = usage.get("cache_read_input_tokens", 0)
            llm_step["status"] = "ok"
            llm_step["note"] = (
                f"{len(raw_output)} chars output"
                + (f", {llm_step['cache_read_tokens']} cached tokens" if llm_step["cache_read_tokens"] else "")
            )
    except Exception as exc:  # noqa: BLE001
        llm_step["note"] = str(exc)
        pipeline.append(llm_step)
        return {"success": False, "error": f"LLM call failed: {exc}", "pipeline": pipeline}
    pipeline.append(llm_step)

    # --- Step 3: parse JSON ---
    clean = raw_output.strip()
    # Strip markdown code fences (```json ... ```)
    if clean.startswith("```"):
        clean = re.sub(r"^```[a-z]*\n?", "", clean)
        clean = re.sub(r"\n?```$", "", clean.rstrip())
    # Fallback: if response is markdown prose, find the first {...} JSON object
    if not clean.startswith("{"):
        m = re.search(r"\{[\s\S]*\}", clean)
        if m:
            clean = m.group(0)
    parse_step: dict[str, Any] = {"step": "parse_json", "model": None}
    try:
        extracted = json.loads(clean)
        facts: dict[str, str] = extracted.get("facts") or {}
        caption: str | None = extracted.get("caption") or None
        parse_step["status"] = "ok"
        parse_step["note"] = f"{len(facts)} fact(s), caption={'yes' if caption else 'no'}"
    except json.JSONDecodeError as exc:
        parse_step["status"] = "failed"
        parse_step["note"] = f"JSON parse error: {exc} | raw[:200]: {raw_output[:200]}"
        pipeline.append(parse_step)
        return {
            "success": False, "error": "LLM returned invalid JSON",
            "raw_output": raw_output, "pipeline": pipeline,
        }
    pipeline.append(parse_step)

    # --- Step 4: write facts ---
    written_facts: list[str] = []
    write_step: dict[str, Any] = {"step": "write_facts", "model": None}
    try:
        for key, value in facts.items():
            await db.fact_set(domain, key, str(value), source=f"extracted:{source_id}", confidence=0.9)
            written_facts.append(key)
        write_step["status"] = "ok"
        write_step["note"] = f"{len(written_facts)} fact(s) written to '{domain}'"
    except Exception as exc:  # noqa: BLE001
        write_step["status"] = "failed"
        write_step["note"] = str(exc)
        pipeline.append(write_step)
        return {"success": False, "error": f"Failed writing facts: {exc}", "pipeline": pipeline}
    pipeline.append(write_step)

    # --- Step 5: embed and store caption as a searchable chunk ---
    if caption:
        cap_step: dict[str, Any] = {"step": "write_caption_chunk", "model": None}
        try:
            cap_embedding = await embeddings.embed(caption)
            cap_sparse = sparse_encoder.encode(caption)
            now = datetime.now(UTC).isoformat()
            # Upsert a single caption chunk linked to the original source_id.
            # Use a deterministic chunk id so re-running extract overwrites it.
            cap_chunk_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{source_id}_caption"))
            await vectors.upsert_chunks(
                [{"id": cap_chunk_id, "domain": domain, "source_id": source_id,
                  "source_type": "caption", "source_name": str(source.get("filename") or source_id),
                  "chunk_index": 0, "content": caption, "ingested_at": now}],
                [cap_embedding],
                [cap_sparse],
            )
            # Ensure chunk_count reflects the caption chunk
            if chunk_count == 0:
                await db.source_update_chunk_count(source_id, 1)
            cap_step["status"] = "ok"
            cap_step["note"] = f"{len(caption)} chars embedded and stored"
        except Exception as exc:  # noqa: BLE001
            cap_step["status"] = "failed"
            cap_step["note"] = str(exc)
        pipeline.append(cap_step)

    return {
        "success": True,
        "source_id": source_id,
        "filename": source.get("filename"),
        "domain": domain,
        "model": settings.extraction_model,
        "facts_written": len(written_facts),
        "facts": facts,
        "caption": caption,
        "pipeline": pipeline,
    }


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


DESTRUCTIVE_CURATION_ACTIONS = {
    "archive_domain",
    "delete_source",
    "domain_archive",
    "fact_delete",
}


def curation_item_has_destructive_actions(item: dict[str, Any]) -> bool:
    """Return True when a curation item proposes removing or archiving data."""
    for action in item.get("proposed_actions") or []:
        action_type = str(action.get("action") or action.get("type") or "")
        if action_type in DESTRUCTIVE_CURATION_ACTIONS:
            return True
    return False


async def _ingest_curation_text(
    *,
    settings: KnowledgeSettings,
    embeddings: EmbeddingClient,
    sparse_encoder: BM25SparseEncoder,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
    domain: str,
    content: str,
    source_name: str,
    source_type: str = "curated_note",
) -> dict[str, Any]:
    if not await db.domain_exists(domain):
        raise ValueError(f"Domain '{domain}' not found")

    chunks_text = chunk_text(content, settings.chunk_max_chars, settings.chunk_overlap)
    if not chunks_text:
        raise ValueError("No content to ingest")

    content_hash = compute_text_hash(content)
    if await db.source_exists(content_hash):
        return {
            "action": "ingest_text",
            "status": "skipped",
            "reason": "identical content already ingested",
        }

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
        "action": "ingest_text",
        "status": "applied",
        "domain": domain,
        "source_id": source_id,
        "source_name": source_name,
        "chunks": len(chunks_text),
    }


async def execute_curation_action(
    action: dict[str, Any],
    *,
    settings: KnowledgeSettings,
    embeddings: EmbeddingClient,
    sparse_encoder: BM25SparseEncoder,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
) -> dict[str, Any]:
    """Apply one reviewed curation action to Knowledge storage."""
    action_type = str(action.get("action") or action.get("type") or "")

    if action_type == "fact_set":
        domain = str(action["domain"])
        key = str(action["key"])
        if not await db.domain_exists(domain):
            raise ValueError(f"Domain '{domain}' not found")
        fact_id = await db.fact_set(
            domain,
            key,
            str(action["value"]),
            action.get("source"),
            float(action.get("confidence", 1.0)),
            action.get("valid_from"),
            action.get("valid_until"),
        )
        return {"action": action_type, "status": "applied", "fact_id": fact_id}

    if action_type == "fact_update_validity":
        domain = str(action["domain"])
        key = str(action["key"])
        fact = await db.fact_get(domain, key)
        if not fact:
            raise ValueError(f"Fact '{domain}/{key}' not found")
        await db.fact_set(
            domain,
            key,
            fact["value"],
            fact.get("source"),
            float(fact.get("confidence", 1.0)),
            action.get("valid_from", fact.get("valid_from")),
            action.get("valid_until", fact.get("valid_until")),
        )
        return {"action": action_type, "status": "applied", "domain": domain, "key": key}

    if action_type == "fact_delete":
        domain = str(action["domain"])
        key = str(action["key"])
        deleted = await db.fact_delete(domain, key)
        if not deleted:
            raise ValueError(f"Fact '{domain}/{key}' not found")
        return {"action": action_type, "status": "applied", "domain": domain, "key": key}

    if action_type == "ingest_text":
        return await _ingest_curation_text(
            settings=settings,
            embeddings=embeddings,
            sparse_encoder=sparse_encoder,
            vectors=vectors,
            db=db,
            domain=str(action["domain"]),
            content=str(action["content"]),
            source_name=str(action.get("source_name") or "curated_conversation_note"),
            source_type=str(action.get("source_type") or "curated_note"),
        )

    if action_type == "delete_source":
        source_id = str(action.get("target_id") or action.get("source_id") or "")
        if not source_id:
            raise ValueError("delete_source action requires target_id or source_id")
        result = await delete_source_record(settings, vectors, db, source_id)
        if not result["success"]:
            raise ValueError(result["error"])
        return {
            "action": action_type,
            "status": "applied",
            "source_id": source_id,
            "source": result["source"],
        }

    if action_type in {"archive_domain", "domain_archive"}:
        domain = str(action.get("target_id") or action.get("domain") or "")
        if not domain:
            raise ValueError("archive_domain action requires target_id or domain")
        archived = await db.domain_archive(domain)
        if not archived:
            raise ValueError(f"Domain '{domain}' not found or already archived")
        return {"action": action_type, "status": "applied", "domain": domain}

    if action_type in {"flag_for_review", "no_action"}:
        return {"action": action_type, "status": "skipped"}

    raise ValueError(f"Unsupported curation action '{action_type}'")


async def apply_curation_item(
    item_id: str,
    *,
    confirmation: str | None,
    settings: KnowledgeSettings,
    embeddings: EmbeddingClient,
    sparse_encoder: BM25SparseEncoder,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
) -> dict[str, Any]:
    """Apply a queue item after review, enforcing destructive-action confirmation."""
    item = await db.curation_get(item_id)
    if not item:
        return {"success": False, "error": f"Curation item '{item_id}' not found"}
    if item["status"] != "pending":
        return {
            "success": False,
            "error": f"Curation item '{item_id}' is {item['status']}, not pending",
        }
    if curation_item_has_destructive_actions(item) and confirmation != item_id:
        return {
            "success": False,
            "error": "Destructive curation actions require confirmation equal to the item id",
            "requires_confirmation": item_id,
        }

    results = []
    try:
        for action in item.get("proposed_actions") or []:
            results.append(await execute_curation_action(
                action,
                settings=settings,
                embeddings=embeddings,
                sparse_encoder=sparse_encoder,
                vectors=vectors,
                db=db,
            ))
    except Exception as exc:
        return {"success": False, "error": str(exc), "applied_before_error": results}

    await db.curation_mark_status(item_id, "applied")
    return {"success": True, "item_id": item_id, "results": results}


async def _resolve_domains(domain: str | None, domains: list[str] | None) -> list[str]:
    """Resolve a domain query to a list of domains including related ones.

    If a single domain is given, automatically includes its related domains.
    The 'core' domain is always included unless the caller explicitly excludes it.
    """
    _, _, _, _, db = _require_ready()

    if domains:
        result = list(domains)
    elif domain:
        result = [domain]
        domain_info = await db.domain_get(domain)
        if domain_info and domain_info["related_domains"]:
            for related in domain_info["related_domains"]:
                if related not in result:
                    result.append(related)
    else:
        # All non-archived domains
        all_domains = await db.domain_list()
        result = [d["name"] for d in all_domains if not d["archived"]]

    # Always include core if it exists and isn't already there
    if "core" not in result and await db.domain_exists("core"):
        result.append("core")

    return result


# ---------------------------------------------------------------------------
# MCP Tools — Domain Management
# ---------------------------------------------------------------------------


@mcp.tool("knowledge_domain_create")
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
async def knowledge_fact_set(
    domain: str,
    key: str,
    value: str,
    source: str | None = None,
    confidence: float = 1.0,
    valid_from: str | None = None,
    valid_until: str | None = None,
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
    """
    _, _, _, _, db = _require_ready()

    if not await db.domain_exists(domain):
        return {"success": False, "error": f"Domain '{domain}' not found. Create it first."}

    fact_id = await db.fact_set(
        domain, key, value, source, confidence, valid_from, valid_until
    )
    return {
        "success": True,
        "fact_id": fact_id,
        "domain": domain,
        "key": key,
        "value": value,
    }


@mcp.tool("knowledge_fact_delete")
async def knowledge_fact_delete(domain: str, key: str) -> dict[str, Any]:
    """Delete a specific fact from a domain.

    Args:
        domain: Domain the fact belongs to.
        key: The fact key to delete.
    """
    _, _, _, _, db = _require_ready()

    deleted = await db.fact_delete(domain, key)
    if not deleted:
        return {"success": False, "error": f"Fact '{key}' not found in domain '{domain}'"}

    return {"success": True, "domain": domain, "key": key, "message": "Fact deleted."}


@mcp.tool("knowledge_facts_list")
async def knowledge_facts_list(domain: str) -> dict[str, Any]:
    """List all structured facts in a domain.

    Args:
        domain: Domain to list facts for.
    """
    _, _, _, _, db = _require_ready()

    facts = await db.facts_list(domain)
    return {"success": True, "domain": domain, "count": len(facts), "facts": facts}


@mcp.tool("knowledge_facts_search")
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

    if await db.source_exists(content_hash):
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
                print(
                    f"  [KNOWLEDGE] Indexed {file_path.name}: "
                    f"{outcome.get('chunks_stored')} chunks",
                    file=sys.stderr,
                )
            else:
                results.append({
                    "file": file_path.name,
                    "status": "skipped",
                    "reason": outcome.get("reason", "unknown"),
                })

        except Exception as exc:
            results.append({"file": file_path.name, "status": "error", "error": str(exc)})
            print(f"  [KNOWLEDGE] Failed {file_path.name}: {exc}", file=sys.stderr)

    return {
        "success": True,
        "domain": domain,
        "total_chunks": total_chunks,
        "files": results,
    }


@mcp.tool("knowledge_upload_file_base64")
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
    domain_dir.mkdir(parents=True, exist_ok=True)

    dest = domain_dir / clean_filename
    if dest.exists() and not overwrite:
        return {
            "success": False,
            "error": (
                f"File '{clean_filename}' already exists in '{domain}'. "
                "Set overwrite=true to replace."
            ),
        }

    if overwrite:
        existing = await db.source_get_by_filename(domain, clean_filename)
        if existing:
            await vectors.delete_by_source(existing["id"])
            await db.source_remove(existing["id"])

    dest.write_bytes(data)

    return await _ingest_file_at_path(
        settings, embeddings, sparse_encoder, vectors, db,
        dest=dest, domain=domain, force=overwrite,
    )


# ---------------------------------------------------------------------------
# MCP Tools — Search
# ---------------------------------------------------------------------------


@mcp.tool("knowledge_search")
async def knowledge_search(
    query: str,
    domain: str | None = None,
    domains: list[str] | None = None,
    limit: int = 10,
    min_similarity: float = 0.25,
    include_facts: bool = True,
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
    """
    settings, embeddings_client, sparse_encoder, vectors, db = _require_ready()

    resolved_domains = await _resolve_domains(domain, domains)

    # Semantic search
    query_embedding = await embeddings_client.embed(query)
    sparse_query = sparse_encoder.encode_query(query)

    results = await vectors.search(
        query_embedding,
        sparse_query=sparse_query,
        domains=resolved_domains,
        limit=limit,
        min_score=min_similarity,
    )

    formatted = []
    for r in results:
        p = r.payload or {}
        formatted.append({
            "content": p.get("content", ""),
            "domain": p.get("domain", ""),
            "source_name": p.get("source_name", ""),
            "source_type": p.get("source_type", ""),
            "chunk_index": p.get("chunk_index", 0),
            "similarity": round(r.score, 4),
        })

    response: dict[str, Any] = {
        "success": True,
        "query": query,
        "searched_domains": resolved_domains,
        "count": len(formatted),
        "results": formatted,
    }

    # Include relevant facts
    if include_facts:
        # Extract keywords from query for fact matching
        keywords = [w for w in query.lower().split() if len(w) > 2]
        if keywords:
            facts = await db.facts_search(resolved_domains, keywords)
            response["facts"] = facts
            response["fact_count"] = len(facts)

    return response


@mcp.tool("knowledge_sources")
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
        except Exception:
            continue
        url = f"{base}/api/download/{token['token']}"
        safe_label = str(filename).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
        src["download_url"] = url
        src["download_markdown"] = f"[{safe_label}]({url})"
        src["download_expires_at"] = token["expires_at"]
    return {"success": True, "domain": domain, "count": len(sources), "sources": sources}


@mcp.tool("knowledge_source_download_base64")
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
async def knowledge_source_delete(source_id: str, delete_file: bool = True) -> dict[str, Any]:
    """Delete one ingested source by source_id, including its vector chunks.

    Use knowledge_sources(domain) first to find the source_id. Set delete_file=false
    only when you want to remove it from search but keep the stored file.
    """
    settings, _, _, vectors, db = _require_ready()
    return await delete_source_record(settings, vectors, db, source_id, delete_file)


@mcp.tool("knowledge_source_rename")
async def knowledge_source_rename(source_id: str, filename: str) -> dict[str, Any]:
    """Rename one ingested source by source_id.

    Updates SQLite metadata and Qdrant source_name. For standard file uploads,
    also renames the stored raw file when it exists.
    """
    settings, _, _, vectors, db = _require_ready()
    return await rename_source_record(settings, vectors, db, source_id, filename)


# ---------------------------------------------------------------------------
# MCP Tools — Curation Queue
# ---------------------------------------------------------------------------


@mcp.tool("knowledge_curation_list")
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
    return {"success": True, "count": len(items), "items": items}


@mcp.tool("knowledge_curation_get")
async def knowledge_curation_get(item_id: str) -> dict[str, Any]:
    """Get one curation queue item by id."""
    _, _, _, _, db = _require_ready()
    item = await db.curation_get(item_id)
    if not item:
        return {"success": False, "error": f"Curation item '{item_id}' not found"}
    return {"success": True, "item": item}


@mcp.tool("knowledge_curation_apply")
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
async def knowledge_curation_reject(item_id: str) -> dict[str, Any]:
    """Reject a curation queue item without applying any proposed actions."""
    _, _, _, _, db = _require_ready()
    updated = await db.curation_mark_status(item_id, "rejected")
    if not updated:
        return {"success": False, "error": f"Curation item '{item_id}' not found"}
    return {"success": True, "item_id": item_id, "status": "rejected"}


@mcp.tool("knowledge_curation_snooze")
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
        print(f"[KNOWLEDGE] Disabled — config error: {exc}", file=sys.stderr)
        return

    _settings.knowledge_path.mkdir(parents=True, exist_ok=True)
    print(f"[KNOWLEDGE] Knowledge path: {_settings.knowledge_path}", file=sys.stderr)

    _embeddings = EmbeddingClient(_settings)
    _sparse_encoder = BM25SparseEncoder()
    _vectors = KnowledgeVectorStore(_settings)
    _db = KnowledgeDB(_settings.db_path)

    try:
        await _vectors.ensure_collection()
    except Exception as exc:
        print(f"[KNOWLEDGE] Disabled — Qdrant unreachable: {exc}", file=sys.stderr)
        return

    await _db.initialize()

    # Warm up BM25 sparse encoder from existing chunks so hybrid search
    # has meaningful IDF scores on startup rather than a cold zero state.
    try:
        all_chunks = await _vectors.chunks_all()
        texts = [p["content"] for p in all_chunks if p.get("content")]
        if texts:
            _sparse_encoder.fit_batch(texts)
            print(f"[KNOWLEDGE] BM25 warmed up on {len(texts)} existing chunks", file=sys.stderr)
    except Exception as exc:
        print(f"[KNOWLEDGE] BM25 warm-up skipped: {exc}", file=sys.stderr)

    # Ensure 'core' domain exists
    await _db.domain_create(
        "core",
        "Foundational personal profile — always included in searches",
        [],
    )

    _ready = True
    print("[KNOWLEDGE] Initialization complete", file=sys.stderr)


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
