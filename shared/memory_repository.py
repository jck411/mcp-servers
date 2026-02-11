"""SQLite metadata store for memory access tracking and TTL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite


class MemoryRepository:
    """Track memory metadata, access patterns, and TTL in SQLite."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Create the database and tables if needed."""
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
        """Insert a new memory metadata record."""
        assert self._conn is not None
        now = datetime.now(UTC).isoformat()
        await self._conn.execute(
            """
            INSERT INTO memory_meta
                (id, user_id, session_id, category, content_preview,
                 importance, pinned, created_at, expires_at, last_accessed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                user_id,
                session_id,
                category,
                content_preview[:200],
                importance,
                pinned,
                now,
                expires_at,
                now,
            ),
        )
        await self._conn.commit()

    async def record_access(self, memory_ids: list[str]) -> None:
        """Increment access count and update last_accessed_at."""
        assert self._conn is not None
        now = datetime.now(UTC).isoformat()
        for mid in memory_ids:
            await self._conn.execute(
                """
                UPDATE memory_meta
                SET access_count = access_count + 1, last_accessed_at = ?
                WHERE id = ?
                """,
                (now, mid),
            )
        await self._conn.commit()

    async def delete(self, memory_ids: list[str]) -> int:
        """Delete memory metadata by IDs. Return count of deleted rows."""
        assert self._conn is not None
        placeholders = ",".join("?" for _ in memory_ids)
        cursor = await self._conn.execute(
            f"DELETE FROM memory_meta WHERE id IN ({placeholders})",  # noqa: S608
            memory_ids,
        )
        await self._conn.commit()
        return cursor.rowcount

    async def delete_by_session(
        self, session_id: str, include_pinned: bool = False
    ) -> list[str]:
        """Delete memories for a session. Return deleted IDs."""
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
        """Return IDs of memories past their expiration time."""
        assert self._conn is not None
        now = datetime.now(UTC).isoformat()
        cursor = await self._conn.execute(
            "SELECT id FROM memory_meta WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        )
        rows = await cursor.fetchall()
        return [row["id"] for row in rows]

    async def get_stale(self, min_importance: float, max_access: int = 0) -> list[str]:
        """Return IDs of low-importance, never-accessed memories."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT id FROM memory_meta
            WHERE pinned = 0 AND importance < ? AND access_count <= ?
            """,
            (min_importance, max_access),
        )
        rows = await cursor.fetchall()
        return [row["id"] for row in rows]

    async def decay_importance(self, factor: float = 0.95, min_age_days: int = 7) -> int:
        """Reduce importance of old non-pinned memories. Return affected count."""
        assert self._conn is not None
        cutoff = (datetime.now(UTC) - timedelta(days=min_age_days)).isoformat()
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

    async def stats(self, user_id: str = "default") -> dict[str, Any]:
        """Return aggregate memory statistics."""
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
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
