"""Nightly Knowledge wiki maintenance entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from servers.knowledge import (
    BM25SparseEncoder,
    EmbeddingClient,
    KnowledgeDB,
    KnowledgeSettings,
    KnowledgeVectorStore,
    preview_wiki_rebuild,
    rebuild_wiki,
)
from shared.logging_config import get_logger

log = get_logger("maintain_wiki")


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    settings = KnowledgeSettings()  # type: ignore[call-arg]
    db = KnowledgeDB(settings.db_path)
    if args.dry_run:
        try:
            await db.initialize()
            return await preview_wiki_rebuild(
                settings,
                db,
                domain=args.domain,
                entity_slug=args.entity_slug,
                force_full=args.force_full,
            )
        finally:
            await db.close()

    embeddings = EmbeddingClient(settings)
    sparse_encoder = BM25SparseEncoder()
    vectors = KnowledgeVectorStore(settings)
    try:
        await vectors.ensure_collection()
        await db.initialize()
        chunks = await vectors.chunks_all()
        texts = [chunk["content"] for chunk in chunks if chunk.get("content")]
        if texts:
            sparse_encoder.fit_batch(texts)
        return await rebuild_wiki(
            settings,
            embeddings,
            sparse_encoder,
            vectors,
            db,
            domain=args.domain,
            entity_slug=args.entity_slug,
            force_full=args.force_full,
        )
    finally:
        await embeddings.close()
        await vectors.close()
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Knowledge wiki rebuild job")
    parser.add_argument("--domain")
    parser.add_argument("--entity-slug")
    parser.add_argument("--force-full", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(_run(args))
    log.info("wiki_maintenance_result %s", json.dumps(result, sort_keys=True))
    print(json.dumps(result, sort_keys=True))
    if not result.get("success"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
