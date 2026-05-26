"""Qdrant vector store for the Knowledge service.

Extracted from knowledge_server.py during Phase 2 modularization.
Contains KnowledgeVectorStore with hybrid search, embedding, and CRUD.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from typing import Any

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

from servers.knowledge.settings import KnowledgeSettings


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
            ("fact_type", PayloadSchemaType.KEYWORD),
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

    async def embed_wiki_page(
        self,
        *,
        slug: str,
        domain: str,
        title: str,
        body_md: str,
        embeddings: Any,
        sparse_encoder: Any,
    ) -> None:
        """Embed a wiki page body into Qdrant as a single chunk.

        Replaces any previous vectors for this slug. The page participates
        in the same hybrid search as source chunks so wiki and document
        results compete on semantic similarity rather than keyword heuristics.
        """
        if not body_md.strip():
            return
        # Remove old vectors for this page.
        await self.delete_by_source(slug)
        text = f"{title}\n\n{body_md}"
        dense = await embeddings.embed(text)
        sparse_encoder.fit_batch([text])
        sparse = sparse_encoder.encode(text)
        now = datetime.now(UTC).isoformat()
        chunk = {
            "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"wiki:{slug}")),
            "domain": domain,
            "source_id": slug,
            "source_type": "wiki_page",
            "source_name": title,
            "chunk_index": 0,
            "content": text,
            "ingested_at": now,
        }
        await self.upsert_chunks([chunk], [dense], [sparse])

    async def embed_fact(
        self,
        *,
        fact: dict[str, Any],
        embeddings: Any,
        sparse_encoder: Any,
    ) -> None:
        """Embed a structured fact into Qdrant as a single derived vector.

        The fact is enriched with domain context, temporal metadata, type,
        tags, and source provenance to improve semantic retrieval. The Qdrant
        point uses type=fact and fact_id, which the maintenance scanner
        recognises as a separate identity scheme (not a source chunk).
        """
        domain = str(fact.get("domain", ""))
        key = str(fact.get("key", ""))
        value = str(fact.get("value", ""))
        if not (domain and key and value):
            return

        fact_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{domain}:{key}"))
        fact_type = str(fact.get("type") or "note")

        # Parse tags — may be a JSON string or already a list
        raw_tags = fact.get("tags")
        if isinstance(raw_tags, str):
            import json
            try:
                tags = json.loads(raw_tags)
            except (json.JSONDecodeError, TypeError):
                tags = []
        elif isinstance(raw_tags, list):
            tags = raw_tags
        else:
            tags = []

        # Build enriched text for embedding — mirrors the old vectorisation
        # payload but is generated deterministically from the fact row.
        parts = [f"Structured {fact_type} fact in {domain} domain."]
        readable_key = key.replace("_", " ")
        parts.append(f"{readable_key}: {value}.")
        if tags:
            parts.append(f"Tags: {', '.join(tags)}.")
        if fact.get("source"):
            parts.append(f"Source: {fact['source']}.")
        temporal_parts: list[str] = []
        if fact.get("valid_from"):
            temporal_parts.append(f"from {fact['valid_from']}")
        if fact.get("valid_until"):
            temporal_parts.append(f"until {fact['valid_until']}")
        elif fact.get("valid_from"):
            temporal_parts.append("until open-ended")
        if temporal_parts:
            parts.append(f"Valid {' '.join(temporal_parts)}.")
        if fact.get("as_of"):
            parts.append(f"As of {fact['as_of']}.")

        enriched_text = " ".join(parts)
        raw_text = f"{key}: {value}"

        # Remove any existing vector for this fact.
        await self._client.delete(
            collection_name=self._collection,
            points_selector=Filter(
                must=[FieldCondition(key="fact_id", match=MatchValue(value=fact_id))]
            ),
        )

        dense = await embeddings.embed(enriched_text)
        sparse_encoder.fit_batch([enriched_text])
        sparse = sparse_encoder.encode(enriched_text)
        now = datetime.now(UTC).isoformat()

        related_domains = [domain]
        if domain != "core":
            related_domains.append("core")

        chunk = {
            "id": fact_id,
            "type": "fact",
            "fact_type": fact_type,
            "domain": domain,
            "fact_id": fact_id,
            "key": key,
            "value": value,
            "raw": raw_text,
            "content": enriched_text,
            "enriched_text": enriched_text,
            "source": fact.get("source") or "",
            "source_name": f"fact:{domain}/{key}",
            "source_type": "fact",
            "entity_links": related_domains,
            "confidence": fact.get("confidence", 1.0),
            "valid_from": fact.get("valid_from"),
            "valid_until": fact.get("valid_until"),
            "as_of": fact.get("as_of"),
            "review_after": fact.get("review_after"),
            "tags": tags,
            "updated_at": fact.get("updated_at") or now,
            "created_at": fact.get("created_at") or now,
            "chunk_index": 0,
            "ingested_at": now,
        }
        await self.upsert_chunks([chunk], [dense], [sparse])

    async def delete_fact_vector(self, domain: str, key: str) -> None:
        """Delete the derived Qdrant vector for a fact."""
        fact_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{domain}:{key}"))
        await self._client.delete(
            collection_name=self._collection,
            points_selector=Filter(
                must=[FieldCondition(key="fact_id", match=MatchValue(value=fact_id))]
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

