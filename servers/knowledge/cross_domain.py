"""Cross-domain signal detection and context enrichment.

Scans query text and primary search results for signals indicating
that additional context from OTHER domains would improve the answer.
Runs broad follow-up searches across all domains — no domain names
are hardcoded.  New domains are automatically included.

MCP tool suggestions describe capabilities ("check calendar",
"check weather") without naming specific servers, so adding or
removing MCP servers requires no changes here.

Three enrichment layers work together:
1. **Signal detection** — pattern-based signals (scheduling, financial,
   outdoor, people, health, projects, transport) that trigger targeted
   cross-domain searches and MCP tool hints.
2. **Entity echo** — always-on extraction of entity references from
   primary search results, followed by targeted searches to pull in
   cross-domain context the model wouldn't otherwise see.
3. **Wiki synthesis** — always-on lookup of active wiki pages covering
   domains and entities found in primary results, providing the model
   with pre-synthesized cross-referenced knowledge.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from servers.knowledge.db import KnowledgeDB
from servers.knowledge.embeddings import BM25SparseEncoder, EmbeddingClient
from servers.knowledge.search import search_knowledge
from servers.knowledge.vectors import KnowledgeVectorStore
from shared.logging_config import get_logger
from shared.time_context import EASTERN_TIMEZONE

log = get_logger("knowledge")

# ---------------------------------------------------------------------------
# Date extraction
# ---------------------------------------------------------------------------

_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_MONTH_DAY_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{1,2})(?:,?\s+(\d{4}))?\b",
    re.IGNORECASE,
)
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def extract_dates(text: str, now: datetime) -> list[str]:
    """Extract concrete date references from text as ISO strings."""
    from datetime import date as _date

    dates: set[_date] = set()
    for m in _ISO_DATE_RE.finditer(text):
        try:
            dates.add(_date.fromisoformat(m.group(1)))
        except ValueError:
            pass
    for m in _MONTH_DAY_RE.finditer(text):
        month = _MONTHS.get(m.group(1).lower())
        day_num = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else now.year
        try:
            dates.add(_date(year, month, day_num))  # type: ignore[arg-type]
        except (ValueError, TypeError):
            pass
    return [d.isoformat() for d in sorted(dates)]


# ---------------------------------------------------------------------------
# Signal detection — pattern-based, zero hardcoded domains
# ---------------------------------------------------------------------------

_PLANNING_RE = re.compile(
    r"\b(when\s+(?:should|can|would|will|could)|"
    r"good\s+time|best\s+(?:time|day)|"
    r"before\s+\w+\s+(?:starts?|arrives?|begins?|comes?)|"
    r"prepare\b|plan\s+(?:for|to|ahead)|"
    r"free\s+(?:time|day)|day\s+off|available|"
    r"what\s+day|which\s+day)",
    re.IGNORECASE,
)
_MONEY_RE = re.compile(
    r"\$[\d,]+(?:\.\d{2})?|\b\d+\s*(?:dollars?|USD)\b",
    re.IGNORECASE,
)
_OUTDOOR_KW = frozenset({
    "yard", "garden", "outside", "outdoor", "lawn", "landscap",
    "plant", "sod", "mow", "patio", "driveway", "pool",
})
_PEOPLE_RE = re.compile(
    r"\b(who\s+(?:is|was|does|did|works|lives)|"
    r"(?:family|mom|dad|wife|husband|brother|sister|son|daughter|"
    r"girlfriend|boyfriend|partner|roommate|friend|neighbor|"
    r"coworker|boss|manager|supervisor)\b)",
    re.IGNORECASE,
)
_HEALTH_RE = re.compile(
    r"\b(doctor|dr\.|physician|dentist|appointment|medication|"
    r"prescription|dose|dosage|supplement|vitamin|symptom|"
    r"diagnosis|lab\s*(?:work|result)|blood|glucose|insulin|"
    r"allergy|allergies|weight|bmi|exercise|workout|therapy|"
    r"medical|health|hospital|clinic|surgery|procedure|vaccine|"
    r"insurance\s+(?:health|medical|dental))\b",
    re.IGNORECASE,
)
_PROJECT_RE = re.compile(
    r"\b(project|homelab|server|deploy|build|install|upgrade|"
    r"migrate|refactor|repo|repository|pipeline|automation|"
    r"setup|configure|debug|troubleshoot)\b",
    re.IGNORECASE,
)
_TRANSPORT_RE = re.compile(
    r"\b(car|vehicle|truck|drive|commute|mileage|oil\s+change|"
    r"tire|maintenance|inspection|registration|insurance\s+(?:car|auto)|"
    r"gas|fuel|mechanic|dealership|lease|loan\s+(?:car|auto)|"
    r"parking|toll|highway)\b",
    re.IGNORECASE,
)
_TASK_RE = re.compile(
    r"\b(todo|to.do|task|tasks|reminder|deadline|due\s+date|"
    r"overdue|checklist|pending|backlog|priority|priorities)\b",
    re.IGNORECASE,
)


def _collect_text(
    query: str,
    results: list[dict[str, Any]],
    facts: list[dict[str, Any]],
) -> str:
    """Concatenate all searchable text for signal scanning."""
    parts = [query]
    for r in results:
        parts.append(str(r.get("content", "")))
    for f in facts:
        parts.append(f"{f.get('key', '')} {f.get('value', '')}")
    return " ".join(parts)


# Each signal type carries an enrichment_query (used for a broad cross-domain
# search) and an optional tool_hint (capability description for the model).
# No domain names or MCP server names appear here.

_SIGNAL_DEFS: list[dict[str, Any]] = [
    {
        "name": "scheduling",
        "detect": lambda q, txt, now: (
            bool(extract_dates(txt, now)) or bool(_PLANNING_RE.search(q))
        ),
        "meta": lambda q, txt, now: {
            "dates_found": extract_dates(txt, now),
            "is_planning_query": bool(_PLANNING_RE.search(q)),
        },
        "enrichment_query": "work schedule shifts days off availability",
        "tool_hint": (
            "Check calendar/scheduling tools for events and conflicts on "
            "the relevant dates. Jack is NOT available for physical tasks "
            "after evening work shifts — recommend days off instead."
        ),
    },
    {
        "name": "financial",
        "detect": lambda q, txt, now: bool(_MONEY_RE.search(txt)),
        "meta": lambda q, txt, now: {},
        "enrichment_query": "budget spending finances cost subscription",
        "tool_hint": (
            "Check finance tools (Monarch) for budget, spending, and "
            "account balance details if relevant."
        ),
    },
    {
        "name": "outdoor",
        "detect": lambda q, txt, now: any(
            w in txt.lower() for w in _OUTDOOR_KW
        ),
        "meta": lambda q, txt, now: {},
        "enrichment_query": None,  # no cross-domain search needed
        "tool_hint": (
            "This involves outdoor activity. Check weather forecast for "
            "the recommended dates before confirming plans."
        ),
    },
    {
        "name": "people",
        "detect": lambda q, txt, now: bool(_PEOPLE_RE.search(txt)),
        "meta": lambda q, txt, now: {},
        "enrichment_query": "family coworker relationship contact person",
        "tool_hint": None,
    },
    {
        "name": "health",
        "detect": lambda q, txt, now: bool(_HEALTH_RE.search(txt)),
        "meta": lambda q, txt, now: {},
        "enrichment_query": "health medication appointment doctor",
        "tool_hint": (
            "Check calendar for upcoming medical appointments. "
            "Cross-reference medication interactions if multiple drugs mentioned."
        ),
    },
    {
        "name": "projects",
        "detect": lambda q, txt, now: bool(_PROJECT_RE.search(txt)),
        "meta": lambda q, txt, now: {},
        "enrichment_query": "project task status deadline progress",
        "tool_hint": None,
    },
    {
        "name": "transport",
        "detect": lambda q, txt, now: bool(_TRANSPORT_RE.search(txt)),
        "meta": lambda q, txt, now: {},
        "enrichment_query": "car vehicle maintenance mileage registration",
        "tool_hint": None,
    },
    {
        "name": "tasks",
        "detect": lambda q, txt, now: bool(_TASK_RE.search(txt)),
        "meta": lambda q, txt, now: {},
        "enrichment_query": None,  # handled by _typed_task_enrichment
        "tool_hint": None,
        "custom_handler": "_typed_task_enrichment",
    },
]


def detect_signals(
    query: str,
    results: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    """Detect cross-domain signals from query and results.

    Returns a list of signal dicts, each with:
      name, meta, enrichment_query (or None), tool_hint (or None)
    """
    all_text = _collect_text(query, results, facts)
    detected: list[dict[str, Any]] = []

    for defn in _SIGNAL_DEFS:
        if defn["detect"](query, all_text, now):
            detected.append({
                "name": defn["name"],
                "meta": defn["meta"](query, all_text, now),
                "enrichment_query": defn["enrichment_query"],
                "tool_hint": defn["tool_hint"],
            })

    return detected


# ---------------------------------------------------------------------------
# Entity echo — extract references from primary results for follow-up
# ---------------------------------------------------------------------------


def _extract_mentioned_domains(
    results: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    available_domains: list[str],
) -> set[str]:
    """Find domain names mentioned in result content or fact values.

    If primary results reference entities that live in other domains,
    we want to pull context from those domains too.
    """
    all_text = _collect_text("", results, facts).lower()
    mentioned: set[str] = set()
    for d in available_domains:
        # Match domain name as word boundary (e.g. "health" but not "healthy")
        # Use underscore-split tokens for multi-word domains like "work_schedule"
        tokens = d.replace("_", " ")
        if re.search(rf"\b{re.escape(tokens)}\b", all_text):
            mentioned.add(d)
        if "_" in d and re.search(rf"\b{re.escape(d)}\b", all_text):
            mentioned.add(d)
    return mentioned


def _build_entity_echo_query(
    query: str,
    results: list[dict[str, Any]],
    facts: list[dict[str, Any]],
) -> str | None:
    """Build a follow-up search query from entity references in primary results.

    Extracts key nouns and proper-noun-like tokens from primary results
    that weren't in the original query, then builds a compact search string
    to find cross-referenced context.
    """
    # Collect unique domain names seen in results
    result_domains = {r.get("domain", "") for r in results if r.get("domain")}
    fact_domains = {f.get("domain", "") for f in facts if f.get("domain")}
    all_domains = result_domains | fact_domains

    # Collect fact keys as entity references — these are often the most
    # semantically useful cross-reference terms
    entity_terms: list[str] = []
    for f in facts[:10]:  # limit to avoid huge queries
        key = str(f.get("key", "")).replace("_", " ").strip()
        if key and len(key) > 2:
            entity_terms.append(key)

    # Extract source names from results as potential entity references
    for r in results[:8]:
        source_name = str(r.get("source_name", "")).strip()
        if source_name and source_name not in ("manual", "note", "fact"):
            # Take just the first few words as entity references
            words = source_name.split()[:4]
            entity_terms.append(" ".join(words))

    if not entity_terms and len(all_domains) <= 1:
        return None

    # Build a compact echo query — original query core + entity references
    query_core = " ".join(query.split()[:6])  # first 6 words of query
    echo_parts = [query_core]
    # Add up to 5 unique entity terms
    seen: set[str] = set()
    for term in entity_terms:
        lower = term.lower()
        if lower not in seen and lower not in query.lower():
            seen.add(lower)
            echo_parts.append(term)
        if len(seen) >= 5:
            break

    return " ".join(echo_parts) if len(echo_parts) > 1 else None


# ---------------------------------------------------------------------------
# Wiki synthesis — fetch pre-synthesized wiki pages for relevant entities
# ---------------------------------------------------------------------------


async def _fetch_wiki_synthesis(
    query: str,
    primary_results: list[dict[str, Any]],
    primary_facts: list[dict[str, Any]],
    searched_domains: list[str],
    *,
    db: KnowledgeDB,
) -> dict[str, Any] | None:
    """Lookup active wiki pages covering entities found in primary results.

    Wiki pages are the 'compiled knowledge' layer — they already contain
    cross-references, citations, and entity relationships. Surfacing
    them gives the model pre-synthesized context instead of raw fragments.
    """
    # Collect unique domains from results + facts
    result_domains = {r.get("domain", "") for r in primary_results if r.get("domain")}
    fact_domains = {f.get("domain", "") for f in primary_facts if f.get("domain")}
    all_domains = sorted((result_domains | fact_domains | set(searched_domains)) - {""})

    if not all_domains:
        return None

    # Collect wiki pages already in primary results (to avoid duplication)
    primary_wiki_slugs = {
        r.get("slug") or r.get("source_id", "")
        for r in primary_results
        if r.get("result_type") == "wiki" or r.get("source_type") == "wiki_page"
    }

    wiki_pages: list[dict[str, Any]] = []

    # For each domain seen in results, fetch its active wiki pages
    for domain in all_domains[:6]:  # cap at 6 domains to limit DB calls
        try:
            pages = await db.wiki_list(domain=domain, status="active", limit=10)
            for page in pages:
                slug = str(page.get("slug", ""))
                if slug in primary_wiki_slugs or page.get("kind") == "index":
                    continue

                # Check if this page's title/slug matches the query terms
                title = str(page.get("title", "")).lower()
                slug_tail = slug.rsplit("/", 1)[-1].replace("-", " ") if "/" in slug else slug
                query_lower = query.lower()

                # Include page if its title appears in the query or if the
                # query terms overlap with the slug/title
                query_words = set(query_lower.split())
                title_words = set(title.split())
                overlap = query_words & title_words - {
                    "a", "an", "the", "is", "my", "what", "when",
                    "where", "who", "how", "do", "does", "i",
                }

                if overlap or slug_tail in query_lower or title in query_lower:
                    wiki_pages.append({
                        "slug": slug,
                        "domain": domain,
                        "title": page.get("title", ""),
                        "kind": page.get("kind", ""),
                        "fact_count": page.get("fact_count", 0),
                    })
        except Exception:
            log.warning("wiki_synthesis_domain_failed domain=%s", domain, exc_info=True)

    # Also do a keyword search across all wiki pages for richer matching
    if not wiki_pages:
        try:
            # Use wiki_search (SQLite keyword ranker) as fallback
            keyword_pages = await db.wiki_search(
                domains=all_domains,
                query=query,
                limit=5,
                statuses={"active"},
            )
            for page in keyword_pages:
                slug = str(page.get("slug", ""))
                if slug not in primary_wiki_slugs and page.get("kind") != "index":
                    wiki_pages.append({
                        "slug": slug,
                        "domain": page.get("domain", ""),
                        "title": page.get("title", ""),
                        "kind": page.get("kind", ""),
                        "fact_count": page.get("fact_count", 0),
                    })
        except Exception:
            log.warning("wiki_synthesis_keyword_failed", exc_info=True)

    if not wiki_pages:
        return None

    # Fetch full body for the top-scoring wiki pages (limit to 3 to stay lean)
    full_pages: list[dict[str, Any]] = []
    for wp in wiki_pages[:3]:
        try:
            full = await db.wiki_get(wp["slug"])
            if full and full.get("body_md"):
                body = str(full["body_md"])
                if len(body) > 1500:
                    body = body[:1500] + "…"
                full_pages.append({
                    "slug": wp["slug"],
                    "domain": wp["domain"],
                    "title": full.get("title", wp["title"]),
                    "kind": full.get("kind", wp["kind"]),
                    "body_md": body,
                    "related_slugs": (
                        full.get("frontmatter", {}).get("related_slugs", [])
                    ),
                })
        except Exception:
            log.warning("wiki_synthesis_get_failed slug=%s", wp["slug"], exc_info=True)

    if not full_pages:
        return None

    return {
        "wiki_pages": full_pages,
        "wiki_page_count": len(full_pages),
        "note": (
            "These wiki pages contain pre-synthesized knowledge with "
            "cross-references and citations. Prefer wiki content over "
            "raw chunks when answering synthesis questions."
        ),
    }


# ---------------------------------------------------------------------------
# Enrichment — broad cross-domain searches, no domain filters
# ---------------------------------------------------------------------------


async def _broad_enrichment_search(
    signal_name: str,
    enrichment_query: str,
    primary_domains: list[str],
    *,
    embeddings: EmbeddingClient,
    sparse_encoder: BM25SparseEncoder,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
) -> dict[str, Any]:
    """Search ALL domains for cross-domain context.

    Results that came from the primary search's domains are de-prioritized
    so the model sees genuinely new information.
    """
    try:
        result = await search_knowledge(
            embeddings=embeddings,
            sparse_encoder=sparse_encoder,
            vectors=vectors,
            db=db,
            query=enrichment_query,
            # No domain filter — search everything
            limit=8,
            min_similarity=0.20,
            include_facts=True,
            max_chars=500,
        )
        primary_set = set(primary_domains)
        all_results = result.get("results", [])
        all_facts = result.get("facts", [])

        # Separate results into new-domain vs already-seen-domain
        new_results = [r for r in all_results if r.get("domain") not in primary_set]
        same_results = [r for r in all_results if r.get("domain") in primary_set]
        new_facts = [f for f in all_facts if f.get("domain") not in primary_set]
        same_facts = [f for f in all_facts if f.get("domain") in primary_set]

        return {
            "signal": signal_name,
            # New-domain results first (the valuable cross-references)
            "facts": [*new_facts[:8], *same_facts[:3]],
            "results": [*new_results[:5], *same_results[:2]],
            "new_domains_found": sorted({
                r.get("domain", "") for r in new_results
            } | {f.get("domain", "") for f in new_facts} - {""}),
        }
    except Exception:
        log.warning("cross_domain_search_failed signal=%s", signal_name, exc_info=True)
        return {"signal": signal_name, "error": "search failed"}


async def _typed_task_enrichment(
    primary_domains: list[str],
    primary_facts: list[dict[str, Any]],
    *,
    db: KnowledgeDB,
) -> dict[str, Any]:
    """Pull all task-typed facts from the DB across all active domains.

    This replaces the old broad search approach — instead of hoping
    semantic search finds scattered tasks, we query the type index directly.
    Results include the domain and tags so the model can group them by
    life area.
    """
    try:
        from servers.knowledge.temporal import fact_temporal_status

        tasks = await db.facts_by_type("task")
        events = await db.facts_by_type("event")
        plans = await db.facts_by_type("plan")

        # Deduplicate against primary facts
        primary_keys = {(f.get("domain"), f.get("key")) for f in primary_facts}
        tasks = [t for t in tasks if (t["domain"], t["key"]) not in primary_keys]
        events = [e for e in events if (e["domain"], e["key"]) not in primary_keys]
        plans = [p for p in plans if (p["domain"], p["key"]) not in primary_keys]

        # Add temporal status for filtering
        now_dt = __import__("datetime").datetime.now(
            __import__("shared.time_context", fromlist=["EASTERN_TIMEZONE"]).EASTERN_TIMEZONE
        )
        for item_list in (tasks, events, plans):
            for item in item_list:
                if "temporal_status" not in item:
                    item["temporal_status"] = fact_temporal_status(item, now_dt)

        # Filter to current/upcoming by default (hide expired)
        active_tasks = [
            t for t in tasks
            if t.get("temporal_status") not in ("expired", "historical")
        ]
        active_events = [
            e for e in events
            if e.get("temporal_status") not in ("expired", "historical")
        ]
        active_plans = plans  # Plans rarely expire

        task_domains = sorted({t["domain"] for t in active_tasks})
        return {
            "signal": "tasks",
            "tasks": active_tasks[:20],
            "upcoming_events": active_events[:10],
            "plans": active_plans[:10],
            "task_count": len(active_tasks),
            "event_count": len(active_events),
            "plan_count": len(active_plans),
            "domains_with_tasks": task_domains,
            "note": (
                "These are typed facts (type=task/event/plan) from across "
                "all life-area domains. Use tags to group by sub-category."
            ),
        }
    except Exception:
        log.warning("typed_task_enrichment_failed", exc_info=True)
        return {"signal": "tasks", "error": "typed task query failed"}


# Custom handler registry — maps handler name to async function
_CUSTOM_HANDLERS = {
    "_typed_task_enrichment": _typed_task_enrichment,
}


# ---------------------------------------------------------------------------
# Main enrichment entry point
# ---------------------------------------------------------------------------


async def enrich_context(
    query: str,
    primary_facts: list[dict[str, Any]],
    primary_results: list[dict[str, Any]],
    searched_domains: list[str],
    *,
    embeddings: EmbeddingClient,
    sparse_encoder: BM25SparseEncoder,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Detect cross-domain signals and gather additional context.

    Returns a dict suitable for merging into the context_pack response.
    All searches are broad (no domain filters) so new domains are
    automatically included without code changes.

    Three layers:
    1. Signal-based enrichment: fires on detected patterns.
    2. Entity echo: always fires when primary results reference
       entities from other domains, pulling in cross-domain context.
    3. Wiki synthesis: fetches pre-synthesized wiki pages covering
       entities found in results, providing compiled cross-references.
    """
    now = now or datetime.now(EASTERN_TIMEZONE)
    signals = detect_signals(query, primary_results, primary_facts, now)

    enrichment: dict[str, Any] = {}

    if signals:
        enrichment["signals_detected"] = [s["name"] for s in signals]

    # --- Layer 1: Signal-based cross-domain searches ---
    for signal in signals:
        key = f"{signal['name']}_context"

        # Check for custom handler first (e.g. typed task queries)
        custom = signal.get("custom_handler")
        if custom and custom in _CUSTOM_HANDLERS:
            enrichment[key] = await _CUSTOM_HANDLERS[custom](
                searched_domains, primary_facts, db=db,
            )
            if signal.get("meta"):
                enrichment[key]["meta"] = signal["meta"]
            continue

        eq = signal.get("enrichment_query")
        if eq:
            enrichment[key] = await _broad_enrichment_search(
                signal["name"],
                eq,
                searched_domains,
                embeddings=embeddings,
                sparse_encoder=sparse_encoder,
                vectors=vectors,
                db=db,
            )
            # Attach signal metadata (e.g. dates_found)
            if signal.get("meta"):
                enrichment[key]["meta"] = signal["meta"]

    # --- Layer 2: Entity echo — always-on cross-domain discovery ---
    # Identify domains mentioned in results that aren't in the primary set
    all_domain_rows = await db.domain_list()
    available_domain_names = [
        d["name"] for d in all_domain_rows if not d.get("archived")
    ]
    mentioned = _extract_mentioned_domains(
        primary_results, primary_facts, available_domain_names,
    )
    primary_set = set(searched_domains)
    new_mentioned = mentioned - primary_set

    # Build and run the echo query
    echo_query = _build_entity_echo_query(query, primary_results, primary_facts)
    if echo_query:
        try:
            echo_result = await search_knowledge(
                embeddings=embeddings,
                sparse_encoder=sparse_encoder,
                vectors=vectors,
                db=db,
                query=echo_query,
                limit=6,
                min_similarity=0.22,
                include_facts=True,
                max_chars=400,
            )
            echo_results = echo_result.get("results", [])
            echo_facts = echo_result.get("facts", [])

            # Filter to only genuinely new information
            primary_chunk_ids = {r.get("chunk_id") for r in primary_results}
            primary_fact_keys = {
                (f.get("domain"), f.get("key")) for f in primary_facts
            }
            new_echo_results = [
                r for r in echo_results
                if r.get("chunk_id") not in primary_chunk_ids
            ]
            new_echo_facts = [
                f for f in echo_facts
                if (f.get("domain"), f.get("key")) not in primary_fact_keys
            ]

            if new_echo_results or new_echo_facts:
                echo_domains = sorted({
                    r.get("domain", "") for r in new_echo_results
                } | {
                    f.get("domain", "") for f in new_echo_facts
                } - {""} - primary_set)

                enrichment["entity_echo"] = {
                    "echo_query": echo_query,
                    "results": new_echo_results[:5],
                    "facts": new_echo_facts[:8],
                    "new_domains_found": echo_domains,
                }
        except Exception:
            log.warning("entity_echo_search_failed", exc_info=True)

    # --- Layer 3: Wiki synthesis — pre-compiled knowledge pages ---
    try:
        wiki_synthesis = await _fetch_wiki_synthesis(
            query, primary_results, primary_facts, searched_domains, db=db,
        )
        if wiki_synthesis:
            enrichment["wiki_synthesis"] = wiki_synthesis
    except Exception:
        log.warning("wiki_synthesis_failed", exc_info=True)

    # Note domains mentioned in results that could be explored
    if new_mentioned:
        enrichment["domains_referenced_in_results"] = sorted(new_mentioned)

    # Capability-based suggestions (no server names)
    suggestions = [s["tool_hint"] for s in signals if s.get("tool_hint")]
    if suggestions:
        enrichment["cross_domain_suggestions"] = suggestions

    return enrichment
