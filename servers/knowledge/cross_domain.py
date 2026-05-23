"""Cross-domain signal detection and context enrichment.

Scans query text and primary search results for signals indicating
that additional context from OTHER domains would improve the answer.
Runs broad follow-up searches across all domains — no domain names
are hardcoded.  New domains are automatically included.

MCP tool suggestions describe capabilities ("check calendar",
"check weather") without naming specific servers, so adding or
removing MCP servers requires no changes here.

Two enrichment layers work together:
1. **Signal detection** — pattern-based signals (scheduling, financial,
   outdoor, people, health, projects, transport) that trigger targeted
   cross-domain searches and MCP tool hints.
2. **Entity echo** — always-on extraction of entity references from
   primary search results, followed by targeted searches to pull in
   cross-domain context the model wouldn't otherwise see.
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
        "enrichment_query": "task todo deadline priority pending",
        "tool_hint": None,
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

    Two layers:
    1. Signal-based enrichment: fires on detected patterns.
    2. Entity echo: always fires when primary results reference
       entities from other domains, pulling in cross-domain context.
    """
    now = now or datetime.now(EASTERN_TIMEZONE)
    signals = detect_signals(query, primary_results, primary_facts, now)

    enrichment: dict[str, Any] = {}

    if signals:
        enrichment["signals_detected"] = [s["name"] for s in signals]

    # --- Layer 1: Signal-based cross-domain searches ---
    for signal in signals:
        eq = signal.get("enrichment_query")
        if eq:
            key = f"{signal['name']}_context"
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

    # Note domains mentioned in results that could be explored
    if new_mentioned:
        enrichment["domains_referenced_in_results"] = sorted(new_mentioned)

    # Capability-based suggestions (no server names)
    suggestions = [s["tool_hint"] for s in signals if s.get("tool_hint")]
    if suggestions:
        enrichment["cross_domain_suggestions"] = suggestions

    return enrichment
