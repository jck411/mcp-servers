"""Nightly Knowledge wiki maintenance entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from servers.knowledge import (
    BM25SparseEncoder,
    EmbeddingClient,
    KnowledgeDB,
    KnowledgeSettings,
    KnowledgeVectorStore,
    preview_wiki_rebuild,
    rebuild_wiki,
    wiki_lint_pass,
)
from shared.logging_config import get_logger

log = get_logger("maintain_wiki")
DEFAULT_BACKUP_MANIFEST = Path("/mnt/backups/knowledge.latest.manifest.json")


def _backup_created_at(manifest: dict[str, Any]) -> datetime:
    created_at = str(manifest.get("created_at") or "")
    if not created_at:
        raise RuntimeError("backup manifest missing created_at")
    if created_at.endswith("Z"):
        created_at = f"{created_at[:-1]}+00:00"
    dt = datetime.fromisoformat(created_at)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _require_fresh_backup(
    manifest_path: Path,
    max_age_hours: float,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if max_age_hours <= 0:
        raise RuntimeError("max backup age must be positive")
    if not manifest_path.is_file():
        raise RuntimeError(f"fresh backup manifest not found: {manifest_path}")

    manifest = json.loads(manifest_path.read_text())
    created_at = _backup_created_at(manifest)
    age = (now or datetime.now(UTC)) - created_at
    if age > timedelta(hours=max_age_hours):
        raise RuntimeError(
            f"backup manifest is stale: {manifest_path} age_hours={age.total_seconds() / 3600:.2f}"
        )

    archive_path = manifest.get("archive_path")
    if archive_path:
        archive = Path(str(archive_path))
        if not archive.is_file() or archive.stat().st_size == 0:
            raise RuntimeError(f"backup archive from manifest is missing or empty: {archive}")
    return manifest


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.dry_run and not args.force:
        manifest = _require_fresh_backup(args.backup_manifest, args.max_backup_age_hours)
        log.info(
            "wiki_backup_gate_ok manifest=%s created_at=%s archive=%s",
            args.backup_manifest,
            manifest.get("created_at"),
            manifest.get("archive_path"),
        )

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
        result = await rebuild_wiki(
            settings,
            embeddings,
            sparse_encoder,
            vectors,
            db,
            domain=args.domain,
            entity_slug=args.entity_slug,
            force_full=args.force_full,
        )
        # Run lint pass after successful rebuild
        if result.get("success"):
            try:
                lint_result = await wiki_lint_pass(db)
                log.info(
                    "wiki_lint_pass_complete items_created=%s",
                    lint_result.get("items_created", 0),
                )
                result["lint"] = lint_result
            except Exception:
                log.warning("wiki_lint_pass_failed", exc_info=True)
        return result
    finally:
        await embeddings.close()
        await vectors.close()
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Knowledge wiki rebuild job")
    parser.add_argument("--domain")
    parser.add_argument("--entity-slug")
    parser.add_argument("--force-full", action="store_true")
    parser.add_argument("--backup-manifest", type=Path, default=DEFAULT_BACKUP_MANIFEST)
    parser.add_argument("--max-backup-age-hours", type=float, default=6.0)
    parser.add_argument("--force", action="store_true", help="bypass the fresh-backup guard")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(_run(args))
    log.info("wiki_maintenance_result %s", json.dumps(result, sort_keys=True))
    print(json.dumps(result, sort_keys=True))
    if not result.get("success"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
