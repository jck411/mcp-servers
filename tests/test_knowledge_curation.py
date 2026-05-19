from pathlib import Path
from types import SimpleNamespace

import pytest

from servers.knowledge import (
    KnowledgeDB,
    apply_curation_item,
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
