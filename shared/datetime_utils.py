"""Datetime parsing and timezone normalization utilities.

Standalone equivalents of backend.utils.datetime_utils — zero imports
from Backend_FastAPI.
"""

from __future__ import annotations

import datetime

from shared.time_context import EASTERN_TIMEZONE


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


def parse_rfc3339_datetime(value: str | None) -> datetime.datetime | None:
    """Best-effort conversion of an RFC3339 string to an aware datetime in UTC."""
    if not value:
        return None

    try:
        parsed = _parse(value)
    except Exception:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    else:
        parsed = parsed.astimezone(datetime.UTC)

    return parsed


def normalize_rfc3339(dt_value: datetime.datetime) -> str:
    """Return an RFC3339 string in canonical UTC form with 'Z' suffix."""
    normalized = dt_value.astimezone(datetime.UTC).isoformat()
    if normalized.endswith("+00:00"):
        normalized = normalized[:-6] + "Z"
    return normalized


def parse_time_string(time_str: str | None) -> str | None:
    """Convert keywords like 'today' or 'tomorrow' to RFC3339 timestamps.

    Supported keywords: today, tomorrow, yesterday, next_week, next_month, next_year.
    Also handles date-only strings (YYYY-MM-DD) and ISO datetime strings.

    All relative date keywords are resolved in the user's local timezone
    (America/New_York) so that "tomorrow" means the next calendar day for
    the user, regardless of the server's system clock timezone.

    Returns RFC3339 UTC midnight for the resolved date (suitable for the
    Google Tasks API which only stores the date portion).
    """
    if not time_str:
        return None

    lowered = time_str.lower()
    today = datetime.datetime.now(EASTERN_TIMEZONE).date()

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
                # Treat naive datetimes as Eastern time
                dt = dt.replace(tzinfo=EASTERN_TIMEZONE)
            date_obj = dt.date()

    utc_midnight = datetime.datetime(
        date_obj.year, date_obj.month, date_obj.day,
        0, 0, 0, tzinfo=datetime.UTC,
    )
    return utc_midnight.isoformat().replace("+00:00", "Z")


def parse_iso_time_string(time_str: str | None) -> str | None:
    """Normalize ISO-like date/time strings to RFC3339 strings.

    Naive datetimes (no timezone offset) are interpreted as Eastern time
    (America/New_York) and converted to UTC.  Strings that already carry
    a timezone offset are converted to UTC directly.
    """
    if not time_str:
        return None

    # ISO date-only — pass through (used for all-day events)
    try:
        if len(time_str) == 10 and time_str[4] == "-" and time_str[7] == "-":
            datetime.date.fromisoformat(time_str)
            return time_str
    except Exception:
        pass

    # Datetime with no timezone → treat as Eastern, convert to UTC
    if "T" in time_str and (
        "+" not in time_str and "-" not in time_str[10:] and "Z" not in time_str
    ):
        try:
            dt = datetime.datetime.fromisoformat(time_str)
            dt_eastern = dt.replace(tzinfo=EASTERN_TIMEZONE)
            dt_utc = dt_eastern.astimezone(datetime.UTC)
            return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            return time_str + "Z"  # best-effort fallback

    # Already has timezone info — convert to UTC
    if "T" in time_str:
        # Handle Z suffix
        if time_str.endswith("Z"):
            return time_str
        try:
            dt = datetime.datetime.fromisoformat(time_str)
            if dt.tzinfo is not None:
                dt_utc = dt.astimezone(datetime.UTC)
                return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass

    return time_str


def compute_task_window(
    time_min_rfc: str | None, time_max_rfc: str | None
) -> tuple[datetime.datetime | None, datetime.datetime, datetime.datetime | None]:
    """Determine the primary task window and overdue cutoff."""
    now = datetime.datetime.now(datetime.UTC)
    start_dt = parse_rfc3339_datetime(time_min_rfc)
    end_dt = parse_rfc3339_datetime(time_max_rfc)

    if end_dt is None:
        base = start_dt if start_dt and start_dt > now else now
        end_dt = base + datetime.timedelta(days=7)

    if end_dt < now:
        end_dt = now

    past_due_cutoff: datetime.datetime | None = None
    return start_dt, end_dt, past_due_cutoff


__all__ = [
    "parse_rfc3339_datetime",
    "normalize_rfc3339",
    "parse_time_string",
    "parse_iso_time_string",
    "compute_task_window",
]
