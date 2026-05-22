"""Curation queue logic for the Knowledge service.

Extracted from knowledge_server.py during Phase 3 modularization.
Contains curation actions, question packs, apply/reject logic.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from servers.knowledge.db import KnowledgeDB
from servers.knowledge.embeddings import BM25SparseEncoder, EmbeddingClient
from servers.knowledge.settings import KnowledgeSettings
from servers.knowledge.vectors import KnowledgeVectorStore
from servers.knowledge_source_files import resolve_source_path
from shared.logging_config import get_logger

log = get_logger("knowledge")


SUPPORTED_CURATION_ACTIONS = {
    "archive_domain",
    "delete_source",
    "domain_archive",
    "fact_delete",
    "fact_set",
    "fact_update_validity",
    "flag_for_review",
    "ingest_text",
    "no_action",
}

DESTRUCTIVE_CURATION_ACTIONS = {
    "archive_domain",
    "delete_source",
    "domain_archive",
    "fact_delete",
}


def validate_curation_actions(actions: list[dict[str, Any]]) -> str | None:
    """Return an error string when proposed curation actions are malformed."""
    if not actions:
        return "At least one proposed action is required"
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            return f"Action {index} must be an object"
        action_type = str(action.get("action") or action.get("type") or "").strip()
        if not action_type:
            return f"Action {index} is missing 'action' or 'type'"
        if action_type not in SUPPORTED_CURATION_ACTIONS:
            supported = ", ".join(sorted(SUPPORTED_CURATION_ACTIONS))
            return f"Unsupported curation action '{action_type}'. Supported: {supported}"
    return None


def _curation_title(kind: str, notes: str) -> str:
    cleaned = " ".join(notes.split())
    if cleaned:
        return cleaned[:77] + "..." if len(cleaned) > 80 else cleaned
    return kind.replace("_", " ").title()


async def create_curation_queue_item(
    *,
    db: KnowledgeDB,
    actions: list[dict[str, Any]],
    notes: str,
    kind: str = "uncertain_fact",
    title: str | None = None,
    source_refs: list[dict[str, Any]] | None = None,
    risk: str = "medium",
    confidence: float = 0.0,
    item_id: str | None = None,
    status: str = "pending",
    created_at: str | None = None,
) -> dict[str, Any]:
    error = validate_curation_actions(actions)
    if error:
        return {"success": False, "error": error}

    curation_id = await db.curation_upsert(
        kind=kind,
        title=title or _curation_title(kind, notes),
        summary=notes,
        source_refs=source_refs or [],
        proposed_actions=actions,
        risk=risk,
        confidence=confidence,
        item_id=item_id,
        status=status,
        created_at=created_at,
    )
    return {"success": True, "item_id": curation_id, "item": await db.curation_get(curation_id)}


def curation_item_has_destructive_actions(item: dict[str, Any]) -> bool:
    """Return True when a curation item proposes removing or archiving data."""
    for action in item.get("proposed_actions") or []:
        action_type = str(action.get("action") or action.get("type") or "")
        if action_type in DESTRUCTIVE_CURATION_ACTIONS:
            return True
    return False


CURATION_PACK_STATUSES = frozenset({"applied", "rejected", "snoozed"})
CURATION_PACK_ITEM_LIMIT = 200
CURATION_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


def _curation_action_type(action: dict[str, Any]) -> str:
    return str(action.get("action") or action.get("type") or "unknown").strip() or "unknown"


def _curation_item_action_types(item: dict[str, Any]) -> list[str]:
    actions = item.get("proposed_actions") or []
    return [_curation_action_type(action) for action in actions if isinstance(action, dict)]


def _curation_item_domain(item: dict[str, Any]) -> str | None:
    for action in item.get("proposed_actions") or []:
        if not isinstance(action, dict):
            continue
        domain = str(action.get("domain") or "").strip()
        if domain:
            return domain
        slug = str(action.get("slug") or "").strip()
        if "/" in slug:
            return slug.split("/", 1)[0]
    for ref in item.get("source_refs") or []:
        if isinstance(ref, dict):
            domain = str(ref.get("domain") or "").strip()
            if domain:
                return domain
    return None


def _curation_item_text(item: dict[str, Any]) -> str:
    return " ".join((
        str(item.get("kind") or ""),
        str(item.get("title") or ""),
        str(item.get("summary") or ""),
        json.dumps(item.get("source_refs") or [], sort_keys=True),
        json.dumps(item.get("proposed_actions") or [], sort_keys=True),
    )).lower()


def _curation_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "unknown"


def _curation_title_topic(item: dict[str, Any]) -> str:
    title = str(item.get("title") or "").strip()
    if ":" in title:
        title = title.split(":", 1)[1].strip()
    return _curation_slug(title)


def _curation_group_key(item: dict[str, Any]) -> tuple[str, str, str]:
    kind = str(item.get("kind") or "unknown")
    domain = _curation_item_domain(item) or "unknown"
    text = _curation_item_text(item)
    action = (_curation_item_action_types(item) or ["unknown"])[0]

    if any(token in text for token in ("andison", "coverage_", "shift_swap", "schedule_change")):
        return ("schedule_cleanup", "work_schedule", "coverage_exchange")
    if kind == "temporal_fact_cleanup":
        return ("temporal_fact_cleanup", domain, "validity_review")
    if kind == "maintenance_action":
        if action in {"archive_domain", "domain_archive"}:
            return ("maintenance_action", "domains", "archive_empty_domain")
        if action == "delete_source":
            return ("maintenance_action", "sources", "verified_duplicate_delete")
        if "missing-vector" in text or "missing vector" in text:
            return ("maintenance_action", "sources", "missing_vectors")
        return ("maintenance_action", domain, action)
    if kind in {"merge_candidate", "split_candidate"}:
        return ("wiki_identity", domain, _curation_title_topic(item))
    return (kind, domain, action)


def _curation_pack_id(group_key: tuple[str, str, str]) -> str:
    return "pack-" + "-".join(_curation_slug(part) for part in group_key)


def _curation_pack_prompt(group_key: tuple[str, str, str], count: int) -> tuple[str, str, str]:
    pack_kind, domain, topic = group_key
    if group_key == ("schedule_cleanup", "work_schedule", "coverage_exchange"):
        return (
            "Jack/Andison coverage exchange cleanup",
            "Should I treat these as one set of 2026 coverage-exchange events, "
            "resolve matching 2025 rows as extraction/OCR errors, and keep split "
            "shifts as one event with multiple segments?",
            "Normalize the schedule interpretation, preserve source evidence, "
            "and resolve the related review rows.",
        )
    if pack_kind == "temporal_fact_cleanup":
        return (
            f"{domain} temporal status review",
            f"Should I treat these {count} {domain} facts as current or historical "
            "unless they contain a real expiry/end date that should stop them being current?",
            "Only add valid_until for explicit expiry/end boundaries; otherwise resolve "
            "noisy temporal review rows without changing facts.",
        )
    if topic == "archive_empty_domain":
        return (
            "Empty domain archive suggestions",
            "Current policy says empty domains are allowed. Should I reject these "
            "empty-domain archive suggestions?",
            "Reject empty-domain archive suggestions unless Jack explicitly asks to archive them.",
        )
    if topic == "verified_duplicate_delete":
        return (
            "Verified duplicate source cleanup",
            "Should I verify these duplicate source records and only delete rows/files "
            "that are proven redundant?",
            "Keep this pack pending until duplicate verification succeeds.",
        )
    if topic == "missing_vectors":
        return (
            "Missing-vector source repair",
            "Should I keep these source/vector mismatches as repair work instead of "
            "deleting source history?",
            "Snooze or keep pending until a repair pass can reindex or explain the "
            "missing vectors.",
        )
    if pack_kind == "wiki_identity":
        topic_label = topic.replace("-", " ")
        return (
            f"{domain} wiki identity review: {topic_label}",
            f"Should I treat these {count} {domain} wiki identity concerns about "
            f"{topic_label} as audit notes unless you want page changes?",
            "Resolve weak speculative merge/split rows and keep useful context in "
            "wiki audit notes.",
        )
    return (
        f"{pack_kind.replace('_', ' ').title()}: {topic.replace('_', ' ')}",
        f"How should I resolve these {count} related {pack_kind.replace('_', ' ')} items?",
        "Resolve the grouped curation rows according to Jack's answer.",
    )


def _curation_pack_from_items(
    group_key: tuple[str, str, str],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    title, question, suggested = _curation_pack_prompt(group_key, len(items))
    action_counts = Counter(
        action
        for item in items
        for action in (_curation_item_action_types(item) or ["unknown"])
    )
    kind_counts = Counter(str(item.get("kind") or "unknown") for item in items)
    risks = [str(item.get("risk") or "medium") for item in items]
    risk = max(risks, key=lambda value: CURATION_RISK_ORDER.get(value, 1), default="medium")
    destructive_ids = [
        str(item["id"]) for item in items
        if curation_item_has_destructive_actions(item)
    ]
    return {
        "id": _curation_pack_id(group_key),
        "title": title,
        "question": question,
        "kind": group_key[0],
        "domain": group_key[1],
        "topic": group_key[2],
        "risk": risk,
        "count": len(items),
        "affected_item_ids": [str(item["id"]) for item in items],
        "suggested_resolution": suggested,
        "requires_confirmation": bool(destructive_ids),
        "destructive_item_ids": destructive_ids,
        "action_counts": dict(sorted(action_counts.items())),
        "kind_counts": dict(sorted(kind_counts.items())),
        "sample_items": [
            {
                "id": item["id"],
                "kind": item["kind"],
                "title": item["title"],
                "summary": item.get("summary", "")[:240],
                "actions": _curation_item_action_types(item),
            }
            for item in items[:5]
        ],
    }


async def build_curation_question_packs(
    db: KnowledgeDB,
    *,
    limit: int = 10,
    kind: str | None = None,
    domain: str | None = None,
) -> list[dict[str, Any]]:
    """Group pending curation rows into chat-friendly question packs."""
    rows = await db.curation_list(status="pending", kind=kind, limit=CURATION_PACK_ITEM_LIMIT)
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in rows:
        item_domain = _curation_item_domain(item)
        group_key = _curation_group_key(item)
        if domain and item_domain != domain and group_key[1] != domain:
            continue
        groups.setdefault(group_key, []).append(item)

    packs = [_curation_pack_from_items(group_key, items) for group_key, items in groups.items()]
    packs.sort(key=lambda pack: (-pack["count"], pack["risk"], pack["title"]))
    return packs[:max(1, min(limit, 50))]


async def get_curation_question_pack(
    db: KnowledgeDB,
    pack_id: str,
) -> dict[str, Any] | None:
    for pack in await build_curation_question_packs(db, limit=50):
        if pack["id"] == pack_id:
            return pack
    return None


def _validate_curation_pack_resolution_status(status: str) -> str | None:
    if status not in CURATION_PACK_STATUSES:
        return f"resolution_status must be one of {', '.join(sorted(CURATION_PACK_STATUSES))}"
    return None


async def preview_curation_pack_resolution(
    db: KnowledgeDB,
    *,
    pack_id: str,
    answer: str,
    resolution_status: str = "applied",
) -> dict[str, Any]:
    clean_answer = " ".join(str(answer or "").split())
    if not clean_answer:
        return {"success": False, "error": "answer is required"}
    if error := _validate_curation_pack_resolution_status(resolution_status):
        return {"success": False, "error": error}
    pack = await get_curation_question_pack(db, pack_id)
    if not pack:
        return {"success": False, "error": f"Curation question pack '{pack_id}' not found"}

    blocked = pack["destructive_item_ids"] if resolution_status == "applied" else []
    status_updates = [
        {"item_id": item_id, "from": "pending", "to": resolution_status}
        for item_id in pack["affected_item_ids"]
    ]
    note_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"curation-resolution:{pack_id}:{clean_answer}"))
    return {
        "success": True,
        "pack": pack,
        "answer": clean_answer,
        "resolution_status": resolution_status,
        "requires_confirmation": pack_id if blocked else None,
        "blocked_destructive_item_ids": blocked,
        "status_updates": status_updates,
        "data_writes": [],
        "resolution_note": {
            "item_id": note_id,
            "kind": "curation_resolution",
            "title": f"Resolved curation pack: {pack['title']}",
            "summary": (
                f"Question: {pack['question']}\n"
                f"Answer: {clean_answer}\n"
                f"Resolution status for affected rows: {resolution_status}"
            ),
            "source_refs": [
                {"type": "curation_item", "id": item_id}
                for item_id in pack["affected_item_ids"]
            ],
            "proposed_actions": [{
                "action": "no_action",
                "description": "Batch curation resolution note; no direct data mutation.",
            }],
        },
    }


async def apply_curation_pack_resolution(
    db: KnowledgeDB,
    *,
    pack_id: str,
    answer: str,
    resolution_status: str = "applied",
    confirmed: bool = False,
) -> dict[str, Any]:
    preview = await preview_curation_pack_resolution(
        db,
        pack_id=pack_id,
        answer=answer,
        resolution_status=resolution_status,
    )
    if not preview["success"]:
        return preview
    if preview["blocked_destructive_item_ids"]:
        if not confirmed:
            return {
                "success": False,
                "error": (
                    "Pack contains destructive actions; inspect and apply those items "
                    "individually after verification."
                ),
                "requires_confirmation": pack_id,
                "preview": preview,
            }
        return {
            "success": False,
            "error": (
                "Batch apply for destructive curation packs is not supported yet; "
                "use individual curation apply."
            ),
            "preview": preview,
        }

    updated = []
    for update in preview["status_updates"]:
        if await db.curation_mark_status(update["item_id"], update["to"]):
            updated.append(update["item_id"])

    note = preview["resolution_note"]
    note_id = await db.curation_upsert(
        kind=note["kind"],
        title=note["title"],
        summary=note["summary"],
        source_refs=note["source_refs"],
        proposed_actions=note["proposed_actions"],
        risk=preview["pack"]["risk"],
        confidence=1.0,
        item_id=note["item_id"],
        status="applied",
    )
    await db.curation_mark_status(note_id, "applied")
    return {
        "success": True,
        "pack_id": pack_id,
        "updated_item_ids": updated,
        "resolution_note_id": note_id,
        "resolution_status": resolution_status,
    }


async def _ingest_curation_text(
    *,
    settings: KnowledgeSettings,
    embeddings: EmbeddingClient,
    sparse_encoder: BM25SparseEncoder,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
    domain: str,
    content: str,
    source_name: str,
    source_type: str = "curated_note",
) -> dict[str, Any]:
    if not await db.domain_exists(domain):
        raise ValueError(f"Domain '{domain}' not found")

    chunks_text = chunk_text(content, settings.chunk_max_chars, settings.chunk_overlap)
    if not chunks_text:
        raise ValueError("No content to ingest")

    content_hash = compute_text_hash(content)
    if await db.source_exists(content_hash, domain=domain):
        return {
            "action": "ingest_text",
            "status": "skipped",
            "reason": "identical content already ingested",
        }

    sparse_encoder.fit_batch(chunks_text)
    sparse_vecs = [sparse_encoder.encode(t) for t in chunks_text]
    dense_vecs = await embeddings.embed_batch(chunks_text)

    source_id = str(uuid.uuid4())
    chunk_payloads = []
    for i, text in enumerate(chunks_text):
        chunk_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{source_id}_{i}"))
        chunk_payloads.append({
            "id": chunk_id,
            "domain": domain,
            "source_id": source_id,
            "source_type": source_type,
            "source_name": source_name,
            "chunk_index": i,
            "content": text,
            "ingested_at": datetime.now(UTC).isoformat(),
        })

    await vectors.upsert_chunks(chunk_payloads, dense_vecs, sparse_vecs)
    await db.source_add(source_id, domain, source_type, source_name, content_hash, len(chunks_text))
    return {
        "action": "ingest_text",
        "status": "applied",
        "domain": domain,
        "source_id": source_id,
        "source_name": source_name,
        "chunks": len(chunks_text),
    }


async def execute_curation_action(
    action: dict[str, Any],
    *,
    curation_item_id: str,
    settings: KnowledgeSettings,
    embeddings: EmbeddingClient,
    sparse_encoder: BM25SparseEncoder,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
) -> dict[str, Any]:
    """Apply one reviewed curation action to Knowledge storage."""
    action_type = str(action.get("action") or action.get("type") or "")

    if action_type == "fact_set":
        domain = str(action["domain"])
        key = str(action["key"])
        if not await db.domain_exists(domain):
            raise ValueError(f"Domain '{domain}' not found")
        fact_id = await db.fact_set(
            domain,
            key,
            str(action["value"]),
            action.get("source"),
            float(action.get("confidence", 1.0)),
            action.get("valid_from"),
            action.get("valid_until"),
            action.get("as_of"),
            action.get("review_after"),
            origin_type="curation",
            origin_ref=curation_item_id,
        )
        return {"action": action_type, "status": "applied", "fact_id": fact_id}

    if action_type == "fact_update_validity":
        domain = str(action["domain"])
        key = str(action["key"])
        fact = await db.fact_get(domain, key)
        if not fact:
            raise ValueError(f"Fact '{domain}/{key}' not found")
        await db.fact_set(
            domain,
            key,
            fact["value"],
            fact.get("source"),
            float(fact.get("confidence", 1.0)),
            action.get("valid_from", fact.get("valid_from")),
            action.get("valid_until", fact.get("valid_until")),
            action.get("as_of", fact.get("as_of")),
            action.get("review_after", fact.get("review_after")),
            origin_type="curation",
            origin_ref=curation_item_id,
        )
        return {"action": action_type, "status": "applied", "domain": domain, "key": key}

    if action_type == "fact_delete":
        domain = str(action["domain"])
        key = str(action["key"])
        deleted = await db.fact_delete(domain, key)
        if not deleted:
            raise ValueError(f"Fact '{domain}/{key}' not found")
        return {"action": action_type, "status": "applied", "domain": domain, "key": key}

    if action_type == "ingest_text":
        return await _ingest_curation_text(
            settings=settings,
            embeddings=embeddings,
            sparse_encoder=sparse_encoder,
            vectors=vectors,
            db=db,
            domain=str(action["domain"]),
            content=str(action["content"]),
            source_name=str(action.get("source_name") or "curated_conversation_note"),
            source_type=str(action.get("source_type") or "curated_note"),
        )

    if action_type == "delete_source":
        source_id = str(action.get("target_id") or action.get("source_id") or "")
        if not source_id:
            raise ValueError("delete_source action requires target_id or source_id")
        result = await delete_source_record(settings, vectors, db, source_id)
        if not result["success"]:
            raise ValueError(result["error"])
        return {
            "action": action_type,
            "status": "applied",
            "source_id": source_id,
            "source": result["source"],
        }

    if action_type in {"archive_domain", "domain_archive"}:
        domain = str(action.get("target_id") or action.get("domain") or "")
        if not domain:
            raise ValueError("archive_domain action requires target_id or domain")
        archived = await db.domain_archive(domain)
        if not archived:
            raise ValueError(f"Domain '{domain}' not found or already archived")
        return {"action": action_type, "status": "applied", "domain": domain}

    if action_type in {"flag_for_review", "no_action"}:
        return {"action": action_type, "status": "skipped"}

    raise ValueError(f"Unsupported curation action '{action_type}'")


async def apply_curation_item(
    item_id: str,
    *,
    confirmation: str | None,
    settings: KnowledgeSettings,
    embeddings: EmbeddingClient,
    sparse_encoder: BM25SparseEncoder,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
) -> dict[str, Any]:
    """Apply a queue item after review, enforcing destructive-action confirmation."""
    item = await db.curation_get(item_id)
    if not item:
        return {"success": False, "error": f"Curation item '{item_id}' not found"}
    if item["status"] != "pending":
        return {
            "success": False,
            "error": f"Curation item '{item_id}' is {item['status']}, not pending",
        }
    if curation_item_has_destructive_actions(item) and confirmation != item_id:
        return {
            "success": False,
            "error": "Destructive curation actions require confirmation equal to the item id",
            "requires_confirmation": item_id,
        }

    results = []
    try:
        for action in item.get("proposed_actions") or []:
            results.append(await execute_curation_action(
                action,
                curation_item_id=item_id,
                settings=settings,
                embeddings=embeddings,
                sparse_encoder=sparse_encoder,
                vectors=vectors,
                db=db,
            ))
    except Exception as exc:
        return {"success": False, "error": str(exc), "applied_before_error": results}

    await db.curation_mark_status(item_id, "applied")
    return {"success": True, "item_id": item_id, "results": results}


