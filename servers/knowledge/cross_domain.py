"""Cross-domain signal detection and context enrichment.

Scans query text and primary search results for signals indicating
additional domains should be searched. Returns enrichment data that
context_pack merges into its response.

Schedule availability is surfaced by auto-searching the work_schedule
domain for relevant facts. Actual calendar event checks are deferred
to the Calendar MCP server via augmentation suggestions.
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

_PLANNING_RE = re.compile(
    r"\b(when\s+(?:should|can|would|will|could)|"
    r"good\s+time|best\s+(?:time|day)|"
    r"before\s+\w+\s+(?:starts?|arrives?|begins?|comes?)|"
    r"prepare\b|plan\s+(?:for|to|ahead)|"
    r"free\s+(?:time|day)|day\s+off|available|"
    r"what\s+day|which\s+day)",
    re.IGNORECASE,
)

# Known people — used to trigger relationship domain searches.
_PEOPLE_RE = re.compile(
    r"\b(Sanja|Zoe|Andison|Dan(?:iel)?|Maja)\b",
    re.IGNORECASE,
)


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
# Signal detection
# ---------------------------------------------------------------------------


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


def detect_signals(
    query: str,
    results: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    searched_domains: list[str],
    now: datetime,
) -> dict[str, Any]:
    """Detect cross-domain signals from query and results.

    Returns a dict mapping signal name → details.  Only signals whose
    target domains were NOT already searched are included.
    """
    all_text = _collect_text(query, results, facts)
    searched = set(searched_domains)
    signals: dict[str, Any] = {}

    # 1. Scheduling / dates
    found_dates = extract_dates(all_text, now)
    is_planning = bool(_PLANNING_RE.search(query))
    if (found_dates or is_planning) and "work_schedule" not in searched:
        signals["scheduling"] = {
            "dates_found": found_dates,
            "is_planning_query": is_planning,
        }

    # 2. People
    people = {m.group(1).title() for m in _PEOPLE_RE.finditer(all_text)}
    people_domains = {"family", "work_people"} - searched
    if people and people_domains:
        signals["people"] = {
            "names": sorted(people),
            "search_domains": sorted(people_domains),
        }

    # 3. Financial
    if re.search(r"\$[\d,]+|\b\d+\s*(?:dollars?|USD)\b", all_text, re.IGNORECASE):
        if "finances" not in searched:
            signals["financial"] = {"search_domains": ["finances"]}

    # 4. Outdoor / weather
    outdoor_kw = {"yard", "garden", "outside", "outdoor", "lawn", "landscap",
                  "plant", "sod", "mow", "patio", "driveway"}
    if any(w in all_text.lower() for w in outdoor_kw):
        signals["outdoor"] = {"suggest_weather": True}

    # 5. Active tasks / projects
    task_kw = {"todo", "task list", "pending", "deadline", "action item"}
    if any(w in all_text.lower() for w in task_kw):
        extra = {"tasks", "projects"} - searched
        if extra:
            signals["tasks"] = {"search_domains": sorted(extra)}

    return signals


# ---------------------------------------------------------------------------
# Enrichment — runs additional searches based on detected signals
# ---------------------------------------------------------------------------


async def _domain_search_enrichment(
    signal_name: str,
    domains: list[str],
    query: str,
    *,
    embeddings: EmbeddingClient,
    sparse_encoder: BM25SparseEncoder,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
) -> dict[str, Any]:
    """Run an additional Knowledge search scoped to specific domains."""
    try:
        result = await search_knowledge(
            embeddings=embeddings,
            sparse_encoder=sparse_encoder,
            vectors=vectors,
            db=db,
            query=query,
            domains=domains,
            limit=5,
            min_similarity=0.20,
            include_facts=True,
            max_chars=500,
        )
        return {
            "reason": f"Cross-domain enrichment for signal '{signal_name}'",
            "domains_searched": domains,
            "facts": result.get("facts", []),
            "results": result.get("results", [])[:3],
        }
    except Exception:
        log.warning("cross_domain_search_failed signal=%s", signal_name, exc_info=True)
        return {"error": f"Cross-domain search for {signal_name} failed"}


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
    Contains auto-searched domain results and augmentation suggestions.
    """
    now = now or datetime.now(EASTERN_TIMEZONE)
    signals = detect_signals(query, primary_results, primary_facts, searched_domains, now)

    if not signals:
        return {}

    enrichment: dict[str, Any] = {"signals_detected": list(signals.keys())}

    # Schedule enrichment — search work_schedule domain for pattern + changes.
    # Actual calendar event checking is deferred to the Calendar MCP tool.
    if "scheduling" in signals:
        enrichment["schedule_context"] = await _domain_search_enrichment(
            "scheduling",
            ["work_schedule"],
            "work schedule pattern days off shifts availability",
            embeddings=embeddings,
            sparse_encoder=sparse_encoder,
            vectors=vectors,
            db=db,
        )
        enrichment["schedule_context"]["note"] = (
            "Jack works 10am–10:30pm (or 2pm–10:30pm Sun/Wed) during on-weeks "
            "and is not home until ~11pm. 'After work' is NOT viable for "
            "physical tasks. Plan outdoor/active tasks for days off only."
        )

    # Other domain-based enrichments
    for sig_name in ("people", "financial", "tasks"):
        sig = signals.get(sig_name)
        if sig and sig.get("search_domains"):
            enrichment[f"{sig_name}_context"] = await _domain_search_enrichment(
                sig_name,
                sig["search_domains"],
                query,
                embeddings=embeddings,
                sparse_encoder=sparse_encoder,
                vectors=vectors,
                db=db,
            )

    # Suggestions
    suggestions: list[str] = []
    if "outdoor" in signals:
        suggestions.append(
            "weather: This involves outdoor activity. Check weather forecast "
            "for the recommended dates before confirming."
        )
    if "scheduling" in signals:
        suggestions.append(
            "calendar: Check Google Calendar for appointments/conflicts on "
            "the relevant dates. Use calendar tools to verify availability."
        )
    if suggestions:
        enrichment["cross_domain_suggestions"] = suggestions

    return enrichment
