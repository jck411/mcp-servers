from __future__ import annotations

import json
from datetime import datetime, timedelta

from shared.hue_import import import_jsonl
from shared.hue_store import HueStore, configured_timezone, now_iso


def event_record(ts: str | None = None) -> dict:
    return {
        "ts": ts or now_iso(),
        "bridge_creationtime": "2026-08-06T04:00:00Z",
        "event_type": "update",
        "resource_type": "light",
        "rid": "light-1",
        "name": "Test light",
        "category": "light",
        "action": "state_change",
        "value": True,
        "summary": "Test light: on -> ON",
        "changed": ["on"],
        "before": {"on": False, "brightness": 50.0},
        "after": {"on": True, "brightness": 50.0},
        "raw": {"id": "light-1", "type": "light", "on": {"on": True}},
    }


def test_store_queries_state_health_and_commands(tmp_path):
    path = tmp_path / "hue.sqlite3"
    with HueStore(path) as store:
        record = event_record()
        with store.connection:
            assert store.insert_event(record, batch_id="batch-1", entry_index=0)
            assert not store.insert_event(record, batch_id="batch-1", entry_index=0)
            store.upsert_current(
                "light-1",
                "light",
                "Test light",
                record["after"],
                record["raw"],
                record["ts"],
            )
            store.record_health("stream_connected", status_code=200)
        command_id = store.start_command("hue_set_light", {"light": "Test", "on": True})
        store.finish_command(command_id, "completed", result="ok")

        assert len(store.query("activity", query="test light")) == 1
        assert len(store.query("changes")) == 1
        assert store.query("state", query="test")[0]["state"]["on"] is True
        assert store.query("health")[0]["kind"] == "stream_connected"
        assert store.query("commands")[0]["target"] == "Test"
        status = store.status()
        assert status["counts"] == {
            "events": 1,
            "current_state": 1,
            "health": 1,
            "commands": 1,
        }
        assert store.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_store_prunes_history_but_keeps_current_state(tmp_path):
    old = (datetime.now(configured_timezone()) - timedelta(days=60)).isoformat()
    with HueStore(tmp_path / "hue.sqlite3") as store:
        with store.connection:
            store.insert_event(event_record(old), legacy_key="old-event")
            store.record_health("old", ts=old)
            store.upsert_current("light-1", "light", "Test", {}, {}, now_iso())
        command_id = store.start_command("hue_identify", {"light": "Test"})
        store.connection.execute("UPDATE commands SET ts=? WHERE id=?", (old, command_id))
        store.connection.commit()
        removed = store.prune(30)
        assert removed == {"events": 1, "health": 1, "commands": 1}
        assert store.status()["counts"]["current_state"] == 1


def test_jsonl_import_is_idempotent_and_enriches_changes(tmp_path):
    log_dir = tmp_path / "legacy"
    log_dir.mkdir()
    activity = event_record("2026-08-05T23:00:00-04:00")
    change = {
        "ts": activity["ts"],
        "bridge_creationtime": activity["bridge_creationtime"],
        "resource_type": "light",
        "rid": "light-1",
        "name": "Test light",
        "changed": ["on"],
        "before": {"on": False},
        "after": {"on": True},
        "raw_update": activity["raw"],
    }
    health = {"ts": activity["ts"], "kind": "metadata_refresh", "resources": 10}
    (log_dir / "hue_activity_2026-08-05.jsonl").write_text(
        json.dumps(activity) + "\n", encoding="utf-8"
    )
    (log_dir / "hue_changes_2026-08-05.jsonl").write_text(
        json.dumps(change) + "\n", encoding="utf-8"
    )
    (log_dir / "hue_health_2026-08-05.jsonl").write_text(
        json.dumps(health) + "\n", encoding="utf-8"
    )

    with HueStore(tmp_path / "hue.sqlite3") as store:
        first = import_jsonl(log_dir, store)
        second = import_jsonl(log_dir, store)
        assert first["activity"] == 1
        assert first["changes"] == 1
        assert first["health"] == 1
        assert second["activity"] == 0
        assert second["changes"] == 0
        assert second["health"] == 0
        records = store.query("changes", date="2026-08-05")
        assert len(records) == 1
        assert records[0]["before"] == {"on": False}


def test_jsonl_import_deduplicates_cutover_overlap(tmp_path):
    log_dir = tmp_path / "legacy"
    log_dir.mkdir()
    record = event_record("2026-08-05T23:00:00-04:00")
    (log_dir / "hue_activity_2026-08-05.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )

    with HueStore(tmp_path / "hue.sqlite3") as store:
        with store.connection:
            store.insert_event(record, batch_id="live-batch", entry_index=0)
        assert import_jsonl(log_dir, store)["activity"] == 0
        assert store.status()["counts"]["events"] == 1
        assert import_jsonl(log_dir, store)["activity"] == 0
