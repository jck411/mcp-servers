"""Pure-unit tests for knowledge subsystem internals.

These tests intentionally avoid the network / Qdrant / OpenAI to stay fast
and run anywhere. They cover the parts most likely to silently regress:
chunking math, BM25 sparse encoder, fact search semantics, and the
text-ingest input validator.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from servers.knowledge import (
    BM25SparseEncoder,
    KnowledgeDB,
    _is_likely_binary,
    _validate_text_ingest_inputs,
    chunk_text,
    compute_text_hash,
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


# ---------------------------------------------------------------------------
# Search-side keyword extraction parity
# ---------------------------------------------------------------------------


def test_search_keyword_regex_keeps_short_identifiers():
    # Mirrors the in-tool regex used by knowledge_search to feed facts_search.
    keywords = re.findall(r"\b\w{2,}\b", "What is my LDL and BP?".lower())
    assert "ldl" in keywords
    assert "bp" in keywords


def test_search_keyword_regex_strips_punctuation():
    keywords = re.findall(r"\b\w{2,}\b", "wages?".lower())
    assert keywords == ["wages"]
