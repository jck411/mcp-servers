"""Pure Hue event normalization and state helpers."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

STATE_TYPES = ("light", "grouped_light")

RESOURCE_TYPES = (
    "device",
    "room",
    "zone",
    "light",
    "grouped_light",
    "button",
    "motion",
    "grouped_motion",
    "temperature",
    "light_level",
    "grouped_light_level",
    "device_power",
    "zigbee_connectivity",
    "scene",
    "smart_scene",
    "behavior_instance",
)


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def state_summary(resource: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    on = (resource.get("on") or {}).get("on")
    if on is not None:
        summary["on"] = on
    brightness = (resource.get("dimming") or {}).get("brightness")
    if brightness is not None:
        summary["brightness"] = brightness
    mirek = (resource.get("color_temperature") or {}).get("mirek")
    if mirek is not None:
        summary["mirek"] = mirek
    for key in ("color", "dynamics", "alert"):
        value = resource.get(key)
        if value is not None:
            summary[key] = value
    return summary


def changed_fields(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    return sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))


def get_nested(data: dict[str, Any], *path: str) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def activity_from_entry(
    batch: dict[str, Any],
    entry: dict[str, Any],
    name: str,
    *,
    ts: str,
) -> dict[str, Any]:
    event_type = batch.get("type")
    resource_type = entry.get("type") or "unknown"
    category = resource_type
    action = event_type or "event"
    value: Any = None
    summary = f"{event_type or 'event'} {resource_type}"

    if resource_type in STATE_TYPES:
        category = "light"
        changed = sorted(
            key
            for key in ("on", "dimming", "color_temperature", "color", "dynamics", "alert")
            if key in entry
        )
        action = "state_change" if event_type == "update" else str(event_type or "state")
        bits: list[str] = []
        on_state = get_nested(entry, "on", "on")
        if on_state is not None:
            bits.append("ON" if on_state else "off")
            value = on_state
        brightness = get_nested(entry, "dimming", "brightness")
        if brightness is not None:
            bits.append(f"{brightness:.0f}%")
        summary = f"{name}: {', '.join(changed) if changed else action}"
        if bits:
            summary += f" -> {' '.join(bits)}"
    elif resource_type == "button":
        category = "input"
        value = get_nested(entry, "button", "last_event") or get_nested(
            entry, "button", "button_report", "event"
        )
        action = "button_event"
        summary = f"{name}: button {value or 'event'}"
    elif resource_type in ("motion", "grouped_motion"):
        category = "sensor"
        value = get_nested(entry, "motion", "motion")
        if value is None:
            value = get_nested(entry, "motion_report", "motion")
        action = "motion"
        state = "detected" if value is True else "clear" if value is False else "event"
        summary = f"{name}: motion {state}"
    elif resource_type == "temperature":
        category = "sensor"
        value = get_nested(entry, "temperature", "temperature")
        action = "temperature"
        summary = f"{name}: temperature {value if value is not None else 'event'}"
    elif resource_type in ("light_level", "grouped_light_level"):
        category = "sensor"
        value = get_nested(entry, "light", "light_level")
        if value is None:
            value = get_nested(entry, "light_level", "light_level")
        action = "light_level"
        summary = f"{name}: light level {value if value is not None else 'event'}"
    elif resource_type == "device_power":
        category = "device"
        value = get_nested(entry, "power_state", "battery_level")
        action = "battery"
        summary = f"{name}: battery {value}%" if value is not None else f"{name}: power event"
    elif resource_type == "behavior_instance":
        category = "automation"
        value = entry.get("status")
        if value is None:
            value = entry.get("enabled")
        action = "automation_status"
        summary = f"{name}: automation {value if value is not None else event_type or 'event'}"
    elif resource_type in ("scene", "smart_scene"):
        category = "scene"
        value = get_nested(entry, "status", "active")
        if value is None:
            value = entry.get("state")
        action = "scene_status"
        summary = f"{name}: scene {value if value is not None else event_type or 'event'}"
    elif resource_type in ("room", "zone", "device", "zigbee_connectivity"):
        category = "metadata"
        action = f"{resource_type}_{event_type or 'event'}"
        summary = f"{name}: {resource_type} {event_type or 'event'}"

    return {
        "ts": ts,
        "bridge_creationtime": batch.get("creationtime"),
        "event_type": event_type,
        "resource_type": resource_type,
        "rid": entry.get("id", ""),
        "name": name,
        "category": category,
        "action": action,
        "value": value,
        "summary": summary,
        "raw": entry,
    }


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
