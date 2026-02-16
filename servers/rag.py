"""Standalone RAG MCP server for document search with hybrid retrieval.

Indexes PDFs from subdirectories into Qdrant using both dense embeddings and
BM25 sparse vectors, combining results via Reciprocal Rank Fusion (RRF) for
improved retrieval accuracy. Categories are derived from subdirectory names.

Features:
- Hybrid search: semantic (dense) + keyword (BM25 sparse) vectors
- Reciprocal Rank Fusion for combining search results
- Category-based organization with per-folder tools
- Incremental indexing (only new/changed documents)

Directory structure:
    /opt/mcp-servers/documents/
    ├── pediatric_policies/     → rag_search_pediatric_policies
    ├── chemo_policies/         → rag_search_chemo_policies
    └── adult_protocols/        → rag_search_adult_protocols

Run:
    python -m servers.rag --transport streamable-http --host 0.0.0.0 --port 9014
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import sys
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
import httpx
import kreuzberg
from fastmcp import FastMCP
from kreuzberg import ChunkingConfig, ExtractionConfig
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
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

# Default port for HTTP transport
DEFAULT_HTTP_PORT = 9014

PROJECT_ROOT = Path(__file__).resolve().parent.parent

mcp = FastMCP("rag")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class RAGSettings(BaseSettings):
    """RAG server configuration from environment variables."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Document storage
    documents_path: Path = Field(
        default_factory=lambda: PROJECT_ROOT / "documents",
        validation_alias="RAG_DOCUMENTS_PATH",
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
        default="rag_documents", validation_alias="RAG_QDRANT_COLLECTION"
    )

    # Index tracking database
    index_db_path: Path = Field(
        default_factory=lambda: PROJECT_ROOT / "data" / "rag_index.db",
        validation_alias="RAG_INDEX_DB_PATH",
    )

    # Chunking
    chunk_max_chars: int = Field(default=1000, validation_alias="RAG_CHUNK_MAX_CHARS")
    chunk_overlap: int = Field(default=200, validation_alias="RAG_CHUNK_OVERLAP")


# ---------------------------------------------------------------------------
# Embedding Client
# ---------------------------------------------------------------------------


class EmbeddingClient:
    """Generate text embeddings via OpenRouter API."""

    def __init__(self, settings: RAGSettings) -> None:
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
                sorted_data = sorted(data["data"], key=lambda x: x["index"])
                return [item["embedding"] for item in sorted_data]
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
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
    """Generate BM25-based sparse vectors for hybrid search.

    Uses feature hashing to create sparse vectors without maintaining a vocabulary.
    This allows documents to be added incrementally without rebuilding the index.
    """

    def __init__(self, vocab_size: int = 30000) -> None:
        self._vocab_size = vocab_size
        # BM25 parameters
        self._k1 = 1.5
        self._b = 0.75
        # Track document statistics for IDF
        self._doc_count = 0
        self._doc_freqs: Counter[int] = Counter()
        self._avg_doc_len = 0.0
        self._total_doc_len = 0

    def _tokenize(self, text: str) -> list[str]:
        """Simple tokenization: lowercase, split on non-alphanumeric."""
        text = text.lower()
        tokens = re.findall(r"\b[a-z0-9]+\b", text)
        # Filter very short tokens
        return [t for t in tokens if len(t) > 1]

    def _hash_token(self, token: str) -> int:
        """Hash a token to a vocab index using murmurhash-style approach."""
        # Use SHA-256 and take first 8 bytes for good distribution
        h = hashlib.sha256(token.encode()).digest()
        return int.from_bytes(h[:4], "little") % self._vocab_size

    def fit_batch(self, texts: list[str]) -> None:
        """Update corpus statistics with a batch of documents."""
        for text in texts:
            tokens = self._tokenize(text)
            self._doc_count += 1
            self._total_doc_len += len(tokens)
            # Track document frequency (how many docs contain each token)
            unique_indices = set(self._hash_token(t) for t in tokens)
            for idx in unique_indices:
                self._doc_freqs[idx] += 1
        if self._doc_count > 0:
            self._avg_doc_len = self._total_doc_len / self._doc_count

    def encode(self, text: str) -> tuple[list[int], list[float]]:
        """Encode text to sparse vector (indices, values)."""
        tokens = self._tokenize(text)
        if not tokens:
            return [], []

        doc_len = len(tokens)
        term_freqs: Counter[int] = Counter()
        for token in tokens:
            idx = self._hash_token(token)
            term_freqs[idx] += 1

        indices = []
        values = []

        for idx, tf in term_freqs.items():
            # BM25 TF component
            tf_score = (tf * (self._k1 + 1)) / (
                tf + self._k1 * (1 - self._b + self._b * doc_len / max(self._avg_doc_len, 1))
            )
            # IDF component (with smoothing)
            df = self._doc_freqs.get(idx, 0)
            idf = max(0.0, (self._doc_count - df + 0.5) / (df + 0.5))
            if idf > 0:
                idf = (idf + 1.0) ** 0.5  # Log-like smoothing

            score = tf_score * idf
            if score > 0:
                indices.append(idx)
                values.append(float(score))

        # Sort by index for consistent ordering
        if indices:
            sorted_pairs = sorted(zip(indices, values, strict=True), key=lambda x: x[0])
            indices, values = zip(*sorted_pairs, strict=True)
            return list(indices), list(values)
        return [], []

    def encode_query(self, text: str) -> tuple[list[int], list[float]]:
        """Encode query - same as document but could differ for other strategies."""
        return self.encode(text)


# ---------------------------------------------------------------------------
# Vector Store
# ---------------------------------------------------------------------------


class RAGVectorStore:
    """Qdrant operations for RAG documents with hybrid search."""

    DENSE_VECTOR_NAME = "dense"
    SPARSE_VECTOR_NAME = "sparse"

    def __init__(self, settings: RAGSettings) -> None:
        self._client = AsyncQdrantClient(url=settings.qdrant_url)
        self._collection = settings.qdrant_collection
        self._dimensions = settings.embedding_dimensions

    async def ensure_collection(self) -> None:
        """Create collection with dense + sparse vectors and indexes."""
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

        # Payload indexes
        indexes: list[tuple[str, PayloadSchemaType]] = [
            ("category", PayloadSchemaType.KEYWORD),
            ("filename", PayloadSchemaType.KEYWORD),
            ("chunk_index", PayloadSchemaType.INTEGER),
        ]
        import contextlib

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
        """Insert document chunks with dense and sparse embeddings."""
        points = []
        for chunk, embedding, (indices, values) in zip(
            chunks, embeddings, sparse_vectors, strict=True
        ):
            vector_data: dict[str, Any] = {
                self.DENSE_VECTOR_NAME: embedding,
            }
            # Only add sparse vector if non-empty
            if indices and values:
                vector_data[self.SPARSE_VECTOR_NAME] = SparseVector(
                    indices=indices, values=values
                )
            points.append(PointStruct(id=chunk["id"], vector=vector_data, payload=chunk))
        await self._client.upsert(collection_name=self._collection, points=points)

    async def delete_by_filename(self, category: str, filename: str) -> None:
        """Delete all chunks for a specific document."""
        await self._client.delete(
            collection_name=self._collection,
            points_selector=Filter(
                must=[
                    FieldCondition(key="category", match=MatchValue(value=category)),
                    FieldCondition(key="filename", match=MatchValue(value=filename)),
                ]
            ),
        )

    async def search(
        self,
        query_embedding: list[float],
        sparse_query: tuple[list[int], list[float]] | None = None,
        category: str | None = None,
        filename: str | None = None,
        limit: int = 5,
        min_score: float = 0.3,
    ) -> list[ScoredPoint]:
        """Hybrid search combining dense embeddings and sparse BM25 vectors."""
        must_conditions: list[FieldCondition] = []
        if category:
            must_conditions.append(
                FieldCondition(key="category", match=MatchValue(value=category))
            )
        if filename:
            must_conditions.append(
                FieldCondition(key="filename", match=MatchValue(value=filename))
            )

        query_filter = Filter(must=must_conditions) if must_conditions else None

        # If we have sparse vectors, use hybrid search with RRF fusion
        if sparse_query and sparse_query[0] and sparse_query[1]:
            indices, values = sparse_query
            prefetch_limit = max(limit * 4, 20)  # Fetch more candidates for fusion

            results = await self._client.query_points(
                collection_name=self._collection,
                prefetch=[
                    Prefetch(
                        query=query_embedding,
                        using=self.DENSE_VECTOR_NAME,
                        filter=query_filter,
                        limit=prefetch_limit,
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
            # Fallback to dense-only search
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

    async def count_by_category(self, category: str) -> int:
        """Count chunks in a category."""
        result = await self._client.count(
            collection_name=self._collection,
            count_filter=Filter(
                must=[FieldCondition(key="category", match=MatchValue(value=category))]
            ),
        )
        return result.count

    async def close(self) -> None:
        await self._client.close()


# ---------------------------------------------------------------------------
# Index Tracking (SQLite)
# ---------------------------------------------------------------------------


class IndexTracker:
    """Track indexed documents to avoid re-processing unchanged files."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Create the tracking table."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS indexed_documents (
                file_hash TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                category TEXT NOT NULL,
                filename TEXT NOT NULL,
                chunk_count INTEGER NOT NULL,
                indexed_at TEXT NOT NULL
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_category ON indexed_documents(category)"
        )
        await self._conn.commit()

    async def is_indexed(self, file_hash: str) -> bool:
        """Check if a file hash is already indexed."""
        if not self._conn:
            return False
        cursor = await self._conn.execute(
            "SELECT 1 FROM indexed_documents WHERE file_hash = ?", (file_hash,)
        )
        return await cursor.fetchone() is not None

    async def mark_indexed(
        self,
        file_hash: str,
        path: str,
        category: str,
        filename: str,
        chunk_count: int,
    ) -> None:
        """Record that a file has been indexed."""
        if not self._conn:
            return
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO indexed_documents
            (file_hash, path, category, filename, chunk_count, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (file_hash, path, category, filename, chunk_count, datetime.now(UTC).isoformat()),
        )
        await self._conn.commit()

    async def remove_by_path(self, path: str) -> None:
        """Remove tracking for a specific file."""
        if not self._conn:
            return
        await self._conn.execute("DELETE FROM indexed_documents WHERE path = ?", (path,))
        await self._conn.commit()

    async def get_documents_by_category(self, category: str) -> list[dict[str, Any]]:
        """List documents in a category."""
        if not self._conn:
            return []
        cursor = await self._conn.execute(
            "SELECT filename, chunk_count, indexed_at FROM indexed_documents WHERE category = ?",
            (category,),
        )
        rows = await cursor.fetchall()
        return [
            {"filename": row[0], "chunk_count": row[1], "indexed_at": row[2]} for row in rows
        ]

    async def get_all_categories(self) -> dict[str, int]:
        """Return category names with document counts."""
        if not self._conn:
            return {}
        cursor = await self._conn.execute(
            "SELECT category, COUNT(*) FROM indexed_documents GROUP BY category"
        )
        rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()


# ---------------------------------------------------------------------------
# Document Processor
# ---------------------------------------------------------------------------


def compute_file_hash(path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


async def extract_and_chunk(path: Path, settings: RAGSettings) -> list[str]:
    """Extract text from PDF and split into chunks."""
    config = ExtractionConfig(
        chunking=ChunkingConfig(
            max_chars=settings.chunk_max_chars,
            max_overlap=settings.chunk_overlap,
        )
    )
    result = await kreuzberg.extract_file(str(path), config=config)

    # Use chunks if available, otherwise split content manually
    if result.chunks:
        return [str(c) for c in result.chunks]

    # Fallback: split by paragraphs then combine to target size
    content = result.content
    if not content:
        return []

    # Simple chunking fallback
    chunks = []
    current = ""
    for para in content.split("\n\n"):
        if len(current) + len(para) > settings.chunk_max_chars:
            if current:
                chunks.append(current.strip())
            current = para
        else:
            current = current + "\n\n" + para if current else para
    if current:
        chunks.append(current.strip())

    return [c for c in chunks if c]


# ---------------------------------------------------------------------------
# Global State
# ---------------------------------------------------------------------------

_settings: RAGSettings | None = None
_embeddings: EmbeddingClient | None = None
_sparse_encoder: BM25SparseEncoder | None = None
_vectors: RAGVectorStore | None = None
_tracker: IndexTracker | None = None
_categories: list[str] = []
_ready = False


def _require_ready() -> (
    tuple[RAGSettings, EmbeddingClient, BM25SparseEncoder, RAGVectorStore, IndexTracker]
):
    """Return components or raise."""
    if (
        not _ready
        or not _settings
        or not _embeddings
        or not _sparse_encoder
        or not _vectors
        or not _tracker
    ):
        raise RuntimeError("RAG subsystem not initialized")
    return _settings, _embeddings, _sparse_encoder, _vectors, _tracker


async def _index_document(
    path: Path,
    category: str,
    settings: RAGSettings,
    embeddings: EmbeddingClient,
    sparse_encoder: BM25SparseEncoder,
    vectors: RAGVectorStore,
    tracker: IndexTracker,
    force: bool = False,
) -> int:
    """Index a single document. Returns chunk count."""
    file_hash = compute_file_hash(path)
    filename = path.name

    if not force and await tracker.is_indexed(file_hash):
        return 0  # Already indexed

    # Remove old version if re-indexing
    await vectors.delete_by_filename(category, filename)
    await tracker.remove_by_path(str(path))

    # Extract and chunk
    chunks_text = await extract_and_chunk(path, settings)
    if not chunks_text:
        print(f"  [RAG] No content extracted from {filename}", file=sys.stderr)
        return 0

    # Update BM25 statistics and generate sparse vectors
    sparse_encoder.fit_batch(chunks_text)
    sparse_vectors = [sparse_encoder.encode(text) for text in chunks_text]

    # Generate dense embeddings
    chunk_embeddings = await embeddings.embed_batch(chunks_text)

    # Prepare payloads
    chunks = []
    for i, text in enumerate(chunks_text):
        # Create deterministic UUID from hash + index for Qdrant compatibility
        chunk_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{file_hash}_{i}"))
        chunks.append(
            {
                "id": chunk_id,
                "category": category,
                "filename": filename,
                "chunk_index": i,
                "content": text,
            }
        )

    # Store with both dense and sparse vectors
    await vectors.upsert_chunks(chunks, chunk_embeddings, sparse_vectors)
    await tracker.mark_indexed(file_hash, str(path), category, filename, len(chunks))

    return len(chunks)


async def _index_category(category: str, category_path: Path, force: bool = False) -> int:
    """Index all PDFs in a category directory. Returns total chunks indexed."""
    settings, embeddings, sparse_encoder, vectors, tracker = _require_ready()

    pdf_files = list(category_path.glob("*.pdf"))
    if not pdf_files:
        return 0

    total_chunks = 0
    for pdf_path in pdf_files:
        try:
            chunks = await _index_document(
                pdf_path, category, settings, embeddings, sparse_encoder, vectors, tracker, force
            )
            if chunks > 0:
                print(
                    f"  [RAG] Indexed {pdf_path.name}: {chunks} chunks",
                    file=sys.stderr,
                )
            total_chunks += chunks
        except Exception as exc:
            print(f"  [RAG] Failed to index {pdf_path.name}: {exc}", file=sys.stderr)

    return total_chunks


def _discover_categories(documents_path: Path) -> list[str]:
    """Find all subdirectories (categories) in the documents path."""
    if not documents_path.exists():
        return []
    return sorted(
        d.name for d in documents_path.iterdir() if d.is_dir() and not d.name.startswith(".")
    )


# ---------------------------------------------------------------------------
# Tool Registration (Dynamic per Category)
# ---------------------------------------------------------------------------


def _register_category_tools(category: str) -> None:
    """Register search, list, and reindex tools for a specific category."""

    @mcp.tool(
        f"rag_search_{category}",
        description=(
            f"Search {category.replace('_', ' ')} documents for relevant information. "
            "Uses hybrid search (semantic + keyword) for best results."
        ),
    )
    async def search(
        query: str,
        document: str | None = None,
        limit: int = 5,
        min_similarity: float = 0.3,
        _cat: str = category,
    ) -> dict[str, Any]:
        """Search documents using hybrid dense + sparse vector search."""
        settings, embeddings, sparse_encoder, vectors, _ = _require_ready()

        # Generate both dense and sparse query vectors
        query_embedding = await embeddings.embed(query)
        sparse_query = sparse_encoder.encode_query(query)

        results = await vectors.search(
            query_embedding,
            sparse_query=sparse_query,
            category=_cat,
            filename=document,
            limit=limit,
            min_score=min_similarity,
        )

        if not results:
            return {
                "success": True,
                "category": _cat,
                "query": query,
                "count": 0,
                "results": [],
                "message": "No matching content found.",
            }

        formatted = []
        for r in results:
            p = r.payload or {}
            formatted.append(
                {
                    "content": p.get("content", ""),
                    "filename": p.get("filename", ""),
                    "chunk_index": p.get("chunk_index", 0),
                    "similarity": round(r.score, 4),
                }
            )

        return {
            "success": True,
            "category": _cat,
            "query": query,
            "count": len(formatted),
            "results": formatted,
        }

    @mcp.tool(
        f"rag_list_documents_{category}",
        description=f"List all indexed documents in the {category.replace('_', ' ')} category.",
    )
    async def list_documents(_cat: str = category) -> dict[str, Any]:
        """List documents in this category."""
        _, _, _, _, tracker = _require_ready()
        docs = await tracker.get_documents_by_category(_cat)
        return {
            "success": True,
            "category": _cat,
            "count": len(docs),
            "documents": docs,
        }

    @mcp.tool(
        f"rag_reindex_{category}",
        description=(
            f"Force re-index documents in {category.replace('_', ' ')}. "
            "Use after adding or updating PDF files."
        ),
    )
    async def reindex(
        document: str | None = None,
        _cat: str = category,
    ) -> dict[str, Any]:
        """Re-index documents in this category."""
        settings, embeddings, sparse_encoder, vectors, tracker = _require_ready()

        cat_path = settings.documents_path / _cat
        if not cat_path.exists():
            return {"success": False, "error": f"Category path not found: {cat_path}"}

        if document:
            doc_path = cat_path / document
            if not doc_path.exists():
                return {"success": False, "error": f"Document not found: {document}"}
            chunks = await _index_document(
                doc_path, _cat, settings, embeddings, sparse_encoder, vectors, tracker, force=True
            )
            return {
                "success": True,
                "category": _cat,
                "document": document,
                "chunks_indexed": chunks,
            }

        chunks = await _index_category(_cat, cat_path, force=True)
        return {
            "success": True,
            "category": _cat,
            "chunks_indexed": chunks,
        }


def _register_global_tools() -> None:
    """Register category-listing tool."""

    @mcp.tool(
        "rag_list_categories",
        description="List all available document categories and their document counts.",
    )
    async def list_categories() -> dict[str, Any]:
        """List all categories."""
        _, _, _, _, tracker = _require_ready()
        categories = await tracker.get_all_categories()
        return {
            "success": True,
            "categories": categories,
        }


# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------


async def _startup() -> None:
    """Initialize RAG subsystem and index documents."""
    global _settings, _embeddings, _sparse_encoder, _vectors, _tracker, _categories, _ready

    try:
        _settings = RAGSettings()  # type: ignore[call-arg]
    except Exception as exc:
        print(f"[RAG] Disabled — config error: {exc}", file=sys.stderr)
        return

    print(f"[RAG] Documents path: {_settings.documents_path}", file=sys.stderr)

    # Discover categories
    _categories = _discover_categories(_settings.documents_path)
    if not _categories:
        print("[RAG] No categories found. Create subdirs in documents path.", file=sys.stderr)

    # Initialize components
    _embeddings = EmbeddingClient(_settings)
    _sparse_encoder = BM25SparseEncoder()
    _vectors = RAGVectorStore(_settings)
    _tracker = IndexTracker(_settings.index_db_path)

    try:
        await _vectors.ensure_collection()
    except Exception as exc:
        print(f"[RAG] Disabled — Qdrant unreachable: {exc}", file=sys.stderr)
        return

    await _tracker.initialize()
    _ready = True

    # Register tools for each category
    for category in _categories:
        _register_category_tools(category)
        print(f"[RAG] Registered tools for category: {category}", file=sys.stderr)

    _register_global_tools()

    # Index documents on startup
    print("[RAG] Starting hybrid search indexing (dense + BM25)...", file=sys.stderr)
    for category in _categories:
        cat_path = _settings.documents_path / category
        indexed = await _index_category(category, cat_path)
        if indexed > 0:
            print(f"[RAG] Category '{category}': indexed {indexed} new chunks", file=sys.stderr)

    print("[RAG] Initialization complete (hybrid search enabled)", file=sys.stderr)


async def _shutdown() -> None:
    """Cleanup resources."""
    if _embeddings:
        await _embeddings.close()
    if _vectors:
        await _vectors.close()
    if _tracker:
        await _tracker.close()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def run(
    transport: str = "stdio",
    host: str = "0.0.0.0",
    port: int = DEFAULT_HTTP_PORT,
) -> None:
    """Run the RAG MCP server."""
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

    parser = argparse.ArgumentParser(description="RAG MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport protocol",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind")
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT, help="Port")
    args = parser.parse_args()
    run(args.transport, args.host, args.port)


if __name__ == "__main__":
    main()


__all__ = ["mcp", "run", "main", "DEFAULT_HTTP_PORT"]
