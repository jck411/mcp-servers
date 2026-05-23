"""Deterministic work schedule computation.

Jack's schedule: week-on/week-off, Thu-Wed pattern.
- On weeks: Long days (10am-10:30pm) except Sun/Wed (2pm-10:30pm)
- Off weeks: no work
- Cycle starts 2026-04-30 (Thursday)
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any


# Base schedule constants
SCHEDULE_START = date(2026, 4, 30)  # First "on" Thursday
CYCLE_DAYS = 14  # 7 on + 7 off
ON_DAYS = 7  # First 7 days of cycle
SHORT_WEEKDAYS = {2, 6}  # Wednesday=2, Sunday=6
LONG_HOURS = "10:00 AM – 10:30 PM"
SHORT_HOURS = "2:00 PM – 10:30 PM"


def is_working(target: date) -> bool:
    """Return True if Jack is working on the given date (base schedule only)."""
    delta = (target - SCHEDULE_START).days
    return 0 <= (delta % CYCLE_DAYS) < ON_DAYS


def schedule_day(target: date) -> dict[str, Any]:
    """Compute work status for a single date."""
    working = is_working(target)
    weekday = target.weekday()

    result: dict[str, Any] = {
        "date": target.isoformat(),
        "weekday": target.strftime("%A"),
        "working": working,
    }
    if working:
        short = weekday in SHORT_WEEKDAYS
        result["shift"] = "short" if short else "long"
        result["hours"] = SHORT_HOURS if short else LONG_HOURS
        result["home_by"] = "~11:00 PM"
        result["viable_for_outdoor_tasks"] = "morning only" if not short else "morning + early afternoon"
    else:
        result["available"] = "all day"

    return result


def availability_range(
    start: date,
    days: int = 14,
) -> list[dict[str, Any]]:
    """Compute availability for a date range."""
    return [schedule_day(start + timedelta(days=i)) for i in range(days)]


def next_free_days(
    from_date: date,
    count: int = 5,
    max_lookahead: int = 30,
) -> list[dict[str, Any]]:
    """Find the next N days off starting from a date."""
    free: list[dict[str, Any]] = []
    for i in range(max_lookahead):
        day = schedule_day(from_date + timedelta(days=i))
        if not day["working"]:
            free.append(day)
            if len(free) >= count:
                break
    return free


def apply_overrides(
    days: list[dict[str, Any]],
    override_facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply schedule_change / shift_swap facts as overrides to base schedule.

    Override logic:
    - covering_for=Jack → Jack is OFF (someone else covers)
    - worker=Jack + covering_for!=Jack → Jack is WORKING (covering someone)
    """
    # Group facts by change number
    changes: dict[str, dict[str, str]] = {}
    for fact in override_facts:
        m = re.match(r"(schedule_change|shift_swap)_(\d+)_(.+)", fact.get("key", ""))
        if m:
            prefix, num, field = m.groups()
            changes.setdefault(f"{prefix}_{num}", {})[field] = fact.get("value", "")

    # Build date → override map
    overrides: dict[str, dict[str, Any]] = {}
    for change_id, fields in changes.items():
        change_date = fields.get("date", "")
        covering_for = fields.get("covering_for", "").lower()
        worker = fields.get("worker", "").lower()

        if covering_for == "jack":
            overrides[change_date] = {
                "working": False,
                "override_reason": f"{fields.get('worker', 'Someone')} covering for Jack",
            }
        elif worker == "jack":
            hours = fields.get("hours", LONG_HOURS)
            overrides[change_date] = {
                "working": True,
                "hours": hours,
                "override_reason": f"Jack covering for {fields.get('covering_for', 'someone')}",
            }

    # Apply overrides to day list
    for day in days:
        override = overrides.get(day["date"])
        if override:
            day.update(override)

    return days
