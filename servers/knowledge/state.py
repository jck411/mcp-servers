"""Shared initialization state for Knowledge MCP servers.

Both the conversational server (servers.knowledge) and the admin server
(servers.knowledge_admin) use the same subsystems (settings, embeddings,
vectors, db) with slightly different startup sequences.  This module holds
the global state and helpers so they aren't copy-pasted across packages.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from servers.knowledge.embeddings import BM25SparseEncoder, EmbeddingClient
from servers.knowledge.settings import KnowledgeSettings

if TYPE_CHECKING:
    from servers.knowledge.db import KnowledgeDB
    from servers.knowledge.vectors import KnowledgeVectorStore

_settings: KnowledgeSettings | None = None
_embeddings: EmbeddingClient | None = None
_sparse_encoder: BM25SparseEncoder | None = None
_vectors: KnowledgeVectorStore | None = None
_db: KnowledgeDB | None = None
_ready = False


def auth_provider(client_id: str = "knowledge") -> StaticTokenVerifier | None:
    token = os.environ.get("MCP_KNOWLEDGE_BEARER_TOKEN")
    if not token:
        return None
    return StaticTokenVerifier({token: {"client_id": client_id, "scopes": []}})


def require_ready(
    label: str = "Knowledge",
) -> tuple[KnowledgeSettings, EmbeddingClient, BM25SparseEncoder, KnowledgeVectorStore, KnowledgeDB]:
    if (
        not _ready
        or not _settings
        or not _embeddings
        or not _sparse_encoder
        or not _vectors
        or not _db
    ):
        raise RuntimeError(f"{label} subsystem not initialized")
    return _settings, _embeddings, _sparse_encoder, _vectors, _db


async def init_subsystems(
    *,
    warm_bm25: bool = False,
    ensure_core_domain: bool = False,
    log_label: str = "knowledge",
) -> bool:
    """Initialize settings, embeddings, vectors, and db.

    Returns True on success, False if initialization was skipped
    (e.g. missing config or unreachable Qdrant).
    """
    global _settings, _embeddings, _sparse_encoder, _vectors, _db, _ready

    from shared.logging_config import get_logger

    log = get_logger(log_label)

    try:
        _settings = KnowledgeSettings()  # type: ignore[call-arg]
    except Exception as exc:
        log.error("disabled config_error=%r", exc)
        return False

    _embeddings = EmbeddingClient(_settings)
    _sparse_encoder = BM25SparseEncoder()
    _vectors = KnowledgeVectorStore(_settings)
    _db = KnowledgeDB(_settings.db_path)

    try:
        await _vectors.ensure_collection()
    except Exception as exc:
        log.error("disabled qdrant_unreachable=%r", exc)
        return False

    await _db.initialize()

    if warm_bm25:
        try:
            all_chunks = await _vectors.chunks_all()
            texts = [p["content"] for p in all_chunks if p.get("content")]
            if texts:
                _sparse_encoder.fit_batch(texts)
                log.info("bm25_warmup chunks=%d", len(texts))
        except Exception as exc:
            log.warning("bm25_warmup_skipped error=%r", exc)

    if ensure_core_domain:
        await _db.domain_create(
            "core",
            "Foundational personal profile — always included in searches",
            [],
        )

    _ready = True
    log.info("initialization complete")
    return True


async def shutdown() -> None:
    if _embeddings:
        await _embeddings.close()
    if _vectors:
        await _vectors.close()
    if _db:
        await _db.close()