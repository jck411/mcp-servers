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

Run:
    python -m servers.knowledge_api --host 0.0.0.0 --port 9018
"""

from __future__ import annotations

import argparse
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Body, FastAPI, File, HTTPException, UploadFile

from servers.knowledge import (
    BM25SparseEncoder,
    EmbeddingClient,
    KnowledgeDB,
    KnowledgeSettings,
    KnowledgeVectorStore,
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
    # Ensure core domain exists (no-op if already present)
    await _db.domain_create(
        "core",
        "Foundational personal profile — always included in searches",
        [],
    )

    yield

    await _embeddings.close()
    await _vectors.close()
    await _db.close()


app = FastAPI(title="Knowledge REST API", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# POST /api/upload/{domain}
# ---------------------------------------------------------------------------


@app.post("/api/upload/{domain}")
async def upload_file(
    domain: str,
    file: UploadFile = File(...),
    ingest: bool = True,
    overwrite: bool = False,
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
            detail=f"File '{filename}' already exists in '{domain}'. Use ?overwrite=true to replace.",
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
    body: dict[str, Any] = Body(...),
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
async def delete_fact(domain: str, key: str) -> dict[str, Any]:
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
