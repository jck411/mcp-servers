"""Qdrant vector store operations for memory server."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    DatetimeRange,
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

from shared.memory_config import MemorySettings


class VectorStore:
    """Manage the Qdrant memories collection."""

    def __init__(self, settings: MemorySettings) -> None:
        self._client = AsyncQdrantClient(url=settings.qdrant_url)
        self._collection = settings.qdrant_collection
        self._dimensions = settings.embedding_dimensions

    async def ensure_collection(self) -> None:
        """Create collection and payload indexes if they don't exist."""
        collections = await self._client.get_collections()
        exists = any(c.name == self._collection for c in collections.collections)

        if not exists:
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(
                    size=self._dimensions,
                    distance=Distance.COSINE,
                ),
            )

        # Payload indexes for efficient filtering
        index_defs: list[tuple[str, PayloadSchemaType]] = [
            ("user_id", PayloadSchemaType.KEYWORD),
            ("category", PayloadSchemaType.KEYWORD),
            ("tags", PayloadSchemaType.KEYWORD),
            ("session_id", PayloadSchemaType.KEYWORD),
            ("pinned", PayloadSchemaType.BOOL),
            ("importance", PayloadSchemaType.FLOAT),
            ("created_at", PayloadSchemaType.DATETIME),
            ("expires_at", PayloadSchemaType.DATETIME),
        ]
        for field, schema in index_defs:
            with contextlib.suppress(Exception):
                await self._client.create_payload_index(
                    collection_name=self._collection,
                    field_name=field,
                    field_schema=schema,
                )

    async def upsert(
        self,
        point_id: str,
        embedding: list[float],
        payload: dict[str, Any],
    ) -> None:
        """Store a memory vector with metadata payload."""
        await self._client.upsert(
            collection_name=self._collection,
            points=[
                PointStruct(id=point_id, vector=embedding, payload=payload),
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
        must_conditions: list[FieldCondition] = [
            FieldCondition(key="user_id", match=MatchValue(value=user_id)),
        ]

        # Exclude expired memories (use DatetimeRange for datetime fields)
        now = datetime.now(UTC)
        must_not_conditions: list[FieldCondition] = [
            FieldCondition(key="expires_at", range=DatetimeRange(lt=now)),
        ]

        if category:
            must_conditions.append(
                FieldCondition(key="category", match=MatchValue(value=category))
            )
        if session_id:
            must_conditions.append(
                FieldCondition(key="session_id", match=MatchValue(value=session_id))
            )
        if tags:
            for tag in tags:
                must_conditions.append(
                    FieldCondition(key="tags", match=MatchValue(value=tag))
                )
        if time_range_hours:
            cutoff = datetime.now(UTC) - timedelta(hours=time_range_hours)
            must_conditions.append(
                FieldCondition(key="created_at", range=DatetimeRange(gte=cutoff))
            )

        results = await self._client.query_points(
            collection_name=self._collection,
            query=query_embedding,
            query_filter=Filter(must=must_conditions, must_not=must_not_conditions),
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
                must=[FieldCondition(key="user_id", match=MatchValue(value=user_id))]
            ),
        )
        return result.count

    async def close(self) -> None:
        """Shut down the Qdrant client."""
        await self._client.close()
