"""Shared utilities for producing consistent time context across MCP servers.

Standalone port of backend.services.time_context — zero imports from Backend_FastAPI.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment,misc]

EASTERN_TIMEZONE_NAME = "America/New_York"


def _determine_default_timezone() -> _dt.tzinfo:
    """Choose the default timezone, biased toward the user's Orlando locale."""
    if ZoneInfo is not None:
        try:
            return ZoneInfo(EASTERN_TIMEZONE_NAME)
        except Exception:
            pass
    return _dt.datetime.now().astimezone().tzinfo or _dt.timezone.utc


_LOCAL_DEFAULT = _determine_default_timezone()


def resolve_timezone(
    timezone_name: Optional[str],
    fallback: Optional[_dt.tzinfo] = None,
) -> _dt.tzinfo:
    """Resolve *timezone_name* to a tzinfo, falling back to sensible defaults."""
    if timezone_name and ZoneInfo is not None:
        try:
            return ZoneInfo(timezone_name)
        except Exception:
            pass
    if fallback is not None:
        return fallback
    return _LOCAL_DEFAULT


EASTERN_TIMEZONE: _dt.tzinfo = resolve_timezone(EASTERN_TIMEZONE_NAME, _LOCAL_DEFAULT)


@dataclass(slots=True)
class TimeSnapshot:
    """Snapshot of the current moment in UTC and a target timezone."""

    tzinfo: _dt.tzinfo
    now_utc: _dt.datetime
    now_local: _dt.datetime

    @property
    def eastern(self) -> _dt.datetime:
        """Return the time converted to US Eastern."""
        return self.now_utc.astimezone(EASTERN_TIMEZONE)

    @property
    def date(self) -> _dt.date:
        return self.now_local.date()

    @property
    def iso_local(self) -> str:
        return self.now_local.isoformat()

    @property
    def iso_utc(self) -> str:
        return self.now_utc.isoformat()

    @property
    def unix_seconds(self) -> int:
        return int(self.now_utc.timestamp())

    @property
    def unix_precise(self) -> str:
        return f"{self.now_utc.timestamp():.6f}"

    def format_time(self) -> str:
        return self.now_local.strftime("%H:%M:%S %Z")

    def timezone_display(self) -> str:
        """Return a human-friendly representation of the timezone."""
        tz = self.tzinfo
        key = getattr(tz, "key", None)
        if key:
            return key
        name = tz.tzname(self.now_local)
        return name or str(tz)


def create_time_snapshot(
    timezone_name: Optional[str] = None,
    *,
    fallback: Optional[_dt.tzinfo] = EASTERN_TIMEZONE,
) -> TimeSnapshot:
    """Return a TimeSnapshot for *timezone_name*."""
    tzinfo = resolve_timezone(timezone_name, fallback)
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    now_local = now_utc.astimezone(tzinfo)
    return TimeSnapshot(tzinfo=tzinfo, now_utc=now_utc, now_local=now_local)


def format_timezone_offset(offset: Optional[_dt.timedelta]) -> str:
    """Return an ISO-8601 style UTC offset string."""
    if offset is None:
        return "UTC+00:00"
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def build_context_lines(
    snapshot: TimeSnapshot,
    *,
    include_week: bool = True,
    upcoming_anchors: Sequence[tuple[str, _dt.timedelta]] = (
        ("Tomorrow", _dt.timedelta(days=1)),
        ("In 3 days", _dt.timedelta(days=3)),
        ("Next week", _dt.timedelta(weeks=1)),
    ),
) -> Iterable[str]:
    """Yield human-readable context lines for *snapshot*."""
    today_local = snapshot.date

    yield f"Current date: {today_local.isoformat()} ({snapshot.now_local.strftime('%A')})"
    yield f"Current time: {snapshot.format_time()}"
    yield f"Timezone: {snapshot.timezone_display()}"
    yield f"ISO timestamp (local): {snapshot.iso_local}"
    yield f"ISO timestamp (UTC): {snapshot.iso_utc}"

    if include_week:
        start_of_week = today_local - _dt.timedelta(days=today_local.weekday())
        end_of_week = start_of_week + _dt.timedelta(days=6)
        yield f"Week range: {start_of_week.isoformat()} → {end_of_week.isoformat()}"

    if upcoming_anchors:
        yield "Upcoming anchors:"
        for label, delta in upcoming_anchors:
            anchor = today_local + delta
            yield f"- {label}: {anchor.isoformat()} ({anchor.strftime('%A')})"


__all__ = [
    "EASTERN_TIMEZONE",
    "EASTERN_TIMEZONE_NAME",
    "TimeSnapshot",
    "build_context_lines",
    "create_time_snapshot",
    "format_timezone_offset",
    "resolve_timezone",
]
