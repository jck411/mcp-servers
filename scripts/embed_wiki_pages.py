#!/usr/bin/env python3
"""One-time script to embed all active wiki pages into Qdrant.

Run from the mcp-servers repo root after deploying the embed_wiki_page code:

    uv run python scripts/embed_wiki_pages.py

Requires the same environment variables as the knowledge server
(OPENROUTER_API_KEY, QDRANT_URL, etc.).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Allow imports from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from servers.knowledge import (  # noqa: E402
    BM25SparseEncoder,
    EmbeddingClient,
    KnowledgeDB,
    KnowledgeSettings,
    KnowledgeVectorStore,
)


async def main() -> None:
    settings = KnowledgeSettings()  # type: ignore[call-arg]
    db = KnowledgeDB(Path(settings.db_path))
    await db.initialize()

    embeddings = EmbeddingClient(settings)
    sparse_encoder = BM25SparseEncoder()
    vectors = KnowledgeVectorStore(settings)
    await vectors.initialize()

    # Warm up BM25 from existing chunks.
    all_chunks = await vectors.chunks_all(limit=50_000)
    texts = [str(c.get("content", "")) for c in all_chunks]
    if texts:
        sparse_encoder.fit_batch(texts)
        print(f"BM25 warmed up with {len(texts)} existing chunks")

    try:
        pages = await db.wiki_list(status="active")
        print(f"Found {len(pages)} active wiki pages to embed")

        for i, page_summary in enumerate(pages):
            slug = page_summary["slug"]
            page = await db.wiki_get(slug)
            if not page:
                print(f"  SKIP {slug} — not found in DB")
                continue

            body = page.get("body_md") or ""
            if not body.strip():
                print(f"  SKIP {slug} — empty body")
                continue

            await vectors.embed_wiki_page(
                slug=slug,
                domain=page["domain"],
                title=page["title"],
                body_md=body,
                embeddings=embeddings,
                sparse_encoder=sparse_encoder,
            )
            print(f"  [{i+1}/{len(pages)}] Embedded {slug} ({len(body)} chars)")

        print(f"\nDone — embedded {len(pages)} wiki pages into Qdrant")
    finally:
        await embeddings.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
