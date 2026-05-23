"""Cross-domain signal detection and context enrichment.

Scans query text and primary search results for signals indicating
that additional context from OTHER domains would improve the answer.
Runs broad follow-up searches across all domains — no domain names
are hardcoded.  New domains are automatically included.

MCP tool suggestions describe capabilities ("check calendar",
"check weather") without naming specific servers, so adding or
removing MCP servers requires no changes here.
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
        "enrichment_query": "budget spending finances cost",
        "tool_hint": None,
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
    """
    now = now or datetime.now(EASTERN_TIMEZONE)
    signals = detect_signals(query, primary_results, primary_facts, now)

    if not signals:
        return {}

    enrichment: dict[str, Any] = {
        "signals_detected": [s["name"] for s in signals],
    }

    # Run broad cross-domain searches for each signal that has a query
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

    # Capability-based suggestions (no server names)
    suggestions = [s["tool_hint"] for s in signals if s.get("tool_hint")]
    if suggestions:
        enrichment["cross_domain_suggestions"] = suggestions

    return enrichment
