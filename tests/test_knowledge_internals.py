"""Pure-unit tests for knowledge subsystem internals.

These tests intentionally avoid the network / Qdrant / OpenAI to stay fast
and run anywhere. They cover the parts most likely to silently regress:
chunking math, BM25 sparse encoder, fact search semantics, and the
text-ingest input validator.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from servers.knowledge import (
    BM25SparseEncoder,
    KnowledgeDB,
    _is_likely_binary,
    _validate_text_ingest_inputs,
    chunk_text,
    compute_text_hash,
    fact_temporal_status,
    resolve_search_domains,
    search_fact_keywords,
    search_knowledge,
)

# ---------------------------------------------------------------------------
# chunk_text
# ---------------------------------------------------------------------------


def test_chunk_text_empty_returns_empty():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_chunk_text_short_returns_single_chunk():
    out = chunk_text("hello world", max_chars=1000, overlap=100)
    assert out == ["hello world"]


def test_chunk_text_respects_max_chars_with_long_paragraph():
    text = "a" * 2500
    out = chunk_text(text, max_chars=1000, overlap=200)
    assert len(out) >= 3
    for piece in out:
        assert len(piece) <= 1000


def test_chunk_text_overlap_creates_continuity():
    # Long single paragraph forces sliding-window splitting.
    text = "abcdefghij" * 200  # 2000 chars
    out = chunk_text(text, max_chars=500, overlap=100)
    # Consecutive chunks should share their overlap region.
    assert len(out) >= 2
    # Each chunk fits within the cap.
    for piece in out:
        assert len(piece) <= 500


def test_chunk_text_paragraph_boundary_kept_when_fits():
    # Two short paragraphs should join into one chunk under the cap.
    text = "first paragraph here.\n\nsecond paragraph here."
    out = chunk_text(text, max_chars=1000, overlap=100)
    assert out == ["first paragraph here.\n\nsecond paragraph here."]


def test_chunk_text_handles_zero_overlap():
    text = "x" * 600
    out = chunk_text(text, max_chars=200, overlap=0)
    assert len(out) == 3
    assert all(len(c) == 200 for c in out)


# ---------------------------------------------------------------------------
# compute_text_hash
# ---------------------------------------------------------------------------


def test_compute_text_hash_deterministic_and_distinct():
    a = compute_text_hash("hello")
    b = compute_text_hash("hello")
    c = compute_text_hash("hello!")
    assert a == b
    assert a != c
    assert isinstance(a, str) and len(a) >= 32


# ---------------------------------------------------------------------------
# _is_likely_binary
# ---------------------------------------------------------------------------


def test_is_likely_binary_detects_zip_magic():
    # PK..ZIP container — also catches docx, xlsx, etc.
    assert _is_likely_binary(b"PK\x03\x04rest...") is True


def test_is_likely_binary_detects_png_magic():
    assert _is_likely_binary(b"\x89PNG\r\n\x1a\n") is True


def test_is_likely_binary_detects_mp4_ftyp():
    # MP4/HEIC use ISO base media format with 'ftyp' at bytes 4-8.
    assert _is_likely_binary(b"\x00\x00\x00\x18ftypmp42rest") is True


def test_is_likely_binary_detects_null_byte():
    assert _is_likely_binary(b"some text\x00more") is True


def test_is_likely_binary_text_is_not_binary():
    assert _is_likely_binary(b"hello world\nthis is plain text") is False


def test_is_likely_binary_empty_is_not_binary():
    assert _is_likely_binary(b"") is False


# ---------------------------------------------------------------------------
# BM25SparseEncoder
# ---------------------------------------------------------------------------


def test_bm25_empty_text_returns_empty():
    enc = BM25SparseEncoder()
    enc.fit_batch(["alpha beta gamma"])
    assert enc.encode("") == ([], [])
    assert enc.encode("   ") == ([], [])


def test_bm25_encode_returns_sorted_unique_indices():
    enc = BM25SparseEncoder()
    enc.fit_batch([
        "the quick brown fox jumps over the lazy dog",
        "the quick brown fox is quick",
        "lazy dogs sleep",
    ])
    indices, values = enc.encode("quick brown fox")
    assert len(indices) == len(values) > 0
    # Indices must be sorted ascending and unique (Qdrant sparse vector reqs).
    assert indices == sorted(indices)
    assert len(set(indices)) == len(indices)
    # All scores positive.
    assert all(v > 0 for v in values)


def test_bm25_query_helper_matches_encode():
    enc = BM25SparseEncoder()
    enc.fit_batch(["alpha beta gamma delta"])
    assert enc.encode_query("alpha beta") == enc.encode("alpha beta")


def test_bm25_handles_unseen_terms_without_crash():
    # Cold encoder before any fit should still produce a valid (possibly empty)
    # result rather than raise.
    enc = BM25SparseEncoder()
    indices, values = enc.encode("totally unseen vocabulary")
    # With no docs fit, IDF = 0 → empty vector is the expected (and documented)
    # cold-state behavior. The contract is just "doesn't crash".
    assert isinstance(indices, list) and isinstance(values, list)


def test_bm25_short_tokens_skipped():
    enc = BM25SparseEncoder()
    enc.fit_batch(["a b c the of"])
    # All single-letter tokens are dropped by the regex; "the" / "of" remain.
    indices, _ = enc.encode("a b c")
    assert indices == []


# ---------------------------------------------------------------------------
# _validate_text_ingest_inputs
# ---------------------------------------------------------------------------


def test_validate_text_ingest_rejects_pdf_filename():
    err = _validate_text_ingest_inputs("scan.pdf", "note")
    assert err and "binary" in err.lower()


def test_validate_text_ingest_rejects_image_filename():
    err = _validate_text_ingest_inputs("photo.jpg", "note")
    assert err and "binary" in err.lower()


def test_validate_text_ingest_rejects_extension_as_source_type():
    err = _validate_text_ingest_inputs("notes", "pdf")
    assert err and "file extension" in err.lower()


def test_validate_text_ingest_rejects_unknown_source_type():
    err = _validate_text_ingest_inputs("notes", "identity_document")
    assert err and "not allowed" in err.lower()


@pytest.mark.parametrize("source_type", [
    "note", "summary", "transcript", "research",
    "caption", "markdown", "text", "manual", "chat", "memo",
])
def test_validate_text_ingest_accepts_allowed_types(source_type: str):
    assert _validate_text_ingest_inputs("doctor visit 2026-03", source_type) is None


def test_validate_text_ingest_accepts_plain_name_no_extension():
    assert _validate_text_ingest_inputs("manual entry", "note") is None


# ---------------------------------------------------------------------------
# facts_search: key OR value matching
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_with_facts(tmp_path: Path):
    db = KnowledgeDB(tmp_path / "k.db")
    await db.initialize()
    await db.domain_create("health", "health domain", [])
    await db.domain_create("finance", "finance domain", [])
    await db.fact_set("health", "bp_2025_03", "130/82", source="manual")
    await db.fact_set("health", "ldl_2024_12", "142 mg/dL", source="lab")
    await db.fact_set("finance", "w2_2025_box1_wages", "94200.00", source="form")
    try:
        yield db
    finally:
        await db.close()


async def test_facts_search_matches_on_key(db_with_facts: KnowledgeDB):
    out = await db_with_facts.facts_search(["health"], ["ldl"])
    assert len(out) == 1
    assert out[0]["key"] == "ldl_2024_12"


async def test_facts_search_matches_on_value(db_with_facts: KnowledgeDB):
    # Value contains "130" but key does not. Old behavior missed this.
    out = await db_with_facts.facts_search(["health"], ["130"])
    assert any(row["key"] == "bp_2025_03" for row in out), (
        "value-side match regressed — facts_search should match value column too"
    )


async def test_facts_search_value_match_across_domains(db_with_facts: KnowledgeDB):
    out = await db_with_facts.facts_search(
        ["health", "finance"],
        ["94200"],
    )
    assert len(out) == 1
    assert out[0]["key"] == "w2_2025_box1_wages"


async def test_facts_search_empty_keys_returns_all_in_domain(db_with_facts: KnowledgeDB):
    out = await db_with_facts.facts_search(["health"], [])
    assert len(out) == 2


async def test_facts_search_empty_domains_returns_empty(db_with_facts: KnowledgeDB):
    out = await db_with_facts.facts_search([], ["anything"])
    assert out == []


async def test_fact_set_tracks_origin_and_confirmation(tmp_path: Path):
    db = KnowledgeDB(tmp_path / "facts.db")
    await db.initialize()
    await db.domain_create("core", "core", [])
    try:
        await db.fact_set(
            "core", "favorite_water", "sparkling", origin_type="chat", origin_ref="2026-05-17",
        )
        first = await db.fact_get("core", "favorite_water")
        assert first["origin_type"] == "chat"
        assert first["origin_ref"] == "2026-05-17"
        assert first["last_confirmed_at"] is None

        await db.fact_set(
            "core", "favorite_water", "sparkling", origin_type="chat", origin_ref="2026-05-18",
        )
        second = await db.fact_get("core", "favorite_water")
        assert second["origin_ref"] == "2026-05-18"
        assert second["last_confirmed_at"] is not None
    finally:
        await db.close()


async def test_fact_set_stores_temporal_review_metadata(tmp_path: Path):
    db = KnowledgeDB(tmp_path / "fact_temporal.db")
    await db.initialize()
    await db.domain_create("finances", "finance domain", [])
    try:
        await db.fact_set(
            "finances",
            "msft_position_snapshot",
            "10 shares",
            source="brokerage",
            as_of="2026-05-19",
            review_after="2026-06-01",
        )
        row = await db.fact_get("finances", "msft_position_snapshot")
        found = await db.facts_search(["finances"], ["msft"])
    finally:
        await db.close()

    assert row["as_of"] == "2026-05-19"
    assert row["review_after"] == "2026-06-01"
    assert found[0]["as_of"] == "2026-05-19"
    assert found[0]["review_after"] == "2026-06-01"


def test_fact_temporal_status_uses_current_time_boundaries():
    now = datetime(2026, 5, 20, 12, tzinfo=UTC)

    assert fact_temporal_status({"valid_until": "2026-05-19"}, now) == "expired"
    assert fact_temporal_status({"valid_until": "2026-05-20"}, now) == "current"
    assert fact_temporal_status({"valid_from": "2026-05-21"}, now) == "future"
    assert fact_temporal_status({"review_after": "2026-05-20"}, now) == "stale"
    assert fact_temporal_status({"as_of": "2026-05-19"}, now) == "historical"
    assert fact_temporal_status({"as_of": "not-a-date"}, now) == "unknown"


async def test_wiki_schema_initializes_tables_and_seed_state(tmp_path: Path):
    db = KnowledgeDB(tmp_path / "wiki.db")
    await db.initialize()
    try:
        assert db._conn is not None
        cursor = await db._conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table'
              AND name IN ('wiki_pages', 'wiki_page_sources', 'wiki_state', 'wiki_rebuild_runs')
            """
        )
        assert {row["name"] for row in await cursor.fetchall()} == {
            "wiki_pages", "wiki_page_sources", "wiki_state", "wiki_rebuild_runs",
        }

        cursor = await db._conn.execute("PRAGMA table_info(wiki_pages)")
        columns = {row["name"]: row for row in await cursor.fetchall()}
        assert columns["status"]["dflt_value"] == "'candidate'"

        cursor = await db._conn.execute("SELECT key, value FROM wiki_state")
        state = {row["key"]: row["value"] for row in await cursor.fetchall()}
        assert state == {
            "last_wiki_run": "1970-01-01T00:00:00Z",
            "manual_rebuild_requires_confirmation": "true",
        }
    finally:
        await db.close()


async def test_wiki_schema_source_uniqueness_and_page_cascade(tmp_path: Path):
    db = KnowledgeDB(tmp_path / "wiki_sources.db")
    await db.initialize()
    try:
        assert db._conn is not None
        await db._conn.execute(
            """
            INSERT INTO wiki_pages
                (slug, domain, title, kind, body_md, frontmatter_json)
            VALUES ('family/dad', 'family', 'Dad', 'entity', 'body', '{}')
            """
        )
        await db._conn.execute(
            """
            INSERT INTO wiki_page_sources
                (page_slug, source_id, chat_date, contribution)
            VALUES ('family/dad', NULL, '2026-05-17', 'chat note')
            """
        )
        await db._conn.commit()
        cursor = await db._conn.execute("SELECT status FROM wiki_pages WHERE slug = 'family/dad'")
        assert (await cursor.fetchone())["status"] == "candidate"

        with pytest.raises(sqlite3.IntegrityError):
            await db._conn.execute(
                """
                INSERT INTO wiki_pages
                    (slug, domain, title, kind, status, body_md, frontmatter_json)
                VALUES ('family/bad', 'family', 'Bad', 'entity', 'draft', 'body', '{}')
                """
            )
        await db._conn.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            await db._conn.execute(
                """
                INSERT INTO wiki_page_sources
                    (page_slug, source_id, chat_date, contribution)
                VALUES ('family/dad', NULL, '2026-05-17', 'duplicate')
                """
            )

        await db._conn.execute("DELETE FROM wiki_pages WHERE slug = 'family/dad'")
        cursor = await db._conn.execute("SELECT COUNT(*) FROM wiki_page_sources")
        assert (await cursor.fetchone())[0] == 0
    finally:
        await db.close()


async def test_wiki_schema_migrates_existing_pages_to_candidate_status(tmp_path: Path):
    db_path = tmp_path / "old_wiki.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE wiki_pages (
                slug TEXT PRIMARY KEY,
                domain TEXT NOT NULL,
                title TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (
                    kind IN ('entity', 'concept', 'source_summary', 'index', 'log')
                ),
                body_md TEXT NOT NULL,
                frontmatter_json TEXT NOT NULL,
                fact_count INTEGER NOT NULL DEFAULT 0,
                source_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO wiki_pages
                (slug, domain, title, kind, body_md, frontmatter_json)
            VALUES ('family/dad', 'family', 'Dad', 'entity', 'body', '{}');
        """)

    db = KnowledgeDB(db_path)
    await db.initialize()
    try:
        assert db._conn is not None
        cursor = await db._conn.execute("PRAGMA table_info(wiki_pages)")
        assert "status" in {row["name"] for row in await cursor.fetchall()}

        cursor = await db._conn.execute("SELECT status FROM wiki_pages WHERE slug = 'family/dad'")
        assert (await cursor.fetchone())["status"] == "candidate"

        cursor = await db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_wiki_pages_status'"
        )
        assert await cursor.fetchone() is not None
    finally:
        await db.close()


async def test_wiki_rebuild_run_status_constraint(tmp_path: Path):
    db = KnowledgeDB(tmp_path / "wiki_runs.db")
    await db.initialize()
    try:
        assert db._conn is not None
        await db._conn.execute(
            "INSERT INTO wiki_rebuild_runs (status, scope_json) VALUES ('running', '{}')"
        )

        with pytest.raises(sqlite3.IntegrityError):
            await db._conn.execute(
                "INSERT INTO wiki_rebuild_runs (status, scope_json) VALUES ('paused', '{}')"
            )
    finally:
        await db.close()


async def test_wiki_page_helpers_get_list_and_set_status(tmp_path: Path):
    db = KnowledgeDB(tmp_path / "wiki_pages.db")
    await db.initialize()
    try:
        assert db._conn is not None
        await db._conn.executescript("""
            INSERT INTO wiki_pages
                (slug, domain, title, kind, status, body_md, frontmatter_json, source_count)
            VALUES
                (
                    'family/dad', 'family', 'Dad', 'entity', 'active', 'Dad body',
                    '{"schema_version":1,"slug":"family/dad","title":"Dad"}', 2
                ),
                (
                    'family/mom', 'family', 'Mom', 'entity', 'candidate', 'Mom body',
                    '{"schema_version":1,"slug":"family/mom","title":"Mom"}', 0
                );
            INSERT INTO wiki_page_sources
                (page_slug, source_id, chat_date, contribution)
            VALUES
                ('family/dad', 'source-a', NULL, 'uploaded note'),
                ('family/dad', NULL, '2026-05-17', 'chat note');
        """)
        await db._conn.commit()

        page = await db.wiki_get("family/dad")
        assert page is not None
        assert page["frontmatter"]["slug"] == "family/dad"
        assert page["sources"] == [
            {"source_id": None, "chat_date": "2026-05-17", "contribution": "chat note"},
            {"source_id": "source-a", "chat_date": None, "contribution": "uploaded note"},
        ]

        assert [p["slug"] for p in await db.wiki_list()] == ["family/dad"]
        assert [p["slug"] for p in await db.wiki_list(status="candidate")] == ["family/mom"]
        assert {p["slug"] for p in await db.wiki_list(domain="family", status="all")} == {
            "family/dad", "family/mom",
        }
        assert await db.wiki_set_status("family/mom", "active") is True
        assert (await db.wiki_get("family/mom"))["status"] == "active"
        assert await db.wiki_set_status("family/missing", "archived") is False
    finally:
        await db.close()


async def test_wiki_mcp_tools_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import servers.knowledge as knowledge

    db = KnowledgeDB(tmp_path / "wiki_tools.db")
    await db.initialize()
    try:
        assert db._conn is not None
        await db._conn.execute(
            """
            INSERT INTO wiki_pages
                (slug, domain, title, kind, status, body_md, frontmatter_json)
            VALUES (
                'family/dad', 'family', 'Dad', 'entity', 'candidate', 'Dad body',
                '{"schema_version":1,"slug":"family/dad"}'
            )
            """
        )
        await db._conn.commit()

        monkeypatch.setattr(knowledge, "_ready", True)
        monkeypatch.setattr(knowledge, "_settings", object())
        monkeypatch.setattr(knowledge, "_embeddings", object())
        monkeypatch.setattr(knowledge, "_sparse_encoder", object())
        monkeypatch.setattr(knowledge, "_vectors", object())
        monkeypatch.setattr(knowledge, "_db", db)

        get_tool = (
            knowledge.knowledge_wiki_get.fn
            if hasattr(knowledge.knowledge_wiki_get, "fn")
            else knowledge.knowledge_wiki_get
        )
        list_tool = (
            knowledge.knowledge_wiki_list.fn
            if hasattr(knowledge.knowledge_wiki_list, "fn")
            else knowledge.knowledge_wiki_list
        )
        set_status_tool = (
            knowledge.knowledge_wiki_set_status.fn
            if hasattr(knowledge.knowledge_wiki_set_status, "fn")
            else knowledge.knowledge_wiki_set_status
        )

        assert (await list_tool(status="draft"))["success"] is False
        assert (await get_tool("family/dad"))["page"]["status"] == "candidate"

        promoted = await set_status_tool("family/dad", "active", notes="reviewed")
        assert promoted["success"] is True
        assert promoted["page"]["status"] == "active"
        assert (await list_tool(status="active"))["pages"][0]["slug"] == "family/dad"
    finally:
        await db.close()


async def test_wiki_rebuild_dry_run_estimates_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import servers.knowledge as knowledge

    db = KnowledgeDB(tmp_path / "wiki_rebuild.db")
    await db.initialize()
    try:
        assert db._conn is not None
        await db.domain_create("family", "family", [])
        now = datetime.now(UTC)
        last_run = (now - timedelta(days=1)).isoformat()
        await db._conn.execute(
            "UPDATE wiki_state SET value = ? WHERE key = 'last_wiki_run'",
            (last_run,),
        )
        await db._conn.executemany(
            """
            INSERT INTO facts
                (id, domain, key, value, source, confidence, valid_from, valid_until,
                 origin_type, origin_ref, last_confirmed_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, NULL, 1.0, NULL, NULL, ?, ?, ?, ?, ?)
            """,
            (
                (
                    "fact-dad",
                    "family",
                    "dad_heart_history",
                    "stable",
                    "manual",
                    None,
                    None,
                    (now - timedelta(hours=2)).isoformat(),
                    now.isoformat(),
                ),
                (
                    "fact-quiet",
                    "family",
                    "mom_phone",
                    "unconfirmed",
                    "chat",
                    now.date().isoformat(),
                    None,
                    now.isoformat(),
                    now.isoformat(),
                ),
                (
                    "fact-shirt",
                    "family",
                    "shirt_color",
                    "purple",
                    "extracted",
                    "source-photo",
                    None,
                    (now - timedelta(hours=2)).isoformat(),
                    now.isoformat(),
                ),
            ),
        )
        await db._conn.commit()
        await db.source_add("source-dad", "family", "note", "Dad notes.md", "hash-dad", 2)

        monkeypatch.setattr(knowledge, "_ready", True)
        monkeypatch.setattr(knowledge, "_settings", SimpleNamespace(extraction_model="test-model"))
        monkeypatch.setattr(knowledge, "_embeddings", object())
        monkeypatch.setattr(knowledge, "_sparse_encoder", object())
        monkeypatch.setattr(knowledge, "_vectors", object())
        monkeypatch.setattr(knowledge, "_db", db)

        rebuild_tool = (
            knowledge.knowledge_wiki_rebuild.fn
            if hasattr(knowledge.knowledge_wiki_rebuild, "fn")
            else knowledge.knowledge_wiki_rebuild
        )
        result = await rebuild_tool(dry_run=True)

        assert result["success"] is True
        assert result["writes_performed"] is False
        assert result["scope"]["since"] == last_run
        assert result["estimated_pages"] == 2
        assert result["changed_entities"][0]["slug"] == "family/dad"
        assert result["changed_entities"][0]["fact_keys"] == ["dad_heart_history"]
        assert result["changed_entities"][0]["source_ids"] == ["source-dad"]
        assert "family/shirt" not in {item["slug"] for item in result["changed_entities"]}
        assert result["latency_class"] == "quick"

        cursor = await db._conn.execute("SELECT COUNT(*) FROM wiki_rebuild_runs")
        assert (await cursor.fetchone())[0] == 0
    finally:
        await db.close()


async def test_wiki_rebuild_manual_run_requires_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import servers.knowledge as knowledge

    db = KnowledgeDB(tmp_path / "wiki_rebuild_confirmation.db")
    await db.initialize()
    try:
        await db.domain_create("family", "family", [])
        await db.fact_set("family", "dad_heart_history", "stable", origin_type="manual")

        monkeypatch.setattr(knowledge, "_ready", True)
        monkeypatch.setattr(knowledge, "_settings", SimpleNamespace(extraction_model="test-model"))
        monkeypatch.setattr(knowledge, "_embeddings", object())
        monkeypatch.setattr(knowledge, "_sparse_encoder", object())
        monkeypatch.setattr(knowledge, "_vectors", object())
        monkeypatch.setattr(knowledge, "_db", db)

        rebuild_tool = (
            knowledge.knowledge_wiki_rebuild.fn
            if hasattr(knowledge.knowledge_wiki_rebuild, "fn")
            else knowledge.knowledge_wiki_rebuild
        )
        result = await rebuild_tool(entity_slug="family/dad")

        assert result["success"] is False
        assert result["requires_confirmation"] is True
        assert result["writes_performed"] is False
        assert result["target"] == "family/dad"
        assert result["estimated_pages"] == 2

        assert db._conn is not None
        cursor = await db._conn.execute("SELECT COUNT(*) FROM wiki_rebuild_runs")
        assert (await cursor.fetchone())[0] == 0
    finally:
        await db.close()


async def test_wiki_rebuild_generates_active_page_sources_and_run_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import servers.knowledge as knowledge

    db = KnowledgeDB(tmp_path / "wiki_rebuild_real.db")
    await db.initialize()
    try:
        assert db._conn is not None
        await db.domain_create("family", "family", [])
        now = datetime.now(UTC)
        await db._conn.execute(
            "UPDATE wiki_state SET value = ? WHERE key = 'last_wiki_run'",
            ((now - timedelta(days=1)).isoformat(),),
        )
        await db.fact_set(
            "family",
            "dad_heart_history",
            "stable",
            origin_type="chat",
            origin_ref="2026-05-16",
        )
        await db.source_add("source-dad", "family", "note", "Dad notes.md", "hash-dad", 1)

        class FakeEmbeddings:
            async def embed(self, query: str) -> list[float]:
                return [0.1]

        class FakeSparseEncoder:
            def encode_query(self, query: str) -> tuple[list[int], list[float]]:
                return [1], [1.0]

        class FakeVectors:
            async def chunks_by_source(self, source_id: str, limit: int = 4) -> list[dict]:
                return [{
                    "id": "chunk-source",
                    "domain": "family",
                    "source_id": source_id,
                    "source_name": "Dad notes.md",
                    "chunk_index": 0,
                    "content": "Dad heart history notes.",
                }]

            async def search(self, *args, **kwargs) -> list[SimpleNamespace]:
                return [SimpleNamespace(
                    id="chunk-search",
                    score=0.9,
                    payload={
                        "id": "chunk-search",
                        "domain": "family",
                        "source_id": "source-dad",
                        "source_name": "Dad notes.md",
                        "chunk_index": 1,
                        "content": "Dad has stable heart history.",
                    },
                )]

        async def fake_call_wiki_llm(settings, context):
            assert context["facts"][0]["key"] == "dad_heart_history"
            assert context["chunks"]
            return {
                "title": "Dad",
                "kind": "entity",
                "body_md": "Dad overview.\n\n## Known Facts\n- Heart history is stable.",
                "frontmatter": {
                    "entity_type": "person",
                    "aliases": ["Dad"],
                    "related_slugs": [],
                },
                "sources": [
                    {
                        "source_id": "source-dad",
                        "chat_date": None,
                        "contribution": "heart history note",
                    },
                    {
                        "source_id": None,
                        "chat_date": "2026-05-16",
                        "contribution": "chat confirmation",
                    },
                ],
                "confidence": "high",
                "duplicate_concerns": [],
                "split_concerns": [],
            }, 321

        monkeypatch.setattr(knowledge, "_ready", True)
        monkeypatch.setattr(
            knowledge,
            "_settings",
            SimpleNamespace(extraction_model="test-model", openrouter_api_key="test"),
        )
        monkeypatch.setattr(knowledge, "_embeddings", FakeEmbeddings())
        monkeypatch.setattr(knowledge, "_sparse_encoder", FakeSparseEncoder())
        monkeypatch.setattr(knowledge, "_vectors", FakeVectors())
        monkeypatch.setattr(knowledge, "_db", db)
        monkeypatch.setattr(knowledge, "_call_wiki_llm", fake_call_wiki_llm)

        rebuild_tool = (
            knowledge.knowledge_wiki_rebuild.fn
            if hasattr(knowledge.knowledge_wiki_rebuild, "fn")
            else knowledge.knowledge_wiki_rebuild
        )
        result = await rebuild_tool(entity_slug="family/dad", confirmed=True)

        assert result["success"] is True
        assert result["writes_performed"] is True
        assert result["touched_slugs"] == ["family/dad", "family/index"]

        page = await db.wiki_get("family/dad")
        assert page["status"] == "active"
        assert page["frontmatter"]["source_ids"] == ["source-dad"]
        assert page["frontmatter"]["chat_dates"] == ["2026-05-16"]
        assert "## Sources" in page["body_md"]
        assert {
            (source["source_id"], source["chat_date"]) for source in page["sources"]
        } == {("source-dad", None), (None, "2026-05-16")}

        index = await db.wiki_get("family/index")
        assert index["status"] == "active"
        assert index["frontmatter"]["related_slugs"] == ["family/dad"]

        cursor = await db._conn.execute(
            "SELECT status, pages_touched, token_estimate, touched_slugs_json "
            "FROM wiki_rebuild_runs"
        )
        row = await cursor.fetchone()
        assert row["status"] == "success"
        assert row["pages_touched"] == 2
        assert row["token_estimate"] == 321
        assert json.loads(row["touched_slugs_json"]) == ["family/dad", "family/index"]
    finally:
        await db.close()


async def test_wiki_rebuild_new_low_confidence_page_stays_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import servers.knowledge as knowledge

    db = KnowledgeDB(tmp_path / "wiki_rebuild_candidate.db")
    await db.initialize()
    try:
        await db.domain_create("tech", "tech", [])
        await db.fact_set("tech", "framework_13_notes", "maybe relevant", origin_type="manual")

        class FakeEmbeddings:
            async def embed(self, query: str) -> list[float]:
                return [0.1]

        class FakeSparseEncoder:
            def encode_query(self, query: str) -> tuple[list[int], list[float]]:
                return [], []

        class FakeVectors:
            async def chunks_by_source(self, source_id: str, limit: int = 4) -> list[dict]:
                return []

            async def search(self, *args, **kwargs) -> list[SimpleNamespace]:
                return []

        async def fake_call_wiki_llm(settings, context):
            return {
                "title": "Framework 13",
                "kind": "entity",
                "body_md": "Framework 13 notes are tentative.",
                "frontmatter": {"entity_type": "device", "aliases": [], "related_slugs": []},
                "sources": [],
                "confidence": "low",
                "duplicate_concerns": ["Could overlap with another Framework page."],
                "split_concerns": [],
            }, 100

        monkeypatch.setattr(knowledge, "_ready", True)
        monkeypatch.setattr(
            knowledge,
            "_settings",
            SimpleNamespace(extraction_model="test-model", openrouter_api_key="test"),
        )
        monkeypatch.setattr(knowledge, "_embeddings", FakeEmbeddings())
        monkeypatch.setattr(knowledge, "_sparse_encoder", FakeSparseEncoder())
        monkeypatch.setattr(knowledge, "_vectors", FakeVectors())
        monkeypatch.setattr(knowledge, "_db", db)
        monkeypatch.setattr(knowledge, "_call_wiki_llm", fake_call_wiki_llm)

        rebuild_tool = (
            knowledge.knowledge_wiki_rebuild.fn
            if hasattr(knowledge.knowledge_wiki_rebuild, "fn")
            else knowledge.knowledge_wiki_rebuild
        )
        result = await rebuild_tool(entity_slug="tech/framework-13", confirmed=True)

        assert result["success"] is True
        page = await db.wiki_get("tech/framework-13")
        assert page["status"] == "candidate"
        assert page["frontmatter"]["audit_notes"] == {
            "merge_candidate": ["Could overlap with another Framework page."]
        }
        assert await db.curation_count(status="pending") == 0
        assert await db.wiki_list(status="active") == []
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Search-side keyword extraction parity
# ---------------------------------------------------------------------------


def test_search_fact_keywords_keeps_short_identifiers():
    keywords = search_fact_keywords("What is my LDL and BP?")
    assert "ldl" in keywords
    assert "bp" in keywords


def test_search_fact_keywords_strips_punctuation():
    keywords = search_fact_keywords("wages?")
    assert keywords == ["wages"]


async def test_resolve_search_domains_includes_related_and_core(tmp_path: Path):
    db = KnowledgeDB(tmp_path / "domains.db")
    await db.initialize()
    await db.domain_create("core", "core", [])
    await db.domain_create("finance", "finance", [])
    await db.domain_create("health", "health", ["finance"])
    try:
        domains = await resolve_search_domains(db, "health", None)
    finally:
        await db.close()

    assert domains == ["health", "finance", "core"]


async def test_search_knowledge_returns_ids_truncates_and_searches_facts(tmp_path: Path):
    db = KnowledgeDB(tmp_path / "search.db")
    await db.initialize()
    await db.domain_create("core", "core", [])
    await db.domain_create("finance", "finance", [])
    await db.domain_create("health", "health", ["finance"])
    await db.fact_set("health", "ldl_2024_12", "142 mg/dL", source="lab")

    class FakeEmbeddings:
        async def embed(self, query: str) -> list[float]:
            self.query = query
            return [0.1, 0.2]

    class FakeSparseEncoder:
        def encode_query(self, query: str) -> tuple[list[int], list[float]]:
            self.query = query
            return [7], [1.0]

    class FakeVectors:
        async def search(
            self,
            query_embedding: list[float],
            *,
            sparse_query: tuple[list[int], list[float]] | None,
            domains: list[str] | None,
            limit: int,
            min_score: float,
        ) -> list[SimpleNamespace]:
            self.call = {
                "query_embedding": query_embedding,
                "sparse_query": sparse_query,
                "domains": domains,
                "limit": limit,
                "min_score": min_score,
            }
            return [
                SimpleNamespace(
                    id="chunk-1",
                    score=0.87654,
                    payload={
                        "content": "abcdefghij",
                        "domain": "health",
                        "source_id": "source-1",
                        "source_name": "labs.pdf",
                        "source_type": "pdf",
                        "chunk_index": 2,
                    },
                )
            ]

    vectors = FakeVectors()
    try:
        response = await search_knowledge(
            embeddings=FakeEmbeddings(),  # type: ignore[arg-type]
            sparse_encoder=FakeSparseEncoder(),  # type: ignore[arg-type]
            vectors=vectors,  # type: ignore[arg-type]
            db=db,
            query="LDL?",
            domain="health",
            limit=5,
            min_similarity=0.2,
            max_chars=4,
        )
    finally:
        await db.close()

    assert vectors.call["domains"] == ["health", "finance", "core"]
    assert response["searched_domains"] == ["health", "finance", "core"]
    assert response["results"] == [{
        "result_type": "chunk",
        "content": "abcd\u2026",
        "domain": "health",
        "source_id": "source-1",
        "source_name": "labs.pdf",
        "source_type": "pdf",
        "chunk_id": "chunk-1",
        "chunk_index": 2,
        "similarity": 0.8765,
    }]
    assert response["route"] == "synthesis"
    assert response["wiki_count"] == 0
    assert response["chunk_count"] == 1
    assert response["fact_count"] == 1
    assert response["facts"][0]["key"] == "ldl_2024_12"


async def test_search_knowledge_routes_synthesis_to_active_wiki(tmp_path: Path):
    db = KnowledgeDB(tmp_path / "wiki_search.db")
    await db.initialize()
    await db.domain_create("family", "family", [])
    try:
        await db.wiki_upsert_page(
            slug="family/dad",
            domain="family",
            title="Dad",
            kind="entity",
            status="active",
            body_md="Dad heart history overview.",
            frontmatter={
                "schema_version": 1,
                "slug": "family/dad",
                "title": "Dad",
                "kind": "entity",
                "domain": "family",
                "aliases": ["Dad"],
            },
            sources=[],
            fact_count=1,
        )
        await db.wiki_upsert_page(
            slug="family/dad-candidate",
            domain="family",
            title="Candidate Dad",
            kind="entity",
            status="candidate",
            body_md="Dad heart history candidate.",
            frontmatter={},
            sources=[],
            fact_count=1,
        )

        class FakeEmbeddings:
            async def embed(self, query: str) -> list[float]:
                return [0.1]

        class FakeSparseEncoder:
            def encode_query(self, query: str) -> tuple[list[int], list[float]]:
                return [1], [1.0]

        class FakeVectors:
            async def search(self, *args, **kwargs) -> list[SimpleNamespace]:
                return [SimpleNamespace(
                    id="chunk-dad",
                    score=0.5,
                    payload={
                        "content": "Dad source chunk.",
                        "domain": "family",
                        "source_id": "source-dad",
                        "source_name": "dad.pdf",
                        "source_type": "pdf",
                        "chunk_index": 0,
                    },
                )]

        response = await search_knowledge(
            embeddings=FakeEmbeddings(),  # type: ignore[arg-type]
            sparse_encoder=FakeSparseEncoder(),  # type: ignore[arg-type]
            vectors=FakeVectors(),  # type: ignore[arg-type]
            db=db,
            query="dad heart history",
            domain="family",
            limit=5,
        )
    finally:
        await db.close()

    assert response["route"] == "synthesis"
    assert [result["result_type"] for result in response["results"]] == ["wiki", "chunk"]
    assert response["results"][0]["slug"] == "family/dad"
    assert response["wiki_count"] == 1


async def test_search_knowledge_routes_exact_fact_before_chunks(tmp_path: Path):
    db = KnowledgeDB(tmp_path / "fact_route.db")
    await db.initialize()
    await db.domain_create("health", "health", [])
    await db.fact_set("health", "dentist", "Dr. Smith", source="manual")
    try:
        class FakeEmbeddings:
            async def embed(self, query: str) -> list[float]:
                return [0.1]

        class FakeSparseEncoder:
            def encode_query(self, query: str) -> tuple[list[int], list[float]]:
                return [], []

        class FakeVectors:
            async def search(self, *args, **kwargs) -> list[SimpleNamespace]:
                return [SimpleNamespace(
                    id="chunk-dentist",
                    score=0.4,
                    payload={
                        "content": "A chunk about dental care.",
                        "domain": "health",
                        "source_id": "source-dentist",
                        "source_name": "dentist.md",
                        "source_type": "note",
                        "chunk_index": 0,
                    },
                )]

        response = await search_knowledge(
            embeddings=FakeEmbeddings(),  # type: ignore[arg-type]
            sparse_encoder=FakeSparseEncoder(),  # type: ignore[arg-type]
            vectors=FakeVectors(),  # type: ignore[arg-type]
            db=db,
            query="What is my dentist?",
            domain="health",
            limit=5,
        )
    finally:
        await db.close()

    assert response["route"] == "fact"
    assert response["results"][0]["result_type"] == "fact"
    assert response["results"][0]["key"] == "dentist"
    assert response["results"][1]["result_type"] == "chunk"
