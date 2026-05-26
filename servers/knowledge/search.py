"""Search logic for the Knowledge service.

Extracted from knowledge_server.py during Phase 3 modularization.
Contains resolve_search_domains, search_knowledge, and helpers.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from servers.knowledge.db import KnowledgeDB, search_fact_keywords
from servers.knowledge.embeddings import BM25SparseEncoder, EmbeddingClient
from servers.knowledge.temporal import (
    fact_temporal_counts,
    fact_temporal_status,
)
from servers.knowledge.vectors import KnowledgeVectorStore
from shared.logging_config import get_logger
from shared.time_context import EASTERN_TIMEZONE

log = get_logger("knowledge")


async def resolve_search_domains(
    db: KnowledgeDB,
    domain: str | None,
    domains: list[str] | None,
    *,
    include_archived: bool = False,
) -> list[str]:
    """Resolve a domain query to a list of domains including related ones.

    If a single domain is given, automatically includes its related domains.
    The 'core' domain is always included when it exists.
    """
    if domains:
        result = []
        for item in domains:
            clean = str(item).strip()
            if clean and clean not in result:
                result.append(clean)
    elif domain:
        clean_domain = str(domain).strip()
        result = [clean_domain] if clean_domain else []
        domain_info = await db.domain_get(clean_domain) if clean_domain else None
        if domain_info and domain_info["related_domains"]:
            for related in domain_info["related_domains"]:
                if related not in result:
                    result.append(related)
    else:
        # All non-archived domains unless a historical search asks for archives too.
        all_domains = await db.domain_list()
        result = [d["name"] for d in all_domains if include_archived or not d["archived"]]

    # Always include core if it exists and isn't already there
    if "core" not in result and await db.domain_exists("core"):
        result.append("core")

    return result


FACT_QUERY_HINTS = frozenset({
    "account", "address", "birthday", "date", "dentist", "doctor", "dose", "email",
    "id", "label", "med", "medication", "number", "phone", "preference", "rate",
})
EVIDENCE_QUERY_HINTS = frozenset({
    "citation", "cite", "conflict", "contradict", "disagree", "document", "evidence",
    "original", "pdf", "proof", "source", "stale", "upload",
})
SEARCH_TEMPORAL_INTENTS = frozenset({"all", "current_upcoming", "historical"})
TEMPORAL_TOPIC_HINTS = frozenset({
    "appointment", "appointments", "bill", "bills", "calendar", "contract", "course",
    "deadline", "deadlines", "event", "events", "flight", "flights", "medication",
    "medications", "meds", "meeting", "meetings", "plan", "plans", "pto", "renewal",
    "schedule", "scheduled", "shift", "shifts", "subscription", "subscriptions",
    "task", "tasks", "travel", "trip", "vacation",
})


def _normalize_search_temporal_intent(value: str | None) -> str | None:
    clean = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "": None,
        "auto": None,
        "archive": "historical",
        "archived": "historical",
        "current": "current_upcoming",
        "deep": "historical",
        "future": "current_upcoming",
        "history": "historical",
        "past": "historical",
        "upcoming": "current_upcoming",
    }
    return aliases.get(clean, clean if clean in SEARCH_TEMPORAL_INTENTS else None)


def _relative_year_terms(query: str, now: datetime) -> list[str]:
    lowered = query.lower()
    terms: list[str] = []
    if re.search(r"\blast\s+year\b", lowered):
        terms.append(str(now.year - 1))
    if re.search(r"\bthis\s+year\b", lowered):
        terms.append(str(now.year))
    if re.search(r"\bnext\s+year\b", lowered):
        terms.append(str(now.year + 1))
    return terms


def _explicit_past_year(query: str, now: datetime) -> bool:
    return any(
        int(year) < now.year
        for year in re.findall(r"\b(?:19|20)\d{2}\b", query)
    )


def classify_search_temporal_intent(
    query: str,
    *,
    now: datetime | None = None,
    override: str | None = None,
) -> str:
    """Infer whether retrieval should prefer current/upcoming, history, or all."""
    explicit = _normalize_search_temporal_intent(override)
    if explicit:
        return explicit

    lowered = query.lower()
    now = now or datetime.now(EASTERN_TIMEZONE)
    if (
        re.search(r"\b(when|where|how|what)\s+did\b", lowered)
        or re.search(
            r"\b(ago|archived?|completed|ended|expired|former|formerly|history|"
            r"historical|past|previous|prior|used|yesterday)\b",
            lowered,
        )
        or re.search(r"\blast\s+(year|month|week|night|time|quarter)\b", lowered)
        or _explicit_past_year(query, now)
    ):
        return "historical"

    if re.search(
        r"\b(active|currently|do i have|future|next|now|scheduled|today|tomorrow|"
        r"upcoming|when do)\b",
        lowered,
    ):
        return "current_upcoming"

    if set(search_fact_keywords(query)) & TEMPORAL_TOPIC_HINTS:
        return "current_upcoming"
    return "all"


def expand_search_query(query: str, temporal_intent: str, now: datetime) -> list[str]:
    """Return the original query plus the compact temporal expansion used for retrieval."""
    extras = _relative_year_terms(query, now)
    if temporal_intent == "historical":
        extras.extend(["historical", "archived", "past", "completed", "expired"])
    elif temporal_intent == "current_upcoming":
        extras.extend(["current", "upcoming", "future", "active", "scheduled"])

    unique_extras = list(dict.fromkeys(extras))
    if not unique_extras:
        return [query]
    return [query, f"{query} {' '.join(unique_extras)}"]


def expanded_search_fact_keywords(query: str, now: datetime) -> list[str]:
    keywords = search_fact_keywords(query)
    for term in _relative_year_terms(query, now):
        if term not in keywords:
            keywords.append(term)
    return keywords


def filter_facts_for_required_terms(
    facts: list[dict[str, Any]],
    terms: set[str],
) -> list[dict[str, Any]]:
    if not terms:
        return facts
    return [
        fact for fact in facts
        if any(term in f"{fact.get('key', '')} {fact.get('value', '')}".lower() for term in terms)
    ]


def filter_facts_for_temporal_intent(
    facts: list[dict[str, Any]],
    temporal_intent: str,
    now: datetime,
) -> list[dict[str, Any]]:
    ranks = {
        "current_upcoming": {"current": 0, "future": 1, "stale": 2, "unknown": 3},
        "historical": {
            "historical": 0, "expired": 1, "stale": 2, "current": 3, "unknown": 4, "future": 5,
        },
        "all": {"current": 0, "future": 1, "stale": 2, "unknown": 3, "historical": 4, "expired": 5},
    }[temporal_intent]

    enriched = [
        {**fact, "temporal_status": fact_temporal_status(fact, now)}
        for fact in facts
    ]
    if temporal_intent == "current_upcoming":
        enriched = [fact for fact in enriched if fact["temporal_status"] in ranks]
    return sorted(
        enriched,
        key=lambda fact: (
            ranks.get(str(fact.get("temporal_status")), 99),
            str(fact.get("domain")),
            str(fact.get("key")),
        ),
    )





def classify_search_route(query: str, facts: list[dict[str, Any]]) -> str:
    """Pick the result ordering for facts and wiki pages."""
    terms = set(search_fact_keywords(query))
    lowered = query.lower()
    if terms & EVIDENCE_QUERY_HINTS or re.search(r"\b(where did|show .*source)", lowered):
        return "evidence"
    if facts and (
        terms & FACT_QUERY_HINTS
        or re.search(
            r"\b(what'?s|what is|who is|when is|when do|when did|where is|which is|"
            r"how many|how much|do i have|did i)\b",
            lowered,
        )
    ):
        return "fact"
    return "synthesis"


async def search_knowledge(
    *,
    embeddings: EmbeddingClient,
    sparse_encoder: BM25SparseEncoder,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
    query: str,
    domain: str | None = None,
    domains: list[str] | None = None,
    limit: int = 10,
    min_similarity: float = 0.25,
    include_facts: bool = True,
    max_chars: int | None = None,
    temporal_intent: str | None = None,
) -> dict[str, Any]:
    """Run the shared Knowledge search path used by MCP and REST."""
    now = datetime.now(EASTERN_TIMEZONE)
    search_temporal_intent = classify_search_temporal_intent(
        query,
        now=now,
        override=temporal_intent,
    )
    include_archived = search_temporal_intent == "historical"
    expanded_queries = expand_search_query(query, search_temporal_intent, now)
    retrieval_query = expanded_queries[-1]
    resolved_domains = await resolve_search_domains(
        db,
        domain,
        domains,
        include_archived=include_archived,
    )
    keywords = expanded_search_fact_keywords(query, now)
    facts = (
        await db.facts_search(resolved_domains, keywords)
        if include_facts and keywords else []
    )
    facts = filter_facts_for_required_terms(
        facts,
        set(search_fact_keywords(query)) & TEMPORAL_TOPIC_HINTS,
    )
    facts = filter_facts_for_temporal_intent(facts, search_temporal_intent, now)
    route = classify_search_route(query, facts)

    query_embedding = await embeddings.embed(retrieval_query)
    sparse_query = sparse_encoder.encode_query(retrieval_query)

    results = await vectors.search(
        query_embedding,
        sparse_query=sparse_query,
        domains=resolved_domains,
        limit=limit,
        min_score=min_similarity,
    )

    # Split Qdrant results into wiki pages and derived fact vectors.
    # Legacy source chunks are ignored; source storage no longer exists here.
    wiki_results = []
    vector_fact_results = []
    wiki_slugs_seen: set[str] = set()
    live_fact_keys = {(fact["domain"], fact["key"]) for fact in facts}
    for r in results:
        p = r.payload or {}
        content = str(p.get("content", ""))
        if max_chars is not None and max_chars > 0 and len(content) > max_chars:
            content = content[:max_chars] + "…"
        if p.get("type") == "fact" or p.get("source_type") == "fact":
            status = fact_temporal_status(p, now)
            if search_temporal_intent == "current_upcoming" and status in {"expired", "historical"}:
                continue
            domain_key = (str(p.get("domain") or ""), str(p.get("key") or ""))
            if domain_key in live_fact_keys:
                continue
            vector_fact_results.append({
                "result_type": "fact",
                "content": f"{p.get('key', '')}: {p.get('value', '')}",
                "domain": p.get("domain", ""),
                "source_id": "",
                "source_name": p.get("source") or "",
                "source_type": "fact",
                "chunk_id": f"{p.get('domain', '')}/{p.get('key', '')}",
                "chunk_index": 0,
                "similarity": round(r.score, 4),
                "key": p.get("key"),
                "value": p.get("value"),
                "valid_from": p.get("valid_from"),
                "valid_until": p.get("valid_until"),
                "as_of": p.get("as_of"),
                "review_after": p.get("review_after"),
                "temporal_status": status,
            })
            continue
        if p.get("source_type") == "wiki_page":
            slug = str(p.get("source_id") or "")
            if slug in wiki_slugs_seen:
                continue
            wiki_slugs_seen.add(slug)
            # Enrich from SQLite for title, kind, status, frontmatter.
            page = await db.wiki_get(slug) if slug else None
            include_archived = search_temporal_intent == "historical"
            if page and (
                page["status"] == "active"
                or (include_archived and page["status"] == "archived")
            ):
                wiki_results.append({
                    "result_type": "wiki",
                    "content": content,
                    "domain": p.get("domain", ""),
                    "source_id": slug,
                    "source_name": page["title"],
                    "source_type": "wiki_page",
                    "chunk_id": slug,
                    "chunk_index": 0,
                    "similarity": round(r.score, 4),
                    "slug": slug,
                    "title": page["title"],
                    "kind": page["kind"],
                    "status": page["status"],
                    "frontmatter": page.get("frontmatter") or {},
                })

    fact_results = [{
        "result_type": "fact",
        "content": f"{fact['key']}: {fact['value']}",
        "domain": fact["domain"],
        "source_id": "",
        "source_name": fact.get("source") or "",
        "source_type": "fact",
        "chunk_id": f"{fact['domain']}/{fact['key']}",
        "chunk_index": 0,
        "similarity": 1.0,
        "key": fact["key"],
        "value": fact["value"],
        "valid_from": fact.get("valid_from"),
        "valid_until": fact.get("valid_until"),
        "as_of": fact.get("as_of"),
        "review_after": fact.get("review_after"),
        "temporal_status": fact.get("temporal_status") or fact_temporal_status(fact),
        "origin_type": fact.get("origin_type"),
        "origin_ref": fact.get("origin_ref"),
        "last_confirmed_at": fact.get("last_confirmed_at"),
        "fact_type": fact.get("type") or "note",
        "tags": fact.get("tags") or [],
    } for fact in facts]
    all_fact_results = [*fact_results, *vector_fact_results]

    if route == "fact":
        ordered_results = [
            *all_fact_results,
            *(wiki_results if not all_fact_results else []),
        ]
    elif route == "evidence":
        ordered_results = [*wiki_results, *vector_fact_results]
    else:
        # Wiki and vector fact results are already on the same similarity scale
        # (cosine similarity from Qdrant), so merge and sort by score.
        merged = [*wiki_results, *vector_fact_results]
        merged.sort(key=lambda r: -float(r.get("similarity") or 0))
        ordered_results = merged
    formatted = ordered_results[:max(1, limit)]

    response: dict[str, Any] = {
        "success": True,
        "query": query,
        "route": route,
        "temporal_intent": search_temporal_intent,
        "include_archived": include_archived,
        "expanded_queries": expanded_queries,
        "searched_domains": resolved_domains,
        "count": len(formatted),
        "results": formatted,
        "wiki_count": len(wiki_results),
        "chunk_count": 0,
    }

    if include_facts:
        response["facts"] = facts
        response["fact_count"] = len(facts)
        response["fact_temporal_counts"] = fact_temporal_counts(facts)

    return response
