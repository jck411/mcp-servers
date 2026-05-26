"""Nightly Knowledge maintenance runner.

Audits SQLite and Qdrant after the ordered backup, applies safe vector repairs,
and mirrors review-worthy findings into the curation queue.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import traceback
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from qdrant_client import AsyncQdrantClient, models

from servers.knowledge.db import KnowledgeDB
from servers.knowledge.embeddings import BM25SparseEncoder, EmbeddingClient
from servers.knowledge.settings import PROJECT_ROOT, KnowledgeSettings
from servers.knowledge.vectors import KnowledgeVectorStore

FACT_AUDIT_COLUMNS = (
    "id",
    "domain",
    "key",
    "value",
    "source",
    "confidence",
    "valid_from",
    "valid_until",
    "as_of",
    "review_after",
    "origin_type",
    "origin_ref",
    "last_confirmed_at",
    "created_at",
    "updated_at",
    "type",
    "tags",
)
EXPLICIT_END_CUE_RE = re.compile(
    r"\b("
    r"expires?\s+(?:on|at|by|in|after)|expiry|expiration|"
    r"valid\s+(?:until|through|thru)|good\s+(?:until|through|thru)|"
    r"end\s+date|ends\s+(?:on|at|by|in)|deadline|due\s+(?:by|on)|"
    r"matures?\s+(?:on|at|in)|renews?\s+on"
    r")\b",
    re.IGNORECASE,
)
SNAPSHOT_RETENTION_DAYS = 30
PENDING_EXPIRY_DAYS = 14
FACT_VECTOR_REPAIR_BATCH = 100
NTFY_TOPIC = "jack-knowledge-system-42x7"
LogFn = Callable[[str], None]


def _existing_columns(con: sqlite3.Connection, table: str, wanted: tuple[str, ...]) -> list[str]:
    existing = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
    return [column for column in wanted if column in existing]


def _today(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).date().isoformat()


def audit_sqlite(
    db_path: Path | str,
    now: datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Find SQLite-side maintenance signals."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        fact_columns = ", ".join(_existing_columns(con, "facts", FACT_AUDIT_COLUMNS))
        expired_facts = [
            dict(row)
            for row in con.execute(
                f"SELECT {fact_columns} FROM facts "  # noqa: S608
                "WHERE valid_until IS NOT NULL AND valid_until < ?",
                (_today(now),),
            )
        ]
        empty_domains = [
            dict(row)
            for row in con.execute("""
                SELECT d.name, d.description
                FROM domains d
                LEFT JOIN facts f ON f.domain = d.name
                WHERE d.archived = 0
                  AND d.name NOT IN (
                      SELECT domain FROM facts
                      WHERE key = 'meta.ignore_empty_check' AND LOWER(value) = 'true'
                  )
                GROUP BY d.name
                HAVING COUNT(f.id) = 0
            """)
        ]
    finally:
        con.close()
    return {"expired_facts": expired_facts, "empty_domains": empty_domains}


def _load_facts_by_id(db_path: Path | str) -> dict[str, dict[str, Any]]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        columns = _existing_columns(con, "facts", FACT_AUDIT_COLUMNS)
        rows = con.execute(f"SELECT {', '.join(columns)} FROM facts ORDER BY domain, key")
        return {row["id"]: dict(row) for row in rows}
    finally:
        con.close()


def scan_temporal_fact_candidates(db_path: Path | str, limit: int = 30) -> list[dict[str, Any]]:
    """Find facts with explicit expiry/end language but no structured valid_until."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT id, domain, key, value, source, valid_from, valid_until, updated_at
            FROM facts
            WHERE valid_until IS NULL OR valid_until = ''
            ORDER BY updated_at DESC
            """
        ).fetchall()
    finally:
        con.close()

    matches = []
    for row in rows:
        fact = dict(row)
        text = f"{fact.get('domain')} {fact.get('key')} {fact.get('value')}".lower()
        if EXPLICIT_END_CUE_RE.search(text):
            matches.append(fact)
        if len(matches) >= limit:
            break
    return matches


def _record_id(point: Any) -> str:
    if isinstance(point, dict):
        return str(point["id"])
    return str(point.id)


def _record_payload(point: Any) -> dict[str, Any]:
    if isinstance(point, dict):
        return dict(point.get("payload") or {})
    return dict(point.payload or {})


async def scan_qdrant_sources(
    client: AsyncQdrantClient,
    collection: str,
    live_source_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Scroll Qdrant once and classify legacy source, fact, and wiki points."""
    representatives: dict[str, str] = {}
    seen_source_ids: set[str] = set()
    seen_fact_ids: set[str] = set()
    seen_wiki_slugs: set[str] = set()
    orphan_points: list[dict[str, Any]] = []
    malformed_points: list[dict[str, Any]] = []
    fact_points: list[dict[str, Any]] = []
    wiki_points: list[dict[str, Any]] = []
    offset = None

    while True:
        records, offset = await client.scroll(
            collection_name=collection,
            limit=250,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in records:
            point_id = _record_id(point)
            payload = _record_payload(point)
            point_type = payload.get("type") or payload.get("source_type")
            if point_type == "fact":
                fact_id = payload.get("fact_id")
                if not fact_id:
                    malformed_points.append({
                        "point_id": point_id,
                        "domain": payload.get("domain"),
                        "filename": payload.get("source_name") or payload.get("filename"),
                        "reason": "fact point missing fact_id",
                    })
                    continue
                seen_fact_ids.add(str(fact_id))
                fact_points.append({
                    "point_id": point_id,
                    "fact_id": str(fact_id),
                    "domain": payload.get("domain"),
                    "key": payload.get("key"),
                    "value": payload.get("value"),
                    "valid_from": payload.get("valid_from"),
                    "valid_until": payload.get("valid_until"),
                    "as_of": payload.get("as_of"),
                    "review_after": payload.get("review_after"),
                    "updated_at": payload.get("updated_at"),
                    "fact_type": payload.get("fact_type"),
                    "tags": payload.get("tags"),
                })
                continue

            if point_type == "wiki_page":
                slug = str(payload.get("source_id") or "")
                if slug:
                    seen_wiki_slugs.add(slug)
                    wiki_points.append({
                        "point_id": point_id,
                        "slug": slug,
                        "domain": payload.get("domain"),
                        "title": payload.get("source_name"),
                    })
                else:
                    malformed_points.append({
                        "point_id": point_id,
                        "domain": payload.get("domain"),
                        "filename": payload.get("source_name") or payload.get("filename"),
                        "reason": "wiki_page point missing source_id (slug)",
                    })
                continue

            source_id = payload.get("source_id")
            if not source_id:
                malformed_points.append({
                    "point_id": point_id,
                    "domain": payload.get("domain"),
                    "filename": payload.get("source_name") or payload.get("filename"),
                    "reason": "chunk point missing source_id",
                })
                continue
            source_id = str(source_id)
            seen_source_ids.add(source_id)
            representatives.setdefault(source_id, point_id)
            if live_source_ids is not None and source_id not in live_source_ids:
                orphan_points.append({
                    "point_id": point_id,
                    "source_id": source_id,
                    "domain": payload.get("domain"),
                    "filename": payload.get("filename"),
                    "chunk_index": payload.get("chunk_index"),
                })
        if offset is None:
            break

    return {
        "representatives": representatives,
        "seen_source_ids": seen_source_ids,
        "seen_fact_ids": seen_fact_ids,
        "seen_wiki_slugs": seen_wiki_slugs,
        "orphan_points": orphan_points,
        "malformed_points": malformed_points,
        "fact_points": fact_points,
        "wiki_points": wiki_points,
    }


def audit_facts_without_vectors(
    facts: dict[str, dict[str, Any]],
    qdrant_scan: dict[str, Any],
) -> list[dict[str, Any]]:
    seen_fact_ids = qdrant_scan["seen_fact_ids"]
    return [fact for fact_id, fact in facts.items() if fact_id not in seen_fact_ids]


def audit_orphan_fact_vectors(
    facts: dict[str, dict[str, Any]],
    qdrant_scan: dict[str, Any],
) -> list[dict[str, Any]]:
    return [point for point in qdrant_scan["fact_points"] if point["fact_id"] not in facts]


def audit_stale_fact_vectors(
    facts: dict[str, dict[str, Any]],
    qdrant_scan: dict[str, Any],
) -> list[dict[str, Any]]:
    stale = []
    comparable = (
        "domain",
        "key",
        "value",
        "valid_from",
        "valid_until",
        "as_of",
        "review_after",
        "updated_at",
    )
    for point in qdrant_scan["fact_points"]:
        fact = facts.get(point["fact_id"])
        if not fact:
            continue
        mismatches = [
            field
            for field in comparable
            if str(point.get(field) or "") != str(fact.get(field) or "")
        ]
        if mismatches:
            stale.append({**point, "mismatches": mismatches})
    return stale


async def delete_legacy_source_vectors(
    client: AsyncQdrantClient,
    collection: str,
    orphans: list[dict[str, Any]],
    log: LogFn,
) -> list[dict[str, Any]]:
    executed = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for orphan in orphans:
        grouped.setdefault(orphan["source_id"], []).append(orphan)

    for source_id, items in grouped.items():
        sample = items[0]
        try:
            await client.delete(
                collection_name=collection,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="source_id",
                                match=models.MatchValue(value=source_id),
                            )
                        ]
                    )
                ),
            )
        except Exception as exc:  # noqa: BLE001
            log(f"  FAIL legacy source vector delete [{source_id}]: {exc}")
            continue
        msg = f"Deleted {len(items)} legacy source vector(s) for source id {source_id}"
        log(f"  AUTO-FIX legacy source vectors [{source_id}]: {msg}")
        executed.append({
            "action": "delete_orphan_vectors",
            "target_id": source_id,
            "description": (
                f"Orphan vectors removed: {len(items)} chunk(s) from "
                f"{sample.get('domain')}/{sample.get('filename')}"
            ),
            "result": msg,
            "confidence": 1.0,
            "risk": "low",
        })
    return executed


async def delete_points_by_id(
    client: AsyncQdrantClient,
    collection: str,
    points: list[dict[str, Any]],
    action: str,
    description: str,
    log: LogFn,
) -> list[dict[str, Any]]:
    if not points:
        return []
    ids = [point["point_id"] for point in points]
    try:
        await client.delete(
            collection_name=collection,
            points_selector=models.PointIdsList(points=ids),
        )
    except Exception as exc:  # noqa: BLE001
        log(f"  FAIL {description}: {exc}")
        return []
    msg = f"Deleted {len(ids)} {description}"
    log(f"  AUTO-FIX {description}: {msg}")
    return [{
        "action": action,
        "target_id": None,
        "description": msg,
        "result": msg,
        "confidence": 1.0,
        "risk": "low",
    }]


async def repair_facts_without_vectors(
    settings: KnowledgeSettings,
    missing_facts: list[dict[str, Any]],
    log: LogFn,
) -> list[dict[str, Any]]:
    if not missing_facts:
        return []

    batch = missing_facts[:FACT_VECTOR_REPAIR_BATCH]
    log(f"  Embedding {len(batch)} fact(s) (of {len(missing_facts)} missing)...")
    embeddings = EmbeddingClient(settings)
    sparse_encoder = BM25SparseEncoder()
    vectors = KnowledgeVectorStore(settings)
    executed = []
    errors = 0
    try:
        await vectors.ensure_collection()
        for index, fact in enumerate(batch, start=1):
            domain = fact.get("domain")
            key = fact.get("key")
            if not domain or not key:
                continue
            try:
                await vectors.embed_fact(
                    fact=fact,
                    embeddings=embeddings,
                    sparse_encoder=sparse_encoder,
                )
            except Exception as exc:  # noqa: BLE001
                errors += 1
                log(f"  FAIL fact-vector repair [{domain}/{key}]: {exc}")
                if errors >= 5:
                    log("  FAIL too many fact-vector repair errors; stopping early")
                    break
                continue
            executed.append({
                "action": "repair_fact_vector",
                "target_id": f"{domain}/{key}",
                "description": f"Embedded missing fact vector: {domain}/{key}",
                "confidence": 1.0,
                "risk": "low",
            })
            if index % 25 == 0:
                log(f"  ... {index}/{len(batch)} embedded ({errors} errors)")
    finally:
        await embeddings.close()
        await vectors.close()

    skipped = len(missing_facts) - len(batch)
    if skipped:
        log(
            f"  INFO {skipped} more facts deferred to next run "
            f"(batch cap={FACT_VECTOR_REPAIR_BATCH})"
        )
    return executed


def export_facts_snapshot(db_path: Path, backup_root: Path, run_date: date) -> tuple[Path, int]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        facts = [dict(row) for row in con.execute("SELECT * FROM facts ORDER BY domain, key")]
    finally:
        con.close()
    snap_path = backup_root / f"facts_snapshot_{run_date.isoformat()}.json"
    snap_path.write_text(json.dumps(facts, indent=2, default=str))
    return snap_path, len(facts)


def rotate_snapshots(backup_root: Path, retention_days: int, now: datetime, log: LogFn) -> int:
    cutoff = (now - timedelta(days=retention_days)).date()
    deleted = 0
    for snap in sorted(backup_root.glob("facts_snapshot_*.json")):
        try:
            snap_date = date.fromisoformat(snap.stem.replace("facts_snapshot_", ""))
        except ValueError:
            log(f"  Skipping snapshot with invalid date format: {snap.name}")
            continue
        if snap_date < cutoff:
            snap.unlink()
            deleted += 1
            log(f"  Rotated old snapshot: {snap.name}")
    return deleted


def _parse_pending_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith(" UTC"):
        text = text[:-4] + "+00:00"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _pending_key(entry: dict[str, Any]) -> str:
    return json.dumps(
        {
            "issue_type": entry.get("issue_type"),
            "action": entry.get("action"),
            "target_id": entry.get("target_id"),
            "description": entry.get("description"),
        },
        sort_keys=True,
        default=str,
    )


def merge_pending_file(
    pending_file: Path,
    deferred: list[dict[str, Any]],
    now: datetime,
) -> tuple[list[dict[str, Any]], int, int]:
    existing = []
    if pending_file.exists():
        try:
            existing = json.loads(pending_file.read_text()).get("pending", [])
        except (OSError, json.JSONDecodeError):
            existing = []

    cutoff = now - timedelta(days=PENDING_EXPIRY_DAYS)
    kept = [
        entry for entry in existing
        if (deferred_at := _parse_pending_datetime(entry.get("deferred_at"))) is None
        or deferred_at > cutoff
    ]
    for entry in deferred:
        entry.setdefault("deferred_at", now.isoformat())

    merged = []
    seen = set()
    for entry in kept + deferred:
        key = _pending_key(entry)
        if key in seen:
            continue
        seen.add(key)
        merged.append(entry)

    pending_file.parent.mkdir(parents=True, exist_ok=True)
    pending_file.write_text(json.dumps({
        "last_updated": now.isoformat(),
        "pending": merged,
    }, indent=2, default=str))
    return merged, len(existing) - len(kept), len(kept + deferred) - len(merged)


async def mirror_review_candidates_to_curation(
    db_path: Path,
    pending: list[dict[str, Any]],
    log: LogFn,
) -> int:
    mirrored = 0
    db = KnowledgeDB(db_path)
    await db.initialize()
    try:
        for entry in pending:
            if entry.get("issue_type") == "empty_domain":
                continue
            item_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"maintenance:{_pending_key(entry)}"))
            await db.curation_upsert(
                item_id=item_id,
                kind="maintenance_action",
                title=entry.get("description") or entry.get("issue_type") or "Maintenance action",
                summary=entry.get("rationale") or "",
                source_refs=[{
                    "type": "pending_maintenance",
                    "issue_type": entry.get("issue_type"),
                    "target_id": entry.get("target_id"),
                }],
                proposed_actions=[{
                    "action": entry.get("action"),
                    "target_id": entry.get("target_id"),
                    "description": entry.get("description"),
                    "rationale": entry.get("rationale"),
                    "confidence": entry.get("confidence"),
                    "risk": entry.get("risk"),
                }],
                risk=entry.get("risk") or "medium",
                confidence=float(entry.get("confidence") or 0.0),
                created_at=entry.get("deferred_at"),
            )
            mirrored += 1

        for fact in scan_temporal_fact_candidates(db_path):
            item_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"temporal_fact:{fact['id']}"))
            await db.curation_upsert(
                item_id=item_id,
                kind="temporal_fact_cleanup",
                title=f"Review temporal status for {fact['domain']}/{fact['key']}",
                summary=(
                    "This fact contains an explicit expiry/end cue but has no "
                    "structured valid_until date."
                ),
                source_refs=[{
                    "type": "fact",
                    "id": fact["id"],
                    "domain": fact["domain"],
                    "key": fact["key"],
                }],
                proposed_actions=[{
                    "action": "flag_for_review",
                    "description": (
                        "Add valid_until only if the end date changes whether "
                        "this fact should be treated as current."
                    ),
                }],
                risk="low",
                confidence=0.55,
            )
            mirrored += 1
    finally:
        await db.close()

    if mirrored:
        log(f"  Mirrored {mirrored} review candidate(s) into curation_items")
    return mirrored


def build_report(
    audit: dict[str, Any],
    executed: list[dict[str, Any]],
    deferred: list[dict[str, Any]],
    snap_path: Path | str,
    fact_count: int,
    now: datetime,
) -> str:
    lines = [
        f"# Knowledge Maintenance Report - {now.date().isoformat()}",
        f"Generated: {now.isoformat()}",
        "",
        "## System Health Summary",
        "Facts, wiki pages, and derived vectors were audited.",
        "",
        "## Issues Detected",
        "| Category | Count |",
        "|---|---|",
        f"| Expired facts (historical, no action) | {len(audit['expired_facts'])} |",
        f"| Legacy source vectors (auto-fixed) | {len(audit['orphan_vectors'])} |",
        f"| Malformed Qdrant points (auto-fixed) | {len(audit.get('malformed_points', []))} |",
        f"| SQLite facts without derived vectors | {len(audit.get('facts_without_vectors', []))} |",
        f"| Orphan derived fact vectors | {len(audit.get('orphan_fact_vectors', []))} |",
        f"| Stale derived fact vectors | {len(audit.get('stale_fact_vectors', []))} |",
        f"| Empty domains | {len(audit['empty_domains'])} |",
        "",
        f"## Actions Executed ({len(executed)})",
    ]
    lines.extend(
        f"- {item.get('description', item.get('action', ''))}: {item.get('result', '')}"
        for item in executed
    )
    if not executed:
        lines.append("- None")

    lines += ["", f"## Deferred Actions ({len(deferred)}) - pending_maintenance.json"]
    lines.extend(
        f"- [{item.get('risk', '?')}/{float(item.get('confidence', 0) or 0):.2f}] "
        f"{item.get('description', '')} -> suggested: {item.get('action', '')}"
        for item in deferred
    )
    if not deferred:
        lines.append("- None")

    lines += [
        "",
        "## Facts Snapshot",
        f"Exported {fact_count} facts to {snap_path}",
        "",
        "## Fact Retrieval Vector Audit",
    ]
    for label, key in (
        ("Facts without vectors", "facts_without_vectors"),
        ("Orphan fact vectors", "orphan_fact_vectors"),
        ("Stale fact vectors", "stale_fact_vectors"),
    ):
        lines.append(f"### {label}")
        items = audit.get(key, [])
        if not items:
            lines.append("- None found")
            continue
        for item in items[:15]:
            lines.append(
                f"- {item.get('domain')}/{item.get('key')} "
                f"(fact_id={item.get('id') or item.get('fact_id')}, "
                f"mismatches={','.join(item.get('mismatches', [])) or 'n/a'})"
            )
    return "\n".join(lines)


def notify(title: str, msg: str, priority: str = "default", log: LogFn | None = None) -> None:
    try:
        httpx.post(
            f"https://ntfy.sh/{os.environ.get('NTFY_TOPIC', NTFY_TOPIC)}",
            content=msg.encode(),
            headers={"Title": title, "Priority": priority},
            timeout=8,
        )
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"  WARN notify failed: {exc}")


async def run_maintenance(
    settings: KnowledgeSettings,
    *,
    backup_root: Path,
    log_dir: Path,
    pending_file: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    run_date = now.date()
    log_dir.mkdir(parents=True, exist_ok=True)
    backup_root.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"maintenance-{run_date.isoformat()}.log"
    log_lines: list[str] = []
    errors: list[str] = []

    def log(message: str) -> None:
        entry = f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {message}"
        log_lines.append(entry)
        print(entry, flush=True)
        if message.startswith("  FAIL"):
            errors.append(message.strip())

    mode_label = " [DRY RUN]" if dry_run else ""
    log(f"=== Knowledge Maintenance Agent - {now.isoformat()}{mode_label} ===")
    if sqlite3.sqlite_version_info < (3, 44, 0):
        raise RuntimeError(f"SQLite {sqlite3.sqlite_version} is too old; requires >= 3.44.0")

    snap_path: Path | str = "(dry-run, skipped)"
    fact_count = 0
    log("Phase 1: Exporting facts snapshot...")
    if dry_run:
        log("  Skipped snapshot export and rotation (dry run)")
    else:
        snap_path, fact_count = export_facts_snapshot(settings.db_path, backup_root, run_date)
        rotated = rotate_snapshots(backup_root, SNAPSHOT_RETENTION_DAYS, now, log)
        log(f"  {fact_count} facts -> {snap_path}; rotated={rotated}")

    log("Phase 2: Auditing SQLite and Qdrant...")
    audit: dict[str, Any] = audit_sqlite(settings.db_path, now)
    qdrant = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        qdrant_scan = await scan_qdrant_sources(qdrant, settings.qdrant_collection, set())
        audit["orphan_vectors"] = qdrant_scan["orphan_points"]
        audit["malformed_points"] = qdrant_scan["malformed_points"]
        facts = _load_facts_by_id(settings.db_path)
        audit["facts_without_vectors"] = audit_facts_without_vectors(facts, qdrant_scan)
        audit["orphan_fact_vectors"] = audit_orphan_fact_vectors(facts, qdrant_scan)
        audit["stale_fact_vectors"] = audit_stale_fact_vectors(facts, qdrant_scan)
    except Exception as exc:  # noqa: BLE001
        log(f"  FAIL Qdrant scan failed: {exc}")
        audit |= {
            "orphan_vectors": [],
            "malformed_points": [],
            "facts_without_vectors": [],
            "orphan_fact_vectors": [],
            "stale_fact_vectors": [],
        }

    log(
        "  "
        f"expired={len(audit['expired_facts'])} empty_domains={len(audit['empty_domains'])} "
        f"legacy_vectors={len(audit['orphan_vectors'])} "
        f"malformed={len(audit['malformed_points'])} "
        f"missing_fact_vectors={len(audit['facts_without_vectors'])} "
        f"orphan_fact_vectors={len(audit['orphan_fact_vectors'])} "
        f"stale_fact_vectors={len(audit['stale_fact_vectors'])}"
    )

    executed: list[dict[str, Any]] = []
    if dry_run:
        log("Phase 3: Skipping safe vector repairs (dry run)")
    else:
        log("Phase 3: Applying safe vector repairs...")
        try:
            executed.extend(
                await delete_legacy_source_vectors(
                    qdrant, settings.qdrant_collection, audit["orphan_vectors"], log
                )
            )
            executed.extend(
                await delete_points_by_id(
                    qdrant,
                    settings.qdrant_collection,
                    audit["malformed_points"],
                    "delete_malformed_points",
                    "malformed Qdrant point(s)",
                    log,
                )
            )
            executed.extend(
                await delete_points_by_id(
                    qdrant,
                    settings.qdrant_collection,
                    audit["orphan_fact_vectors"],
                    "delete_orphan_fact_vectors",
                    "orphan fact vector(s)",
                    log,
                )
            )
            executed.extend(
                await repair_facts_without_vectors(settings, audit["facts_without_vectors"], log)
            )
        finally:
            await qdrant.close()
    if dry_run:
        await qdrant.close()

    deferred: list[dict[str, Any]] = []
    log("Phase 4: Updating pending review queue...")
    if dry_run:
        log("  Skipped pending file and curation writes (dry run)")
    else:
        merged_pending, expired_count, duplicate_count = merge_pending_file(
            pending_file,
            deferred,
            now,
        )
        if expired_count:
            log(f"  Expired {expired_count} stale pending action(s)")
        if duplicate_count:
            log(f"  Deduped {duplicate_count} repeated pending action(s)")
        await mirror_review_candidates_to_curation(settings.db_path, merged_pending, log)
        log(f"  {len(merged_pending)} total pending action(s)")

    report_text = build_report(audit, executed, deferred, snap_path, fact_count, now)
    if dry_run:
        log("Phase 5: Skipped log/report write (dry run)")
    else:
        log_path.write_text("\n".join(log_lines) + "\n\n---\n\n" + report_text)
        log(f"Phase 5: Log written to {log_path}")

    total_issues = (
        len(audit["orphan_vectors"])
        + len(audit["malformed_points"])
        + len(audit["facts_without_vectors"])
        + len(audit["orphan_fact_vectors"])
        + len(audit["stale_fact_vectors"])
        + len(audit["empty_domains"])
    )
    summary = (
        f"{total_issues} issues | {len(executed)} fixed | "
        f"{len(deferred)} deferred | {len(errors)} errors"
    )
    log(f"=== Done - {summary}{mode_label} ===")
    if dry_run:
        return {"summary": summary, "audit": audit, "executed": executed, "errors": errors}
    if errors:
        notify(
            "Maintenance completed with errors",
            f"{summary}\n" + "\n".join(errors[:5]),
            "high",
            log,
        )
    elif not deferred:
        notify("Maintenance OK", f"{run_date.isoformat()}: {summary}", log=log)
    return {"summary": summary, "audit": audit, "executed": executed, "errors": errors}


def _positive_path(value: str) -> Path:
    return Path(value).expanduser()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Knowledge maintenance runner")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backup-root", type=_positive_path, default=Path("/mnt/backups"))
    parser.add_argument("--log-dir", type=_positive_path, default=PROJECT_ROOT / "logs")
    parser.add_argument(
        "--pending-file",
        type=_positive_path,
        default=PROJECT_ROOT / "data" / "pending_maintenance.json",
    )
    args = parser.parse_args(argv)
    try:
        asyncio.run(
            run_maintenance(
                KnowledgeSettings(),  # type: ignore[call-arg]
                backup_root=args.backup_root,
                log_dir=args.log_dir,
                pending_file=args.pending_file,
                dry_run=args.dry_run,
            )
        )
    except Exception:  # noqa: BLE001
        tb = traceback.format_exc()
        print(tb, flush=True)
        if not args.dry_run:
            notify("Maintenance CRASHED", tb[:400], "urgent")
        raise


if __name__ == "__main__":
    main()
