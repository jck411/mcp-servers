"""Background maintenance tasks: TTL cleanup, importance decay."""

from __future__ import annotations

import asyncio
import sys

from shared.memory_config import MemorySettings
from shared.memory_repository import MemoryRepository
from shared.vector_store import VectorStore


async def _cleanup_loop(
    repo: MemoryRepository,
    vectors: VectorStore,
    interval_minutes: int,
    min_importance: float,
) -> None:
    """Periodically remove expired and stale memories."""
    while True:
        await asyncio.sleep(interval_minutes * 60)
        try:
            expired_ids = await repo.get_expired()
            if expired_ids:
                await vectors.delete(expired_ids)
                await repo.delete(expired_ids)
                print(
                    f"[MEMORY-MAINT] Cleaned {len(expired_ids)} expired memories",
                    file=sys.stderr,
                    flush=True,
                )

            stale_ids = await repo.get_stale(min_importance)
            if stale_ids:
                await vectors.delete(stale_ids)
                await repo.delete(stale_ids)
                print(
                    f"[MEMORY-MAINT] Cleaned {len(stale_ids)} stale memories",
                    file=sys.stderr,
                    flush=True,
                )
        except Exception as exc:
            print(f"[MEMORY-MAINT] Cleanup error: {exc}", file=sys.stderr, flush=True)


async def _decay_loop(repo: MemoryRepository, interval_hours: int) -> None:
    """Periodically decay importance of old memories."""
    while True:
        await asyncio.sleep(interval_hours * 3600)
        try:
            count = await repo.decay_importance()
            if count:
                print(
                    f"[MEMORY-MAINT] Decayed importance for {count} memories",
                    file=sys.stderr,
                    flush=True,
                )
        except Exception as exc:
            print(f"[MEMORY-MAINT] Decay error: {exc}", file=sys.stderr, flush=True)


def start_maintenance(
    repo: MemoryRepository,
    vectors: VectorStore,
    settings: MemorySettings,
) -> None:
    """Launch maintenance background tasks on the running event loop."""
    loop = asyncio.get_event_loop()
    loop.create_task(
        _cleanup_loop(
            repo,
            vectors,
            settings.cleanup_interval_minutes,
            settings.min_importance_threshold,
        )
    )
    loop.create_task(_decay_loop(repo, settings.decay_interval_hours))
    print("[MEMORY-MAINT] Maintenance tasks started", file=sys.stderr, flush=True)
