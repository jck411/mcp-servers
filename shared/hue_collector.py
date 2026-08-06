"""Always-on Hue v2 event collector backed by the local SQLite event store."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import ssl
import sys
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx

from shared.hue_model import (
    RESOURCE_TYPES,
    STATE_TYPES,
    activity_from_entry,
    changed_fields,
    deep_merge,
    state_summary,
)
from shared.hue_store import HueStore, now_iso


@dataclass(frozen=True)
class CollectorConfig:
    key: str
    bridge_ip: str
    retention_days: int = 30
    reconcile_interval_sec: int = 300
    metadata_interval_sec: int = 21_600
    read_timeout_sec: float = 360
    reconnect_max_sec: float = 60
    ca_cert: str | None = None

    @classmethod
    def from_environment(cls) -> CollectorConfig:
        return cls(
            key=os.environ.get("HUE_KEY", ""),
            bridge_ip=os.environ.get("HUE_BRIDGE_IP", os.environ.get("HUE_IP", "192.168.1.4")),
            retention_days=int(os.environ.get("HUE_RETENTION_DAYS", "30")),
            reconcile_interval_sec=int(os.environ.get("HUE_RECONCILE_INTERVAL_SEC", "300")),
            metadata_interval_sec=int(os.environ.get("HUE_METADATA_REFRESH_SEC", "21600")),
            read_timeout_sec=float(os.environ.get("HUE_READ_TIMEOUT_SEC", "360")),
            reconnect_max_sec=float(os.environ.get("HUE_RECONNECT_MAX_SEC", "60")),
            ca_cert=os.environ.get("HUE_CA_CERT") or None,
        )


@dataclass(frozen=True)
class SseFrame:
    event_id: str | None
    data: str


def iter_sse_frames(lines: Iterable[str | bytes]) -> Iterator[SseFrame]:
    event_id: str | None = None
    data_lines: list[str] = []
    for raw_line in lines:
        line = (
            raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
        )
        line = line.rstrip("\r\n")
        if not line:
            if data_lines:
                yield SseFrame(event_id, "\n".join(data_lines))
            event_id = None
            data_lines = []
            continue
        if line.startswith("id:"):
            event_id = line[3:].lstrip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield SseFrame(event_id, "\n".join(data_lines))


class HueBridgeClient:
    def __init__(self, config: CollectorConfig) -> None:
        verify: bool | ssl.SSLContext
        verify = ssl.create_default_context(cafile=config.ca_cert) if config.ca_cert else False
        timeout = httpx.Timeout(
            connect=10.0,
            read=config.read_timeout_sec,
            write=10.0,
            pool=10.0,
        )
        self.base_url = f"https://{config.bridge_ip}"
        self.client = httpx.Client(
            verify=verify,
            timeout=timeout,
            headers={"hue-application-key": config.key},
        )

    def close(self) -> None:
        self.client.close()

    def get_resource(self, resource_type: str) -> list[dict[str, Any]]:
        response = self.client.get(f"{self.base_url}/clip/v2/resource/{resource_type}")
        response.raise_for_status()
        data = response.json().get("data", [])
        return data if isinstance(data, list) else []

    @contextlib.contextmanager
    def event_stream(self) -> Iterator[httpx.Response]:
        with self.client.stream(
            "GET",
            f"{self.base_url}/eventstream/clip/v2",
            headers={"Accept": "text/event-stream"},
        ) as response:
            response.raise_for_status()
            yield response


@dataclass
class ResourceName:
    name: str = ""
    owner_rid: str = ""


class HueCollector:
    def __init__(
        self,
        config: CollectorConfig,
        store: HueStore,
        client: HueBridgeClient,
    ) -> None:
        self.config = config
        self.store = store
        self.client = client
        self.names: dict[str, ResourceName] = {}
        self.state = store.load_current()

    def _health(self, kind: str, **details: Any) -> None:
        with self.store.connection:
            self.store.record_health(kind, **details)

    def absorb_name(self, item: dict[str, Any]) -> None:
        rid = item.get("id")
        if not rid:
            return
        metadata = item.get("metadata") or {}
        owner = item.get("owner") or {}
        existing = self.names.get(rid, ResourceName())
        self.names[rid] = ResourceName(
            name=metadata.get("name") or existing.name,
            owner_rid=owner.get("rid") or existing.owner_rid,
        )

    def name_for(self, rid: str) -> str:
        resource = self.names.get(rid)
        if not resource:
            return rid[:8]
        if resource.name:
            return resource.name
        owner = self.names.get(resource.owner_rid)
        return owner.name if owner and owner.name else rid[:8]

    def refresh_resources(self) -> int:
        loaded = 0
        ts = now_iso()
        for resource_type in RESOURCE_TYPES:
            try:
                resources = self.client.get_resource(resource_type)
            except Exception as exc:  # noqa: BLE001
                self._health("metadata_error", resource_type=resource_type, error=repr(exc))
                continue
            for item in resources:
                self.absorb_name(item)
                loaded += 1
            with self.store.connection:
                for item in resources:
                    rid = item.get("id")
                    if not rid:
                        continue
                    if resource_type in STATE_TYPES:
                        # Reconciliation owns state resources so a restart can
                        # compare the live Bridge against the persisted state.
                        continue
                    self.state[rid] = item
                    self.store.upsert_current(
                        rid,
                        resource_type,
                        self.name_for(rid),
                        state_summary(item),
                        item,
                        ts,
                    )
        self._health("metadata_refresh", resources=loaded)
        return loaded

    def reconcile(self, reason: str) -> int:
        differences = 0
        checked = 0
        ts = now_iso()
        for resource_type in STATE_TYPES:
            resources = self.client.get_resource(resource_type)
            seen: set[str] = set()
            with self.store.connection:
                for item in resources:
                    rid = item.get("id")
                    if not rid:
                        continue
                    seen.add(rid)
                    checked += 1
                    self.absorb_name(item)
                    before_raw = self.state.get(rid)
                    before = state_summary(before_raw or {})
                    after = state_summary(item)
                    changed = changed_fields(before, after)
                    if before_raw is not None and changed:
                        batch = {"type": "reconcile", "creationtime": None}
                        record = activity_from_entry(batch, item, self.name_for(rid), ts=ts)
                        record.update({"changed": changed, "before": before, "after": after})
                        inserted = self.store.insert_event(
                            record,
                            batch_id=f"reconcile:{ts}:{rid}",
                            entry_index=0,
                            source=f"reconcile:{reason}",
                        )
                        differences += int(inserted)
                    self.state[rid] = item
                    self.store.upsert_current(
                        rid,
                        resource_type,
                        self.name_for(rid),
                        after,
                        item,
                        ts,
                    )
                known = {
                    rid
                    for rid, raw in self.state.items()
                    if raw.get("type") == resource_type and rid not in seen
                }
                for rid in known:
                    self.state.pop(rid, None)
                    self.store.delete_current(rid)
        self._health("reconcile", reason=reason, checked=checked, differences=differences)
        return differences

    def process_frame(self, frame: SseFrame) -> int:
        try:
            parsed = json.loads(frame.data)
        except json.JSONDecodeError:
            self._health("invalid_event_json", sse_id=frame.event_id, payload=frame.data[:2000])
            return 0
        batches = parsed if isinstance(parsed, list) else [parsed]
        inserted_count = 0
        with self.store.connection:
            for batch in batches:
                if not isinstance(batch, dict):
                    self.store.record_health("invalid_event_batch", raw=batch)
                    continue
                entries = batch.get("data", [])
                if not isinstance(entries, list):
                    self.store.record_health("invalid_event_data", raw=batch)
                    continue
                for index, entry in enumerate(entries):
                    if not isinstance(entry, dict):
                        continue
                    inserted_count += int(self._process_entry(frame, batch, entry, index))
        return inserted_count

    def _process_entry(
        self,
        frame: SseFrame,
        batch: dict[str, Any],
        entry: dict[str, Any],
        entry_index: int,
    ) -> bool:
        rid = entry.get("id", "")
        resource_type = entry.get("type") or "unknown"
        if batch.get("type") in ("add", "update"):
            self.absorb_name(entry)
        ts = now_iso()
        record = activity_from_entry(batch, entry, self.name_for(rid), ts=ts)
        before_raw = self.state.get(rid, {})
        before = state_summary(before_raw)
        after_raw = deep_merge(before_raw, entry)
        after = state_summary(after_raw)
        if resource_type in STATE_TYPES and batch.get("type") == "update":
            record.update(
                {
                    "changed": changed_fields(before, after),
                    "before": before,
                    "after": after,
                }
            )
        inserted = self.store.insert_event(
            record,
            sse_id=frame.event_id,
            batch_id=batch.get("id"),
            entry_index=entry_index,
        )
        if not inserted:
            return False
        if batch.get("type") == "delete":
            self.state.pop(rid, None)
            self.store.delete_current(rid)
        elif rid:
            self.state[rid] = after_raw
            self.store.upsert_current(
                rid,
                resource_type,
                self.name_for(rid),
                after,
                after_raw,
                ts,
            )
        return True


running = True


def _handle_signal(signum: int, _frame: Any) -> None:
    global running
    running = False
    print(f"[{now_iso()}] caught signal {signum}; shutting down", file=sys.stderr, flush=True)


def _sleep_interruptibly(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while running and time.monotonic() < deadline:
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))


def run(*, reconcile_once: bool = False) -> int:
    global running
    running = True
    config = CollectorConfig.from_environment()
    if not config.key:
        print("HUE_KEY is required", file=sys.stderr)
        return 2
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    with HueStore() as store:
        client = HueBridgeClient(config)
        collector = HueCollector(config, store, client)
        collector._health(
            "collector_start",
            bridge_ip=config.bridge_ip,
            database=str(store.path),
            retention_days=config.retention_days,
            reconcile_interval_sec=config.reconcile_interval_sec,
        )
        try:
            collector.refresh_resources()
            collector.reconcile("startup")
            store.prune(config.retention_days)
            if reconcile_once:
                collector._health("reconcile_once_complete")
                return 0

            backoff = 1.0
            connected_once = False
            last_reconcile = time.monotonic()
            last_metadata = time.monotonic()
            while running:
                try:
                    collector._health("stream_connecting", bridge_ip=config.bridge_ip)
                    with client.event_stream() as response:
                        collector._health("stream_connected", status_code=response.status_code)
                        if connected_once:
                            collector.reconcile("reconnect")
                            last_reconcile = time.monotonic()
                        connected_once = True
                        backoff = 1.0
                        for frame in iter_sse_frames(response.iter_lines()):
                            if not running:
                                break
                            collector.process_frame(frame)
                            now = time.monotonic()
                            if now - last_reconcile >= config.reconcile_interval_sec:
                                collector.reconcile("interval")
                                last_reconcile = now
                            if now - last_metadata >= config.metadata_interval_sec:
                                collector.refresh_resources()
                                store.prune(config.retention_days)
                                last_metadata = now
                except Exception as exc:  # noqa: BLE001
                    if not running:
                        break
                    collector._health("stream_error", error=repr(exc), reconnect_in_sec=backoff)
                    _sleep_interruptibly(backoff)
                    backoff = min(backoff * 2, config.reconnect_max_sec)
        finally:
            collector._health("collector_stop")
            client.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect Hue v2 events into SQLite.")
    parser.add_argument(
        "--reconcile-once",
        action="store_true",
        help="Refresh metadata/state, record differences, and exit.",
    )
    args = parser.parse_args()
    return run(reconcile_once=args.reconcile_once)


if __name__ == "__main__":
    raise SystemExit(main())
