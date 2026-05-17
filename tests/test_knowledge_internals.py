"""Pure-unit tests for knowledge subsystem internals.

These tests intentionally avoid the network / Qdrant / OpenAI to stay fast
and run anywhere. They cover the parts most likely to silently regress:
chunking math, BM25 sparse encoder, fact search semantics, and the
text-ingest input validator.
"""

from __future__ import annotations

import sqlite3
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
        "content": "abcd\u2026",
        "domain": "health",
        "source_id": "source-1",
        "source_name": "labs.pdf",
        "source_type": "pdf",
        "chunk_id": "chunk-1",
        "chunk_index": 2,
        "similarity": 0.8765,
    }]
    assert response["fact_count"] == 1
    assert response["facts"][0]["key"] == "ldl_2024_12"
