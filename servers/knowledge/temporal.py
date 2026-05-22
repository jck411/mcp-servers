"""Temporal fact classification helpers.

Used by search routing, fact display, and maintenance to classify
facts as current, expired, future, stale, or historical relative to
Eastern time.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from shared.time_context import EASTERN_TIMEZONE


def _parse_temporal_value(value: Any) -> date | datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=EASTERN_TIMEZONE)
    return parsed.astimezone(EASTERN_TIMEZONE)


def _before_today(value: date | datetime, now: datetime, *, inclusive: bool = False) -> bool:
    if isinstance(value, datetime):
        return value <= now if inclusive else value < now
    return value <= now.date() if inclusive else value < now.date()


def _after_today(value: date | datetime, now: datetime) -> bool:
    if isinstance(value, datetime):
        return value > now
    return value > now.date()


def _before_local_date(value: date | datetime, now: datetime) -> bool:
    value_date = value.date() if isinstance(value, datetime) else value
    return value_date < now.date()


def fact_temporal_status(fact: dict[str, Any], now: datetime | None = None) -> str:
    """Classify live fact timing relative to America/New_York runtime time."""
    now = now or datetime.now(EASTERN_TIMEZONE)
    try:
        valid_until = _parse_temporal_value(fact.get("valid_until"))
        valid_from = _parse_temporal_value(fact.get("valid_from"))
        review_after = _parse_temporal_value(fact.get("review_after"))
        as_of = _parse_temporal_value(fact.get("as_of"))
    except ValueError:
        return "unknown"
    if valid_until and _before_today(valid_until, now):
        return "expired"
    if valid_from and _after_today(valid_from, now):
        return "future"
    if review_after and _before_today(review_after, now, inclusive=True):
        return "stale"
    if as_of and _before_local_date(as_of, now):
        return "historical"
    return "current"


def add_fact_temporal_status(fact: dict[str, Any]) -> dict[str, Any]:
    return {**fact, "temporal_status": fact_temporal_status(fact)}


def fact_temporal_counts(facts: list[dict[str, Any]]) -> dict[str, int]:
    """Count facts by temporal status."""
    counts: dict[str, int] = {}
    for fact in facts:
        status = fact.get("temporal_status") or fact_temporal_status(fact)
        counts[status] = counts.get(status, 0) + 1
    return counts
