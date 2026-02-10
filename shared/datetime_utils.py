"""Datetime parsing and timezone normalization utilities.

Standalone equivalents of backend.utils.datetime_utils — zero imports
from Backend_FastAPI.
"""

from __future__ import annotations

import datetime
from typing import Optional


class _FallbackParser:
    """Fallback datetime parser when python-dateutil is not available."""

    @staticmethod
    def parse(timestr: str) -> datetime.datetime:
        return datetime.datetime.fromisoformat(timestr.replace("Z", "+00:00"))


try:
    from dateutil import parser as _dateutil_parser  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    _dateutil_parser = None


def _parse(timestr: str) -> datetime.datetime:
    """Parse a datetime string using dateutil if available, otherwise fromisoformat."""
    if _dateutil_parser is not None:
        return _dateutil_parser.parse(timestr)  # type: ignore[no-any-return]
    return _FallbackParser.parse(timestr)


def parse_rfc3339_datetime(value: Optional[str]) -> Optional[datetime.datetime]:
    """Best-effort conversion of an RFC3339 string to an aware datetime in UTC."""
    if not value:
        return None

    try:
        parsed = _parse(value)
    except Exception:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    else:
        parsed = parsed.astimezone(datetime.timezone.utc)

    return parsed


def normalize_rfc3339(dt_value: datetime.datetime) -> str:
    """Return an RFC3339 string in canonical UTC form with 'Z' suffix."""
    normalized = dt_value.astimezone(datetime.timezone.utc).isoformat()
    if normalized.endswith("+00:00"):
        normalized = normalized[:-6] + "Z"
    return normalized


def parse_time_string(time_str: Optional[str]) -> Optional[str]:
    """Convert keywords like 'today' or 'tomorrow' to RFC3339 timestamps.

    Supported keywords: today, tomorrow, yesterday, next_week, next_month, next_year.
    Also handles date-only strings (YYYY-MM-DD) and ISO datetime strings.
    """
    if not time_str:
        return None

    lowered = time_str.lower()
    today = datetime.date.today()

    if lowered == "today":
        date_obj = today
    elif lowered == "tomorrow":
        date_obj = today + datetime.timedelta(days=1)
    elif lowered == "yesterday":
        date_obj = today - datetime.timedelta(days=1)
    elif lowered == "next_week":
        date_obj = today + datetime.timedelta(days=7)
    elif lowered == "next_month":
        next_month = (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
        date_obj = next_month
    elif lowered == "next_year":
        date_obj = today.replace(year=today.year + 1)
    else:
        try:
            date_obj = datetime.date.fromisoformat(time_str)
        except ValueError:
            try:
                dt = datetime.datetime.fromisoformat(time_str)
            except ValueError:
                return time_str
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
                date_obj = dt.date()
            else:
                date_obj = dt.date()
                utc_midnight = datetime.datetime(
                    date_obj.year, date_obj.month, date_obj.day,
                    0, 0, 0, tzinfo=datetime.timezone.utc,
                )
                return utc_midnight.isoformat().replace("+00:00", "Z")

    utc_midnight = datetime.datetime(
        date_obj.year, date_obj.month, date_obj.day,
        0, 0, 0, tzinfo=datetime.timezone.utc,
    )
    return utc_midnight.isoformat().replace("+00:00", "Z")


def parse_iso_time_string(time_str: Optional[str]) -> Optional[str]:
    """Normalize ISO-like date/time strings to RFC3339 (UTC) strings."""
    if not time_str:
        return None

    # ISO date-only
    try:
        if len(time_str) == 10 and time_str[4] == "-" and time_str[7] == "-":
            datetime.date.fromisoformat(time_str)
            return f"{time_str}T00:00:00Z"
    except Exception:
        pass

    # Datetime with no timezone → treat as UTC
    if "T" in time_str and (
        "+" not in time_str and "-" not in time_str[10:] and "Z" not in time_str
    ):
        return time_str + "Z"

    # If timezone is present (and not Z), convert to UTC
    if "T" in time_str and (
        "+" in time_str or ("-" in time_str[10:] and "Z" not in time_str)
    ):
        try:
            dt = datetime.datetime.fromisoformat(time_str)
            if dt.tzinfo is not None:
                dt_utc = dt.astimezone(datetime.timezone.utc)
                return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass

    return time_str


def compute_task_window(
    time_min_rfc: Optional[str], time_max_rfc: Optional[str]
) -> tuple[Optional[datetime.datetime], datetime.datetime, Optional[datetime.datetime]]:
    """Determine the primary task window and overdue cutoff."""
    now = datetime.datetime.now(datetime.timezone.utc)
    start_dt = parse_rfc3339_datetime(time_min_rfc)
    end_dt = parse_rfc3339_datetime(time_max_rfc)

    if end_dt is None:
        base = start_dt if start_dt and start_dt > now else now
        end_dt = base + datetime.timedelta(days=7)

    if end_dt < now:
        end_dt = now

    past_due_cutoff: Optional[datetime.datetime] = None
    return start_dt, end_dt, past_due_cutoff


__all__ = [
    "parse_rfc3339_datetime",
    "normalize_rfc3339",
    "parse_time_string",
    "parse_iso_time_string",
    "compute_task_window",
]
