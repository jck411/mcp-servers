from pathlib import Path

import pytest

from servers.knowledge import (
    KnowledgeDB,
    apply_curation_item,
    curation_item_has_destructive_actions,
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
