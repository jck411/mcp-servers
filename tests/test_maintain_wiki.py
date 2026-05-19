import json
from datetime import UTC, datetime, timedelta

import pytest

from maintain_wiki import _require_fresh_backup


def test_require_fresh_backup_accepts_recent_manifest(tmp_path):
    now = datetime(2026, 5, 18, 7, 0, tzinfo=UTC)
    archive = tmp_path / "knowledge.tar.gz"
    archive.write_bytes(b"backup")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "created_at": (now - timedelta(minutes=5)).isoformat(),
        "archive_path": str(archive),
    }))

    assert _require_fresh_backup(manifest, 6, now=now)["archive_path"] == str(archive)


def test_require_fresh_backup_rejects_stale_manifest(tmp_path):
    now = datetime(2026, 5, 18, 7, 0, tzinfo=UTC)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"created_at": (now - timedelta(hours=7)).isoformat()}))

    with pytest.raises(RuntimeError, match="stale"):
        _require_fresh_backup(manifest, 6, now=now)


def test_require_fresh_backup_rejects_missing_archive(tmp_path):
    now = datetime(2026, 5, 18, 7, 0, tzinfo=UTC)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "created_at": now.isoformat(),
        "archive_path": str(tmp_path / "missing.tar.gz"),
    }))

    with pytest.raises(RuntimeError, match="missing or empty"):
        _require_fresh_backup(manifest, 6, now=now)
