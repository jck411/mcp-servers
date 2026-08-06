from __future__ import annotations

import json

from shared.hue_collector import (
    CollectorConfig,
    HueCollector,
    SseFrame,
    iter_sse_frames,
)
from shared.hue_model import state_summary
from shared.hue_store import HueStore, now_iso


class FakeClient:
    def __init__(self, resources=None):
        self.resources = resources or {}

    def get_resource(self, resource_type):
        return self.resources.get(resource_type, [])


def light(brightness: float, on: bool = True) -> dict:
    return {
        "id": "light-1",
        "type": "light",
        "metadata": {"name": "Test light"},
        "on": {"on": on},
        "dimming": {"brightness": brightness},
    }


def config() -> CollectorConfig:
    return CollectorConfig(key="test", bridge_ip="127.0.0.1")


def test_sse_parser_preserves_id_and_multiline_data():
    frames = list(iter_sse_frames(["id: 10:0", 'data: [{"x":', "data: 1}]", "", ": keepalive", ""]))
    assert frames == [SseFrame("10:0", '[{"x":\n1}]')]


def test_collector_records_event_change_and_deduplicates(tmp_path):
    with HueStore(tmp_path / "hue.sqlite3") as store:
        collector = HueCollector(config(), store, FakeClient())
        collector.names["light-1"] = type("Name", (), {"name": "Test light", "owner_rid": ""})()
        collector.state["light-1"] = light(10)
        payload = [
            {
                "id": "batch-1",
                "type": "update",
                "creationtime": "2026-08-06T04:00:00Z",
                "data": [
                    {
                        "id": "light-1",
                        "type": "light",
                        "dimming": {"brightness": 20.0},
                    }
                ],
            }
        ]
        frame = SseFrame("100:0", json.dumps(payload))
        assert collector.process_frame(frame) == 1
        assert collector.process_frame(frame) == 0
        change = store.query("changes")[0]
        assert change["before"]["brightness"] == 10
        assert change["after"]["brightness"] == 20
        assert store.load_current()["light-1"]["dimming"]["brightness"] == 20


def test_reconcile_records_only_differences(tmp_path):
    current = light(10)
    live = light(30)
    with HueStore(tmp_path / "hue.sqlite3") as store:
        with store.connection:
            store.upsert_current(
                "light-1", "light", "Test light", state_summary(current), current, now_iso()
            )
        collector = HueCollector(config(), store, FakeClient({"light": [live]}))
        collector.absorb_name(live)
        assert collector.reconcile("reconnect") == 1
        assert collector.reconcile("interval") == 0
        change = store.query("changes")[0]
        assert change["source"] == "reconcile:reconnect"
        assert change["changed"] == ["brightness"]
