"""Background maintenance tasks: TTL cleanup only.

Memories are stored permanently by default.  Only memories with an explicit
TTL (expires_at) are automatically removed.  Importance decay and stale-memory
cleanup have been removed so that facts, dates, and other important information
are never silently deleted.
"""

from __future__ import annotations

import asyncio
import sys

from shared.memory_config import MemorySettings
from shared.memory_repository import MemoryRepository
from shared.vector_store import VectorStore


async def _ttl_cleanup_loop(
    repo: MemoryRepository,
    vectors: VectorStore,
    interval_minutes: int,
) -> None:
    """Periodically remove memories that have an explicit expiration time."""
    while True:
        await asyncio.sleep(interval_minutes * 60)
        try:
            expired_ids = await repo.get_expired()
            if expired_ids:
                await vectors.delete(expired_ids)
                await repo.delete(expired_ids)
                print(
                    f"[MEMORY-MAINT] Cleaned {len(expired_ids)} TTL-expired memories",
                    file=sys.stderr,
                    flush=True,
                )
        except Exception as exc:
            print(f"[MEMORY-MAINT] Cleanup error: {exc}", file=sys.stderr, flush=True)


def start_maintenance(
    repo: MemoryRepository,
    vectors: VectorStore,
    settings: MemorySettings,
) -> None:
    """Launch maintenance background tasks on the running event loop."""
    loop = asyncio.get_event_loop()
    loop.create_task(
        _ttl_cleanup_loop(
            repo,
            vectors,
            settings.cleanup_interval_minutes,
        )
    )
    print("[MEMORY-MAINT] TTL cleanup task started", file=sys.stderr, flush=True)
