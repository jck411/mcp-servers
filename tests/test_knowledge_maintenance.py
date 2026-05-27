"""Unit tests for the repo-owned Knowledge maintenance runner."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

import servers.knowledge.maintenance as maintenance


def _make_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE domains (
            name TEXT PRIMARY KEY,
            description TEXT,
            related_domains TEXT NOT NULL DEFAULT '[]',
            created_at TEXT,
            archived INTEGER DEFAULT 0
        );
        CREATE TABLE facts (
            id TEXT PRIMARY KEY,
            domain TEXT,
            key TEXT,
            value TEXT,
            source TEXT,
            confidence REAL DEFAULT 1.0,
            valid_from TEXT,
            valid_until TEXT,
            as_of TEXT,
            review_after TEXT,
            origin_type TEXT,
            origin_ref TEXT,
            last_confirmed_at TEXT,
            created_at TEXT,
            updated_at TEXT,
            type TEXT,
            tags TEXT
        );
    """)
    conn.commit()
    conn.close()
    return path


def test_audit_sqlite_finds_expired_facts_and_empty_domains(tmp_path: Path):
    db_path = _make_db(tmp_path / "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO domains (name, description, created_at, archived) VALUES (?, ?, ?, ?)",
        [
            ("ghost", "empty placeholder", "2026-01-01", 0),
            ("ignored", "intentionally empty", "2026-01-01", 0),
            ("filled", "has facts", "2026-01-01", 0),
        ],
    )
    conn.executemany(
        "INSERT INTO facts (id, domain, key, value, valid_until) VALUES (?, ?, ?, ?, ?)",
        [
            ("f1", "filled", "temp_medication", "as needed", "2000-01-01"),
            ("f2", "ignored", "meta.ignore_empty_check", "true", None),
        ],
    )
    conn.commit()
    conn.close()

    audit = maintenance.audit_sqlite(db_path)

    assert [fact["key"] for fact in audit["expired_facts"]] == ["temp_medication"]
    assert {domain["name"] for domain in audit["empty_domains"]} == {"ghost"}


def test_temporal_candidates_require_explicit_end_cue(tmp_path: Path):
    db_path = _make_db(tmp_path / "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO facts (id, domain, key, value, valid_from, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                "stock-snapshot",
                "finances",
                "msft_position_current_as_of_2026_05_19",
                "Jack has 100 shares of MSFT as of 2026-05-19.",
                None,
                "2026-05-19T00:00:00Z",
            ),
            (
                "option-expiry",
                "finances",
                "msft_short_put",
                "Short put expires on 2026-06-19.",
                None,
                "2026-05-19T02:00:00Z",
            ),
            (
                "passport-expiry",
                "identity",
                "passport_expiration",
                "Passport expiration date is 2030-01-01.",
                None,
                "2026-05-19T03:00:00Z",
            ),
            (
                "caffeine-plan",
                "health",
                "caffeine_strategy",
                "Current caffeine schedule for workout days.",
                "2026-05-25",
                "2026-05-19T01:00:00Z",
            ),
        ],
    )
    conn.commit()
    conn.close()

    candidates = maintenance.scan_temporal_fact_candidates(db_path)

    assert {fact["id"] for fact in candidates} == {"option-expiry", "passport-expiry"}


class FakeQdrant:
    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self.pages = pages
        self.scroll_calls = 0
        self.deleted = []

    async def scroll(self, **kwargs):
        page_index = kwargs["offset"] or 0
        self.scroll_calls += 1
        next_offset = page_index + 1 if page_index + 1 < len(self.pages) else None
        return self.pages[page_index], next_offset

    async def delete(self, **kwargs):
        self.deleted.append(kwargs["points_selector"])


@pytest.mark.asyncio
async def test_scan_qdrant_sources_classifies_current_vector_shapes():
    client = FakeQdrant([
        [
            {"id": "legacy", "payload": {"source_id": "old-source", "domain": "legacy"}},
            {
                "id": "fact",
                "payload": {
                    "type": "fact",
                    "fact_id": "fact-1",
                    "domain": "health",
                    "key": "blood_type",
                    "value": "O+",
                },
            },
            {
                "id": "wiki",
                "payload": {
                    "source_type": "wiki_page",
                    "source_id": "family/dad",
                    "domain": "family",
                },
            },
            {"id": "bad", "payload": {"domain": "health"}},
        ],
        [],
    ])

    scan = await maintenance.scan_qdrant_sources(client, "knowledge", set())

    assert client.scroll_calls == 2
    assert scan["seen_fact_ids"] == {"fact-1"}
    assert scan["seen_wiki_slugs"] == {"family/dad"}
    assert [point["source_id"] for point in scan["orphan_points"]] == ["old-source"]
    assert [point["point_id"] for point in scan["malformed_points"]] == ["bad"]


def test_fact_vector_audits_detect_missing_orphan_and_stale(tmp_path: Path):
    db_path = _make_db(tmp_path / "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO facts (id, domain, key, value, updated_at) VALUES (?, ?, ?, ?, ?)",
        [
            ("live-fact", "health", "blood_type", "O+", "2026-04-25T00:00:00Z"),
            ("missing-fact", "pets", "dog_benji", "Yorkie/Maltese", "2026-04-25T00:00:00Z"),
        ],
    )
    conn.commit()
    conn.close()
    qdrant_scan = {
        "seen_fact_ids": {"live-fact", "orphan-fact"},
        "fact_points": [
            {
                "point_id": "p-live",
                "fact_id": "live-fact",
                "domain": "health",
                "key": "blood_type",
                "value": "stale",
                "valid_from": None,
                "valid_until": None,
                "updated_at": "2026-04-25T00:00:00Z",
            },
            {
                "point_id": "p-orphan",
                "fact_id": "orphan-fact",
                "domain": "health",
                "key": "old_fact",
                "value": "gone",
            },
        ],
    }

    facts = maintenance._load_facts_by_id(db_path)

    assert [fact["id"] for fact in maintenance.audit_facts_without_vectors(facts, qdrant_scan)] == [
        "missing-fact"
    ]
    orphan = maintenance.audit_orphan_fact_vectors(facts, qdrant_scan)
    assert [point["fact_id"] for point in orphan] == ["orphan-fact"]
    stale = maintenance.audit_stale_fact_vectors(facts, qdrant_scan)
    assert stale[0]["fact_id"] == "live-fact"
    assert stale[0]["mismatches"] == ["value"]
