"""Indexed Hue history query implementations for the MCP facade."""

from __future__ import annotations

import json
from typing import Any

from shared.hue_store import VALID_LOG_KINDS, HueStore, configured_db_path


def _state_text(state: dict[str, Any]) -> str:
    parts: list[str] = []
    if state.get("on") is True:
        parts.append("ON")
    elif state.get("on") is False:
        parts.append("off")
    if state.get("brightness") is not None:
        parts.append(f"{state['brightness']:.0f}%")
    if state.get("mirek") is not None:
        parts.append(f"mirek={state['mirek']}")
    if "color" in state:
        parts.append("color")
    return " ".join(parts) if parts else "state unknown"


def _format_record(kind: str, record: dict[str, Any]) -> str:
    ts = record.get("ts", "?")
    if kind == "activity":
        category = record.get("category") or record.get("resource_type") or "event"
        return f"{ts}  [{category}]  {record.get('summary') or record.get('action') or 'event'}"
    if kind == "changes":
        changed = ", ".join(record.get("changed") or []) or "change"
        return (
            f"{ts}  {record.get('name') or record.get('rid') or '?'}  {changed}  "
            f"{_state_text(record.get('before') or {})} -> "
            f"{_state_text(record.get('after') or {})}"
        )
    if kind == "health":
        details = [ts, record.get("kind", "?")]
        if record.get("error"):
            details.append(str(record["error"]))
        if record.get("reason"):
            details.append(f"reason={record['reason']}")
        if record.get("differences") is not None:
            details.append(f"differences={record['differences']}")
        return "  ".join(details)
    if kind == "commands":
        target = f" {record['target']}" if record.get("target") else ""
        suffix = f" error={record['error']}" if record.get("error") else ""
        if record.get("result"):
            suffix += f" result={str(record['result'])[:300]}"
        return f"{ts}  {record.get('tool', '?')}{target}  {record.get('outcome', '?')}{suffix}"
    if kind == "state":
        return (
            f"{ts}  {record.get('name') or record.get('rid') or '?'}  "
            f"{_state_text(record.get('state') or {})}"
        )
    name = record.get("name") or record.get("resource_type") or "event"
    return f"{ts}  {name}  {json.dumps(record, ensure_ascii=False)[:500]}"


async def _log_recent_impl(
    query: str | None = None,
    light: str | None = None,
    kind: str = "activity",
    category: str | None = None,
    minutes: int = 60,
    limit: int = 50,
    date: str | None = None,
) -> str:
    if kind not in VALID_LOG_KINDS:
        return f"Unknown kind '{kind}'. Use one of: {', '.join(VALID_LOG_KINDS)}."
    if limit < 1:
        return "limit must be at least 1."
    path = configured_db_path()
    if not path.exists():
        return f"Hue event database not found: {path}"
    effective_query = query if query is not None else light
    try:
        with HueStore(path, create=False) as store:
            records = store.query(
                kind,
                minutes=minutes,
                limit=limit,
                date=date,
                category=category if kind == "activity" else None,
                query=effective_query,
            )
    except ValueError:
        return f"Invalid date '{date}'. Use YYYY-MM-DD."
    if not records:
        scope = f"date {date}" if date else f"last {minutes} minutes"
        return f"No Hue {kind} records found for {scope}."
    lines = [f"Hue {kind} ({len(records)} shown, newest last):"]
    lines.extend(_format_record(kind, record) for record in records)
    return "\n".join(lines)


async def log_recent(
    query: str | None = None,
    light: str | None = None,
    kind: str = "activity",
    category: str | None = None,
    minutes: int = 60,
    limit: int = 50,
    date: str | None = None,
) -> str:
    """Show indexed Hue history.

    Args:
        query: Optional case-insensitive text filter.
        light: Alias for query.
        kind: One of activity, changes, events, health, commands, state.
        category: Optional activity category filter.
        minutes: Look back this many minutes; ignored when date is supplied.
        limit: Maximum rows.
        date: Optional local date as YYYY-MM-DD.
    """
    return await _log_recent_impl(query, light, kind, category, minutes, limit, date)


async def log_status() -> str:
    """Summarize the Hue event database and collector health."""
    path = configured_db_path()
    if not path.exists():
        return f"Hue event database not found: {path}"
    with HueStore(path, create=False) as store:
        status = store.status()
        health = store.query("health", minutes=7 * 24 * 60, limit=8)
    size_mb = (status["bytes"] + status["wal_bytes"]) / (1024 * 1024)
    lines = [
        f"Hue event database: {path}",
        f"Size: {size_mb:.1f} MB (database + WAL)",
        f"Rows: {status['counts']}",
        f"Events: {status['oldest_event'] or '?'} -> {status['newest_event'] or '?'}",
    ]
    if health:
        lines.append("\nRecent health:")
        lines.extend("  " + _format_record("health", record) for record in health)
    return "\n".join(lines)


async def log_activity(
    category: str | None = None,
    query: str | None = None,
    minutes: int = 180,
    limit: int = 80,
    date: str | None = None,
) -> str:
    """Show normalized Hue activity including inputs, sensors, scenes, and lights.

    Args:
        category: Optional activity category.
        query: Optional case-insensitive text filter.
        minutes: Look back this many minutes; ignored when date is supplied.
        limit: Maximum rows.
        date: Optional local date as YYYY-MM-DD.
    """
    return await _log_recent_impl(
        query=query,
        kind="activity",
        category=category,
        minutes=minutes,
        limit=limit,
        date=date,
    )


async def log_search(
    query: str,
    kind: str = "changes",
    date: str | None = None,
    limit: int = 50,
) -> str:
    """Search indexed Hue history by plain text.

    Args:
        query: Required case-insensitive text.
        kind: One of activity, changes, events, health, commands, state.
        date: Optional local date as YYYY-MM-DD.
        limit: Maximum matches.
    """
    if not query.strip():
        return "query is required."
    return await _log_recent_impl(query=query, kind=kind, date=date, limit=limit)
