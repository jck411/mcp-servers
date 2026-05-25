from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from servers import knowledge_api
from servers.knowledge import (
    KnowledgeDB,
    apply_curation_item,
    apply_curation_pack_resolution,
    build_curation_question_packs,
    chunk_text,
    create_curation_queue_item,
    curation_item_has_destructive_actions,
    delete_source_record,
    delete_sources_for_overwrite,
    rename_source_record,
)
from servers.knowledge_source_files import resolve_source_path, sanitize_source_filename


@pytest.fixture
async def knowledge_db(tmp_path: Path):
    db = KnowledgeDB(tmp_path / "knowledge.db")
    await db.initialize()
    await db.domain_create("core", "Core test domain", [])
    try:
        yield db
    finally:
        await db.close()


async def test_curation_queue_round_trip(knowledge_db: KnowledgeDB):
    item_id = await knowledge_db.curation_upsert(
        kind="conversation_distill",
        title="Remember a preference",
        summary="User stated a stable preference.",
        source_refs=[{"type": "librechat_conversation", "conversationId": "conv-1"}],
        proposed_actions=[{
            "action": "fact_set",
            "domain": "core",
            "key": "test.preference",
            "value": "Likes concise answers",
        }],
        risk="low",
        confidence=0.9,
        item_id="curation-test",
    )

    assert item_id == "curation-test"

    listed = await knowledge_db.curation_list(status="pending")
    assert [item["id"] for item in listed] == ["curation-test"]
    assert await knowledge_db.curation_count(status="pending") == 1
    assert listed[0]["source_refs"][0]["conversationId"] == "conv-1"
    assert not curation_item_has_destructive_actions(listed[0])


async def test_curation_api_action_routes_accept_slash_ids(
    knowledge_db: KnowledgeDB,
    monkeypatch: pytest.MonkeyPatch,
):
    item_id = "wiki:merge_candidate:family/sanja"
    await knowledge_db.curation_upsert(
        kind="wiki_merge",
        title="Review wiki identity: Sanja",
        proposed_actions=[{"action": "flag_for_review", "slug": "family/sanja"}],
        item_id=item_id,
    )
    monkeypatch.setattr(knowledge_api, "_settings", SimpleNamespace())
    monkeypatch.setattr(knowledge_api, "_embeddings", SimpleNamespace())
    monkeypatch.setattr(knowledge_api, "_sparse_encoder", SimpleNamespace())
    monkeypatch.setattr(knowledge_api, "_vectors", SimpleNamespace())
    monkeypatch.setattr(knowledge_api, "_db", knowledge_db)

    transport = httpx.ASGITransport(app=knowledge_api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        detail = await client.get("/api/curation/item/wiki%3Amerge_candidate%3Afamily%2Fsanja")
        reject = await client.post("/api/curation/reject/wiki%3Amerge_candidate%3Afamily%2Fsanja")

    assert detail.status_code == 200
    assert detail.json()["item"]["id"] == item_id
    assert reject.status_code == 200
    assert reject.json() == {"item_id": item_id, "status": "rejected"}
    assert (await knowledge_db.curation_get(item_id))["status"] == "rejected"


async def test_create_curation_queue_item_accepts_type_alias(knowledge_db: KnowledgeDB):
    result = await create_curation_queue_item(
        db=knowledge_db,
        kind="uncertain_fact",
        notes="Jack might prefer terse updates.",
        actions=[{
            "type": "fact_set",
            "domain": "core",
            "key": "test.preference",
            "value": "Might prefer terse updates",
        }],
        confidence=0.4,
        item_id="create-helper-test",
    )

    assert result["success"] is True
    item = await knowledge_db.curation_get("create-helper-test")
    assert item["title"] == "Jack might prefer terse updates."
    assert item["proposed_actions"][0]["type"] == "fact_set"


async def test_create_curation_queue_item_rejects_unknown_action(knowledge_db: KnowledgeDB):
    result = await create_curation_queue_item(
        db=knowledge_db,
        notes="Bad action should not enter the review queue.",
        actions=[{"type": "launch_confetti"}],
    )

    assert result["success"] is False
    assert "Unsupported curation action" in result["error"]
    assert await knowledge_db.curation_list(status="pending") == []


async def test_pending_upsert_does_not_reopen_reviewed_item(knowledge_db: KnowledgeDB):
    await knowledge_db.curation_upsert(
        kind="split_candidate",
        title="Review schedule split",
        summary="Original concern",
        proposed_actions=[{"type": "flag_for_review"}],
        item_id="reviewed-item",
    )
    assert await knowledge_db.curation_mark_status("reviewed-item", "rejected")

    await knowledge_db.curation_upsert(
        kind="split_candidate",
        title="Review schedule split again",
        summary="Nightly found the same concern again",
        proposed_actions=[{"type": "flag_for_review"}],
        item_id="reviewed-item",
    )

    item = await knowledge_db.curation_get("reviewed-item")
    assert item["status"] == "rejected"
    assert item["summary"] == "Original concern"


async def test_question_packs_group_schedule_items(knowledge_db: KnowledgeDB):
    for item_id, kind, title, summary in (
        (
            "schedule-merge",
            "merge_candidate",
            "Review wiki identity: Coverage 8",
            "coverage_8 and shift_swap_8 describe the same Andison coverage event.",
        ),
        (
            "schedule-split",
            "split_candidate",
            "Review wiki identity: Shift",
            "Shift swap records for Jack and Andison could be split into worker pages.",
        ),
    ):
        await knowledge_db.curation_upsert(
            kind=kind,
            title=title,
            summary=summary,
            source_refs=[{"type": "source", "domain": "work_schedule", "id": item_id}],
            proposed_actions=[{
                "type": "flag_for_review",
                "slug": "work_schedule/coverage-8",
            }],
            item_id=item_id,
        )

    packs = await build_curation_question_packs(knowledge_db)

    assert len(packs) == 1
    pack = packs[0]
    assert pack["id"] == "pack-schedule-cleanup-work-schedule-coverage-exchange"
    assert pack["count"] == 2
    assert set(pack["affected_item_ids"]) == {"schedule-merge", "schedule-split"}
    assert "2026 coverage-exchange" in pack["question"]


async def test_question_packs_split_wiki_identity_by_title(knowledge_db: KnowledgeDB):
    for item_id, title in (
        ("family-sky", "Review wiki identity: Sky"),
        ("family-zoe", "Review wiki identity: Zoe Frankowicz"),
    ):
        await knowledge_db.curation_upsert(
            kind="merge_candidate",
            title=title,
            summary="Potential family page cleanup.",
            proposed_actions=[{
                "type": "flag_for_review",
                "slug": "family/example",
            }],
            item_id=item_id,
        )

    packs = await build_curation_question_packs(knowledge_db, limit=10)

    assert {pack["id"] for pack in packs} == {
        "pack-wiki-identity-family-sky",
        "pack-wiki-identity-family-zoe-frankowicz",
    }


async def test_temporal_question_pack_asks_about_status_not_generic_time_bound(
    knowledge_db: KnowledgeDB,
):
    await knowledge_db.curation_upsert(
        kind="temporal_fact_cleanup",
        title="Review temporal status for finances/msft_position_current_as_of_2026_05_19",
        summary="This fact contains an explicit expiry/end cue but has no structured valid_until date.",
        source_refs=[{
            "type": "fact",
            "domain": "finances",
            "id": "fact-msft",
        }],
        proposed_actions=[{"action": "flag_for_review"}],
        item_id="temporal-finance",
    )

    packs = await build_curation_question_packs(knowledge_db, limit=10)

    pack = next(pack for pack in packs if pack["id"] == "pack-temporal-fact-cleanup-finances-validity-review")
    assert pack["title"] == "finances temporal status review"
    assert "real expiry/end date" in pack["question"]
    assert "Only add valid_until for explicit expiry/end boundaries" in pack["suggested_resolution"]


async def test_apply_question_pack_records_resolution_note(knowledge_db: KnowledgeDB):
    await knowledge_db.curation_upsert(
        kind="split_candidate",
        title="Review wiki identity: Shift",
        summary="Shift swap records for Jack and Andison could be split.",
        source_refs=[{"type": "source", "domain": "work_schedule", "id": "schedule-source"}],
        proposed_actions=[{
            "type": "flag_for_review",
            "slug": "work_schedule/shift",
        }],
        item_id="schedule-item",
    )

    result = await apply_curation_pack_resolution(
        knowledge_db,
        pack_id="pack-schedule-cleanup-work-schedule-coverage-exchange",
        answer="Treat these as one coverage-exchange event and preserve history.",
        resolution_status="applied",
    )

    assert result["success"] is True
    assert result["updated_item_ids"] == ["schedule-item"]
    assert (await knowledge_db.curation_get("schedule-item"))["status"] == "applied"
    note = await knowledge_db.curation_get(result["resolution_note_id"])
    assert note["kind"] == "curation_resolution"
    assert note["status"] == "applied"
    assert "coverage-exchange event" in note["summary"]


async def test_question_pack_apply_blocks_destructive_items(knowledge_db: KnowledgeDB):
    await knowledge_db.curation_upsert(
        kind="maintenance_action",
        title="Domain 'yard' has zero sources and facts",
        summary="Archive empty domain.",
        proposed_actions=[{"action": "archive_domain", "target_id": "yard"}],
        item_id="archive-yard",
    )

    result = await apply_curation_pack_resolution(
        knowledge_db,
        pack_id="pack-maintenance-action-domains-archive-empty-domain",
        answer="Archive the empty domain.",
        resolution_status="applied",
    )

    assert result["success"] is False
    assert result["requires_confirmation"] == "pack-maintenance-action-domains-archive-empty-domain"
    assert (await knowledge_db.curation_get("archive-yard"))["status"] == "pending"


async def test_question_pack_can_reject_destructive_suggestions(knowledge_db: KnowledgeDB):
    await knowledge_db.curation_upsert(
        kind="maintenance_action",
        title="Domain 'yard' has zero sources and facts",
        summary="Archive empty domain.",
        proposed_actions=[{"action": "archive_domain", "target_id": "yard"}],
        item_id="archive-yard",
    )

    result = await apply_curation_pack_resolution(
        knowledge_db,
        pack_id="pack-maintenance-action-domains-archive-empty-domain",
        answer="Empty domains are allowed; ask Jack before archiving.",
        resolution_status="rejected",
    )

    assert result["success"] is True
    assert result["updated_item_ids"] == ["archive-yard"]
    assert (await knowledge_db.curation_get("archive-yard"))["status"] == "rejected"


async def test_apply_non_destructive_curation_item_sets_fact(knowledge_db: KnowledgeDB):
    await knowledge_db.curation_upsert(
        kind="conversation_distill",
        title="Remember a preference",
        proposed_actions=[{
            "action": "fact_set",
            "domain": "core",
            "key": "test.preference",
            "value": "Likes concise answers",
            "source": "unit test",
        }],
        risk="low",
        confidence=0.95,
        item_id="apply-test",
    )

    result = await apply_curation_item(
        "apply-test",
        confirmation=None,
        settings=None,  # type: ignore[arg-type]
        embeddings=None,  # type: ignore[arg-type]
        sparse_encoder=None,  # type: ignore[arg-type]
        vectors=None,  # type: ignore[arg-type]
        db=knowledge_db,
    )

    assert result["success"] is True
    fact = await knowledge_db.fact_get("core", "test.preference")
    assert fact["value"] == "Likes concise answers"
    assert fact["origin_type"] == "curation"
    assert fact["origin_ref"] == "apply-test"
    assert (await knowledge_db.curation_get("apply-test"))["status"] == "applied"


async def test_apply_legacy_review_curation_item_marks_reviewed(knowledge_db: KnowledgeDB):
    await knowledge_db.curation_upsert(
        kind="wiki_merge",
        title="Review wiki identity",
        proposed_actions=[{"action": "merge_candidate", "slug": "core/example"}],
        risk="low",
        confidence=0.8,
        item_id="legacy-review-test",
    )

    result = await apply_curation_item(
        "legacy-review-test",
        confirmation=None,
        settings=None,  # type: ignore[arg-type]
        embeddings=None,  # type: ignore[arg-type]
        sparse_encoder=None,  # type: ignore[arg-type]
        vectors=None,  # type: ignore[arg-type]
        db=knowledge_db,
    )

    assert result["success"] is True
    assert result["results"] == [{"action": "merge_candidate", "status": "reviewed"}]
    assert (await knowledge_db.curation_get("legacy-review-test"))["status"] == "applied"


async def test_apply_reingest_source_curation_item(knowledge_db: KnowledgeDB, tmp_path: Path):
    await knowledge_db.domain_create("tech", "Tech test domain", [])
    knowledge_root = tmp_path / "knowledge"
    source_path = knowledge_root / "tech" / "note.md"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("Reindex this note for search.", encoding="utf-8")

    await knowledge_db.source_add(
        "old-source",
        "tech",
        "md",
        "note.md",
        "old-hash",
        0,
        "tech/note.md",
        "text/markdown",
        source_path.stat().st_size,
    )
    await knowledge_db.curation_upsert(
        kind="maintenance_action",
        title="Reingest note",
        proposed_actions=[{"action": "reingest_source", "target_id": "old-source"}],
        item_id="reingest-test",
    )

    class FakeEmbeddings:
        async def embed_batch(self, texts: list[str]) -> list[list[float]]:
            return [[0.1] for _ in texts]

    class FakeSparseEncoder:
        def fit_batch(self, texts: list[str]) -> None:
            self.fitted = texts

        def encode(self, text: str) -> dict[str, list[float] | list[int]]:
            return {"indices": [0], "values": [1.0]}

    class FakeVectors:
        def __init__(self) -> None:
            self.deleted: list[str] = []
            self.chunks: list[dict] = []

        async def upsert_chunks(self, chunks, dense_vectors, sparse_vectors) -> None:
            self.chunks = chunks

        async def delete_by_source(self, source_id: str) -> None:
            self.deleted.append(source_id)

    vectors = FakeVectors()
    result = await apply_curation_item(
        "reingest-test",
        confirmation=None,
        settings=SimpleNamespace(
            knowledge_path=knowledge_root,
            chunk_max_chars=1000,
            chunk_overlap=200,
            ocr_enabled=False,
        ),  # type: ignore[arg-type]
        embeddings=FakeEmbeddings(),  # type: ignore[arg-type]
        sparse_encoder=FakeSparseEncoder(),  # type: ignore[arg-type]
        vectors=vectors,  # type: ignore[arg-type]
        db=knowledge_db,
    )

    assert result["success"] is True
    applied = result["results"][0]
    assert applied["action"] == "reingest_source"
    assert applied["old_source_id"] == "old-source"
    assert applied["new_source_id"] != "old-source"
    assert applied["chunks_stored"] == 1
    assert vectors.deleted == ["old-source"]
    assert len(vectors.chunks) == 1
    assert await knowledge_db.source_get("old-source") is None
    new_source = await knowledge_db.source_get(applied["new_source_id"])
    assert new_source["filename"] == "note.md"
    assert new_source["chunk_count"] == 1
    assert (await knowledge_db.curation_get("reingest-test"))["status"] == "applied"


async def test_destructive_curation_requires_exact_confirmation(knowledge_db: KnowledgeDB):
    await knowledge_db.curation_upsert(
        kind="maintenance_action",
        title="Archive core",
        proposed_actions=[{"action": "archive_domain", "target_id": "core"}],
        risk="medium",
        confidence=0.8,
        item_id="destructive-test",
    )

    item = await knowledge_db.curation_get("destructive-test")
    assert curation_item_has_destructive_actions(item)

    result = await apply_curation_item(
        "destructive-test",
        confirmation=None,
        settings=None,  # type: ignore[arg-type]
        embeddings=None,  # type: ignore[arg-type]
        sparse_encoder=None,  # type: ignore[arg-type]
        vectors=None,  # type: ignore[arg-type]
        db=knowledge_db,
    )

    assert result["success"] is False
    assert result["requires_confirmation"] == "destructive-test"
    assert (await knowledge_db.domain_get("core"))["archived"] is False


async def test_delete_source_preserves_file_referenced_by_another_source(
    knowledge_db: KnowledgeDB,
    tmp_path: Path,
):
    await knowledge_db.domain_create("pets", "Pets test domain", [])
    image_path = tmp_path / "pets" / "benji.jpg"
    image_path.parent.mkdir()
    image_path.write_bytes(b"new benji bytes")

    await knowledge_db.source_add(
        "old-source",
        "pets",
        "jpg",
        "benji.jpg",
        "old-hash",
        0,
        "pets/benji.jpg",
        "image/jpeg",
        18_883,
    )
    await knowledge_db.source_add(
        "new-source",
        "pets",
        "jpg",
        "benji.jpg",
        "new-hash",
        0,
        "pets/benji.jpg",
        "image/jpeg",
        114_044,
    )

    class FakeVectors:
        async def delete_by_source(self, source_id: str) -> None:
            self.deleted_source_id = source_id

    result = await delete_source_record(
        SimpleNamespace(knowledge_path=tmp_path),  # type: ignore[arg-type]
        FakeVectors(),  # type: ignore[arg-type]
        knowledge_db,
        "old-source",
        delete_file=True,
    )

    assert result["success"] is True
    assert result["deleted_files"] == []
    assert result["preserved_files"] == ["pets/benji.jpg"]
    assert image_path.exists()
    assert await knowledge_db.source_get("old-source") is None
    assert await knowledge_db.source_get("new-source") is not None


async def test_overwrite_cleanup_removes_all_sources_for_filename(
    knowledge_db: KnowledgeDB,
    tmp_path: Path,
):
    await knowledge_db.domain_create("pets", "Pets test domain", [])
    image_path = tmp_path / "pets" / "benji.jpg"
    image_path.parent.mkdir()
    image_path.write_bytes(b"old bytes")

    for source_id, content_hash in (("older-source", "older-hash"), ("newer-source", "newer-hash")):
        await knowledge_db.source_add(
            source_id,
            "pets",
            "jpg",
            "benji.jpg",
            content_hash,
            1,
            "pets/benji.jpg",
            "image/jpeg",
            9,
        )

    class FakeVectors:
        def __init__(self) -> None:
            self.deleted_source_ids: list[str] = []

        async def delete_by_source(self, source_id: str) -> None:
            self.deleted_source_ids.append(source_id)

    vectors = FakeVectors()
    results = await delete_sources_for_overwrite(
        SimpleNamespace(knowledge_path=tmp_path),  # type: ignore[arg-type]
        vectors,  # type: ignore[arg-type]
        knowledge_db,
        "pets",
        "benji.jpg",
    )

    assert [result["source"]["id"] for result in results] == ["newer-source", "older-source"]
    assert vectors.deleted_source_ids == ["newer-source", "older-source"]
    assert not image_path.exists()
    assert await knowledge_db.source_get("older-source") is None
    assert await knowledge_db.source_get("newer-source") is None


async def test_rename_source_does_not_move_file_shared_by_another_source(
    knowledge_db: KnowledgeDB,
    tmp_path: Path,
):
    await knowledge_db.domain_create("pets", "Pets test domain", [])
    image_path = tmp_path / "pets" / "benji.jpg"
    image_path.parent.mkdir()
    image_path.write_bytes(b"benji bytes")

    await knowledge_db.source_add(
        "old-source",
        "pets",
        "jpg",
        "benji.jpg",
        "old-hash",
        0,
        "pets/benji.jpg",
        "image/jpeg",
        11,
    )
    await knowledge_db.source_add(
        "new-source",
        "pets",
        "jpg",
        "benji.jpg",
        "new-hash",
        0,
        "pets/benji.jpg",
        "image/jpeg",
        11,
    )

    class FakeVectors:
        def __init__(self) -> None:
            self.updated: list[tuple[str, str]] = []

        async def update_source_name(self, source_id: str, source_name: str) -> None:
            self.updated.append((source_id, source_name))

    result = await rename_source_record(
        SimpleNamespace(knowledge_path=tmp_path),  # type: ignore[arg-type]
        FakeVectors(),  # type: ignore[arg-type]
        knowledge_db,
        "old-source",
        "benji-old.jpg",
    )

    old_source = await knowledge_db.source_get("old-source")
    new_source = await knowledge_db.source_get("new-source")
    assert result["success"] is True
    assert result["renamed_file"] is False
    assert result["preserved_files"] == ["pets/benji.jpg"]
    assert old_source["filename"] == "benji-old.jpg"
    assert old_source["stored_path"] == "pets/benji.jpg"
    assert new_source["filename"] == "benji.jpg"
    assert image_path.exists()


async def test_source_get_by_domain_filename_returns_newest_match(
    knowledge_db: KnowledgeDB,
):
    await knowledge_db.domain_create("pets", "Pets test domain", [])
    await knowledge_db.source_add(
        "older-source",
        "pets",
        "jpg",
        "benji.jpg",
        "older-hash",
        0,
        "pets/benji.jpg",
        "image/jpeg",
        18_883,
    )
    await knowledge_db.source_add(
        "newer-source",
        "pets",
        "jpg",
        "benji.jpg",
        "newer-hash",
        0,
        "pets/.sources/newer-source/benji.jpg",
        "image/jpeg",
        114_044,
    )

    source = await knowledge_db.source_get_by_domain_filename("pets", "benji.jpg")

    assert source is not None
    assert source["id"] == "newer-source"
    assert source["stored_path"] == "pets/.sources/newer-source/benji.jpg"


def test_chunk_text_splits_single_long_paragraph():
    chunks = chunk_text("a" * 2500, max_chars=1000, overlap=100)

    assert len(chunks) == 3
    assert all(len(chunk) <= 1000 for chunk in chunks)
    assert chunks[1].startswith("a" * 100)


def test_source_filename_sanitizes_windows_paths_and_control_chars(tmp_path: Path):
    assert sanitize_source_filename("C:\\fakepath\\scan\n.pdf") == "scan.pdf"

    root = tmp_path / "knowledge"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")

    source = {"stored_path": "../secret.txt", "domain": "core", "filename": "secret.txt"}
    assert resolve_source_path(root, source) is None
