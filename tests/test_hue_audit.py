from __future__ import annotations

import pytest

from shared.hue_audit import audited_hue_command
from shared.hue_log_tools import log_recent
from shared.hue_store import HueStore


async def test_audit_records_success(monkeypatch, tmp_path):
    path = tmp_path / "hue.sqlite3"
    monkeypatch.setenv("HUE_DB_PATH", str(path))

    @audited_hue_command("hue_test")
    async def command(light: str, on: bool) -> str:
        return "done"

    assert await command("Lamp", True) == "done"
    with HueStore(path, create=False) as store:
        row = store.query("commands")[0]
    assert row["tool"] == "hue_test"
    assert row["target"] == "Lamp"
    assert row["outcome"] == "completed"
    assert "result=done" in await log_recent(kind="commands", query="Lamp")


async def test_audit_records_error(monkeypatch, tmp_path):
    path = tmp_path / "hue.sqlite3"
    monkeypatch.setenv("HUE_DB_PATH", str(path))

    @audited_hue_command("hue_test")
    async def command(room: str) -> str:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await command("Room")
    with HueStore(path, create=False) as store:
        row = store.query("commands")[0]
    assert row["outcome"] == "error"
    assert "boom" in row["error"]
