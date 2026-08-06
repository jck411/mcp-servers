"""One-time importer from the retired Hue JSONL layout into SQLite."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from shared.hue_store import HueStore


def iter_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield line_number, record


def import_jsonl(log_dir: Path, store: HueStore) -> dict[str, int]:
    counts = {"activity": 0, "changes": 0, "health": 0, "unmatched_changes": 0}

    for path in sorted(log_dir.glob("hue_activity_*.jsonl")):
        with store.connection:
            for line_number, record in iter_jsonl(path):
                legacy_key = f"activity:{path.name}:{line_number}"
                if store.has_import(legacy_key):
                    continue
                # During cutover both collectors run briefly. Prefer the live
                # SSE row when the same Bridge event already reached SQLite.
                if store.matching_event_id(record, exclude_source="legacy_jsonl") is not None:
                    store.mark_import(legacy_key)
                    continue
                inserted = store.insert_event(
                    record,
                    record_kind="activity",
                    source="legacy_jsonl",
                    legacy_key=legacy_key,
                )
                counts["activity"] += int(inserted)
                store.mark_import(legacy_key)

    for path in sorted(log_dir.glob("hue_changes_*.jsonl")):
        with store.connection:
            for line_number, record in iter_jsonl(path):
                legacy_key = f"change:{path.name}:{line_number}"
                if store.has_import(legacy_key):
                    continue
                event_id = store.matching_event_id(record)
                if event_id is not None:
                    store.enrich_event_change(event_id, record)
                else:
                    imported = {
                        **record,
                        "category": "light",
                        "action": "state_change",
                        "summary": f"{record.get('name', record.get('rid', '?'))}: legacy change",
                        "raw": record.get("raw_update") or {},
                    }
                    store.insert_event(
                        imported,
                        record_kind="change",
                        source="legacy_jsonl",
                        legacy_key=legacy_key,
                    )
                    counts["unmatched_changes"] += 1
                store.mark_import(legacy_key)
                counts["changes"] += 1

    for path in sorted(log_dir.glob("hue_health_*.jsonl")):
        with store.connection:
            for line_number, record in iter_jsonl(path):
                details = {key: value for key, value in record.items() if key not in {"ts", "kind"}}
                before = store.connection.total_changes
                store.record_health(
                    record.get("kind", "legacy_health"),
                    ts=record.get("ts"),
                    legacy_key=f"health:{path.name}:{line_number}",
                    **details,
                )
                counts["health"] += int(store.connection.total_changes > before)

    with store.connection:
        store.record_health("legacy_import", log_dir=str(log_dir), counts=counts)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Import Hue JSONL history into SQLite.")
    parser.add_argument("log_dir", type=Path)
    args = parser.parse_args()
    if not args.log_dir.is_dir():
        parser.error(f"not a directory: {args.log_dir}")
    with HueStore() as store:
        counts = import_jsonl(args.log_dir, store)
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
