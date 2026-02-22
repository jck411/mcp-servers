"""Standalone Google Calendar + Tasks MCP server.

Exposes calendar event CRUD, task CRUD, and schedule views via MCP protocol.
Zero imports from Backend_FastAPI — fully standalone.

Run:
    python -m servers.calendar --transport streamable-http --host 0.0.0.0 --port 9004
"""

from __future__ import annotations

import asyncio
import datetime
import json
import unicodedata
from dataclasses import dataclass
from typing import Any, List, Optional, TypedDict

from fastmcp import FastMCP

from shared.datetime_utils import (
    normalize_rfc3339,
    parse_iso_time_string,
    parse_rfc3339_datetime,
)
from shared.google_auth import (
    DEFAULT_USER_EMAIL,
    get_calendar_service,
    get_tasks_service,
)
from shared.time_context import EASTERN_TIMEZONE_NAME
from shared.tasks import (
    ScheduledTask,
    Task,
    TaskAuthorizationError,
    TaskSearchResult,
    TaskService,
    TaskServiceError,
)

# Default port for HTTP transport
DEFAULT_HTTP_PORT = 9004

mcp: FastMCP = FastMCP("calendar")


# ---------------------------------------------------------------------------
# Calendar alias configuration
# ---------------------------------------------------------------------------


@dataclass
class EventInfo:
    """Simple data class for event information."""

    title: str
    start: str
    end: str
    is_all_day: bool
    calendar: str
    calendar_id: str
    id: str
    link: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None
    ical_uid: Optional[str] = None


class CalendarDefinition(TypedDict):
    """Typed mapping describing a known calendar."""

    id: str
    label: str
    access: str
    aliases: tuple[str, ...]


DEFAULT_CALENDAR_DEFINITIONS: tuple[CalendarDefinition, ...] = (
    {
        "id": "primary",
        "label": "Your Primary Calendar",
        "access": "owner",
        "aliases": (
            "primary",
            "primary calendar",
            "main calendar",
            "default calendar",
            DEFAULT_USER_EMAIL,
        ),
    },
    {
        "id": "family08001023161820261147@group.calendar.google.com",
        "label": "Family Calendar",
        "access": "owner",
        "aliases": (
            "family",
            "family calendar",
        ),
    },
    {
        "id": "en.usa#holiday@group.v.calendar.google.com",
        "label": "Holidays in United States",
        "access": "reader",
        "aliases": (
            "holidays",
            "holiday calendar",
            "holidays in united states",
            "us holidays",
        ),
    },
    {
        "id": "4b779996b31f84a4dc520b2f0255e437863f0c826f3249c05f5f13f020fe3ba6@group.calendar.google.com",
        "label": "Mom Work Schedule",
        "access": "reader",
        "aliases": (
            "mom",
            "mom schedule",
            "mom work",
            "mom work schedule",
            "Sanja's work scheduleSanja in office",
        ),
    },
    {
        "id": "0d02885a194bb2bfab4573ac6188f079498c768aa22659656b248962d03af863@group.calendar.google.com",
        "label": "Dad Work Schedule",
        "access": "owner",
        "aliases": (
            "dad",
            "dad schedule",
            "dad work",
            "dad work schedule",
        ),
    },
)

DEFAULT_READ_CALENDAR_IDS: tuple[str, ...] = tuple(
    definition["id"] for definition in DEFAULT_CALENDAR_DEFINITIONS
)

_CALENDAR_ALIAS_TO_ID: dict[str, str] = {}
_CALENDAR_ID_TO_LABEL: dict[str, str] = {}


def _alias_key(value: str) -> str:
    """Normalize alias keys for robust matching."""
    txt = value.strip().lower()
    txt = txt.replace("\u2018", "'").replace("\u2019", "'")
    txt = txt.replace("\u201c", '"').replace("\u201d", '"')
    for who in ("mom", "dad"):
        txt = txt.replace(f"{who}'s", who)
    for who in ("mom", "dad"):
        txt = txt.replace(f"{who}s ", f"{who} ")
        if txt.endswith(f"{who}s"):
            txt = txt[:-1]

    allowed = set("@+#-_. ")
    txt = "".join(ch for ch in txt if ch.isalnum() or ch in allowed)
    txt = " ".join(txt.split())
    txt = "".join(
        c for c in unicodedata.normalize("NFKD", txt) if not unicodedata.combining(c)
    )
    return txt


for _definition in DEFAULT_CALENDAR_DEFINITIONS:
    _cal_id = _definition["id"]
    _CALENDAR_ID_TO_LABEL[_cal_id] = _definition["label"]
    _CALENDAR_ALIAS_TO_ID[_alias_key(_cal_id)] = _cal_id
    for _alias in _definition["aliases"]:
        _CALENDAR_ALIAS_TO_ID[_alias_key(_alias)] = _cal_id
    _label = _definition["label"]
    _CALENDAR_ALIAS_TO_ID[_alias_key(_label)] = _cal_id

AGGREGATE_CALENDAR_ALIASES: set[str] = {
    "my calendar",
    "my calendars",
    "my schedule",
    "schedule",
    "reading schedule",
}


def _normalize_calendar_id(calendar_id: str) -> str:
    """Map friendly calendar names to canonical IDs when possible."""
    normalized = _alias_key(calendar_id)
    return _CALENDAR_ALIAS_TO_ID.get(normalized, calendar_id)


def _calendar_label(calendar_id: str) -> str:
    """Return a human-friendly label for a calendar ID."""
    canonical = _CALENDAR_ALIAS_TO_ID.get(_alias_key(calendar_id), calendar_id)
    return _CALENDAR_ID_TO_LABEL.get(canonical, calendar_id)


def _should_use_aggregate(calendar_id: Optional[str]) -> bool:
    """Determine whether calls should use the default multi-calendar view."""
    if calendar_id is None:
        return True
    return calendar_id.strip().lower() in AGGREGATE_CALENDAR_ALIASES


def _resolve_calendar_id_for_write(calendar_id: Optional[str]) -> str:
    """Resolve calendar identifiers for create/update/delete operations."""
    if not calendar_id:
        return "primary"
    normalized = calendar_id.strip().lower()
    if normalized in AGGREGATE_CALENDAR_ALIASES:
        return "primary"
    return _CALENDAR_ALIAS_TO_ID.get(normalized, calendar_id)


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------


def _event_sort_key(start_value: str) -> datetime.datetime:
    """Convert an event start value to a sortable datetime."""
    try:
        parsed = parse_rfc3339_datetime(start_value)
        if parsed is None:
            return datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
        return parsed
    except Exception:
        return datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)


def _event_bounds(event: EventInfo) -> tuple[datetime.datetime, datetime.datetime]:
    """Return aware datetime bounds for overlap checks."""
    if event.is_all_day:
        try:
            start_date = datetime.date.fromisoformat(event.start)
            end_date = datetime.date.fromisoformat(event.end)
        except Exception:
            try:
                start_date = datetime.date.fromisoformat(event.start)
            except Exception:
                start_date = datetime.date.min
            end_date = start_date

        start_dt = datetime.datetime.combine(
            start_date, datetime.time.min, tzinfo=datetime.timezone.utc
        )
        end_dt = datetime.datetime.combine(
            end_date, datetime.time.min, tzinfo=datetime.timezone.utc
        )
        return start_dt, end_dt

    start_dt = parse_rfc3339_datetime(event.start) or datetime.datetime.max.replace(
        tzinfo=datetime.timezone.utc
    )
    end_dt = parse_rfc3339_datetime(event.end) or start_dt
    return start_dt, end_dt


def _overlaps_window(
    event: EventInfo,
    range_min_dt: Optional[datetime.datetime],
    range_max_dt: Optional[datetime.datetime],
) -> bool:
    """Return True when the event overlaps the requested window."""
    event_start, event_end = _event_bounds(event)

    window_start = (
        range_min_dt
        if range_min_dt
        else datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    )
    window_end = (
        range_max_dt
        if range_max_dt
        else datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
    )

    return event_start < window_end and window_start < event_end


def _truncate_text(value: Optional[str], limit: int) -> Optional[str]:
    """Return a truncated version of the text to keep payloads compact."""
    if value is None:
        return None
    stripped = value.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 3] + "..."


# ---------------------------------------------------------------------------
# Task list resolution helpers
# ---------------------------------------------------------------------------


async def _resolve_task_list_identifier(
    task_service: TaskService, identifier: str
) -> tuple[str, str] | None:
    """Resolve a task list identifier to an (id, title) pair."""
    ident = identifier.strip()
    if not ident:
        return None

    try:
        info = await task_service.get_task_list(ident)
        return (info.id, info.title)
    except TaskServiceError:
        pass

    partial_candidates: list[tuple[str, str]] = []
    page_token: str | None = None
    while True:
        lists, next_token = await task_service.list_task_lists(
            max_results=100, page_token=page_token
        )
        for lst in lists:
            title_lower = (lst.title or "").strip().lower()
            ident_lower = ident.lower()
            if title_lower == ident_lower:
                return (lst.id, lst.title)
            if ident_lower and ident_lower in title_lower:
                partial_candidates.append((lst.id, lst.title))

        if not next_token:
            break
        page_token = next_token

    if len(partial_candidates) == 1:
        return partial_candidates[0]

    return None


# ---------------------------------------------------------------------------
# Task formatting helpers
# ---------------------------------------------------------------------------


def _format_task_search_result(task: TaskSearchResult) -> List[str]:
    """Format a search result entry for display."""
    due_text = f" | Due: {task.due}" if task.due else ""
    completed_text = f" | Completed: {task.completed}" if task.completed else ""
    line = (
        f'- "{task.title}" [List: {task.list_title}] '
        f"Status: {task.status}{due_text}{completed_text} ID: {task.id}"
    )

    if task.web_link:
        line += f" | Link: {task.web_link}"

    lines = [line]

    if task.notes:
        snippet = task.notes.strip()
        if len(snippet) > 200:
            snippet = snippet[:197] + "..."
        if snippet:
            lines.append(f"  Notes: {snippet}")

    if task.updated:
        lines.append(f"  Updated: {task.updated}")

    return lines


# ---------------------------------------------------------------------------
# MCP tools — Calendar events
# ---------------------------------------------------------------------------


@mcp.tool("calendar_get_events")
async def calendar_get_events(
    user_email: str = DEFAULT_USER_EMAIL,
    calendar_id: Optional[str] = None,
    time_min: Optional[str] = None,
    time_max: Optional[str] = None,
    max_events: int = 200,
) -> str:
    """List calendar events and scheduled tasks for a time window.

    Return all events and scheduled tasks (tasks with due dates) in JSON format.
    Infer the most reasonable time window from context.

    All returned event times are in America/New_York (Eastern Time).
    Provide time_min/time_max as ISO dates (YYYY-MM-DD) or datetimes.
    Naive datetimes are interpreted as Eastern Time.
    """
    try:
        service = get_calendar_service(user_email)

        time_min_rfc = parse_iso_time_string(time_min)
        time_max_rfc = parse_iso_time_string(time_max)

        if not time_min_rfc and not time_max_rfc:
            now_utc = datetime.datetime.now(datetime.timezone.utc).replace(
                microsecond=0
            )
            time_min_rfc = normalize_rfc3339(now_utc)
            time_max_rfc = normalize_rfc3339(now_utc + datetime.timedelta(days=30))

        range_min_dt = parse_rfc3339_datetime(time_min_rfc) if time_min_rfc else None
        range_max_dt = parse_rfc3339_datetime(time_max_rfc) if time_max_rfc else None

        aggregate = _should_use_aggregate(calendar_id)
        if aggregate:
            calendars_to_query = list(DEFAULT_READ_CALENDAR_IDS)
        else:
            if calendar_id is None:
                calendars_to_query = list(DEFAULT_READ_CALENDAR_IDS)
                aggregate = True
            else:
                resolved_calendar_id = _normalize_calendar_id(calendar_id)
                calendars_to_query = [resolved_calendar_id]

        params: dict[str, Any] = {
            "maxResults": 250,
            "orderBy": "startTime",
            "singleEvents": True,
            "timeZone": EASTERN_TIMEZONE_NAME,
        }

        if time_min_rfc:
            params["timeMin"] = time_min_rfc
        if time_max_rfc:
            params["timeMax"] = time_max_rfc

        events_with_keys: list[tuple[datetime.datetime, EventInfo]] = []
        warnings: list[str] = []

        async def _fetch_calendar_events(
            cal_id: str,
        ) -> tuple[str, list[dict[str, Any]] | None, Exception | None]:
            collected: list[dict[str, Any]] = []
            page_token: str | None = None
            internal_limit = max(max_events, 1) * 4

            while True:
                try:
                    request = service.events().list(
                        calendarId=cal_id, pageToken=page_token, **params
                    )
                except Exception as exc:
                    return (cal_id, None, exc)

                try:
                    events_result = await asyncio.to_thread(request.execute)
                except Exception as exc:
                    return (cal_id, None, exc)

                collected.extend(events_result.get("items", []))
                page_token = events_result.get("nextPageToken")
                if not page_token or len(collected) >= internal_limit:
                    break

            return (cal_id, collected, None)

        fetch_results: list[
            tuple[str, list[dict[str, Any]] | None, Exception | None]
        ] = []
        if calendars_to_query:
            fetch_results = await asyncio.gather(
                *(_fetch_calendar_events(cal_id) for cal_id in calendars_to_query)
            )

        for cal_id, events_result, error in fetch_results:
            if error:
                warnings.append(f"{_calendar_label(cal_id)}: {error}")
                continue

            if not events_result:
                continue

            calendar_name = _calendar_label(cal_id)

            for event in events_result:
                start = event.get("start", {})
                end = event.get("end", {})

                is_all_day = "date" in start

                event_start = start.get("date", start.get("dateTime", "")) or ""
                event_end = end.get("date", end.get("dateTime", "")) or event_start

                event_info = EventInfo(
                    title=event.get("summary", "(No title)"),
                    start=event_start,
                    end=event_end,
                    is_all_day=is_all_day,
                    calendar=calendar_name,
                    calendar_id=cal_id,
                    id=event.get("id", ""),
                    link=event.get("htmlLink", ""),
                    location=event.get("location", None),
                    description=_truncate_text(event.get("description", None), 500),
                    ical_uid=event.get("iCalUID"),
                )

                if not _overlaps_window(event_info, range_min_dt, range_max_dt):
                    continue

                events_with_keys.append(
                    (_event_sort_key(event_info.start), event_info)
                )

        events_with_keys.sort(key=lambda item: item[0])
        ordered_events = [event for _, event in events_with_keys]

        truncated_events = len(ordered_events) > max_events
        selected_events = ordered_events[:max_events]

        # Collect scheduled tasks
        tasks_payload: list[dict[str, Any]] = []
        truncated_tasks = False
        task_service = TaskService(user_email, service_factory=get_tasks_service)

        async def _collect_tasks_for_window(
            max_tasks: int,
        ) -> tuple[list[Task], bool]:
            collected_tasks: list[Task] = []
            list_page_token: Optional[str] = None
            truncated = False

            while True:
                lists, list_page_token = await task_service.list_task_lists(
                    max_results=100, page_token=list_page_token
                )
                for task_list in lists:
                    task_page_token: Optional[str] = None
                    while True:
                        tasks, task_page_token = await task_service.list_tasks(
                            task_list.id,
                            max_results=100,
                            page_token=task_page_token,
                            show_completed=False,
                            show_deleted=False,
                            show_hidden=False,
                            due_min=None,
                            due_max=time_max_rfc,
                        )

                        collected_tasks.extend(tasks)
                        if len(collected_tasks) >= max_tasks:
                            truncated = True
                            break

                        if not task_page_token:
                            break

                    if len(collected_tasks) >= max_tasks:
                        break

                if len(collected_tasks) >= max_tasks:
                    break
                if not list_page_token:
                    break

            return collected_tasks, truncated

        try:
            collected_tasks, truncated_tasks = await _collect_tasks_for_window(
                max_events
            )
        except TaskAuthorizationError as exc:
            warnings.append(f"Tasks unavailable: {exc}.")
            collected_tasks = []
        except TaskServiceError as exc:
            warnings.append(f"Tasks unavailable: {exc}.")
            collected_tasks = []

        for task in collected_tasks[:max_events]:
            due_value: Optional[str]
            if task.due:
                due_value = normalize_rfc3339(task.due)
            else:
                due_value = None

            tasks_payload.append(
                {
                    "id": task.id,
                    "title": task.title,
                    "due": due_value,
                    "status": task.status,
                    "list_title": task.list_title,
                    "web_link": task.web_link,
                    "notes": _truncate_text(task.notes, 500),
                }
            )

        events_payload = [
            {
                "id": event.id,
                "summary": event.title,
                "start": event.start,
                "end": event.end,
                "is_all_day": event.is_all_day,
                "calendar_id": event.calendar_id,
                "calendar_label": event.calendar,
                "html_link": event.link,
                "location": event.location,
                "description": event.description,
            }
            for event in selected_events
        ]

        payload = {
            "events": events_payload,
            "tasks": tasks_payload,
            "meta": {
                "user_email": user_email,
                "time_min": time_min_rfc,
                "time_max": time_max_rfc,
                "calendar_ids": calendars_to_query,
                "truncated": truncated_events or truncated_tasks,
                "warnings": warnings,
            },
        }

        return json.dumps(payload)

    except ValueError as e:
        return f"Authentication error: {e}."
    except Exception as e:
        return f"Error retrieving calendar events: {e}"


@mcp.tool("calendar_create_event")
async def create_event(
    user_email: str = DEFAULT_USER_EMAIL,
    *,
    summary: str,
    start_time: str,
    end_time: str,
    calendar_id: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[List[str]] = None,
) -> str:
    """Create a new calendar event.

    All-day events: use date-only strings (YYYY-MM-DD) for start_time and end_time.
    Timed events: use ISO datetime (YYYY-MM-DDTHH:MM:SS) in Eastern Time
    (America/New_York). The server handles timezone conversion automatically.
    """
    try:
        service = get_calendar_service(user_email)

        resolved_calendar_id = _resolve_calendar_id_for_write(calendar_id)
        calendar_label_str = _calendar_label(resolved_calendar_id)

        is_all_day = "T" not in start_time and ":" not in start_time

        event: dict[str, Any] = {
            "summary": summary,
            "description": description,
            "location": location,
        }

        if is_all_day:
            event["start"] = {"date": start_time}
            event["end"] = {"date": end_time}
        else:
            start_formatted = parse_iso_time_string(start_time)
            end_formatted = parse_iso_time_string(end_time)
            event["start"] = {
                "dateTime": start_formatted,
                "timeZone": EASTERN_TIMEZONE_NAME,
            }
            event["end"] = {
                "dateTime": end_formatted,
                "timeZone": EASTERN_TIMEZONE_NAME,
            }

        if attendees:
            event["attendees"] = [{"email": email} for email in attendees]

        event["reminders"] = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 10}],
        }

        created_event = await asyncio.to_thread(
            service.events()
            .insert(calendarId=resolved_calendar_id, body=event)
            .execute
        )

        timing = (
            f"all day event on {start_time}"
            if is_all_day
            else f"from {start_time} to {end_time}"
        )

        event_id = created_event.get("id", "")
        event_link = created_event.get("htmlLink", "")

        return (
            f"Successfully created event '{summary}' ({timing}) in calendar "
            f"'{calendar_label_str}' (ID: {resolved_calendar_id}). "
            f"Event ID: {event_id} | Link: {event_link}"
        )

    except ValueError as e:
        return f"Authentication error: {e}."
    except Exception as e:
        return f"Error creating calendar event: {e}"


@mcp.tool("calendar_update_event")
async def update_event(
    user_email: str = DEFAULT_USER_EMAIL,
    *,
    event_id: str,
    calendar_id: Optional[str] = None,
    summary: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[List[str]] = None,
) -> str:
    """Update an existing calendar event.

    Provide times in Eastern Time (America/New_York) as ISO datetimes
    (YYYY-MM-DDTHH:MM:SS) or dates (YYYY-MM-DD) for all-day events.
    """
    try:
        service = get_calendar_service(user_email)

        resolved_calendar_id = _resolve_calendar_id_for_write(calendar_id)
        calendar_label_str = _calendar_label(resolved_calendar_id)

        event = await asyncio.to_thread(
            service.events()
            .get(calendarId=resolved_calendar_id, eventId=event_id)
            .execute
        )

        if summary:
            event["summary"] = summary
        if description is not None:
            event["description"] = description
        if location is not None:
            event["location"] = location

        if start_time and end_time:
            is_all_day = "T" not in start_time and ":" not in start_time
            if is_all_day:
                event["start"] = {"date": start_time}
                event["end"] = {"date": end_time}
            else:
                start_formatted = parse_iso_time_string(start_time)
                end_formatted = parse_iso_time_string(end_time)
                event["start"] = {
                    "dateTime": start_formatted,
                    "timeZone": EASTERN_TIMEZONE_NAME,
                }
                event["end"] = {
                    "dateTime": end_formatted,
                    "timeZone": EASTERN_TIMEZONE_NAME,
                }

        if attendees is not None:
            event["attendees"] = [{"email": email} for email in attendees]

        updated_event = await asyncio.to_thread(
            service.events()
            .update(calendarId=resolved_calendar_id, eventId=event_id, body=event)
            .execute
        )

        event_link = updated_event.get("htmlLink", "")

        return (
            f"Successfully updated event '{updated_event.get('summary')}' "
            f"in calendar '{calendar_label_str}' (ID: {resolved_calendar_id}). "
            f"Link: {event_link}"
        )

    except ValueError as e:
        return f"Authentication error: {e}."
    except Exception as e:
        return f"Error updating calendar event: {e}"


@mcp.tool("calendar_delete_event")
async def delete_event(
    user_email: str = DEFAULT_USER_EMAIL,
    *,
    event_id: str,
    calendar_id: Optional[str] = None,
) -> str:
    """Delete a calendar event by ID."""
    try:
        service = get_calendar_service(user_email)

        resolved_calendar_id = _resolve_calendar_id_for_write(calendar_id)
        calendar_label_str = _calendar_label(resolved_calendar_id)

        event = await asyncio.to_thread(
            service.events()
            .get(calendarId=resolved_calendar_id, eventId=event_id)
            .execute
        )

        event_summary = event.get("summary", "Unknown event")

        await asyncio.to_thread(
            service.events()
            .delete(calendarId=resolved_calendar_id, eventId=event_id)
            .execute
        )

        return (
            f"Successfully deleted event '{event_summary}' from calendar "
            f"'{calendar_label_str}' (ID: {resolved_calendar_id})."
        )

    except ValueError as e:
        return f"Authentication error: {e}."
    except Exception as e:
        return f"Error deleting calendar event: {e}"


@mcp.tool("calendar_list_calendars")
async def list_calendars(
    user_email: str = DEFAULT_USER_EMAIL, max_results: int = 100
) -> str:
    """List calendars the user has access to."""
    try:
        service = get_calendar_service(user_email)
    except ValueError as exc:
        return f"Authentication error: {exc}."
    except Exception as exc:
        return f"Error creating calendar service: {exc}"

    try:
        calendars: list[dict[str, str]] = []
        page_token: Optional[str] = None

        while len(calendars) < max_results:
            remaining = max_results - len(calendars)
            cal_params: dict[str, Any] = {"maxResults": min(250, remaining)}
            if page_token:
                cal_params["pageToken"] = page_token

            response = await asyncio.to_thread(
                service.calendarList().list(**cal_params).execute
            )

            items = response.get("items", [])
            for item in items:
                calendars.append(
                    {
                        "name": item.get("summary", "(Unnamed calendar)"),
                        "id": item.get("id", ""),
                        "primary": "true" if item.get("primary") else "false",
                        "access_role": item.get("accessRole", "unknown"),
                    }
                )
                if len(calendars) >= max_results:
                    break

            page_token = response.get("nextPageToken")
            if not page_token or len(calendars) >= max_results:
                break

        if not calendars:
            return f"No calendars found for {user_email}."

        lines = [f"Found {len(calendars)} calendars for {user_email}:"]
        for calendar in calendars:
            primary_marker = " [primary]" if calendar["primary"] == "true" else ""
            lines.append(
                f'- "{calendar["name"]}"{primary_marker} '
                f'(ID: {calendar["id"]}, access: {calendar["access_role"]})'
            )

        if page_token:
            lines.append(
                "Additional calendars exist; rerun with a higher max_results."
            )

        return "\n".join(lines)
    except Exception as exc:
        return f"Error listing calendars: {exc}"


# ---------------------------------------------------------------------------
# MCP tools — Google Tasks
# ---------------------------------------------------------------------------


@mcp.tool("calendar_list_task_lists")
async def list_task_lists(
    user_email: str = DEFAULT_USER_EMAIL,
    max_results: int = 100,
    page_token: Optional[str] = None,
) -> str:
    """List Google Task lists available to the user."""
    try:
        task_service = TaskService(user_email, service_factory=get_tasks_service)
        task_lists, next_page = await task_service.list_task_lists(
            max_results=max_results, page_token=page_token
        )
    except TaskAuthorizationError as exc:
        return f"Authentication error: {exc}."
    except TaskServiceError as exc:
        return str(exc)

    if not task_lists:
        return f"No task lists found for {user_email}."

    lines = [f"Task lists for {user_email}:"]
    for task_list in task_lists:
        lines.append(f"- {task_list.title} (ID: {task_list.id})")
        lines.append(f"  Updated: {task_list.updated or 'N/A'}")

    if next_page:
        lines.append(f"Next page token: {next_page}")

    return "\n".join(lines)


@mcp.tool("calendar_list_tasks")
async def list_tasks(
    user_email: str = DEFAULT_USER_EMAIL,
    task_list_id: Optional[str] = None,
    max_results: Optional[int] = 100,
    page_token: Optional[str] = None,
    show_completed: bool = False,
    show_deleted: bool = False,
    show_hidden: bool = False,
    due_min: Optional[str] = None,
    due_max: Optional[str] = None,
    task_filter: str = "all",
) -> str:
    """List tasks from a single task list.

    If task_list_id is not provided, returns a list of available lists.
    task_filter: 'all', 'scheduled', 'unscheduled', 'overdue', or 'upcoming'.
    """
    try:
        task_service = TaskService(user_email, service_factory=get_tasks_service)
    except TaskAuthorizationError as exc:
        return f"Authentication error: {exc}."
    except TaskServiceError as exc:
        return str(exc)

    normalized_filter = (task_filter or "all").strip().lower()
    allowed_filters = {"all", "scheduled", "unscheduled", "overdue", "upcoming"}
    if normalized_filter not in allowed_filters:
        return (
            "Invalid task_filter. Supported: all, scheduled, unscheduled, "
            "overdue, upcoming."
        )

    if not task_list_id:
        try:
            task_lists_result, _ = await task_service.list_task_lists(max_results=50)
        except TaskAuthorizationError as exc:
            return f"Authentication error: {exc}."
        except TaskServiceError as exc:
            return str(exc)

        if not task_lists_result:
            return f"No task lists found for {user_email}."

        lines = [
            f"Task lists for {user_email}. Specify one when calling list_tasks:",
        ]
        for tl in task_lists_result:
            lines.append(f"- {tl.title} (ID: {tl.id})")
        return "\n".join(lines)

    try:
        resolved = await _resolve_task_list_identifier(task_service, task_list_id)
    except TaskServiceError as exc:
        return str(exc)

    if resolved is None:
        effective_list_id, list_label = task_list_id, task_list_id
    else:
        effective_list_id, list_label = resolved

    collected: List[Task] = []
    token = page_token
    next_token: Optional[str] = None

    def apply_filter(tasks: List[Task]) -> List[Task]:
        now = datetime.datetime.now(datetime.timezone.utc)
        if normalized_filter == "scheduled":
            return [t for t in tasks if t.due is not None]
        if normalized_filter == "unscheduled":
            return [t for t in tasks if t.due is None]
        if normalized_filter == "overdue":
            return [t for t in tasks if t.due and t.due < now]
        if normalized_filter == "upcoming":
            return [t for t in tasks if t.due and t.due >= now]
        return list(tasks)

    target_count = max_results if max_results is not None else None

    while True:
        per_page = 100 if target_count is None else max(1, min(target_count, 100))

        try:
            page_tasks, next_token = await task_service.list_tasks(
                effective_list_id,
                max_results=per_page,
                page_token=token,
                show_completed=show_completed,
                show_deleted=show_deleted,
                show_hidden=show_hidden,
                due_min=due_min,
                due_max=due_max,
            )
        except TaskAuthorizationError as exc:
            return f"Authentication error: {exc}."
        except TaskServiceError as exc:
            return str(exc)

        collected.extend(page_tasks)
        filtered = apply_filter(collected)

        if target_count is not None and len(filtered) >= target_count:
            break
        if not next_token:
            break
        token = next_token

    if not filtered:
        msg = {
            "all": "No tasks",
            "scheduled": "No scheduled tasks",
            "unscheduled": "No unscheduled tasks",
            "overdue": "No overdue tasks",
            "upcoming": "No upcoming tasks",
        }[normalized_filter]
        return f"{msg} in '{list_label}' for {user_email}."

    filtered.sort(
        key=lambda t: (
            t.due or datetime.datetime.max.replace(tzinfo=datetime.timezone.utc),
            t.title.lower(),
        )
    )

    display = filtered[:target_count] if target_count else filtered
    has_more = (target_count and len(filtered) > len(display)) or next_token

    header = f"Tasks in '{list_label}' for {user_email}: {len(display)} shown"
    if normalized_filter != "all":
        header += f" (filter: {normalized_filter})."
    else:
        header += "."

    lines = [header]

    for task in display:
        due_text = f"Due {normalize_rfc3339(task.due)}" if task.due else "Unscheduled"
        lines.append(f"- {task.title} [{task.status}] ({due_text}) ID: {task.id}")
        if task.notes:
            lines.append(f"  Notes: {task.notes}")
        if task.web_link:
            lines.append(f"  Link: {task.web_link}")

    if has_more and next_token:
        lines.append(f"Next page token: {next_token}")
    elif has_more:
        lines.append("Additional tasks available; adjust max_results.")

    return "\n".join(lines)


@mcp.tool("calendar_search_all_tasks")
async def search_all_tasks(
    user_email: str = DEFAULT_USER_EMAIL,
    query: str = "",
    task_list_id: Optional[str] = None,
    max_results: Optional[int] = 100,
    include_completed: bool = False,
    include_hidden: bool = False,
    include_deleted: bool = False,
    search_notes: bool = True,
    due_min: Optional[str] = None,
    due_max: Optional[str] = None,
) -> str:
    """Search Google Tasks across multiple lists.

    Prefer simple keyword queries (e.g., "books"). If query is empty, returns
    a general overview.
    """
    trimmed_query = (query or "").strip()
    general_search = not trimmed_query

    try:
        task_service = TaskService(user_email, service_factory=get_tasks_service)
        effective_max_results = max_results if max_results is not None else 100
        search_response = await task_service.search_tasks(
            trimmed_query,
            task_list_id=task_list_id,
            max_results=effective_max_results,
            include_completed=include_completed,
            include_hidden=include_hidden,
            include_deleted=include_deleted,
            search_notes=search_notes,
            due_min=due_min,
            due_max=due_max,
        )
    except TaskAuthorizationError as exc:
        return f"Authentication error: {exc}."
    except TaskServiceError as exc:
        return str(exc)

    matches = search_response.matches
    if not matches:
        if search_response.warnings:
            warning_text = "; ".join(search_response.warnings)
            if general_search:
                return (
                    f"No tasks found for {user_email} across available lists. "
                    f"Warnings: {warning_text}."
                )
            return (
                f"No tasks matched query '{trimmed_query}' for {user_email}. "
                f"Warnings: {warning_text}."
            )
        if general_search:
            return f"No tasks found for {user_email} across available lists."
        return f"No tasks matched query '{trimmed_query}' for {user_email}."

    if general_search:
        header = (
            f"Task overview for {user_email}: {len(matches)} item"
            + ("s" if len(matches) != 1 else "")
            + " highlighted."
        )
    else:
        header = (
            f"Task search for {user_email}: {len(matches)} match"
            + ("es" if len(matches) != 1 else "")
            + f" for '{trimmed_query}'."
        )

    header_lines = [header]

    if not task_list_id and search_response.scanned_lists:
        header_lines.append(
            "Task lists scanned: " + ", ".join(search_response.scanned_lists)
        )
    elif task_list_id and search_response.scanned_lists:
        header_lines.append(f"Task list: {search_response.scanned_lists[0]}")

    for task in matches:
        header_lines.extend(_format_task_search_result(task))

    if search_response.truncated:
        header_lines.append(
            f"(+{search_response.truncated} additional matches not shown; "
            "increase max_results or refine the query.)"
        )

    if search_response.warnings:
        header_lines.append("Warnings:")
        for warning in search_response.warnings:
            header_lines.append(f"- {warning}")

    return "\n".join(header_lines)


@mcp.tool("calendar_get_task")
async def get_task(
    user_email: str = DEFAULT_USER_EMAIL,
    task_list_id: str = "@default",
    task_id: str = "",
) -> str:
    """Retrieve a specific task by ID."""
    try:
        task_service = TaskService(user_email, service_factory=get_tasks_service)
        task = await task_service.get_task(task_list_id, task_id)
    except TaskAuthorizationError as exc:
        return f"Authentication error: {exc}."
    except TaskServiceError as exc:
        return str(exc)

    lines = [
        f"Task details for {user_email}:",
        f"- Title: {task.title}",
        f"- ID: {task.id or task_id}",
        f"- Status: {task.status}",
        f"- Updated: {task.updated or 'N/A'}",
    ]

    if task.due:
        lines.append(f"- Due: {normalize_rfc3339(task.due)}")
    if task.completed:
        lines.append(f"- Completed: {task.completed}")
    if task.notes:
        lines.append(f"- Notes: {task.notes}")
    if task.parent:
        lines.append(f"- Parent: {task.parent}")
    if task.position:
        lines.append(f"- Position: {task.position}")
    if task.web_link:
        lines.append(f"- Link: {task.web_link}")

    return "\n".join(lines)


@mcp.tool("calendar_create_task")
async def create_task(
    user_email: str = DEFAULT_USER_EMAIL,
    task_list_id: str = "@default",
    *,
    title: str,
    notes: Optional[str] = None,
    due: Optional[str] = None,
    parent: Optional[str] = None,
    previous: Optional[str] = None,
) -> str:
    """Create a new Google Task.

    Set due to schedule the task.  Accepted formats:
    - Keywords: "today", "tomorrow", "next_week", "next_month", "next_year"
    - Date: YYYY-MM-DD (in Eastern Time / America/New_York)
    Note: Google Tasks only stores the date portion; time-of-day is discarded.
    When scheduling EXISTING tasks, use calendar_update_task to avoid duplicates.
    """
    try:
        task_service = TaskService(user_email, service_factory=get_tasks_service)
        created = await task_service.create_task(
            task_list_id,
            title=title,
            notes=notes,
            due=due,
            parent=parent,
            previous=previous,
        )
    except TaskAuthorizationError as exc:
        return f"Authentication error: {exc}."
    except TaskServiceError as exc:
        return str(exc)

    lines = [
        f"Created task '{created.title}' in list {task_list_id}:",
        f"- ID: {created.id or '(unknown)'}",
        f"- Status: {created.status}",
        f"- Updated: {created.updated or 'N/A'}",
    ]

    if created.due:
        lines.append(f"- Due: {normalize_rfc3339(created.due)}")
    if created.web_link:
        lines.append(f"- Link: {created.web_link}")

    return "\n".join(lines)


@mcp.tool("calendar_update_task")
async def update_task(
    user_email: str = DEFAULT_USER_EMAIL,
    task_list_id: str = "@default",
    task_id: str = "",
    *,
    title: Optional[str] = None,
    notes: Optional[str] = None,
    status: Optional[str] = None,
    due: Optional[str] = None,
) -> str:
    """Update an existing task.

    Use this to schedule an existing task by adding/updating due.
    due accepts: keywords ("today", "tomorrow"), or YYYY-MM-DD in Eastern Time.
    Google Tasks only stores the date portion; time-of-day is discarded.
    Status should be 'needsAction' or 'completed'.
    """
    try:
        task_service = TaskService(user_email, service_factory=get_tasks_service)
        updated_task = await task_service.update_task(
            task_list_id,
            task_id,
            title=title,
            notes=notes,
            status=status,
            due=due,
        )
    except TaskAuthorizationError as exc:
        return f"Authentication error: {exc}."
    except TaskServiceError as exc:
        return str(exc)

    lines = [
        f"Updated task '{updated_task.title}' (ID: {updated_task.id or task_id}):",
        f"- Status: {updated_task.status}",
        f"- Updated: {updated_task.updated or 'N/A'}",
    ]

    if updated_task.due:
        lines.append(f"- Due: {normalize_rfc3339(updated_task.due)}")
    if updated_task.completed:
        lines.append(f"- Completed: {updated_task.completed}")
    if updated_task.web_link:
        lines.append(f"- Link: {updated_task.web_link}")

    return "\n".join(lines)


@mcp.tool("calendar_delete_task")
async def delete_task(
    user_email: str = DEFAULT_USER_EMAIL,
    task_list_id: str = "@default",
    task_id: str = "",
) -> str:
    """Delete a task from a given list."""
    try:
        task_service = TaskService(user_email, service_factory=get_tasks_service)
        await task_service.delete_task(task_list_id, task_id)
    except TaskAuthorizationError as exc:
        return f"Authentication error: {exc}."
    except TaskServiceError as exc:
        return str(exc)

    return f"Task {task_id} deleted from list {task_list_id}."


@mcp.tool("calendar_move_task")
async def move_task(
    user_email: str = DEFAULT_USER_EMAIL,
    task_list_id: str = "@default",
    task_id: str = "",
    parent: Optional[str] = None,
    previous: Optional[str] = None,
    destination_task_list: Optional[str] = None,
) -> str:
    """Move or reparent a task within or between lists."""
    try:
        task_service = TaskService(user_email, service_factory=get_tasks_service)
        moved = await task_service.move_task(
            task_list_id,
            task_id,
            parent=parent,
            previous=previous,
            destination_task_list=destination_task_list,
        )
    except TaskAuthorizationError as exc:
        return f"Authentication error: {exc}."
    except TaskServiceError as exc:
        return str(exc)

    lines = [f"Moved task '{moved.title}' (ID: {moved.id or task_id})."]

    if moved.parent:
        lines.append(f"- Parent: {moved.parent}")
    if moved.position:
        lines.append(f"- Position: {moved.position}")
    if destination_task_list:
        lines.append(f"- Destination list: {destination_task_list}")

    return "\n".join(lines)


@mcp.tool("calendar_clear_completed_tasks")
async def clear_completed_tasks(
    user_email: str = DEFAULT_USER_EMAIL,
    task_list_id: str = "@default",
) -> str:
    """Clear all completed tasks from a task list (they become hidden)."""
    try:
        task_service = TaskService(user_email, service_factory=get_tasks_service)
        await task_service.clear_completed_tasks(task_list_id)
    except TaskAuthorizationError as exc:
        return f"Authentication error: {exc}."
    except TaskServiceError as exc:
        return str(exc)

    return (
        f"Completed tasks cleared from list {task_list_id}. "
        "Hidden tasks may take a moment to disappear from other clients."
    )


@mcp.tool("calendar_delete_task_list")
async def delete_task_list(
    user_email: str = DEFAULT_USER_EMAIL,
    task_list_id: str = "",
) -> str:
    """Delete a task list and all tasks within it.

    Warning: This permanently deletes the task list. The user's default/primary
    task list cannot be deleted. Use calendar_list_task_lists first to get the ID.
    """
    if not task_list_id:
        return (
            "Error: task_list_id is required. "
            "Use calendar_list_task_lists to find the ID."
        )

    try:
        task_service = TaskService(user_email, service_factory=get_tasks_service)
        try:
            list_info = await task_service.get_task_list(task_list_id)
            list_name = list_info.title
        except TaskServiceError:
            list_name = task_list_id

        await task_service.delete_task_list(task_list_id)
    except TaskAuthorizationError as exc:
        return f"Authentication error: {exc}."
    except TaskServiceError as exc:
        return str(exc)

    return f"Successfully deleted task list '{list_name}' (ID: {task_list_id})."


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def run(
    transport: str = "stdio",
    host: str = "0.0.0.0",
    port: int = DEFAULT_HTTP_PORT,
) -> None:  # pragma: no cover - integration entrypoint
    """Run the Calendar MCP server with the specified transport."""
    if transport == "streamable-http":
        mcp.run(
            transport="streamable-http",
            host=host,
            port=port,
            json_response=True,
            stateless_http=True,
            uvicorn_config={"access_log": False},
        )
    else:
        mcp.run(transport="stdio")


def main() -> None:  # pragma: no cover - CLI helper
    import argparse

    parser = argparse.ArgumentParser(description="Calendar MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport protocol to use",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind HTTP server to",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help="Port for HTTP server",
    )
    args = parser.parse_args()
    run(args.transport, args.host, args.port)


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "mcp",
    "run",
    "main",
    "DEFAULT_HTTP_PORT",
    "calendar_get_events",
    "create_event",
    "update_event",
    "delete_event",
    "list_calendars",
    "list_task_lists",
    "list_tasks",
    "search_all_tasks",
    "get_task",
    "create_task",
    "update_task",
    "delete_task",
    "move_task",
    "clear_completed_tasks",
    "delete_task_list",
]
