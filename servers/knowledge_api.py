"""REST API for the Knowledge system.

Thin FastAPI wrapper around the knowledge MCP server's internal classes.
Provides search, domain listing, facts CRUD, and curation over plain HTTP
— no MCP client needed.

Endpoints:
    GET  /api/health                    Liveness + dependency status
    GET  /api/search?q=...             Semantic search
    GET  /api/domains                  List all domains with counts
    GET  /api/facts/{domain}           List facts in a domain
    POST /api/facts/{domain}/{key}     Upsert a fact
    DELETE /api/facts/{domain}/{key}   Delete a fact
    GET  /api/curation                 List curation queue items
    POST /api/curation                 Create/update a curation queue item
    GET  /api/curation/item/{item_id}  Get one curation queue item
    POST /api/curation/apply/{item_id} Apply a reviewed curation item
    POST /api/curation/reject/{item_id} Reject a curation item
    POST /api/curation/snooze/{item_id} Snooze a curation item

Run:
    python -m servers.knowledge_api --host 0.0.0.0 --port 9018
"""

from __future__ import annotations

import argparse
import os
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import Body, Depends, FastAPI, Header, HTTPException

from servers.knowledge.settings import KnowledgeSettings
from servers.knowledge.embeddings import BM25SparseEncoder, EmbeddingClient
from servers.knowledge.db import KnowledgeDB
from servers.knowledge.vectors import KnowledgeVectorStore
from servers.knowledge.search import search_knowledge
from servers.knowledge.curation import apply_curation_item, create_curation_queue_item
from shared.logging_config import get_logger

log = get_logger("knowledge_api")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_settings: KnowledgeSettings | None = None
_embeddings: EmbeddingClient | None = None
_sparse_encoder: BM25SparseEncoder | None = None
_vectors: KnowledgeVectorStore | None = None
_db: KnowledgeDB | None = None


def _require_ready() -> (
    tuple[KnowledgeSettings, EmbeddingClient, BM25SparseEncoder, KnowledgeVectorStore, KnowledgeDB]
):
    if not all([_settings, _embeddings, _sparse_encoder, _vectors, _db]):
        raise HTTPException(status_code=503, detail="Knowledge subsystem not initialized")
    return _settings, _embeddings, _sparse_encoder, _vectors, _db  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _settings, _embeddings, _sparse_encoder, _vectors, _db

    _settings = KnowledgeSettings()  # type: ignore[call-arg]

    _embeddings = EmbeddingClient(_settings)
    _sparse_encoder = BM25SparseEncoder()
    _vectors = KnowledgeVectorStore(_settings)
    _db = KnowledgeDB(_settings.db_path)

    await _vectors.ensure_collection()
    await _db.initialize()

    try:
        all_chunks = await _vectors.chunks_all()
        texts = [p["content"] for p in all_chunks if p.get("content")]
        if texts:
            _sparse_encoder.fit_batch(texts)
            log.info("bm25_warmup chunks=%d", len(texts))
    except Exception as exc:  # noqa: BLE001
        log.warning("bm25_warmup_skipped error=%r", exc)

    yield

    await _embeddings.close()
    await _vectors.close()
    await _db.close()


app = FastAPI(title="Knowledge REST API", version="2.0.0", lifespan=lifespan)

REQUIRED_BODY = Body(...)
OPTIONAL_BODY = Body(None)


# ---------------------------------------------------------------------------
# Bearer token authentication
# ---------------------------------------------------------------------------


def _get_api_token() -> str | None:
    """Read the API bearer token from environment.

    Checks KNOWLEDGE_API_TOKEN first (canonical for REST clients),
    then MCP_KNOWLEDGE_BEARER_TOKEN (shared with MCP servers on LXC 110).
    """
    return os.environ.get("KNOWLEDGE_API_TOKEN") or os.environ.get("MCP_KNOWLEDGE_BEARER_TOKEN")


async def require_bearer(authorization: str | None = Header(None)) -> None:
    """Verify Bearer token on mutating routes.

    If no token is configured (KNOWLEDGE_API_TOKEN / MCP_KNOWLEDGE_BEARER_TOKEN
    are both unset), all requests pass through — local dev mode.
    """
    expected = _get_api_token()
    if not expected:
        return
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or token != expected:
        raise HTTPException(status_code=401, detail="Invalid bearer token")


# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------


@app.get("/api/health")
async def health() -> dict[str, Any]:
    """Liveness + dependency status for the Knowledge subsystem."""
    if not all([_settings, _embeddings, _sparse_encoder, _vectors, _db]):
        return {"status": "starting", "ready": False}

    settings, _, sparse_encoder, vectors, db = _require_ready()

    qdrant_ok = True
    vector_count: int | None = None
    try:
        info = await vectors._client.get_collection(vectors._collection)
        vector_count = int(info.points_count or 0)
    except Exception as exc:  # noqa: BLE001
        qdrant_ok = False
        log.warning("health qdrant_error=%r", exc)

    fact_count: int | None = None
    try:
        assert db._conn is not None
        cursor = await db._conn.execute("SELECT COUNT(*) FROM facts")
        row = await cursor.fetchone()
        fact_count = int(row[0]) if row else 0
    except Exception as exc:  # noqa: BLE001
        log.warning("health sqlite_error=%r", exc)

    return {
        "status": "ok" if qdrant_ok else "degraded",
        "ready": True,
        "qdrant_reachable": qdrant_ok,
        "fact_count": fact_count,
        "vector_count": vector_count,
        "bm25_doc_count": sparse_encoder._doc_count,
        "embedding_model": settings.embedding_model,
    }


# ---------------------------------------------------------------------------
# GET /api/search
# ---------------------------------------------------------------------------


@app.get("/api/search")
async def search(
    q: str,
    domain: str | None = None,
    domains: str | None = None,
    limit: int = 10,
    min_similarity: float = 0.25,
    include_facts: bool = True,
    max_chars: int | None = None,
    temporal_intent: str | None = None,
) -> dict[str, Any]:
    """Semantic + keyword search across the knowledge base."""
    _, embeddings, sparse_encoder, vectors, db = _require_ready()
    domain_list = [d.strip() for d in domains.split(",") if d.strip()] if domains else None
    return await search_knowledge(
        embeddings=embeddings,
        sparse_encoder=sparse_encoder,
        vectors=vectors,
        db=db,
        query=q,
        domain=domain,
        domains=domain_list,
        limit=limit,
        min_similarity=min_similarity,
        include_facts=include_facts,
        max_chars=max_chars,
        temporal_intent=temporal_intent,
    )


# ---------------------------------------------------------------------------
# GET /api/domains
# ---------------------------------------------------------------------------


@app.get("/api/domains")
async def list_domains() -> dict[str, Any]:
    """List all knowledge domains with fact counts."""
    _, _, _, vectors, db = _require_ready()

    domains = await db.domain_list()
    for d in domains:
        d["fact_count"] = len(await db.facts_list(d["name"]))

    return {"count": len(domains), "domains": domains}


# ---------------------------------------------------------------------------
# GET /api/facts/{domain}
# ---------------------------------------------------------------------------


@app.get("/api/facts/{domain}")
async def get_facts(domain: str) -> dict[str, Any]:
    """List all structured facts in a domain."""
    settings, _, _, _, db = _require_ready()

    if not await db.domain_exists(domain):
        raise HTTPException(status_code=404, detail=f"Domain '{domain}' not found")

    facts = await db.facts_list(domain)
    return {
        "domain": domain,
        "facts": {f["key"]: f["value"] for f in facts},
        "raw": facts,
    }


# ---------------------------------------------------------------------------
# POST /api/facts/{domain}/{key}
# ---------------------------------------------------------------------------


@app.post("/api/facts/{domain}/{key}")
async def set_fact(
    domain: str,
    key: str,
    body: dict[str, Any] = REQUIRED_BODY,
    _auth: None = Depends(require_bearer),
) -> dict[str, Any]:
    """Upsert a structured fact in a domain."""
    settings, embeddings, sparse_encoder, vectors, db = _require_ready()

    if not await db.domain_exists(domain):
        raise HTTPException(status_code=404, detail=f"Domain '{domain}' not found")

    value = body.get("value")
    if value is None:
        raise HTTPException(status_code=422, detail="'value' is required in request body")

    await db.fact_set(
        domain,
        key,
        str(value),
        body.get("source"),
        float(body.get("confidence", 1.0)),
        body.get("valid_from"),
        body.get("valid_until"),
        body.get("as_of"),
        body.get("review_after"),
        str(body.get("origin_type") or "manual"),
        body.get("origin_ref"),
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

    return {"domain": domain, "key": key, "value": value}


# ---------------------------------------------------------------------------
# DELETE /api/facts/{domain}/{key}
# ---------------------------------------------------------------------------


@app.delete("/api/facts/{domain}/{key}")
async def delete_fact(domain: str, key: str, _auth: None = Depends(require_bearer)) -> dict[str, Any]:
    """Delete a structured fact from a domain."""
    _, _, _, vectors, db = _require_ready()

    deleted = await db.fact_delete(domain, key)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Fact '{key}' not found in domain '{domain}'",
        )

    # Clean up the derived Qdrant vector.
    try:
        await vectors.delete_fact_vector(domain, key)
    except Exception:
        log.warning("fact_vector_delete_failed domain=%s key=%s", domain, key, exc_info=True)

    return {"deleted": True, "domain": domain, "key": key}


# ---------------------------------------------------------------------------
# Curation Queue
# ---------------------------------------------------------------------------


@app.get("/api/curation")
async def list_curation(
    status: str | None = "pending",
    kind: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List curation queue items."""
    _, _, _, _, db = _require_ready()
    items = await db.curation_list(status=status, kind=kind, limit=limit)
    total_count = await db.curation_count(status=status, kind=kind)
    return {"count": len(items), "total_count": total_count, "items": items}


@app.post("/api/curation")
async def create_curation_item(
    body: dict[str, Any] = REQUIRED_BODY,
    _auth: None = Depends(require_bearer),
) -> dict[str, Any]:
    """Create or replace a curation queue item."""
    _, _, _, _, db = _require_ready()
    missing = [key for key in ("kind", "title") if not body.get(key)]
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing required field(s): {missing}")

    result = await create_curation_queue_item(
        db=db,
        actions=body.get("proposed_actions") or body.get("actions") or [],
        kind=str(body["kind"]),
        title=str(body["title"]),
        notes=str(body.get("summary") or body.get("notes") or ""),
        source_refs=body.get("source_refs") or [],
        risk=str(body.get("risk") or "medium"),
        confidence=float(body.get("confidence", 0.0)),
        item_id=body.get("id"),
        status=str(body.get("status") or "pending"),
        created_at=body.get("created_at"),
    )
    if not result["success"]:
        raise HTTPException(status_code=422, detail=result["error"])
    return {"id": result["item_id"], "item": result["item"]}


@app.get("/api/curation/item/{item_id:path}")
@app.get("/api/curation/{item_id}")
async def get_curation_item(item_id: str) -> dict[str, Any]:
    """Get one curation queue item."""
    _, _, _, _, db = _require_ready()
    item = await db.curation_get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Curation item '{item_id}' not found")
    return {"item": item}


@app.post("/api/curation/apply/{item_id:path}")
@app.post("/api/curation/{item_id}/apply")
async def apply_curation(
    item_id: str,
    body: dict[str, Any] | None = OPTIONAL_BODY,
    _auth: None = Depends(require_bearer),
):
    """Apply a reviewed curation item."""
    settings, embeddings, sparse_encoder, vectors, db = _require_ready()
    result = await apply_curation_item(
        item_id,
        confirmation=(body or {}).get("confirmation"),
        settings=settings,
        embeddings=embeddings,
        sparse_encoder=sparse_encoder,
        vectors=vectors,
        db=db,
    )
    if not result.get("success"):
        raise HTTPException(status_code=409, detail=result)
    return result


@app.post("/api/curation/reject/{item_id:path}")
@app.post("/api/curation/{item_id}/reject")
async def reject_curation(item_id: str, _auth: None = Depends(require_bearer)) -> dict[str, Any]:
    """Reject a curation queue item without applying it."""
    _, _, _, _, db = _require_ready()
    updated = await db.curation_mark_status(item_id, "rejected")
    if not updated:
        raise HTTPException(status_code=404, detail=f"Curation item '{item_id}' not found")
    return {"item_id": item_id, "status": "rejected"}


@app.post("/api/curation/snooze/{item_id:path}")
@app.post("/api/curation/{item_id}/snooze")
async def snooze_curation(item_id: str, _auth: None = Depends(require_bearer)) -> dict[str, Any]:
    """Snooze a curation queue item."""
    _, _, _, _, db = _require_ready()
    updated = await db.curation_mark_status(item_id, "snoozed")
    if not updated:
        raise HTTPException(status_code=404, detail=f"Curation item '{item_id}' not found")
    return {"item_id": item_id, "status": "snoozed"}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Knowledge REST API")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9018)
    # --transport accepted for compat with the mcp-server@ systemd template (ignored here)
    parser.add_argument("--transport", default="http")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
