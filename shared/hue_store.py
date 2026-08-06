"""SQLite persistence for Hue events, state, health, and command audits."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from shared.hue_model import compact_json

DEFAULT_DB_PATH = "/var/lib/hue-events/hue.sqlite3"
VALID_LOG_KINDS = ("activity", "changes", "events", "health", "commands", "state")


def configured_db_path() -> Path:
    return Path(os.environ.get("HUE_DB_PATH", DEFAULT_DB_PATH))


def configured_timezone() -> Any:
    name = os.environ.get("HUE_TIMEZONE", "America/New_York")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return datetime.now().astimezone().tzinfo


def now_iso() -> str:
    return datetime.now(configured_timezone()).isoformat(timespec="milliseconds")


def _encode(value: Any) -> str | None:
    return None if value is None else compact_json(value)


def _decode(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


class HueStore(AbstractContextManager["HueStore"]):
    """One local SQLite database with a single collector writer and MCP readers."""

    def __init__(self, path: Path | str | None = None, *, create: bool = True) -> None:
        self.path = Path(path) if path is not None else configured_db_path()
        if not create and not self.path.exists():
            raise FileNotFoundError(self.path)
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("PRAGMA foreign_keys=ON")
        if create:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=NORMAL")
            self._initialize()

    def __exit__(self, *_args: object) -> None:
        self.connection.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                bridge_creationtime TEXT,
                sse_id TEXT,
                batch_id TEXT,
                entry_index INTEGER,
                legacy_key TEXT UNIQUE,
                record_kind TEXT NOT NULL DEFAULT 'event',
                source TEXT NOT NULL DEFAULT 'bridge',
                event_type TEXT,
                resource_type TEXT,
                rid TEXT,
                name TEXT,
                category TEXT,
                action TEXT,
                value_json TEXT,
                summary TEXT,
                changed_json TEXT,
                before_json TEXT,
                after_json TEXT,
                raw_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE UNIQUE INDEX IF NOT EXISTS events_batch_entry
                ON events(batch_id, entry_index)
                WHERE batch_id IS NOT NULL AND entry_index IS NOT NULL;
            CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
            CREATE INDEX IF NOT EXISTS events_name_ts ON events(name, ts);
            CREATE INDEX IF NOT EXISTS events_category_ts ON events(category, ts);
            CREATE INDEX IF NOT EXISTS events_rid_ts ON events(rid, ts);

            CREATE TABLE IF NOT EXISTS current_state (
                rid TEXT PRIMARY KEY,
                resource_type TEXT NOT NULL,
                name TEXT NOT NULL,
                state_json TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS current_state_type_name
                ON current_state(resource_type, name);

            CREATE TABLE IF NOT EXISTS health (
                id INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                kind TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                legacy_key TEXT UNIQUE
            );
            CREATE INDEX IF NOT EXISTS health_ts ON health(ts);

            CREATE TABLE IF NOT EXISTS commands (
                id INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                completed_at TEXT,
                tool TEXT NOT NULL,
                target TEXT,
                request_json TEXT NOT NULL,
                outcome TEXT NOT NULL,
                result TEXT,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS commands_ts ON commands(ts);
            CREATE INDEX IF NOT EXISTS commands_tool_ts ON commands(tool, ts);

            CREATE TABLE IF NOT EXISTS imports (
                legacy_key TEXT PRIMARY KEY,
                imported_at TEXT NOT NULL
            );
            """
        )
        self.connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', '1')"
        )
        self.connection.commit()

    def insert_event(
        self,
        record: dict[str, Any],
        *,
        sse_id: str | None = None,
        batch_id: str | None = None,
        entry_index: int | None = None,
        record_kind: str = "event",
        source: str = "bridge",
        legacy_key: str | None = None,
    ) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO events (
                ts, bridge_creationtime, sse_id, batch_id, entry_index, legacy_key,
                record_kind, source, event_type, resource_type, rid, name, category,
                action, value_json, summary, changed_json, before_json, after_json, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["ts"],
                record.get("bridge_creationtime"),
                sse_id,
                batch_id,
                entry_index,
                legacy_key,
                record_kind,
                source,
                record.get("event_type"),
                record.get("resource_type"),
                record.get("rid"),
                record.get("name"),
                record.get("category"),
                record.get("action"),
                _encode(record.get("value")),
                record.get("summary"),
                _encode(record.get("changed")),
                _encode(record.get("before")),
                _encode(record.get("after")),
                _encode(record.get("raw")) or "{}",
            ),
        )
        return cursor.rowcount == 1

    def enrich_event_change(self, event_id: int, change: dict[str, Any]) -> None:
        self.connection.execute(
            """
            UPDATE events
            SET changed_json=?, before_json=?, after_json=?
            WHERE id=?
            """,
            (
                _encode(change.get("changed")),
                _encode(change.get("before")),
                _encode(change.get("after")),
                event_id,
            ),
        )

    def matching_event_id(
        self, record: dict[str, Any], *, exclude_source: str | None = None
    ) -> int | None:
        raw = record.get("raw")
        if raw is None:
            raw = record.get("raw_update")
        clauses = [
            "bridge_creationtime IS ?",
            "rid IS ?",
            "resource_type IS ?",
            "raw_json=?",
        ]
        params: list[Any] = [
            record.get("bridge_creationtime"),
            record.get("rid"),
            record.get("resource_type"),
            _encode(raw) or "{}",
        ]
        if exclude_source is not None:
            clauses.append("source<>?")
            params.append(exclude_source)
        row = self.connection.execute(
            f"SELECT id FROM events WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT 1",
            params,
        ).fetchone()
        return int(row["id"]) if row else None

    def has_import(self, legacy_key: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM imports WHERE legacy_key=?", (legacy_key,)
        ).fetchone()
        return row is not None

    def mark_import(self, legacy_key: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO imports(legacy_key, imported_at) VALUES (?, ?)",
            (legacy_key, now_iso()),
        )

    def upsert_current(
        self,
        rid: str,
        resource_type: str,
        name: str,
        state: dict[str, Any],
        raw: dict[str, Any],
        updated_at: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO current_state(rid, resource_type, name, state_json, raw_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(rid) DO UPDATE SET
                resource_type=excluded.resource_type,
                name=excluded.name,
                state_json=excluded.state_json,
                raw_json=excluded.raw_json,
                updated_at=excluded.updated_at
            """,
            (rid, resource_type, name, _encode(state) or "{}", _encode(raw) or "{}", updated_at),
        )

    def delete_current(self, rid: str) -> None:
        self.connection.execute("DELETE FROM current_state WHERE rid=?", (rid,))

    def load_current(self) -> dict[str, dict[str, Any]]:
        rows = self.connection.execute("SELECT rid, raw_json FROM current_state").fetchall()
        return {row["rid"]: _decode(row["raw_json"]) or {} for row in rows}

    def record_health(
        self, kind: str, *, ts: str | None = None, legacy_key: str | None = None, **details: Any
    ) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO health(ts, kind, details_json, legacy_key) VALUES (?, ?, ?, ?)",
            (ts or now_iso(), kind, _encode(details) or "{}", legacy_key),
        )

    def start_command(self, tool: str, request: dict[str, Any]) -> int:
        target = next(
            (
                str(request[key])
                for key in ("light", "room", "scene", "automation")
                if request.get(key)
            ),
            None,
        )
        cursor = self.connection.execute(
            """
            INSERT INTO commands(ts, tool, target, request_json, outcome)
            VALUES (?, ?, ?, ?, 'started')
            """,
            (now_iso(), tool, target, _encode(request) or "{}"),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def finish_command(
        self, command_id: int, outcome: str, *, result: str | None = None, error: str | None = None
    ) -> None:
        self.connection.execute(
            """
            UPDATE commands SET completed_at=?, outcome=?, result=?, error=? WHERE id=?
            """,
            (now_iso(), outcome, result, error, command_id),
        )
        self.connection.commit()

    def query(
        self,
        kind: str,
        *,
        minutes: int = 60,
        limit: int = 50,
        date: str | None = None,
        category: str | None = None,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        if kind not in VALID_LOG_KINDS:
            raise ValueError(kind)
        if kind == "health":
            return self._query_health(minutes, limit, date, query)
        if kind == "commands":
            return self._query_commands(minutes, limit, date, query)
        if kind == "state":
            return self._query_state(limit, query)

        clauses: list[str] = []
        params: list[Any] = []
        if kind == "changes":
            clauses.append("before_json IS NOT NULL AND after_json IS NOT NULL")
        if category:
            clauses.append("lower(category)=?")
            params.append(category.lower())
        self._append_time_filter(clauses, params, date, minutes)
        if query:
            clauses.append(
                "lower(coalesce(name,'') || ' ' || coalesce(summary,'') || ' ' || raw_json) LIKE ?"
            )
            params.append(f"%{query.lower()}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 200)))
        rows = self.connection.execute(
            f"SELECT * FROM events {where} ORDER BY ts DESC, id DESC LIMIT ?", params
        ).fetchall()
        return [self._event_record(row) for row in reversed(rows)]

    def _append_time_filter(
        self, clauses: list[str], params: list[Any], date: str | None, minutes: int
    ) -> None:
        if date:
            start = datetime.fromisoformat(date).replace(tzinfo=configured_timezone())
            end = start + timedelta(days=1)
            clauses.extend(("ts>=?", "ts<?"))
            params.extend((start.isoformat(), end.isoformat()))
        else:
            cutoff = datetime.now(configured_timezone()) - timedelta(minutes=max(1, minutes))
            clauses.append("ts>=?")
            params.append(cutoff.isoformat())

    def _query_health(
        self, minutes: int, limit: int, date: str | None, query: str | None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        self._append_time_filter(clauses, params, date, minutes)
        if query:
            clauses.append("lower(kind || ' ' || details_json) LIKE ?")
            params.append(f"%{query.lower()}%")
        params.append(max(1, min(limit, 200)))
        rows = self.connection.execute(
            f"SELECT * FROM health WHERE {' AND '.join(clauses)} ORDER BY ts DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [
            dict(ts=row["ts"], kind=row["kind"], **(_decode(row["details_json"]) or {}))
            for row in reversed(rows)
        ]

    def _query_commands(
        self, minutes: int, limit: int, date: str | None, query: str | None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        self._append_time_filter(clauses, params, date, minutes)
        if query:
            clauses.append(
                "lower(tool || ' ' || coalesce(target,'') || ' ' || request_json) LIKE ?"
            )
            params.append(f"%{query.lower()}%")
        params.append(max(1, min(limit, 200)))
        where = " AND ".join(clauses)
        rows = self.connection.execute(
            f"SELECT * FROM commands WHERE {where} ORDER BY ts DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [
            {
                "ts": row["ts"],
                "completed_at": row["completed_at"],
                "tool": row["tool"],
                "target": row["target"],
                "request": _decode(row["request_json"]),
                "outcome": row["outcome"],
                "result": row["result"],
                "error": row["error"],
            }
            for row in reversed(rows)
        ]

    def _query_state(self, limit: int, query: str | None) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if query:
            where = "WHERE lower(name || ' ' || resource_type || ' ' || raw_json) LIKE ?"
            params.append(f"%{query.lower()}%")
        params.append(max(1, min(limit, 500)))
        rows = self.connection.execute(
            f"SELECT * FROM current_state {where} ORDER BY name LIMIT ?", params
        ).fetchall()
        return [
            {
                "ts": row["updated_at"],
                "rid": row["rid"],
                "name": row["name"],
                "resource_type": row["resource_type"],
                "state": _decode(row["state_json"]),
                "raw": _decode(row["raw_json"]),
            }
            for row in rows
        ]

    def _event_record(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "ts": row["ts"],
            "bridge_creationtime": row["bridge_creationtime"],
            "event_type": row["event_type"],
            "resource_type": row["resource_type"],
            "rid": row["rid"],
            "name": row["name"],
            "category": row["category"],
            "action": row["action"],
            "value": _decode(row["value_json"]),
            "summary": row["summary"],
            "changed": _decode(row["changed_json"]),
            "before": _decode(row["before_json"]),
            "after": _decode(row["after_json"]),
            "raw": _decode(row["raw_json"]),
            "source": row["source"],
        }

    def status(self) -> dict[str, Any]:
        counts = {
            table: self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("events", "current_state", "health", "commands")
        }
        bounds = self.connection.execute("SELECT min(ts), max(ts) FROM events").fetchone()
        return {
            "path": str(self.path),
            "bytes": self.path.stat().st_size if self.path.exists() else 0,
            "wal_bytes": self.path.with_name(self.path.name + "-wal").stat().st_size
            if self.path.with_name(self.path.name + "-wal").exists()
            else 0,
            "counts": counts,
            "oldest_event": bounds[0],
            "newest_event": bounds[1],
        }

    def prune(self, retention_days: int) -> dict[str, int]:
        cutoff = (
            datetime.now(configured_timezone()) - timedelta(days=max(1, retention_days))
        ).isoformat()
        removed: dict[str, int] = {}
        with self.connection:
            for table in ("events", "health", "commands"):
                cursor = self.connection.execute(f"DELETE FROM {table} WHERE ts<?", (cutoff,))
                removed[table] = cursor.rowcount
        self.connection.execute("PRAGMA optimize")
        return removed
