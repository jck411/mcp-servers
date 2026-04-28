"""REST API for the Knowledge system.

Thin FastAPI wrapper around the knowledge MCP server's internal classes.
Provides file upload, search, domain listing, and facts CRUD over plain HTTP
— no MCP client needed.

Endpoints:
    POST /api/upload/{domain}          Upload + ingest a file
    GET  /api/search?q=...             Semantic search
    GET  /api/domains                  List all domains with counts
    GET  /api/facts/{domain}           List facts in a domain
    POST /api/facts/{domain}/{key}     Upsert a fact
    DELETE /api/facts/{domain}/{key}   Delete a fact
    GET  /api/curation                 List curation queue items
    POST /api/curation                 Create/update a curation queue item
    GET  /api/curation/{item_id}       Get one curation queue item
    POST /api/curation/{item_id}/apply Apply a reviewed curation item
    POST /api/curation/{item_id}/reject Reject a curation item
    POST /api/curation/{item_id}/snooze Snooze a curation item

Run:
    python -m servers.knowledge_api --host 0.0.0.0 --port 9018
"""

from __future__ import annotations

import argparse
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Body, Depends, FastAPI, File, Header, HTTPException, UploadFile

from servers.knowledge import (
    BM25SparseEncoder,
    EmbeddingClient,
    KnowledgeDB,
    KnowledgeSettings,
    KnowledgeVectorStore,
    apply_curation_item,
    compute_file_hash,
    extract_and_chunk,
)

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
    _settings.knowledge_path.mkdir(parents=True, exist_ok=True)

    _embeddings = EmbeddingClient(_settings)
    _sparse_encoder = BM25SparseEncoder()
    _vectors = KnowledgeVectorStore(_settings)
    _db = KnowledgeDB(_settings.db_path)

    await _vectors.ensure_collection()
    await _db.initialize()

    yield

    await _embeddings.close()
    await _vectors.close()
    await _db.close()


app = FastAPI(title="Knowledge REST API", version="1.0.0", lifespan=lifespan)

UPLOAD_FILE = File(...)
REQUIRED_BODY = Body(...)
OPTIONAL_BODY = Body(None)
AUTH_HEADER = Header(None)


def require_write_auth(authorization: str | None = AUTH_HEADER) -> None:
    """Require bearer auth for mutating routes when KNOWLEDGE_API_TOKEN is configured."""
    token = os.environ.get("KNOWLEDGE_API_TOKEN")
    if not token:
        return
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="Knowledge write token required")


WRITE_AUTH = Depends(require_write_auth)


# ---------------------------------------------------------------------------
# POST /api/upload/{domain}
# ---------------------------------------------------------------------------


@app.post("/api/upload/{domain}")
async def upload_file(
    domain: str,
    file: UploadFile = UPLOAD_FILE,
    ingest: bool = True,
    overwrite: bool = False,
    _auth: None = WRITE_AUTH,
) -> dict[str, Any]:
    """Upload a file to a domain folder and optionally ingest it immediately."""
    settings, embeddings, sparse_encoder, vectors, db = _require_ready()

    if not await db.domain_exists(domain):
        raise HTTPException(
            status_code=422,
            detail=f"Domain '{domain}' not found. Create it first via the MCP tools.",
        )

    # Sanitise filename — strip any path components supplied by the client
    raw_name = file.filename or "upload"
    filename = Path(raw_name).name
    if not filename:
        raise HTTPException(status_code=422, detail="Invalid filename")

    dest = settings.knowledge_path / domain / filename

    if dest.exists() and not overwrite:
        raise HTTPException(
            status_code=409,
            detail=(
                f"File '{filename}' already exists in '{domain}'. "
                "Use ?overwrite=true to replace."
            ),
        )

    data = await file.read()
    dest.write_bytes(data)

    if not ingest:
        return {"file": filename, "domain": domain, "ingested": False}

    # Check if already ingested (hash match)
    file_hash = compute_file_hash(dest)
    if await db.source_exists(file_hash) and not overwrite:
        return {
            "file": filename,
            "domain": domain,
            "ingested": False,
            "reason": "unchanged (already ingested)",
        }

    chunks_text = await extract_and_chunk(dest, settings)
    if not chunks_text:
        return {
            "file": filename,
            "domain": domain,
            "ingested": False,
            "reason": "no extractable content",
        }

    sparse_encoder.fit_batch(chunks_text)
    sparse_vecs = [sparse_encoder.encode(t) for t in chunks_text]
    dense_vecs = await embeddings.embed_batch(chunks_text)

    source_id = str(uuid.uuid4())
    chunk_payloads = []
    for i, text in enumerate(chunks_text):
        chunk_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{file_hash}_{i}"))
        chunk_payloads.append({
            "id": chunk_id,
            "domain": domain,
            "source_id": source_id,
            "source_type": dest.suffix.lstrip(".") or "file",
            "source_name": filename,
            "chunk_index": i,
            "content": text,
            "ingested_at": datetime.now(UTC).isoformat(),
        })

    await vectors.upsert_chunks(chunk_payloads, dense_vecs, sparse_vecs)
    await db.source_add(
        source_id,
        domain,
        dest.suffix.lstrip(".") or "file",
        filename,
        file_hash,
        len(chunks_text),
    )

    return {
        "file": filename,
        "domain": domain,
        "ingested": True,
        "chunks_stored": len(chunks_text),
    }


# ---------------------------------------------------------------------------
# GET /api/search
# ---------------------------------------------------------------------------


@app.get("/api/search")
async def search(
    q: str,
    domains: str | None = None,
    limit: int = 10,
    min_similarity: float = 0.25,
) -> dict[str, Any]:
    """Semantic + keyword search across the knowledge base."""
    settings, embeddings, sparse_encoder, vectors, db = _require_ready()

    if domains:
        domain_list = [d.strip() for d in domains.split(",")]
    else:
        all_domains = await db.domain_list()
        domain_list = [d["name"] for d in all_domains if not d["archived"]]

    if "core" not in domain_list and await db.domain_exists("core"):
        domain_list.append("core")

    query_embedding = await embeddings.embed(q)
    sparse_query = sparse_encoder.encode_query(q)

    results = await vectors.search(
        query_embedding,
        sparse_query=sparse_query,
        domains=domain_list,
        limit=limit,
        min_score=min_similarity,
    )

    keywords = [w for w in q.lower().split() if len(w) > 2]
    facts = await db.facts_search(domain_list, keywords) if keywords else []

    return {
        "query": q,
        "searched_domains": domain_list,
        "results": [
            {
                "content": (r.payload or {}).get("content", ""),
                "domain": (r.payload or {}).get("domain", ""),
                "source_name": (r.payload or {}).get("source_name", ""),
                "similarity": round(r.score, 4),
            }
            for r in results
        ],
        "facts": facts,
    }


# ---------------------------------------------------------------------------
# GET /api/domains
# ---------------------------------------------------------------------------


@app.get("/api/domains")
async def list_domains() -> dict[str, Any]:
    """List all knowledge domains with fact and chunk counts."""
    _, _, _, vectors, db = _require_ready()

    domains = await db.domain_list()
    for d in domains:
        d["chunk_count"] = await vectors.count_by_domain(d["name"])
        d["fact_count"] = len(await db.facts_list(d["name"]))

    return {"count": len(domains), "domains": domains}


# ---------------------------------------------------------------------------
# GET /api/facts/{domain}
# ---------------------------------------------------------------------------


@app.get("/api/facts/{domain}")
async def get_facts(domain: str) -> dict[str, Any]:
    """List all structured facts in a domain."""
    _, _, _, _, db = _require_ready()

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
    _auth: None = WRITE_AUTH,
) -> dict[str, Any]:
    """Upsert a structured fact in a domain."""
    _, _, _, _, db = _require_ready()

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
    )

    return {"domain": domain, "key": key, "value": value}


# ---------------------------------------------------------------------------
# DELETE /api/facts/{domain}/{key}
# ---------------------------------------------------------------------------


@app.delete("/api/facts/{domain}/{key}")
async def delete_fact(domain: str, key: str, _auth: None = WRITE_AUTH) -> dict[str, Any]:
    """Delete a structured fact from a domain."""
    _, _, _, _, db = _require_ready()

    deleted = await db.fact_delete(domain, key)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Fact '{key}' not found in domain '{domain}'",
        )

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
    return {"count": len(items), "items": items}


@app.post("/api/curation")
async def create_curation_item(
    body: dict[str, Any] = REQUIRED_BODY,
    _auth: None = WRITE_AUTH,
) -> dict[str, Any]:
    """Create or replace a curation queue item."""
    _, _, _, _, db = _require_ready()
    missing = [key for key in ("kind", "title") if not body.get(key)]
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing required field(s): {missing}")

    item_id = await db.curation_upsert(
        kind=str(body["kind"]),
        title=str(body["title"]),
        summary=str(body.get("summary") or ""),
        source_refs=body.get("source_refs") or [],
        proposed_actions=body.get("proposed_actions") or [],
        risk=str(body.get("risk") or "medium"),
        confidence=float(body.get("confidence", 0.0)),
        item_id=body.get("id"),
        status=str(body.get("status") or "pending"),
        created_at=body.get("created_at"),
    )
    item = await db.curation_get(item_id)
    return {"id": item_id, "item": item}


@app.get("/api/curation/{item_id}")
async def get_curation_item(item_id: str) -> dict[str, Any]:
    """Get one curation queue item."""
    _, _, _, _, db = _require_ready()
    item = await db.curation_get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Curation item '{item_id}' not found")
    return {"item": item}


@app.post("/api/curation/{item_id}/apply")
async def apply_curation(
    item_id: str,
    body: dict[str, Any] | None = OPTIONAL_BODY,
    _auth: None = WRITE_AUTH,
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


@app.post("/api/curation/{item_id}/reject")
async def reject_curation(item_id: str, _auth: None = WRITE_AUTH) -> dict[str, Any]:
    """Reject a curation queue item without applying it."""
    _, _, _, _, db = _require_ready()
    updated = await db.curation_mark_status(item_id, "rejected")
    if not updated:
        raise HTTPException(status_code=404, detail=f"Curation item '{item_id}' not found")
    return {"item_id": item_id, "status": "rejected"}


@app.post("/api/curation/{item_id}/snooze")
async def snooze_curation(item_id: str, _auth: None = WRITE_AUTH) -> dict[str, Any]:
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
