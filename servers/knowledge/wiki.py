"""Wiki rebuild pipeline for the Knowledge service.

Extracted from knowledge_server.py during Phase 3 modularization.
Contains wiki page generation, LLM calls, index pages, and rebuild logic.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from servers.knowledge.db import KnowledgeDB, search_fact_keywords
from servers.knowledge.embeddings import BM25SparseEncoder, EmbeddingClient
from servers.knowledge.extraction import _decode_llm_json_object
from servers.knowledge.settings import KnowledgeSettings
from servers.knowledge.vectors import KnowledgeVectorStore
from shared.logging_config import get_logger

log = get_logger("knowledge")


WIKI_PAGE_STATUSES = frozenset({"candidate", "active", "archived"})
WIKI_PAGE_LIST_STATUSES = WIKI_PAGE_STATUSES | frozenset({"all"})
WIKI_PAGE_KINDS = frozenset({"entity", "concept", "source_summary", "index"})
WIKI_REBUILD_QUIET_WINDOW = timedelta(hours=1)
WIKI_REBUILD_STOPWORDS = frozenset({
    "a", "an", "and", "chat", "current", "doc", "document", "file", "for", "from",
    "latest", "log", "manual", "my", "note", "notes", "of", "pdf", "record", "records",
    "report", "source", "summary", "the", "upload",
})
WIKI_PHOTO_DETAIL_PREFIXES = frozenset({
    "bleacher", "clothing", "hair", "pants", "photo", "railing", "seating",
    "shirt", "shoe", "sky",
})
WIKI_PAGE_SYSTEM_PROMPT = (
    "You maintain concise personal knowledge wiki pages. Return only the forced "
    "tool call. Write traceable markdown from the supplied facts and chunks only. "
    "Do not invent missing details. Use Open Questions for gaps or conflicts. "
    "Every concrete claim should be covered by a source_id or chat_date citation. "
    "Flag duplicate or split concerns instead of resolving identity silently. "
    "Do not create standalone wiki pages for ordinary photo details such as clothing, "
    "hair, seating, sky, background objects, or other visible incidental objects; keep "
    "those details in source captions or a person/event page."
)


def _wiki_slug_for_change(domain: str, text: str) -> str:
    terms = [
        term for term in re.findall(r"[a-z0-9]+", text.lower())
        if term not in WIKI_REBUILD_STOPWORDS
    ]
    if not terms:
        return f"{domain}/index"
    tail = f"{terms[0]}-{terms[1]}" if len(terms) > 1 and terms[1].isdigit() else terms[0]
    return f"{domain}/{tail}"


def _wiki_title_from_slug(slug: str) -> str:
    return " ".join(part.upper() if part.isdigit() else part.title()
                    for part in slug.rsplit("/", 1)[-1].split("-"))


def _wiki_fact_can_seed_page(key: str) -> bool:
    return key.split("_", 1)[0].lower() not in WIKI_PHOTO_DETAIL_PREFIXES


def _wiki_row_matches_slug(slug: str, domain: str, text: str) -> bool:
    if not slug.startswith(f"{domain}/"):
        return False
    tail = slug.rsplit("/", 1)[-1]
    terms = re.findall(r"[a-z0-9]+", text.lower())
    return _wiki_slug_for_change(domain, text) == slug or tail.replace("-", " ") in " ".join(terms)


def _wiki_latency_class(pages: int, tokens: int) -> str:
    if pages <= 5 and tokens < 20_000:
        return "quick"
    if pages <= 20 and tokens < 80_000:
        return "medium"
    return "slow"


async def preview_wiki_rebuild(
    settings: KnowledgeSettings,
    db: KnowledgeDB,
    *,
    domain: str | None = None,
    entity_slug: str | None = None,
    force_full: bool = False,
) -> dict[str, Any]:
    clean_domain = domain.strip() if domain else None
    clean_entity = entity_slug.strip() if entity_slug else None
    if clean_entity:
        if "/" not in clean_entity:
            return {"success": False, "error": "entity_slug must use '<domain>/<slug>'"}
        entity_domain = clean_entity.split("/", 1)[0]
        if clean_domain and clean_domain != entity_domain:
            return {"success": False, "error": "domain must match entity_slug domain"}
        clean_domain = entity_domain

    since = "1970-01-01T00:00:00Z" if force_full else await db.wiki_state_get(
        "last_wiki_run", "1970-01-01T00:00:00Z"
    )
    quiet_after = (datetime.now(UTC) - WIKI_REBUILD_QUIET_WINDOW).isoformat()
    inputs = await db.wiki_rebuild_inputs(
        since=since or "1970-01-01T00:00:00Z",
        domain=clean_domain,
        force_full=force_full,
        quiet_after=quiet_after,
    )
    entities: dict[str, dict[str, Any]] = {}

    def ensure_entity(slug: str, item_domain: str) -> dict[str, Any]:
        return entities.setdefault(slug, {
            "slug": slug,
            "domain": item_domain,
            "title": _wiki_title_from_slug(slug),
            "fact_count": 0,
            "source_count": 0,
            "fact_keys": [],
            "source_ids": [],
        })

    for fact in inputs["facts"]:
        item_domain = str(fact["domain"])
        text = str(fact["key"])
        if not _wiki_fact_can_seed_page(text):
            continue
        if clean_entity and not _wiki_row_matches_slug(clean_entity, item_domain, text):
            continue
        slug = clean_entity or _wiki_slug_for_change(item_domain, text)
        entity = ensure_entity(slug, item_domain)
        entity["fact_count"] += 1
        entity["fact_keys"].append(text)

    for source in inputs["sources"]:
        item_domain = str(source["domain"])
        text = str(source.get("filename") or source["id"])
        if clean_entity and not _wiki_row_matches_slug(clean_entity, item_domain, text):
            continue
        slug = clean_entity or _wiki_slug_for_change(item_domain, text)
        entity = ensure_entity(slug, item_domain)
        entity["source_count"] += 1
        entity["source_ids"].append(source["id"])

    if clean_entity:
        page = await db.wiki_get(clean_entity)
        entity = ensure_entity(clean_entity, clean_domain or clean_entity.split("/", 1)[0])
        if page:
            entity["title"] = page["title"]
    elif force_full:
        for page in await db.wiki_list(domain=clean_domain, status="all", limit=200):
            if page["kind"] != "index":
                ensure_entity(str(page["slug"]), str(page["domain"]))["title"] = page["title"]

    changed_entities = sorted(entities.values(), key=lambda item: item["slug"])
    entity_pages = len(changed_entities)
    index_pages = len({item["domain"] for item in changed_entities}) if entity_pages else 0
    token_estimate = sum(
        1_200 + item["fact_count"] * 180 + item["source_count"] * 650
        for item in changed_entities
    ) + index_pages * 500
    estimated_pages = entity_pages + index_pages
    return {
        "success": True,
        "dry_run": True,
        "writes_performed": False,
        "scope": {
            "domain": clean_domain,
            "entity_slug": clean_entity,
            "force_full": force_full,
            "since": since,
            "quiet_window_hours": WIKI_REBUILD_QUIET_WINDOW.total_seconds() / 3600,
        },
        "changed_entities": changed_entities,
        "estimated_entity_pages": entity_pages,
        "estimated_index_pages": index_pages,
        "estimated_pages": estimated_pages,
        "token_estimate": token_estimate,
        "estimated_cost": {
            "currency": "USD",
            "low": None,
            "high": None,
            "note": "model pricing is not configured; use token_estimate for cost planning",
        },
        "latency_class": _wiki_latency_class(estimated_pages, token_estimate),
        "model": settings.extraction_model,
    }


def _wiki_slug_terms(slug: str) -> list[str]:
    tail = slug.rsplit("/", 1)[-1]
    return [
        term for term in re.findall(r"[a-z0-9]+", tail.lower())
        if term not in WIKI_REBUILD_STOPWORDS
    ]


def _wiki_iso_date(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else None


def _wiki_fact_source_id(fact: dict[str, Any]) -> str | None:
    origin_ref = str(fact.get("origin_ref") or "").strip()
    if origin_ref and not _wiki_iso_date(origin_ref):
        return origin_ref
    source = str(fact.get("source") or "")
    return source.split(":", 1)[1].strip() if source.startswith("extracted:") else None


def _wiki_source_rows(
    facts: list[dict[str, Any]],
    sources: dict[str, dict[str, Any]],
    chunks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}

    def add(
        *,
        source_id: str | None = None,
        chat_date: str | None = None,
        contribution: str = "cited evidence",
    ) -> None:
        key = (source_id or "", chat_date or "")
        if any(key) and key not in rows:
            rows[key] = {
                "source_id": source_id,
                "chat_date": chat_date,
                "contribution": contribution[:200],
            }

    for fact in facts:
        contribution = f"fact: {fact.get('key')}"
        if fact.get("origin_type") == "chat" and (
            chat_date := _wiki_iso_date(fact.get("origin_ref"))
        ):
            add(chat_date=chat_date, contribution=contribution)
        elif (source_id := _wiki_fact_source_id(fact)) and source_id in sources:
            add(source_id=source_id, contribution=contribution)

    for source_id, source in sources.items():
        add(
            source_id=source_id,
            contribution=str(source.get("filename") or source.get("source_type") or "source"),
        )

    for chunk in chunks:
        source_id = str(chunk.get("source_id") or "").strip()
        if source_id:
            add(
                source_id=source_id,
                contribution=str(chunk.get("source_name") or "matched source chunk"),
            )
    return list(rows.values())


def _wiki_merge_source_rows(
    rows: list[dict[str, Any]],
    *,
    allowed_source_ids: set[str],
    allowed_chat_dates: set[str],
) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        source_id = str(row.get("source_id") or "").strip() or None
        chat_date = _wiki_iso_date(row.get("chat_date"))
        if source_id and source_id not in allowed_source_ids:
            continue
        if chat_date and chat_date not in allowed_chat_dates:
            continue
        key = (source_id or "", chat_date or "")
        if any(key) and key not in merged:
            merged[key] = {
                "source_id": source_id,
                "chat_date": chat_date,
                "contribution": str(row.get("contribution") or "cited evidence").strip()[:200],
            }
    return list(merged.values())


def _wiki_citation_lines(
    rows: list[dict[str, Any]], sources: dict[str, dict[str, Any]]
) -> list[str]:
    lines = []
    for row in rows:
        if row.get("source_id"):
            source_id = str(row["source_id"])
            title = sources.get(source_id, {}).get("filename")
            label = f"Source: {source_id}" + (f" - {title}" if title else "")
        else:
            label = f"Chat: {row.get('chat_date')}"
        contribution = str(row.get("contribution") or "").strip()
        lines.append(label + (f" ({contribution})" if contribution else ""))
    return lines


def _wiki_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [str(value)] if value else []


def _wiki_generated_page(
    raw_page: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    slug = str(context["slug"])
    domain = str(context["domain"])
    existing = context.get("existing_page") or {}
    title = str(raw_page.get("title") or context["title"]).strip()[:200]
    kind = str(raw_page.get("kind") or "entity").strip()
    kind = kind if kind in WIKI_PAGE_KINDS - {"index"} else "entity"
    raw_frontmatter = raw_page.get("frontmatter")
    frontmatter = dict(raw_frontmatter) if isinstance(raw_frontmatter, dict) else {}
    confidence = str(
        raw_page.get("confidence") or frontmatter.get("confidence") or "medium"
    ).lower()
    confidence = confidence if confidence in {"high", "medium", "low"} else "medium"

    default_rows = _wiki_source_rows(context["facts"], context["sources"], context["chunks"])
    llm_rows = raw_page.get("sources") if isinstance(raw_page.get("sources"), list) else []
    source_rows = _wiki_merge_source_rows(
        [*llm_rows, *default_rows],
        allowed_source_ids=set(context["sources"]) | {
            str(c.get("source_id")) for c in context["chunks"] if c.get("source_id")
        },
        allowed_chat_dates={
            date for f in context["facts"] if (date := _wiki_iso_date(f.get("origin_ref")))
        },
    )
    source_ids = sorted({str(row["source_id"]) for row in source_rows if row.get("source_id")})
    chat_dates = sorted({str(row["chat_date"]) for row in source_rows if row.get("chat_date")})

    body = str(raw_page.get("body_md") or "").strip()
    if not body:
        body = f"{title} is tracked as a {domain} wiki page."
    if source_rows and "## Sources" not in body:
        body += "\n\n## Sources\n" + "\n".join(
            f"- {line}" for line in _wiki_citation_lines(source_rows, context["sources"])
        )

    concerns = {
        "merge_candidate": _wiki_str_list(raw_page.get("duplicate_concerns")),
        "split_candidate": _wiki_str_list(raw_page.get("split_concerns")),
    }
    audit_notes = {kind: values for kind, values in concerns.items() if values}

    frontmatter.update({
        "schema_version": 1,
        "slug": slug,
        "title": title,
        "kind": kind,
        "domain": domain,
        "entity_type": str(frontmatter.get("entity_type") or "unknown"),
        "aliases": _wiki_str_list(frontmatter.get("aliases")),
        "related_slugs": _wiki_str_list(frontmatter.get("related_slugs")),
        "source_ids": source_ids,
        "chat_dates": chat_dates,
        "confidence": confidence,
        "orphan": bool(frontmatter.get("orphan", False)),
        "audit_notes": audit_notes,
    })

    status = existing.get("status")
    if status not in WIKI_PAGE_STATUSES:
        status = (
            "active"
            if confidence == "high" and source_rows and not any(concerns.values())
            else "candidate"
        )
    return {
        "slug": slug,
        "domain": domain,
        "title": title,
        "kind": kind,
        "status": status,
        "body_md": body,
        "frontmatter": frontmatter,
        "sources": source_rows,
        "fact_count": len(context["facts"]),
        "concerns": concerns,
    }


async def _wiki_context(
    embeddings: EmbeddingClient,
    sparse_encoder: BM25SparseEncoder,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
    entity: dict[str, Any],
) -> dict[str, Any]:
    slug = str(entity["slug"])
    domain = str(entity["domain"])
    title = str(entity.get("title") or _wiki_title_from_slug(slug))
    fact_keys = set(entity.get("fact_keys") or [])
    terms = _wiki_slug_terms(slug)
    facts = [
        fact for fact in await db.facts_list(domain)
        if fact.get("key") in fact_keys
        or any(term in f"{fact.get('key', '')} {fact.get('value', '')}".lower() for term in terms)
    ]

    chunks: list[dict[str, Any]] = []
    for source_id in entity.get("source_ids") or []:
        with contextlib.suppress(Exception):
            chunks.extend(await vectors.chunks_by_source(str(source_id), limit=4))

    with contextlib.suppress(Exception):
        query = " ".join([title, *terms])
        query_embedding = await embeddings.embed(query)
        sparse_query = sparse_encoder.encode_query(query)
        for point in await vectors.search(
            query_embedding,
            sparse_query=sparse_query,
            domains=[domain],
            limit=8,
            min_score=0.15,
        ):
            chunks.append(dict(point.payload or {}))

    unique_chunks: dict[tuple[str, int, str], dict[str, Any]] = {}
    for chunk in chunks:
        key = (
            str(chunk.get("source_id") or ""),
            int(chunk.get("chunk_index") or 0),
            str(chunk.get("id") or ""),
        )
        if key not in unique_chunks:
            copy = dict(chunk)
            copy["content"] = str(copy.get("content") or "")[:1400]
            unique_chunks[key] = copy

    source_ids = set(entity.get("source_ids") or [])
    source_ids.update(str(c.get("source_id")) for c in unique_chunks.values() if c.get("source_id"))
    source_ids.update(sid for f in facts if (sid := _wiki_fact_source_id(f)))
    sources = {}
    for source_id in sorted(str(s) for s in source_ids if s):
        source = await db.source_get(source_id)
        if source:
            sources[source_id] = source

    return {
        "slug": slug,
        "domain": domain,
        "title": title,
        "facts": facts,
        "chunks": list(unique_chunks.values())[:12],
        "sources": sources,
        "existing_page": await db.wiki_get(slug),
    }


async def _call_wiki_llm(
    settings: KnowledgeSettings,
    context: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    if not settings.extraction_model:
        raise RuntimeError("KNOWLEDGE_EXTRACTION_MODEL not configured")
    tool = {
        "type": "function",
        "function": {
            "name": "write_wiki_page",
            "description": "Return one generated wiki page with citations and concerns.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "kind": {"type": "string", "enum": ["entity", "concept", "source_summary"]},
                    "body_md": {"type": "string"},
                    "frontmatter": {"type": "object"},
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source_id": {"type": ["string", "null"]},
                                "chat_date": {"type": ["string", "null"]},
                                "contribution": {"type": "string"},
                            },
                        },
                    },
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "duplicate_concerns": {"type": "array", "items": {"type": "string"}},
                    "split_concerns": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "kind", "body_md", "frontmatter", "sources", "confidence"],
            },
        },
    }
    payload: dict[str, Any] = {
        "model": settings.extraction_model,
        "messages": [{
            "role": "user",
            "content": json.dumps({
                "slug": context["slug"],
                "domain": context["domain"],
                "title": context["title"],
                "facts": context["facts"],
                "sources": list(context["sources"].values()),
                "chunks": context["chunks"],
                "existing_page": context["existing_page"],
            }, default=str),
        }],
        "temperature": 0,
        "max_tokens": 4096,
        "tools": [tool],
        "tool_choice": {"type": "function", "function": {"name": "write_wiki_page"}},
    }
    if "claude" in settings.extraction_model or "anthropic" in settings.extraction_model:
        payload["system"] = [{
            "type": "text",
            "text": WIKI_PAGE_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }]
    else:
        payload["system"] = WIKI_PAGE_SYSTEM_PROMPT

    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
    msg = data["choices"][0]["message"]
    tool_calls = msg.get("tool_calls") or []
    raw_output = (
        tool_calls[0]["function"]["arguments"]
        if tool_calls else (msg.get("content") or "").strip()
    )
    usage = data.get("usage") or {}
    return _decode_llm_json_object(raw_output), int(usage.get("total_tokens") or 0)


async def _rebuild_wiki_index(
    db: KnowledgeDB,
    domain: str,
    *,
    vectors: KnowledgeVectorStore | None = None,
    embeddings: Any | None = None,
    sparse_encoder: Any | None = None,
) -> str:
    pages = [
        page for page in await db.wiki_list(domain=domain, status="active", limit=200)
        if page["kind"] != "index"
    ]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for page in pages:
        grouped.setdefault(str(page["kind"]), []).append(page)
    lines = [f"# {domain.replace('_', ' ').title()} Index"]
    for kind, items in sorted(grouped.items()):
        lines.extend(["", f"## {kind.replace('_', ' ').title()}"])
        lines.extend(f"- `{item['slug']}` - {item['title']}" for item in items)
    slug = f"{domain}/index"
    title = f"{domain.replace('_', ' ').title()} Index"
    status = "active" if pages else "candidate"
    body_md = "\n".join(lines)
    await db.wiki_upsert_page(
        slug=slug,
        domain=domain,
        title=title,
        kind="index",
        status=status,
        body_md=body_md,
        frontmatter={
            "schema_version": 1,
            "slug": slug,
            "title": title,
            "kind": "index",
            "domain": domain,
            "entity_type": "index",
            "aliases": [],
            "related_slugs": [str(page["slug"]) for page in pages],
            "source_ids": [],
            "chat_dates": [],
            "confidence": "high" if pages else "medium",
            "orphan": False,
        },
        sources=[],
        fact_count=0,
    )
    # Embed active index pages into Qdrant for semantic search.
    if status == "active" and vectors and embeddings and sparse_encoder:
        await vectors.embed_wiki_page(
            slug=slug,
            domain=domain,
            title=title,
            body_md=body_md,
            embeddings=embeddings,
            sparse_encoder=sparse_encoder,
        )
    elif status != "active" and vectors:
        await vectors.delete_by_source(slug)
    return slug


async def rebuild_wiki(
    settings: KnowledgeSettings,
    embeddings: EmbeddingClient,
    sparse_encoder: BM25SparseEncoder,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
    *,
    domain: str | None = None,
    entity_slug: str | None = None,
    force_full: bool = False,
) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    preview = await preview_wiki_rebuild(
        settings, db, domain=domain, entity_slug=entity_slug, force_full=force_full,
    )
    if not preview["success"]:
        return preview

    run_id = await db.wiki_rebuild_run_start(
        scope=preview["scope"],
        token_estimate=preview["token_estimate"],
        model=preview["model"],
    )
    touched: list[str] = []
    usage_tokens = 0
    try:
        for entity in preview["changed_entities"]:
            context = await _wiki_context(embeddings, sparse_encoder, vectors, db, entity)
            raw_page, tokens = await _call_wiki_llm(settings, context)
            usage_tokens += tokens
            page = _wiki_generated_page(raw_page, context)
            await db.wiki_upsert_page(
                slug=page["slug"],
                domain=page["domain"],
                title=page["title"],
                kind=page["kind"],
                status=page["status"],
                body_md=page["body_md"],
                frontmatter=page["frontmatter"],
                sources=page["sources"],
                fact_count=page["fact_count"],
            )
            # Embed wiki page into Qdrant for semantic search.
            if page["status"] == "active":
                await vectors.embed_wiki_page(
                    slug=page["slug"],
                    domain=page["domain"],
                    title=page["title"],
                    body_md=page["body_md"],
                    embeddings=embeddings,
                    sparse_encoder=sparse_encoder,
                )
            else:
                # Non-active pages should not appear in vector search.
                await vectors.delete_by_source(page["slug"])
            touched.append(page["slug"])

            # --- Auto-create curation items for pages with concerns ---
            concerns = page.get("concerns") or {}
            for concern_type, items in concerns.items():
                if not items:
                    continue
                curation_kind = {
                    "merge_candidate": "wiki_merge",
                    "split_candidate": "wiki_split",
                }.get(concern_type, f"wiki_{concern_type}")
                summary_lines = items if isinstance(items, list) else [str(items)]
                await db.curation_upsert(
                    kind=curation_kind,
                    title=f"{concern_type.replace('_', ' ').title()}: {page['title']} ({page['slug']})",
                    summary="\n".join(summary_lines),
                    source_refs=[{"type": "wiki_page", "slug": page["slug"]}],
                    proposed_actions=[{
                        "action": concern_type,
                        "slug": page["slug"],
                        "detail": summary_lines[0] if summary_lines else "",
                    }],
                    risk="low",
                    confidence=0.8,
                    item_id=f"wiki:{concern_type}:{page['slug']}",
                )

        for touched_domain in sorted({str(item["domain"]) for item in preview["changed_entities"]}):
            touched.append(await _rebuild_wiki_index(
                db, touched_domain,
                vectors=vectors, embeddings=embeddings, sparse_encoder=sparse_encoder,
            ))

        await db.wiki_state_set("last_wiki_run", started_at)
        final_tokens = usage_tokens or preview["token_estimate"]
        await db.wiki_rebuild_run_finish(
            run_id, status="success", touched_slugs=touched, token_estimate=final_tokens,
        )
        log.info(
            "wiki_rebuild_success run_id=%s pages=%s tokens=%s touched=%s",
            run_id, len(touched), final_tokens, touched,
        )
        return {
            "success": True,
            "dry_run": False,
            "writes_performed": True,
            "run_id": run_id,
            "scope": preview["scope"],
            "changed_entities": preview["changed_entities"],
            "pages_touched": len(touched),
            "touched_slugs": touched,
            "token_estimate": final_tokens,
            "model": preview["model"],
        }
    except Exception as exc:  # noqa: BLE001
        final_tokens = usage_tokens or preview["token_estimate"]
        await db.wiki_rebuild_run_finish(
            run_id,
            status="failed",
            touched_slugs=touched,
            token_estimate=final_tokens,
            error_summary=str(exc)[:500],
        )
        log.exception("wiki_rebuild_failed run_id=%s touched=%s", run_id, touched)
        return {
            "success": False,
            "error": f"wiki rebuild failed: {exc}",
            "run_id": run_id,
            "touched_slugs": touched,
        }


async def wiki_lint_pass(db: KnowledgeDB) -> dict[str, Any]:
    """Post-rebuild lint: create curation items for expired facts and orphan pages.

    Runs after wiki rebuild. Detects:
    1. Expired facts (valid_until < now) that should be archived
    2. Orphan wiki pages with no inbound related_slugs from other pages
    3. Candidate pages stuck with concerns (merge/split) for >7 days
    """
    items_created = 0
    now = datetime.now(UTC)

    # --- 1. Expired facts ---
    try:
        all_domains = await db.domain_list()
        for domain_row in all_domains:
            if domain_row.get("archived"):
                continue
            domain = str(domain_row["name"])
            facts = await db.fact_list(domain)
            for fact in facts:
                valid_until = fact.get("valid_until")
                if not valid_until:
                    continue
                try:
                    expiry = datetime.fromisoformat(str(valid_until))
                    if expiry.tzinfo is None:
                        expiry = expiry.replace(tzinfo=UTC)
                    if expiry < now:
                        await db.curation_upsert(
                            kind="expired_fact",
                            title=f"Expired fact: {domain}/{fact['key']}",
                            summary=(
                                f"Fact '{fact['key']}' in domain '{domain}' "
                                f"expired on {valid_until}. "
                                f"Current value: {fact.get('value', '')[:200]}"
                            ),
                            source_refs=[{"type": "fact", "domain": domain, "key": fact["key"]}],
                            proposed_actions=[{
                                "action": "archive_or_update",
                                "domain": domain,
                                "key": fact["key"],
                            }],
                            risk="low",
                            confidence=0.95,
                            item_id=f"lint:expired:{domain}/{fact['key']}",
                        )
                        items_created += 1
                except (ValueError, TypeError):
                    pass
    except Exception:
        log.warning("lint_expired_facts_failed", exc_info=True)

    # --- 2. Orphan wiki pages (no inbound related_slugs) ---
    try:
        all_pages = await db.wiki_list(status="active", limit=200)
        # Build set of all slugs referenced by related_slugs
        all_related: set[str] = set()
        slug_to_title: dict[str, str] = {}
        for page in all_pages:
            slug = str(page.get("slug", ""))
            slug_to_title[slug] = str(page.get("title", slug))
            # wiki_list doesn't return frontmatter, so fetch it
            full = await db.wiki_get(slug)
            if full:
                fm = full.get("frontmatter") or {}
                for related in fm.get("related_slugs", []):
                    all_related.add(str(related))

        for page in all_pages:
            slug = str(page.get("slug", ""))
            if page.get("kind") == "index":
                continue
            # Orphan = not referenced by any other page's related_slugs
            if slug not in all_related:
                await db.curation_upsert(
                    kind="orphan_page",
                    title=f"Orphan wiki page: {slug_to_title.get(slug, slug)}",
                    summary=(
                        f"Wiki page '{slug}' has no inbound links from "
                        f"other pages' related_slugs. Consider adding "
                        f"cross-references or archiving if stale."
                    ),
                    source_refs=[{"type": "wiki_page", "slug": slug}],
                    proposed_actions=[{
                        "action": "add_cross_references_or_archive",
                        "slug": slug,
                    }],
                    risk="low",
                    confidence=0.7,
                    item_id=f"lint:orphan:{slug}",
                )
                items_created += 1
    except Exception:
        log.warning("lint_orphan_pages_failed", exc_info=True)

    # --- 3. Stale candidates with concerns ---
    try:
        stale_cutoff = (now - timedelta(days=7)).isoformat()
        candidate_pages = await db.wiki_list(status="candidate", limit=200)
        for page in candidate_pages:
            updated = str(page.get("updated_at") or page.get("created_at") or "")
            if updated and updated < stale_cutoff:
                full = await db.wiki_get(str(page["slug"]))
                if not full:
                    continue
                fm = full.get("frontmatter") or {}
                audit_notes = fm.get("audit_notes") or {}
                if audit_notes:
                    concern_summary = json.dumps(audit_notes, indent=2)[:500]
                    await db.curation_upsert(
                        kind="stale_candidate",
                        title=f"Stale candidate: {page.get('title', page['slug'])}",
                        summary=(
                            f"Wiki page '{page['slug']}' has been a candidate "
                            f"for >7 days with unresolved concerns:\n{concern_summary}"
                        ),
                        source_refs=[{"type": "wiki_page", "slug": str(page["slug"])}],
                        proposed_actions=[{
                            "action": "review_and_promote_or_archive",
                            "slug": str(page["slug"]),
                            "concerns": audit_notes,
                        }],
                        risk="low",
                        confidence=0.6,
                        item_id=f"lint:stale:{page['slug']}",
                    )
                    items_created += 1
    except Exception:
        log.warning("lint_stale_candidates_failed", exc_info=True)

    log.info("wiki_lint_pass_complete items_created=%s", items_created)
    return {"items_created": items_created}
