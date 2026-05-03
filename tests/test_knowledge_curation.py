from pathlib import Path
from types import SimpleNamespace

import pytest

from servers.knowledge import (
    KnowledgeDB,
    apply_curation_item,
    curation_item_has_destructive_actions,
    delete_source_record,
)


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
    assert listed[0]["source_refs"][0]["conversationId"] == "conv-1"
    assert not curation_item_has_destructive_actions(listed[0])


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
