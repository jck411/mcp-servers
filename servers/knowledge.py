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
import contextlib
import hashlib
import json
import os
import re
import secrets
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import httpx
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
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
from shared.logging_config import get_logger, logged_tool

log = get_logger("knowledge")

# Default port for HTTP transport
DEFAULT_HTTP_PORT = 9017

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _auth_provider() -> StaticTokenVerifier | None:
    token = os.environ.get("MCP_KNOWLEDGE_BEARER_TOKEN")
    if not token:
        return None
    return StaticTokenVerifier({token: {"client_id": "knowledge", "scopes": []}})


mcp = FastMCP("knowledge", auth=_auth_provider())


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
        default="anthropic/claude-sonnet-4-6",
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

    _DOMAIN_COLUMNS = "name, description, related_domains, created_at, archived"
    _SOURCE_COLUMNS = (
        "id, domain, source_type, filename, content_hash, chunk_count, "
        "ingested_at, stored_path, media_type, size_bytes"
    )
    _SOURCE_COLUMNS_QUALIFIED = (
        "s.id, s.domain, s.source_type, s.filename, s.content_hash, "
        "s.chunk_count, s.ingested_at, s.stored_path, s.media_type, s.size_bytes"
    )
    _SOURCE_COLUMNS_LIST = (
        "s.id, s.source_type, s.filename, s.content_hash, s.chunk_count, "
        "s.ingested_at, s.stored_path, s.media_type, s.size_bytes"
    )
    _WIKI_PAGE_COLUMNS = (
        "slug, domain, title, kind, status, body_md, frontmatter_json, "
        "fact_count, source_count, created_at, updated_at"
    )
    _WIKI_PAGE_LIST_COLUMNS = (
        "slug, domain, title, kind, status, fact_count, source_count, created_at, updated_at"
    )

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._conn: aiosqlite.Connection | None = None

    @staticmethod
    def _decode_domain_row(row: aiosqlite.Row) -> dict[str, Any]:
        return {
            "name": row["name"],
            "description": row["description"],
            "related_domains": json.loads(row["related_domains"]),
            "created_at": row["created_at"],
            "archived": bool(row["archived"]),
        }

    @staticmethod
    def _decode_wiki_page_row(row: aiosqlite.Row) -> dict[str, Any]:
        page = dict(row)
        raw_frontmatter = str(page.pop("frontmatter_json") or "{}")
        try:
            page["frontmatter"] = json.loads(raw_frontmatter)
        except json.JSONDecodeError:
            page["frontmatter"] = {}
            page["frontmatter_json_error"] = raw_frontmatter
        return page

    async def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys=ON")
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
                origin_type TEXT NOT NULL DEFAULT 'unknown',
                origin_ref TEXT,
                last_confirmed_at TEXT,
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

            CREATE TABLE IF NOT EXISTS wiki_pages (
                slug TEXT PRIMARY KEY,
                domain TEXT NOT NULL,
                title TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (
                    kind IN ('entity', 'concept', 'source_summary', 'index')
                ),
                status TEXT NOT NULL DEFAULT 'candidate' CHECK (
                    status IN ('candidate', 'active', 'archived')
                ),
                body_md TEXT NOT NULL,
                frontmatter_json TEXT NOT NULL,
                fact_count INTEGER NOT NULL DEFAULT 0,
                source_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_wiki_pages_domain
                ON wiki_pages(domain);
            CREATE INDEX IF NOT EXISTS idx_wiki_pages_kind
                ON wiki_pages(kind);

            CREATE TABLE IF NOT EXISTS wiki_page_sources (
                page_slug TEXT NOT NULL REFERENCES wiki_pages(slug) ON DELETE CASCADE,
                source_id TEXT,
                chat_date TEXT,
                contribution TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_wiki_page_sources_unique
                ON wiki_page_sources(page_slug, COALESCE(source_id, ''), COALESCE(chat_date, ''));

            CREATE TABLE IF NOT EXISTS wiki_state (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS wiki_rebuild_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT,
                status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
                scope_json TEXT NOT NULL,
                touched_slugs_json TEXT NOT NULL DEFAULT '[]',
                pages_touched INTEGER NOT NULL DEFAULT 0,
                token_estimate INTEGER NOT NULL DEFAULT 0,
                model TEXT,
                error_summary TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_wiki_rebuild_runs_started_at
                ON wiki_rebuild_runs(started_at);

        """)
        await self._conn.executemany(
            "INSERT OR IGNORE INTO wiki_state (key, value) VALUES (?, ?)",
            (
                ("last_wiki_run", "1970-01-01T00:00:00Z"),
                ("manual_rebuild_requires_confirmation", "true"),
            ),
        )
        await self._ensure_fact_metadata_columns()
        await self._ensure_source_metadata_columns()
        await self._ensure_wiki_metadata_columns()
        await self._conn.commit()

    async def _ensure_fact_metadata_columns(self) -> None:
        """Add fact provenance columns to older Knowledge databases."""
        assert self._conn is not None
        cursor = await self._conn.execute("PRAGMA table_info(facts)")
        existing = {str(row["name"]) for row in await cursor.fetchall()}
        additions = {
            "origin_type": "TEXT NOT NULL DEFAULT 'unknown'",
            "origin_ref": "TEXT",
            "last_confirmed_at": "TEXT",
        }
        for column, declaration in additions.items():
            if column not in existing:
                await self._conn.execute(
                    f"ALTER TABLE facts ADD COLUMN {column} {declaration}"  # noqa: S608
                )

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

    async def _ensure_wiki_metadata_columns(self) -> None:
        """Add wiki lifecycle columns to older Knowledge databases."""
        assert self._conn is not None
        cursor = await self._conn.execute("PRAGMA table_info(wiki_pages)")
        existing = {str(row["name"]) for row in await cursor.fetchall()}
        if "status" not in existing:
            await self._conn.execute(
                "ALTER TABLE wiki_pages ADD COLUMN status TEXT NOT NULL DEFAULT 'candidate' "
                "CHECK (status IN ('candidate', 'active', 'archived'))"
            )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wiki_pages_status ON wiki_pages(status)"
        )

    # -- Domains --

    async def domain_create(
        self, name: str, description: str, related_domains: list[str]
    ) -> bool:
        """Create a domain. Returns False if it already exists."""
        assert self._conn is not None

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
        cursor = await self._conn.execute(f"SELECT {self._DOMAIN_COLUMNS} FROM domains")
        rows = await cursor.fetchall()
        return [self._decode_domain_row(row) for row in rows]

    async def domain_get(self, name: str) -> dict[str, Any] | None:
        assert self._conn is not None
        cursor = await self._conn.execute(
            f"SELECT {self._DOMAIN_COLUMNS} FROM domains WHERE name = ?",
            (name,),
        )
        row = await cursor.fetchone()
        return self._decode_domain_row(row) if row else None

    async def domain_update_related(self, name: str, related_domains: list[str]) -> bool:
        assert self._conn is not None

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
        origin_type: str = "unknown",
        origin_ref: str | None = None,
    ) -> str:
        """Set a fact. Upserts by (domain, key). Returns fact ID."""
        assert self._conn is not None
        now = datetime.now(UTC).isoformat()
        fact_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{domain}:{key}"))

        await self._conn.execute(
            """
            INSERT INTO facts (id, domain, key, value, source, confidence,
                               valid_from, valid_until, origin_type, origin_ref,
                               last_confirmed_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            ON CONFLICT(domain, key) DO UPDATE SET
                last_confirmed_at = CASE
                    WHEN facts.value = excluded.value THEN excluded.updated_at
                    ELSE NULL
                END,
                value = excluded.value,
                source = excluded.source,
                confidence = excluded.confidence,
                valid_from = excluded.valid_from,
                valid_until = excluded.valid_until,
                origin_type = excluded.origin_type,
                origin_ref = excluded.origin_ref,
                updated_at = excluded.updated_at
            """,
            (fact_id, domain, key, value, source, confidence,
             valid_from, valid_until, origin_type, origin_ref, now, now),
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
            "SELECT key, value, source, confidence, valid_from, valid_until, "
            "origin_type, origin_ref, last_confirmed_at, updated_at "
            "FROM facts WHERE domain = ? ORDER BY key",
            (domain,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def facts_search(self, domains: list[str], keys: list[str]) -> list[dict[str, Any]]:
        """Search facts across multiple domains by substring match on key OR value."""
        assert self._conn is not None
        if not domains:
            return []
        placeholders_d = ",".join("?" for _ in domains)
        conditions = [f"domain IN ({placeholders_d})"]
        params: list[Any] = list(domains)

        if keys:
            term_conditions = []
            for k in keys:
                term_conditions.append("(key LIKE ? OR value LIKE ?)")
                params.append(f"%{k}%")
                params.append(f"%{k}%")
            conditions.append(f"({' OR '.join(term_conditions)})")

        where = " AND ".join(conditions)
        cursor = await self._conn.execute(
            f"SELECT domain, key, value, source, confidence, valid_from, valid_until, "  # noqa: S608
            f"origin_type, origin_ref, last_confirmed_at, updated_at "
            f"FROM facts WHERE {where} ORDER BY domain, key",
            params,
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # -- Sources --

    async def source_exists(self, content_hash: str, domain: str | None = None) -> bool:
        assert self._conn is not None
        if domain is not None:
            cursor = await self._conn.execute(
                "SELECT 1 FROM sources WHERE content_hash = ? AND domain = ?",
                (content_hash, domain),
            )
            return await cursor.fetchone() is not None
        cursor = await self._conn.execute(
            "SELECT 1 FROM sources WHERE content_hash = ?", (content_hash,)
        )
        return await cursor.fetchone() is not None

    async def source_get_by_hash(
        self,
        content_hash: str,
        domain: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the first existing source row matching this content hash, if any."""
        assert self._conn is not None
        where = "content_hash = ?"
        params: list[Any] = [content_hash]
        if domain is not None:
            where += " AND domain = ?"
            params.append(domain)
        cursor = await self._conn.execute(
            f"""
            SELECT {self._SOURCE_COLUMNS}
            FROM sources WHERE {where}
            ORDER BY ingested_at ASC LIMIT 1
            """,
            params,
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def source_get_by_filename(self, domain: str, filename: str) -> dict[str, Any] | None:
        """Return the most-recent source row matching domain + filename, if any."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            f"""
            SELECT {self._SOURCE_COLUMNS}
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
            f"""
            SELECT {self._SOURCE_COLUMNS_QUALIFIED}
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
            f"""
            SELECT {self._SOURCE_COLUMNS_QUALIFIED}
            FROM sources s
            WHERE s.domain = ? AND s.filename = ?
            ORDER BY s.ingested_at DESC
            LIMIT 1
            """,
            (domain, filename),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def sources_list_by_domain_filename(
        self, domain: str, filename: str
    ) -> list[dict[str, Any]]:
        """Return all source rows matching domain + filename, newest first."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            f"""
            SELECT {self._SOURCE_COLUMNS_QUALIFIED}
            FROM sources s
            WHERE s.domain = ? AND s.filename = ?
            ORDER BY s.ingested_at DESC
            """,
            (domain, filename),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

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
            f"""
            SELECT {self._SOURCE_COLUMNS_LIST}
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
            SELECT {self._SOURCE_COLUMNS_QUALIFIED}
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

    # -- Wiki Pages --

    async def wiki_get(self, slug: str) -> dict[str, Any] | None:
        assert self._conn is not None
        cursor = await self._conn.execute(
            f"SELECT {self._WIKI_PAGE_COLUMNS} FROM wiki_pages WHERE slug = ?",  # noqa: S608
            (slug,),
        )
        row = await cursor.fetchone()
        if not row:
            return None

        page = self._decode_wiki_page_row(row)
        cursor = await self._conn.execute(
            """
            SELECT source_id, chat_date, contribution
            FROM wiki_page_sources
            WHERE page_slug = ?
            ORDER BY COALESCE(source_id, ''), COALESCE(chat_date, ''), contribution
            """,
            (slug,),
        )
        page["sources"] = [dict(source) for source in await cursor.fetchall()]
        return page

    async def wiki_list(
        self,
        *,
        domain: str | None = None,
        kind: str | None = None,
        status: str = "active",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        assert self._conn is not None
        limit = max(1, min(int(limit or 50), 200))
        conditions: list[str] = []
        params: list[Any] = []
        if domain:
            conditions.append("domain = ?")
            params.append(domain)
        if kind:
            conditions.append("kind = ?")
            params.append(kind)
        if status != "all":
            conditions.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._conn.execute(
            f"""
            SELECT {self._WIKI_PAGE_LIST_COLUMNS}
            FROM wiki_pages
            {where}
            ORDER BY updated_at DESC, slug
            LIMIT ?
            """,  # noqa: S608
            [*params, limit],
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def wiki_search(
        self,
        domains: list[str],
        query: str,
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Search active wiki pages with a lightweight local ranker."""
        assert self._conn is not None
        terms = search_fact_keywords(query)
        if not terms:
            return []
        conditions = ["status = 'active'"]
        params: list[Any] = []
        if domains:
            placeholders = ",".join("?" for _ in domains)
            conditions.append(f"domain IN ({placeholders})")
            params.extend(domains)

        cursor = await self._conn.execute(
            f"""
            SELECT {self._WIKI_PAGE_COLUMNS}
            FROM wiki_pages
            WHERE {' AND '.join(conditions)}
            ORDER BY updated_at DESC
            LIMIT 500
            """,  # noqa: S608
            params,
        )
        ranked = []
        phrase = query.lower().strip()
        for row in await cursor.fetchall():
            page = self._decode_wiki_page_row(row)
            frontmatter = page.get("frontmatter") or {}
            aliases = " ".join(str(a) for a in frontmatter.get("aliases") or [])
            title_text = f"{page['slug']} {page['title']} {aliases}".lower()
            body_text = str(page.get("body_md") or "").lower()
            score = 0
            if phrase and phrase in title_text:
                score += 30
            if phrase and phrase in body_text:
                score += 6
            for term in terms:
                if term in title_text:
                    score += 10
                if term in body_text:
                    score += 1
            if page["kind"] == "index":
                score -= 3
            if score > 0:
                page["score"] = score
                ranked.append(page)
        ranked.sort(key=lambda page: (-int(page["score"]), str(page["slug"])))
        return ranked[:max(1, min(limit, 20))]

    async def wiki_set_status(self, slug: str, status: str) -> bool:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "UPDATE wiki_pages SET status = ?, updated_at = ? WHERE slug = ?",
            (status, datetime.now(UTC).isoformat(), slug),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def wiki_upsert_page(
        self,
        *,
        slug: str,
        domain: str,
        title: str,
        kind: str,
        status: str,
        body_md: str,
        frontmatter: dict[str, Any],
        sources: list[dict[str, Any]],
        fact_count: int,
    ) -> None:
        assert self._conn is not None
        now = datetime.now(UTC).isoformat()
        await self._conn.execute(
            """
            INSERT INTO wiki_pages
                (slug, domain, title, kind, status, body_md, frontmatter_json,
                 fact_count, source_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                domain = excluded.domain,
                title = excluded.title,
                kind = excluded.kind,
                status = excluded.status,
                body_md = excluded.body_md,
                frontmatter_json = excluded.frontmatter_json,
                fact_count = excluded.fact_count,
                source_count = excluded.source_count,
                updated_at = excluded.updated_at
            """,
            (
                slug, domain, title, kind, status, body_md,
                json.dumps(frontmatter, sort_keys=True), fact_count, len(sources), now, now,
            ),
        )
        await self._conn.execute("DELETE FROM wiki_page_sources WHERE page_slug = ?", (slug,))
        seen: set[tuple[str, str]] = set()
        rows = []
        for source in sources:
            source_id = str(source.get("source_id") or "").strip() or None
            chat_date = str(source.get("chat_date") or "").strip() or None
            key = (source_id or "", chat_date or "")
            if not any(key) or key in seen:
                continue
            seen.add(key)
            rows.append((
                slug,
                source_id,
                chat_date,
                str(source.get("contribution") or "cited evidence").strip()[:200],
            ))
        await self._conn.executemany(
            """
            INSERT OR IGNORE INTO wiki_page_sources
                (page_slug, source_id, chat_date, contribution)
            VALUES (?, ?, ?, ?)
            """,
            rows,
        )
        await self._conn.commit()

    async def wiki_state_get(self, key: str, default: str | None = None) -> str | None:
        assert self._conn is not None
        cursor = await self._conn.execute("SELECT value FROM wiki_state WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return str(row["value"]) if row else default

    async def wiki_state_set(self, key: str, value: str) -> None:
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO wiki_state (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await self._conn.commit()

    async def wiki_rebuild_run_start(
        self, *, scope: dict[str, Any], token_estimate: int, model: str | None
    ) -> int:
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO wiki_rebuild_runs (status, scope_json, token_estimate, model)
            VALUES ('running', ?, ?, ?)
            """,
            (json.dumps(scope, sort_keys=True), token_estimate, model),
        )
        await self._conn.commit()
        return int(cursor.lastrowid)

    async def wiki_rebuild_run_finish(
        self,
        run_id: int,
        *,
        status: str,
        touched_slugs: list[str],
        token_estimate: int,
        error_summary: str | None = None,
    ) -> None:
        assert self._conn is not None
        await self._conn.execute(
            """
            UPDATE wiki_rebuild_runs
            SET finished_at = ?, status = ?, touched_slugs_json = ?,
                pages_touched = ?, token_estimate = ?, error_summary = ?
            WHERE id = ?
            """,
            (
                datetime.now(UTC).isoformat(), status, json.dumps(touched_slugs),
                len(touched_slugs), token_estimate, error_summary, run_id,
            ),
        )
        await self._conn.commit()

    async def wiki_rebuild_inputs(
        self,
        *,
        since: str,
        domain: str | None = None,
        force_full: bool = False,
        quiet_after: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        assert self._conn is not None
        fact_conditions: list[str] = []
        source_conditions: list[str] = []
        fact_params: list[Any] = []
        source_params: list[Any] = []
        if domain:
            fact_conditions.append("domain = ?")
            source_conditions.append("domain = ?")
            fact_params.append(domain)
            source_params.append(domain)
        if not force_full:
            fact_conditions.append("updated_at > ?")
            source_conditions.append("ingested_at > ?")
            fact_params.append(since)
            source_params.append(since)
        if quiet_after:
            fact_conditions.append(
                "NOT (origin_type = 'chat' AND created_at > ? AND last_confirmed_at IS NULL)"
            )
            fact_params.append(quiet_after)

        fact_where = f"WHERE {' AND '.join(fact_conditions)}" if fact_conditions else ""
        source_where = f"WHERE {' AND '.join(source_conditions)}" if source_conditions else ""
        cursor = await self._conn.execute(
            f"""
            SELECT domain, key, origin_type, origin_ref, last_confirmed_at, created_at, updated_at
            FROM facts
            {fact_where}
            ORDER BY domain, key
            """,  # noqa: S608
            fact_params,
        )
        facts = [dict(row) for row in await cursor.fetchall()]

        cursor = await self._conn.execute(
            f"""
            SELECT id, domain, source_type, filename, chunk_count, ingested_at
            FROM sources
            {source_where}
            ORDER BY domain, filename, id
            """,  # noqa: S608
            source_params,
        )
        return {"facts": facts, "sources": [dict(row) for row in await cursor.fetchall()]}

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
            WHERE curation_items.status = 'pending' OR excluded.status != 'pending'
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

    async def curation_count(
        self,
        *,
        status: str | None = "pending",
        kind: str | None = None,
    ) -> int:
        assert self._conn is not None
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
            f"SELECT COUNT(*) FROM curation_items {where}",  # noqa: S608
            params,
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

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
        limit = max(1, limit)
        points = []
        offset = None
        while True:
            remaining = limit - len(points)
            batch, offset = await self._client.scroll(
                collection_name=self._collection,
                scroll_filter=Filter(
                    must=[FieldCondition(key="source_id", match=MatchValue(value=source_id))]
                ),
                limit=min(remaining, 256),
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
        limit = max(1, limit)
        points = []
        offset = None
        while True:
            remaining = limit - len(points)
            batch, offset = await self._client.scroll(
                collection_name=self._collection,
                limit=min(remaining, 256),
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
    "Cover the main subject, setting, notable people (no names needed), "
    "any visible text, and specific details that would help someone find this "
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
    "to a 2-3 sentence description and set 'facts' to {}; do not create facts for "
    "visible clothing, background objects, sky, seating, hair, or similar photo details.\n"
    "- For documents: set 'facts' to all extracted key/value pairs, set 'caption' to null.\n"
    "- Omit fields that are not legible or not present — do not set null or 'unknown'.\n"
    "- Put brief uncertainty notes in 'warnings', e.g. unreadable or partially obscured fields.\n"
    "- Output only valid JSON. No markdown fences, no commentary.\n"
    "Output format: {\"facts\": {\"key\": \"value\", ...}, \"caption\": null, \"warnings\": []}"
)


def _decode_llm_json_object(raw_output: str) -> dict[str, Any]:
    clean = raw_output.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```[a-zA-Z]*\n?", "", clean)
        clean = re.sub(r"\n?```$", "", clean.rstrip())
    if not clean.startswith("{"):
        match = re.search(r"\{[\s\S]*\}", clean)
        if match:
            clean = match.group(0)
    decoded, _ = json.JSONDecoder().raw_decode(clean)
    if not isinstance(decoded, dict):
        raise ValueError("JSON root must be an object")
    return decoded


async def _vision_ocr_bytes(
    image_bytes: bytes, media_type: str, settings: KnowledgeSettings
) -> str:
    """OCR an image via OpenRouter vision LLM. Returns text or empty on failure."""
    if not settings.vision_model or not settings.openrouter_api_key:
        return ""

    b64 = base64.b64encode(image_bytes).decode("ascii")
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
        log.warning(
            "vision_ocr_failed model=%s error=%r", settings.vision_model, exc,
        )
        return ""


async def _tesseract_image(path: Path, language: str) -> str:
    rc, out, _ = await _run(["tesseract", str(path), "-", "-l", language])
    return out.decode("utf-8", errors="replace") if rc == 0 else ""


_IMAGE_MEDIA = {
    ".avif": "image/avif",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".heic": "image/heic", ".heif": "image/heif",
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

    b64 = base64.b64encode(image_bytes).decode("ascii")
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
        log.warning("vision_call failed model=%s step=%s error=%r", model, step_name, exc)
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
    text = text.strip()
    if not text:
        return []

    max_chars = max(1, int(max_chars or 1000))
    overlap = max(0, min(int(overlap or 0), max_chars - 1))
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current = ""

    def append_current() -> None:
        nonlocal current
        clean = current.strip()
        if clean:
            chunks.append(clean)
        current = ""

    def append_long_segment(segment: str) -> None:
        start = 0
        while start < len(segment):
            end = min(start + max_chars, len(segment))
            piece = segment[start:end].strip()
            if piece:
                chunks.append(piece)
            if end >= len(segment):
                break
            start = end - overlap if overlap else end

    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue

        if len(para) > max_chars:
            append_current()
            append_long_segment(para)
            continue

        separator = "\n\n" if current else ""
        if len(current) + len(separator) + len(para) <= max_chars:
            current = f"{current}{separator}{para}" if current else para
        else:
            append_current()
            if chunks and overlap > 0:
                prefix = chunks[-1][-overlap:].strip()
                candidate = f"{prefix}\n\n{para}" if prefix else para
                current = candidate if len(candidate) <= max_chars else para
            else:
                current = para
    append_current()

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

    existing = await db.source_get_by_hash(file_hash, domain=domain)
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

    if is_image:
        source_path = resolve_source_path(settings.knowledge_path, source)
        if not source_path:
            pipeline.append({
                "step": "load_source", "status": "failed",
                "note": "file not found on disk",
            })
            return {
                "success": False,
                "error": "Source file not found on disk",
                "pipeline": pipeline,
            }
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
            return {
                "success": False,
                "error": f"Could not read source file: {exc}",
                "pipeline": pipeline,
            }

        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{image_media_type};base64,{b64}"
        hint_text = f"\nDocument type hint: {hint}" if hint else ""
        user_content = [
            {"type": "text", "text": f"Extract all information from this document.{hint_text}"},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
    elif chunk_count == 0:
        pipeline.append({
            "step": "load_chunks", "status": "failed",
            "note": "source has no indexed text chunks and is not a supported image",
        })
        return {
            "success": False,
            "error": (
                "Source has no indexed text chunks; "
                "Extract Facts only supports images or indexed text"
            ),
            "pipeline": pipeline,
        }
    else:
        chunks = await vectors.chunks_by_source(source_id)
        text_body = "\n\n".join(
            str(c.get("content") or "").strip() for c in chunks if c.get("content")
        )
        pipeline.append({
            "step": "load_chunks", "status": "ok",
            "note": f"{len(chunks)} chunks, {len(text_body)} chars total",
        })
        if not text_body.strip():
            return {
                "success": False,
                "error": "No stored chunk text found for source",
                "pipeline": pipeline,
            }
        hint_text = f"\nDocument type hint: {hint}" if hint else ""
        user_content = (
            f"Extract all information from this document.{hint_text}\n\n---\n\n{text_body}"
        )

    # --- Step 2: call extraction model ---
    is_claude = "anthropic" in settings.extraction_model or "claude" in settings.extraction_model
    messages: list[dict[str, Any]] = [{
        "role": "user",
        "content": (
            user_content if isinstance(user_content, list)
            else [{"type": "text", "text": user_content}]
        ),
    }]

    # Forced tool use: the model MUST call store_extracted_facts, so it returns
    # structured JSON regardless of whether it would otherwise output markdown.
    # This is the only reliable way to get structured output from Claude via OpenRouter.
    _extract_tool: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": "store_extracted_facts",
            "description": "Store all facts extracted from the document",
            "parameters": {
                "type": "object",
                "properties": {
                    "facts": {
                        "type": "object",
                        "description": (
                            "All extracted key-value pairs using stable snake_case keys "
                            "with meaningful prefixes (e.g. w2_2025_box1_wages). "
                            "Dates in ISO format YYYY-MM-DD. Currency as numbers only."
                        ),
                        "additionalProperties": {"type": "string"},
                    },
                    "caption": {
                        "type": ["string", "null"],
                        "description": (
                            "2-3 sentence description for photos/images with no document "
                            "structure. null for documents."
                        ),
                    },
                    "warnings": {
                        "type": "array",
                        "description": "Brief notes about unreadable, obscured, or skipped values.",
                        "items": {"type": "string"},
                    },
                },
                "required": ["facts", "caption"],
            },
        },
    }

    payload: dict[str, Any] = {
        "model": settings.extraction_model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": 4096,
        "tools": [_extract_tool],
        "tool_choice": {"type": "function", "function": {"name": "store_extracted_facts"}},
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
            msg = data["choices"][0]["message"]
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                # Forced tool call — arguments are already structured JSON
                raw_output = tool_calls[0]["function"]["arguments"]
            else:
                # Fallback: model returned text content instead of a tool call
                raw_output = (msg.get("content") or "").strip()
            usage = data.get("usage") or {}
            llm_step["tokens_in"] = usage.get("prompt_tokens", 0)
            llm_step["tokens_out"] = usage.get("completion_tokens", 0)
            llm_step["cache_read_tokens"] = usage.get("cache_read_input_tokens", 0)
            llm_step["status"] = "ok"
            output_type = "tool_call" if tool_calls else "text"
            cache_note = (
                f", {llm_step['cache_read_tokens']} cached tokens"
                if llm_step["cache_read_tokens"]
                else ""
            )
            llm_step["note"] = f"{output_type}, {len(raw_output)} chars{cache_note}"
    except Exception as exc:  # noqa: BLE001
        # Capture response body for HTTP errors to expose the provider's error message
        body = ""
        if hasattr(exc, "response") and exc.response is not None:  # type: ignore[union-attr]
            with contextlib.suppress(Exception):
                body = exc.response.text[:500]  # type: ignore[union-attr]
        llm_step["note"] = f"{exc}" + (f" | body: {body}" if body else "")
        pipeline.append(llm_step)
        return {"success": False, "error": f"LLM call failed: {exc}", "pipeline": pipeline}
    pipeline.append(llm_step)

    # --- Step 3: parse JSON ---
    parse_step: dict[str, Any] = {"step": "parse_json", "model": None}
    try:
        extracted = _decode_llm_json_object(raw_output)
        raw_caption = extracted.get("caption")
        caption: str | None = str(raw_caption) if raw_caption not in (None, "") else None
        raw_warnings = extracted.get("warnings") or []
        if isinstance(raw_warnings, list):
            warnings = [str(w) for w in raw_warnings if w]
        elif raw_warnings:
            warnings = [str(raw_warnings)]
        else:
            warnings = []
        if "facts" in extracted:
            raw_facts = extracted.get("facts") or {}
            if not isinstance(raw_facts, dict):
                raise ValueError("'facts' must be an object")
            facts: dict[str, str] = {
                str(k): str(v) for k, v in raw_facts.items()
                if v is not None and v != ""
            }
        else:
            facts = {
                str(k): str(v) for k, v in extracted.items()
                if k not in {"caption", "warnings"} and v is not None and v != ""
            }
        if is_image and caption and facts and not hint:
            warnings.append(
                f"Skipped {len(facts)} photo-detail fact(s); ordinary photos use captions."
            )
            facts = {}
        parse_step["status"] = "ok"
        warn_note = f", {len(warnings)} warning(s)" if warnings else ""
        parse_step["note"] = (
            f"{len(facts)} fact(s), caption={'yes' if caption else 'no'}{warn_note}"
        )
    except (json.JSONDecodeError, ValueError) as exc:
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
            await db.fact_set(
                domain,
                key,
                str(value),
                source=f"extracted:{source_id}",
                confidence=0.9,
                origin_type="extracted",
                origin_ref=source_id,
            )
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
            sparse_encoder.fit_batch([caption])
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
        "warnings": warnings,
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


WIKI_PAGE_STATUSES = frozenset({"candidate", "active", "archived"})
WIKI_PAGE_LIST_STATUSES = WIKI_PAGE_STATUSES | frozenset({"all"})
WIKI_PAGE_KINDS = frozenset({"entity", "concept", "source_summary", "index"})
WIKI_REBUILD_QUIET_WINDOW = timedelta(hours=1)
WIKI_REBUILD_STOPWORDS = frozenset({
    "a", "an", "and", "chat", "current", "doc", "document", "file", "for", "from",
    "latest", "log", "manual", "my", "note", "notes", "of", "pdf", "record", "records",
    "report", "source", "summary", "the", "upload",
})
WIKI_PHOTO_DETAIL_PREFIXES = frozenset({
    "bleacher", "clothing", "hair", "pants", "photo", "railing", "seating",
    "shirt", "shoe", "sky",
})
WIKI_PAGE_SYSTEM_PROMPT = (
    "You maintain concise personal knowledge wiki pages. Return only the forced "
    "tool call. Write traceable markdown from the supplied facts and chunks only. "
    "Do not invent missing details. Use Open Questions for gaps or conflicts. "
    "Every concrete claim should be covered by a source_id or chat_date citation. "
    "Flag duplicate or split concerns instead of resolving identity silently. "
    "Do not create standalone wiki pages for ordinary photo details such as clothing, "
    "hair, seating, sky, background objects, or other visible incidental objects; keep "
    "those details in source captions or a person/event page."
)


def _wiki_slug_for_change(domain: str, text: str) -> str:
    terms = [
        term for term in re.findall(r"[a-z0-9]+", text.lower())
        if term not in WIKI_REBUILD_STOPWORDS
    ]
    if not terms:
        return f"{domain}/index"
    tail = f"{terms[0]}-{terms[1]}" if len(terms) > 1 and terms[1].isdigit() else terms[0]
    return f"{domain}/{tail}"


def _wiki_title_from_slug(slug: str) -> str:
    return " ".join(part.upper() if part.isdigit() else part.title()
                    for part in slug.rsplit("/", 1)[-1].split("-"))


def _wiki_fact_can_seed_page(key: str) -> bool:
    return key.split("_", 1)[0].lower() not in WIKI_PHOTO_DETAIL_PREFIXES


def _wiki_row_matches_slug(slug: str, domain: str, text: str) -> bool:
    if not slug.startswith(f"{domain}/"):
        return False
    tail = slug.rsplit("/", 1)[-1]
    terms = re.findall(r"[a-z0-9]+", text.lower())
    return _wiki_slug_for_change(domain, text) == slug or tail.replace("-", " ") in " ".join(terms)


def _wiki_latency_class(pages: int, tokens: int) -> str:
    if pages <= 5 and tokens < 20_000:
        return "quick"
    if pages <= 20 and tokens < 80_000:
        return "medium"
    return "slow"


async def preview_wiki_rebuild(
    settings: KnowledgeSettings,
    db: KnowledgeDB,
    *,
    domain: str | None = None,
    entity_slug: str | None = None,
    force_full: bool = False,
) -> dict[str, Any]:
    clean_domain = domain.strip() if domain else None
    clean_entity = entity_slug.strip() if entity_slug else None
    if clean_entity:
        if "/" not in clean_entity:
            return {"success": False, "error": "entity_slug must use '<domain>/<slug>'"}
        entity_domain = clean_entity.split("/", 1)[0]
        if clean_domain and clean_domain != entity_domain:
            return {"success": False, "error": "domain must match entity_slug domain"}
        clean_domain = entity_domain

    since = "1970-01-01T00:00:00Z" if force_full else await db.wiki_state_get(
        "last_wiki_run", "1970-01-01T00:00:00Z"
    )
    quiet_after = (datetime.now(UTC) - WIKI_REBUILD_QUIET_WINDOW).isoformat()
    inputs = await db.wiki_rebuild_inputs(
        since=since or "1970-01-01T00:00:00Z",
        domain=clean_domain,
        force_full=force_full,
        quiet_after=quiet_after,
    )
    entities: dict[str, dict[str, Any]] = {}

    def ensure_entity(slug: str, item_domain: str) -> dict[str, Any]:
        return entities.setdefault(slug, {
            "slug": slug,
            "domain": item_domain,
            "title": _wiki_title_from_slug(slug),
            "fact_count": 0,
            "source_count": 0,
            "fact_keys": [],
            "source_ids": [],
        })

    for fact in inputs["facts"]:
        item_domain = str(fact["domain"])
        text = str(fact["key"])
        if not _wiki_fact_can_seed_page(text):
            continue
        if clean_entity and not _wiki_row_matches_slug(clean_entity, item_domain, text):
            continue
        slug = clean_entity or _wiki_slug_for_change(item_domain, text)
        entity = ensure_entity(slug, item_domain)
        entity["fact_count"] += 1
        entity["fact_keys"].append(text)

    for source in inputs["sources"]:
        item_domain = str(source["domain"])
        text = str(source.get("filename") or source["id"])
        if clean_entity and not _wiki_row_matches_slug(clean_entity, item_domain, text):
            continue
        slug = clean_entity or _wiki_slug_for_change(item_domain, text)
        entity = ensure_entity(slug, item_domain)
        entity["source_count"] += 1
        entity["source_ids"].append(source["id"])

    if clean_entity:
        page = await db.wiki_get(clean_entity)
        entity = ensure_entity(clean_entity, clean_domain or clean_entity.split("/", 1)[0])
        if page:
            entity["title"] = page["title"]
    elif force_full:
        for page in await db.wiki_list(domain=clean_domain, status="all", limit=200):
            if page["kind"] != "index":
                ensure_entity(str(page["slug"]), str(page["domain"]))["title"] = page["title"]

    changed_entities = sorted(entities.values(), key=lambda item: item["slug"])
    entity_pages = len(changed_entities)
    index_pages = len({item["domain"] for item in changed_entities}) if entity_pages else 0
    token_estimate = sum(
        1_200 + item["fact_count"] * 180 + item["source_count"] * 650
        for item in changed_entities
    ) + index_pages * 500
    estimated_pages = entity_pages + index_pages
    return {
        "success": True,
        "dry_run": True,
        "writes_performed": False,
        "scope": {
            "domain": clean_domain,
            "entity_slug": clean_entity,
            "force_full": force_full,
            "since": since,
            "quiet_window_hours": WIKI_REBUILD_QUIET_WINDOW.total_seconds() / 3600,
        },
        "changed_entities": changed_entities,
        "estimated_entity_pages": entity_pages,
        "estimated_index_pages": index_pages,
        "estimated_pages": estimated_pages,
        "token_estimate": token_estimate,
        "estimated_cost": {
            "currency": "USD",
            "low": None,
            "high": None,
            "note": "model pricing is not configured; use token_estimate for cost planning",
        },
        "latency_class": _wiki_latency_class(estimated_pages, token_estimate),
        "model": settings.extraction_model,
    }


def _wiki_slug_terms(slug: str) -> list[str]:
    tail = slug.rsplit("/", 1)[-1]
    return [
        term for term in re.findall(r"[a-z0-9]+", tail.lower())
        if term not in WIKI_REBUILD_STOPWORDS
    ]


def _wiki_iso_date(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else None


def _wiki_fact_source_id(fact: dict[str, Any]) -> str | None:
    origin_ref = str(fact.get("origin_ref") or "").strip()
    if origin_ref and not _wiki_iso_date(origin_ref):
        return origin_ref
    source = str(fact.get("source") or "")
    return source.split(":", 1)[1].strip() if source.startswith("extracted:") else None


def _wiki_source_rows(
    facts: list[dict[str, Any]],
    sources: dict[str, dict[str, Any]],
    chunks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}

    def add(
        *,
        source_id: str | None = None,
        chat_date: str | None = None,
        contribution: str = "cited evidence",
    ) -> None:
        key = (source_id or "", chat_date or "")
        if any(key) and key not in rows:
            rows[key] = {
                "source_id": source_id,
                "chat_date": chat_date,
                "contribution": contribution[:200],
            }

    for fact in facts:
        contribution = f"fact: {fact.get('key')}"
        if fact.get("origin_type") == "chat" and (
            chat_date := _wiki_iso_date(fact.get("origin_ref"))
        ):
            add(chat_date=chat_date, contribution=contribution)
        elif (source_id := _wiki_fact_source_id(fact)) and source_id in sources:
            add(source_id=source_id, contribution=contribution)

    for source_id, source in sources.items():
        add(
            source_id=source_id,
            contribution=str(source.get("filename") or source.get("source_type") or "source"),
        )

    for chunk in chunks:
        source_id = str(chunk.get("source_id") or "").strip()
        if source_id:
            add(
                source_id=source_id,
                contribution=str(chunk.get("source_name") or "matched source chunk"),
            )
    return list(rows.values())


def _wiki_merge_source_rows(
    rows: list[dict[str, Any]],
    *,
    allowed_source_ids: set[str],
    allowed_chat_dates: set[str],
) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        source_id = str(row.get("source_id") or "").strip() or None
        chat_date = _wiki_iso_date(row.get("chat_date"))
        if source_id and source_id not in allowed_source_ids:
            continue
        if chat_date and chat_date not in allowed_chat_dates:
            continue
        key = (source_id or "", chat_date or "")
        if any(key) and key not in merged:
            merged[key] = {
                "source_id": source_id,
                "chat_date": chat_date,
                "contribution": str(row.get("contribution") or "cited evidence").strip()[:200],
            }
    return list(merged.values())


def _wiki_citation_lines(
    rows: list[dict[str, Any]], sources: dict[str, dict[str, Any]]
) -> list[str]:
    lines = []
    for row in rows:
        if row.get("source_id"):
            source_id = str(row["source_id"])
            title = sources.get(source_id, {}).get("filename")
            label = f"Source: {source_id}" + (f" - {title}" if title else "")
        else:
            label = f"Chat: {row.get('chat_date')}"
        contribution = str(row.get("contribution") or "").strip()
        lines.append(label + (f" ({contribution})" if contribution else ""))
    return lines


def _wiki_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [str(value)] if value else []


def _wiki_generated_page(
    raw_page: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    slug = str(context["slug"])
    domain = str(context["domain"])
    existing = context.get("existing_page") or {}
    title = str(raw_page.get("title") or context["title"]).strip()[:200]
    kind = str(raw_page.get("kind") or "entity").strip()
    kind = kind if kind in WIKI_PAGE_KINDS - {"index"} else "entity"
    raw_frontmatter = raw_page.get("frontmatter")
    frontmatter = dict(raw_frontmatter) if isinstance(raw_frontmatter, dict) else {}
    confidence = str(
        raw_page.get("confidence") or frontmatter.get("confidence") or "medium"
    ).lower()
    confidence = confidence if confidence in {"high", "medium", "low"} else "medium"

    default_rows = _wiki_source_rows(context["facts"], context["sources"], context["chunks"])
    llm_rows = raw_page.get("sources") if isinstance(raw_page.get("sources"), list) else []
    source_rows = _wiki_merge_source_rows(
        [*llm_rows, *default_rows],
        allowed_source_ids=set(context["sources"]) | {
            str(c.get("source_id")) for c in context["chunks"] if c.get("source_id")
        },
        allowed_chat_dates={
            date for f in context["facts"] if (date := _wiki_iso_date(f.get("origin_ref")))
        },
    )
    source_ids = sorted({str(row["source_id"]) for row in source_rows if row.get("source_id")})
    chat_dates = sorted({str(row["chat_date"]) for row in source_rows if row.get("chat_date")})

    body = str(raw_page.get("body_md") or "").strip()
    if not body:
        body = f"{title} is tracked as a {domain} wiki page."
    if source_rows and "## Sources" not in body:
        body += "\n\n## Sources\n" + "\n".join(
            f"- {line}" for line in _wiki_citation_lines(source_rows, context["sources"])
        )

    concerns = {
        "merge_candidate": _wiki_str_list(raw_page.get("duplicate_concerns")),
        "split_candidate": _wiki_str_list(raw_page.get("split_concerns")),
    }
    audit_notes = {kind: values for kind, values in concerns.items() if values}

    frontmatter.update({
        "schema_version": 1,
        "slug": slug,
        "title": title,
        "kind": kind,
        "domain": domain,
        "entity_type": str(frontmatter.get("entity_type") or "unknown"),
        "aliases": _wiki_str_list(frontmatter.get("aliases")),
        "related_slugs": _wiki_str_list(frontmatter.get("related_slugs")),
        "source_ids": source_ids,
        "chat_dates": chat_dates,
        "confidence": confidence,
        "orphan": bool(frontmatter.get("orphan", False)),
        "audit_notes": audit_notes,
    })

    status = existing.get("status")
    if status not in WIKI_PAGE_STATUSES:
        status = (
            "active"
            if confidence == "high" and source_rows and not any(concerns.values())
            else "candidate"
        )
    return {
        "slug": slug,
        "domain": domain,
        "title": title,
        "kind": kind,
        "status": status,
        "body_md": body,
        "frontmatter": frontmatter,
        "sources": source_rows,
        "fact_count": len(context["facts"]),
        "concerns": concerns,
    }


async def _wiki_context(
    embeddings: EmbeddingClient,
    sparse_encoder: BM25SparseEncoder,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
    entity: dict[str, Any],
) -> dict[str, Any]:
    slug = str(entity["slug"])
    domain = str(entity["domain"])
    title = str(entity.get("title") or _wiki_title_from_slug(slug))
    fact_keys = set(entity.get("fact_keys") or [])
    terms = _wiki_slug_terms(slug)
    facts = [
        fact for fact in await db.facts_list(domain)
        if fact.get("key") in fact_keys
        or any(term in f"{fact.get('key', '')} {fact.get('value', '')}".lower() for term in terms)
    ]

    chunks: list[dict[str, Any]] = []
    for source_id in entity.get("source_ids") or []:
        with contextlib.suppress(Exception):
            chunks.extend(await vectors.chunks_by_source(str(source_id), limit=4))

    with contextlib.suppress(Exception):
        query = " ".join([title, *terms])
        query_embedding = await embeddings.embed(query)
        sparse_query = sparse_encoder.encode_query(query)
        for point in await vectors.search(
            query_embedding,
            sparse_query=sparse_query,
            domains=[domain],
            limit=8,
            min_score=0.15,
        ):
            chunks.append(dict(point.payload or {}))

    unique_chunks: dict[tuple[str, int, str], dict[str, Any]] = {}
    for chunk in chunks:
        key = (
            str(chunk.get("source_id") or ""),
            int(chunk.get("chunk_index") or 0),
            str(chunk.get("id") or ""),
        )
        if key not in unique_chunks:
            copy = dict(chunk)
            copy["content"] = str(copy.get("content") or "")[:1400]
            unique_chunks[key] = copy

    source_ids = set(entity.get("source_ids") or [])
    source_ids.update(str(c.get("source_id")) for c in unique_chunks.values() if c.get("source_id"))
    source_ids.update(sid for f in facts if (sid := _wiki_fact_source_id(f)))
    sources = {}
    for source_id in sorted(str(s) for s in source_ids if s):
        source = await db.source_get(source_id)
        if source:
            sources[source_id] = source

    return {
        "slug": slug,
        "domain": domain,
        "title": title,
        "facts": facts,
        "chunks": list(unique_chunks.values())[:12],
        "sources": sources,
        "existing_page": await db.wiki_get(slug),
    }


async def _call_wiki_llm(
    settings: KnowledgeSettings,
    context: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    if not settings.extraction_model:
        raise RuntimeError("KNOWLEDGE_EXTRACTION_MODEL not configured")
    tool = {
        "type": "function",
        "function": {
            "name": "write_wiki_page",
            "description": "Return one generated wiki page with citations and concerns.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "kind": {"type": "string", "enum": ["entity", "concept", "source_summary"]},
                    "body_md": {"type": "string"},
                    "frontmatter": {"type": "object"},
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source_id": {"type": ["string", "null"]},
                                "chat_date": {"type": ["string", "null"]},
                                "contribution": {"type": "string"},
                            },
                        },
                    },
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "duplicate_concerns": {"type": "array", "items": {"type": "string"}},
                    "split_concerns": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "kind", "body_md", "frontmatter", "sources", "confidence"],
            },
        },
    }
    payload: dict[str, Any] = {
        "model": settings.extraction_model,
        "messages": [{
            "role": "user",
            "content": json.dumps({
                "slug": context["slug"],
                "domain": context["domain"],
                "title": context["title"],
                "facts": context["facts"],
                "sources": list(context["sources"].values()),
                "chunks": context["chunks"],
                "existing_page": context["existing_page"],
            }, default=str),
        }],
        "temperature": 0,
        "max_tokens": 4096,
        "tools": [tool],
        "tool_choice": {"type": "function", "function": {"name": "write_wiki_page"}},
    }
    if "claude" in settings.extraction_model or "anthropic" in settings.extraction_model:
        payload["system"] = [{
            "type": "text",
            "text": WIKI_PAGE_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }]
    else:
        payload["system"] = WIKI_PAGE_SYSTEM_PROMPT

    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
    msg = data["choices"][0]["message"]
    tool_calls = msg.get("tool_calls") or []
    raw_output = (
        tool_calls[0]["function"]["arguments"]
        if tool_calls else (msg.get("content") or "").strip()
    )
    usage = data.get("usage") or {}
    return _decode_llm_json_object(raw_output), int(usage.get("total_tokens") or 0)


async def _rebuild_wiki_index(db: KnowledgeDB, domain: str) -> str:
    pages = [
        page for page in await db.wiki_list(domain=domain, status="active", limit=200)
        if page["kind"] != "index"
    ]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for page in pages:
        grouped.setdefault(str(page["kind"]), []).append(page)
    lines = [f"# {domain.replace('_', ' ').title()} Index"]
    for kind, items in sorted(grouped.items()):
        lines.extend(["", f"## {kind.replace('_', ' ').title()}"])
        lines.extend(f"- `{item['slug']}` - {item['title']}" for item in items)
    slug = f"{domain}/index"
    await db.wiki_upsert_page(
        slug=slug,
        domain=domain,
        title=f"{domain.replace('_', ' ').title()} Index",
        kind="index",
        status="active" if pages else "candidate",
        body_md="\n".join(lines),
        frontmatter={
            "schema_version": 1,
            "slug": slug,
            "title": f"{domain.replace('_', ' ').title()} Index",
            "kind": "index",
            "domain": domain,
            "entity_type": "index",
            "aliases": [],
            "related_slugs": [str(page["slug"]) for page in pages],
            "source_ids": [],
            "chat_dates": [],
            "confidence": "high" if pages else "medium",
            "orphan": False,
        },
        sources=[],
        fact_count=0,
    )
    return slug


async def rebuild_wiki(
    settings: KnowledgeSettings,
    embeddings: EmbeddingClient,
    sparse_encoder: BM25SparseEncoder,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
    *,
    domain: str | None = None,
    entity_slug: str | None = None,
    force_full: bool = False,
) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    preview = await preview_wiki_rebuild(
        settings, db, domain=domain, entity_slug=entity_slug, force_full=force_full,
    )
    if not preview["success"]:
        return preview

    run_id = await db.wiki_rebuild_run_start(
        scope=preview["scope"],
        token_estimate=preview["token_estimate"],
        model=preview["model"],
    )
    touched: list[str] = []
    usage_tokens = 0
    try:
        for entity in preview["changed_entities"]:
            context = await _wiki_context(embeddings, sparse_encoder, vectors, db, entity)
            raw_page, tokens = await _call_wiki_llm(settings, context)
            usage_tokens += tokens
            page = _wiki_generated_page(raw_page, context)
            await db.wiki_upsert_page(
                slug=page["slug"],
                domain=page["domain"],
                title=page["title"],
                kind=page["kind"],
                status=page["status"],
                body_md=page["body_md"],
                frontmatter=page["frontmatter"],
                sources=page["sources"],
                fact_count=page["fact_count"],
            )
            touched.append(page["slug"])

        for touched_domain in sorted({str(item["domain"]) for item in preview["changed_entities"]}):
            touched.append(await _rebuild_wiki_index(db, touched_domain))

        await db.wiki_state_set("last_wiki_run", started_at)
        final_tokens = usage_tokens or preview["token_estimate"]
        await db.wiki_rebuild_run_finish(
            run_id, status="success", touched_slugs=touched, token_estimate=final_tokens,
        )
        log.info(
            "wiki_rebuild_success run_id=%s pages=%s tokens=%s touched=%s",
            run_id, len(touched), final_tokens, touched,
        )
        return {
            "success": True,
            "dry_run": False,
            "writes_performed": True,
            "run_id": run_id,
            "scope": preview["scope"],
            "changed_entities": preview["changed_entities"],
            "pages_touched": len(touched),
            "touched_slugs": touched,
            "token_estimate": final_tokens,
            "model": preview["model"],
        }
    except Exception as exc:  # noqa: BLE001
        final_tokens = usage_tokens or preview["token_estimate"]
        await db.wiki_rebuild_run_finish(
            run_id,
            status="failed",
            touched_slugs=touched,
            token_estimate=final_tokens,
            error_summary=str(exc)[:500],
        )
        log.exception("wiki_rebuild_failed run_id=%s touched=%s", run_id, touched)
        return {
            "success": False,
            "error": f"wiki rebuild failed: {exc}",
            "run_id": run_id,
            "touched_slugs": touched,
        }


SUPPORTED_CURATION_ACTIONS = {
    "archive_domain",
    "delete_source",
    "domain_archive",
    "fact_delete",
    "fact_set",
    "fact_update_validity",
    "flag_for_review",
    "ingest_text",
    "no_action",
}

DESTRUCTIVE_CURATION_ACTIONS = {
    "archive_domain",
    "delete_source",
    "domain_archive",
    "fact_delete",
}


def validate_curation_actions(actions: list[dict[str, Any]]) -> str | None:
    """Return an error string when proposed curation actions are malformed."""
    if not actions:
        return "At least one proposed action is required"
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            return f"Action {index} must be an object"
        action_type = str(action.get("action") or action.get("type") or "").strip()
        if not action_type:
            return f"Action {index} is missing 'action' or 'type'"
        if action_type not in SUPPORTED_CURATION_ACTIONS:
            supported = ", ".join(sorted(SUPPORTED_CURATION_ACTIONS))
            return f"Unsupported curation action '{action_type}'. Supported: {supported}"
    return None


def _curation_title(kind: str, notes: str) -> str:
    cleaned = " ".join(notes.split())
    if cleaned:
        return cleaned[:77] + "..." if len(cleaned) > 80 else cleaned
    return kind.replace("_", " ").title()


async def create_curation_queue_item(
    *,
    db: KnowledgeDB,
    actions: list[dict[str, Any]],
    notes: str,
    kind: str = "uncertain_fact",
    title: str | None = None,
    source_refs: list[dict[str, Any]] | None = None,
    risk: str = "medium",
    confidence: float = 0.0,
    item_id: str | None = None,
    status: str = "pending",
    created_at: str | None = None,
) -> dict[str, Any]:
    error = validate_curation_actions(actions)
    if error:
        return {"success": False, "error": error}

    curation_id = await db.curation_upsert(
        kind=kind,
        title=title or _curation_title(kind, notes),
        summary=notes,
        source_refs=source_refs or [],
        proposed_actions=actions,
        risk=risk,
        confidence=confidence,
        item_id=item_id,
        status=status,
        created_at=created_at,
    )
    return {"success": True, "item_id": curation_id, "item": await db.curation_get(curation_id)}


def curation_item_has_destructive_actions(item: dict[str, Any]) -> bool:
    """Return True when a curation item proposes removing or archiving data."""
    for action in item.get("proposed_actions") or []:
        action_type = str(action.get("action") or action.get("type") or "")
        if action_type in DESTRUCTIVE_CURATION_ACTIONS:
            return True
    return False


CURATION_PACK_STATUSES = frozenset({"applied", "rejected", "snoozed"})
CURATION_PACK_ITEM_LIMIT = 200
CURATION_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


def _curation_action_type(action: dict[str, Any]) -> str:
    return str(action.get("action") or action.get("type") or "unknown").strip() or "unknown"


def _curation_item_action_types(item: dict[str, Any]) -> list[str]:
    actions = item.get("proposed_actions") or []
    return [_curation_action_type(action) for action in actions if isinstance(action, dict)]


def _curation_item_domain(item: dict[str, Any]) -> str | None:
    for action in item.get("proposed_actions") or []:
        if not isinstance(action, dict):
            continue
        domain = str(action.get("domain") or "").strip()
        if domain:
            return domain
        slug = str(action.get("slug") or "").strip()
        if "/" in slug:
            return slug.split("/", 1)[0]
    for ref in item.get("source_refs") or []:
        if isinstance(ref, dict):
            domain = str(ref.get("domain") or "").strip()
            if domain:
                return domain
    return None


def _curation_item_text(item: dict[str, Any]) -> str:
    return " ".join((
        str(item.get("kind") or ""),
        str(item.get("title") or ""),
        str(item.get("summary") or ""),
        json.dumps(item.get("source_refs") or [], sort_keys=True),
        json.dumps(item.get("proposed_actions") or [], sort_keys=True),
    )).lower()


def _curation_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "unknown"


def _curation_title_topic(item: dict[str, Any]) -> str:
    title = str(item.get("title") or "").strip()
    if ":" in title:
        title = title.split(":", 1)[1].strip()
    return _curation_slug(title)


def _curation_group_key(item: dict[str, Any]) -> tuple[str, str, str]:
    kind = str(item.get("kind") or "unknown")
    domain = _curation_item_domain(item) or "unknown"
    text = _curation_item_text(item)
    action = (_curation_item_action_types(item) or ["unknown"])[0]

    if any(token in text for token in ("andison", "coverage_", "shift_swap", "schedule_change")):
        return ("schedule_cleanup", "work_schedule", "coverage_exchange")
    if kind == "temporal_fact_cleanup":
        return ("temporal_fact_cleanup", domain, "validity_review")
    if kind == "maintenance_action":
        if action in {"archive_domain", "domain_archive"}:
            return ("maintenance_action", "domains", "archive_empty_domain")
        if action == "delete_source":
            return ("maintenance_action", "sources", "verified_duplicate_delete")
        if "missing-vector" in text or "missing vector" in text:
            return ("maintenance_action", "sources", "missing_vectors")
        return ("maintenance_action", domain, action)
    if kind in {"merge_candidate", "split_candidate"}:
        return ("wiki_identity", domain, _curation_title_topic(item))
    return (kind, domain, action)


def _curation_pack_id(group_key: tuple[str, str, str]) -> str:
    return "pack-" + "-".join(_curation_slug(part) for part in group_key)


def _curation_pack_prompt(group_key: tuple[str, str, str], count: int) -> tuple[str, str, str]:
    pack_kind, domain, topic = group_key
    if group_key == ("schedule_cleanup", "work_schedule", "coverage_exchange"):
        return (
            "Jack/Andison coverage exchange cleanup",
            "Should I treat these as one set of 2026 coverage-exchange events, "
            "resolve matching 2025 rows as extraction/OCR errors, and keep split "
            "shifts as one event with multiple segments?",
            "Normalize the schedule interpretation, preserve source evidence, "
            "and resolve the related review rows.",
        )
    if pack_kind == "temporal_fact_cleanup":
        return (
            f"{domain} time-bound fact review",
            f"Should I treat these {count} {domain} facts as historical/current "
            "based on their dates and only add valid_until when it changes current truth?",
            "Resolve time-bound review rows without deleting historical evidence.",
        )
    if topic == "archive_empty_domain":
        return (
            "Empty domain archive suggestions",
            "Current policy says empty domains are allowed. Should I reject these "
            "empty-domain archive suggestions?",
            "Reject empty-domain archive suggestions unless Jack explicitly asks to archive them.",
        )
    if topic == "verified_duplicate_delete":
        return (
            "Verified duplicate source cleanup",
            "Should I verify these duplicate source records and only delete rows/files "
            "that are proven redundant?",
            "Keep this pack pending until duplicate verification succeeds.",
        )
    if topic == "missing_vectors":
        return (
            "Missing-vector source repair",
            "Should I keep these source/vector mismatches as repair work instead of "
            "deleting source history?",
            "Snooze or keep pending until a repair pass can reindex or explain the "
            "missing vectors.",
        )
    if pack_kind == "wiki_identity":
        topic_label = topic.replace("-", " ")
        return (
            f"{domain} wiki identity review: {topic_label}",
            f"Should I treat these {count} {domain} wiki identity concerns about "
            f"{topic_label} as audit notes unless you want page changes?",
            "Resolve weak speculative merge/split rows and keep useful context in "
            "wiki audit notes.",
        )
    return (
        f"{pack_kind.replace('_', ' ').title()}: {topic.replace('_', ' ')}",
        f"How should I resolve these {count} related {pack_kind.replace('_', ' ')} items?",
        "Resolve the grouped curation rows according to Jack's answer.",
    )


def _curation_pack_from_items(
    group_key: tuple[str, str, str],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    title, question, suggested = _curation_pack_prompt(group_key, len(items))
    action_counts = Counter(
        action
        for item in items
        for action in (_curation_item_action_types(item) or ["unknown"])
    )
    kind_counts = Counter(str(item.get("kind") or "unknown") for item in items)
    risks = [str(item.get("risk") or "medium") for item in items]
    risk = max(risks, key=lambda value: CURATION_RISK_ORDER.get(value, 1), default="medium")
    destructive_ids = [
        str(item["id"]) for item in items
        if curation_item_has_destructive_actions(item)
    ]
    return {
        "id": _curation_pack_id(group_key),
        "title": title,
        "question": question,
        "kind": group_key[0],
        "domain": group_key[1],
        "topic": group_key[2],
        "risk": risk,
        "count": len(items),
        "affected_item_ids": [str(item["id"]) for item in items],
        "suggested_resolution": suggested,
        "requires_confirmation": bool(destructive_ids),
        "destructive_item_ids": destructive_ids,
        "action_counts": dict(sorted(action_counts.items())),
        "kind_counts": dict(sorted(kind_counts.items())),
        "sample_items": [
            {
                "id": item["id"],
                "kind": item["kind"],
                "title": item["title"],
                "summary": item.get("summary", "")[:240],
                "actions": _curation_item_action_types(item),
            }
            for item in items[:5]
        ],
    }


async def build_curation_question_packs(
    db: KnowledgeDB,
    *,
    limit: int = 10,
    kind: str | None = None,
    domain: str | None = None,
) -> list[dict[str, Any]]:
    """Group pending curation rows into chat-friendly question packs."""
    rows = await db.curation_list(status="pending", kind=kind, limit=CURATION_PACK_ITEM_LIMIT)
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in rows:
        item_domain = _curation_item_domain(item)
        group_key = _curation_group_key(item)
        if domain and item_domain != domain and group_key[1] != domain:
            continue
        groups.setdefault(group_key, []).append(item)

    packs = [_curation_pack_from_items(group_key, items) for group_key, items in groups.items()]
    packs.sort(key=lambda pack: (-pack["count"], pack["risk"], pack["title"]))
    return packs[:max(1, min(limit, 50))]


async def get_curation_question_pack(
    db: KnowledgeDB,
    pack_id: str,
) -> dict[str, Any] | None:
    for pack in await build_curation_question_packs(db, limit=50):
        if pack["id"] == pack_id:
            return pack
    return None


def _validate_curation_pack_resolution_status(status: str) -> str | None:
    if status not in CURATION_PACK_STATUSES:
        return f"resolution_status must be one of {', '.join(sorted(CURATION_PACK_STATUSES))}"
    return None


async def preview_curation_pack_resolution(
    db: KnowledgeDB,
    *,
    pack_id: str,
    answer: str,
    resolution_status: str = "applied",
) -> dict[str, Any]:
    clean_answer = " ".join(str(answer or "").split())
    if not clean_answer:
        return {"success": False, "error": "answer is required"}
    if error := _validate_curation_pack_resolution_status(resolution_status):
        return {"success": False, "error": error}
    pack = await get_curation_question_pack(db, pack_id)
    if not pack:
        return {"success": False, "error": f"Curation question pack '{pack_id}' not found"}

    blocked = pack["destructive_item_ids"] if resolution_status == "applied" else []
    status_updates = [
        {"item_id": item_id, "from": "pending", "to": resolution_status}
        for item_id in pack["affected_item_ids"]
    ]
    note_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"curation-resolution:{pack_id}:{clean_answer}"))
    return {
        "success": True,
        "pack": pack,
        "answer": clean_answer,
        "resolution_status": resolution_status,
        "requires_confirmation": pack_id if blocked else None,
        "blocked_destructive_item_ids": blocked,
        "status_updates": status_updates,
        "data_writes": [],
        "resolution_note": {
            "item_id": note_id,
            "kind": "curation_resolution",
            "title": f"Resolved curation pack: {pack['title']}",
            "summary": (
                f"Question: {pack['question']}\n"
                f"Answer: {clean_answer}\n"
                f"Resolution status for affected rows: {resolution_status}"
            ),
            "source_refs": [
                {"type": "curation_item", "id": item_id}
                for item_id in pack["affected_item_ids"]
            ],
            "proposed_actions": [{
                "action": "no_action",
                "description": "Batch curation resolution note; no direct data mutation.",
            }],
        },
    }


async def apply_curation_pack_resolution(
    db: KnowledgeDB,
    *,
    pack_id: str,
    answer: str,
    resolution_status: str = "applied",
    confirmed: bool = False,
) -> dict[str, Any]:
    preview = await preview_curation_pack_resolution(
        db,
        pack_id=pack_id,
        answer=answer,
        resolution_status=resolution_status,
    )
    if not preview["success"]:
        return preview
    if preview["blocked_destructive_item_ids"]:
        if not confirmed:
            return {
                "success": False,
                "error": (
                    "Pack contains destructive actions; inspect and apply those items "
                    "individually after verification."
                ),
                "requires_confirmation": pack_id,
                "preview": preview,
            }
        return {
            "success": False,
            "error": (
                "Batch apply for destructive curation packs is not supported yet; "
                "use individual curation apply."
            ),
            "preview": preview,
        }

    updated = []
    for update in preview["status_updates"]:
        if await db.curation_mark_status(update["item_id"], update["to"]):
            updated.append(update["item_id"])

    note = preview["resolution_note"]
    note_id = await db.curation_upsert(
        kind=note["kind"],
        title=note["title"],
        summary=note["summary"],
        source_refs=note["source_refs"],
        proposed_actions=note["proposed_actions"],
        risk=preview["pack"]["risk"],
        confidence=1.0,
        item_id=note["item_id"],
        status="applied",
    )
    await db.curation_mark_status(note_id, "applied")
    return {
        "success": True,
        "pack_id": pack_id,
        "updated_item_ids": updated,
        "resolution_note_id": note_id,
        "resolution_status": resolution_status,
    }


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
    if await db.source_exists(content_hash, domain=domain):
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
    curation_item_id: str,
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
            origin_type="curation",
            origin_ref=curation_item_id,
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
            origin_type="curation",
            origin_ref=curation_item_id,
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
                curation_item_id=item_id,
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


async def resolve_search_domains(
    db: KnowledgeDB,
    domain: str | None,
    domains: list[str] | None,
) -> list[str]:
    """Resolve a domain query to a list of domains including related ones.

    If a single domain is given, automatically includes its related domains.
    The 'core' domain is always included when it exists.
    """
    if domains:
        result = []
        for item in domains:
            clean = str(item).strip()
            if clean and clean not in result:
                result.append(clean)
    elif domain:
        clean_domain = str(domain).strip()
        result = [clean_domain] if clean_domain else []
        domain_info = await db.domain_get(clean_domain) if clean_domain else None
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


SEARCH_STOPWORDS = frozenset({
    "about", "and", "are", "can", "current", "did", "does", "for", "from", "give",
    "have", "how", "is", "latest", "me", "my", "of", "on", "show", "tell", "the",
    "to", "was", "what", "when", "where", "which", "who", "with",
})
FACT_QUERY_HINTS = frozenset({
    "account", "address", "birthday", "date", "dentist", "doctor", "dose", "email",
    "id", "label", "med", "medication", "number", "phone", "preference", "rate",
})
EVIDENCE_QUERY_HINTS = frozenset({
    "citation", "cite", "conflict", "contradict", "disagree", "document", "evidence",
    "original", "pdf", "proof", "source", "stale", "upload",
})


def search_fact_keywords(query: str) -> list[str]:
    """Extract fact-search keywords from a free-form search query."""
    return [
        term for term in re.findall(r"\b\w{2,}\b", query.lower())
        if term not in SEARCH_STOPWORDS
    ]


def classify_search_route(query: str, facts: list[dict[str, Any]]) -> str:
    """Pick the result ordering for facts, wiki pages, and source chunks."""
    terms = set(search_fact_keywords(query))
    lowered = query.lower()
    if terms & EVIDENCE_QUERY_HINTS or re.search(r"\b(where did|show .*source)", lowered):
        return "evidence"
    if facts and (
        terms & FACT_QUERY_HINTS
        or re.search(
            r"\b(what'?s|what is|who is|when is|where is|which is|how many|how much)\b",
            lowered,
        )
    ):
        return "fact"
    return "synthesis"


async def search_knowledge(
    *,
    embeddings: EmbeddingClient,
    sparse_encoder: BM25SparseEncoder,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
    query: str,
    domain: str | None = None,
    domains: list[str] | None = None,
    limit: int = 10,
    min_similarity: float = 0.25,
    include_facts: bool = True,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Run the shared Knowledge search path used by MCP and REST."""
    resolved_domains = await resolve_search_domains(db, domain, domains)
    keywords = search_fact_keywords(query)
    facts = (
        await db.facts_search(resolved_domains, keywords)
        if include_facts and keywords else []
    )
    route = classify_search_route(query, facts)
    wiki_matches = await db.wiki_search(resolved_domains, query, limit=min(limit, 5))

    query_embedding = await embeddings.embed(query)
    sparse_query = sparse_encoder.encode_query(query)

    results = await vectors.search(
        query_embedding,
        sparse_query=sparse_query,
        domains=resolved_domains,
        limit=limit,
        min_score=min_similarity,
    )

    chunk_results = []
    for r in results:
        p = r.payload or {}
        content = str(p.get("content", ""))
        if max_chars is not None and max_chars > 0 and len(content) > max_chars:
            content = content[:max_chars] + "…"
        chunk_results.append({
            "result_type": "chunk",
            "content": content,
            "domain": p.get("domain", ""),
            "source_id": p.get("source_id", ""),
            "source_name": p.get("source_name", ""),
            "source_type": p.get("source_type", ""),
            "chunk_id": str(r.id),
            "chunk_index": p.get("chunk_index", 0),
            "similarity": round(r.score, 4),
        })

    fact_results = [{
        "result_type": "fact",
        "content": f"{fact['key']}: {fact['value']}",
        "domain": fact["domain"],
        "source_id": "",
        "source_name": fact.get("source") or "",
        "source_type": "fact",
        "chunk_id": f"{fact['domain']}/{fact['key']}",
        "chunk_index": 0,
        "similarity": 1.0,
        "key": fact["key"],
        "value": fact["value"],
        "origin_type": fact.get("origin_type"),
        "origin_ref": fact.get("origin_ref"),
        "last_confirmed_at": fact.get("last_confirmed_at"),
    } for fact in facts]
    wiki_results = []
    for page in wiki_matches:
        content = str(page.get("body_md") or "")
        if max_chars is not None and max_chars > 0 and len(content) > max_chars:
            content = content[:max_chars] + "…"
        wiki_results.append({
            "result_type": "wiki",
            "content": content,
            "domain": page["domain"],
            "source_id": "",
            "source_name": page["title"],
            "source_type": "wiki_page",
            "chunk_id": page["slug"],
            "chunk_index": 0,
            "similarity": round(float(page.get("score") or 0), 4),
            "slug": page["slug"],
            "title": page["title"],
            "kind": page["kind"],
            "status": page["status"],
            "frontmatter": page.get("frontmatter") or {},
        })
    if route == "fact":
        ordered_results = [
            *fact_results,
            *(wiki_results if not fact_results else []),
            *chunk_results,
        ]
    elif route == "evidence":
        ordered_results = [*chunk_results, *wiki_results]
    else:
        ordered_results = [*wiki_results, *chunk_results]
    formatted = ordered_results[:max(1, limit)]

    response: dict[str, Any] = {
        "success": True,
        "query": query,
        "route": route,
        "searched_domains": resolved_domains,
        "count": len(formatted),
        "results": formatted,
        "wiki_count": len(wiki_results),
        "chunk_count": len(chunk_results),
    }

    if include_facts:
        response["facts"] = facts
        response["fact_count"] = len(facts)

    return response


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
        origin_type: Provenance category. Defaults to "chat" for MCP writes.
        origin_ref: Provenance reference. Defaults to today's date for chat writes.
    """
    _, _, _, _, db = _require_ready()

    if not await db.domain_exists(domain):
        return {"success": False, "error": f"Domain '{domain}' not found. Create it first."}

    if origin_type == "chat" and not origin_ref:
        origin_ref = datetime.now(UTC).date().isoformat()

    fact_id = await db.fact_set(
        domain, key, value, source, confidence, valid_from, valid_until,
        origin_type, origin_ref,
    )
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
    _, _, _, _, db = _require_ready()

    deleted = await db.fact_delete(domain, key)
    if not deleted:
        return {"success": False, "error": f"Fact '{key}' not found in domain '{domain}'"}

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
    """Queue a proposed Knowledge change for human review.

    Use this instead of direct writes when Jack hedges, contradicts stored facts,
    mentions something in passing, or when the agent is inferring.

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
