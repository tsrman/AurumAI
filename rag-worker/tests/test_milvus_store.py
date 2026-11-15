from unittest.mock import MagicMock, patch

import pytest

from src.milvus_store import init_milvus_collection, missing_collection_fields


def _described(*field_names: str) -> dict:
    return {"fields": [{"name": name} for name in field_names]}


def test_missing_collection_fields_flags_a_collection_predating_knowledge_base_id():
    """A collection built before Authorization v2 has no knowledge_base_id
    field, and Milvus cannot backfill one into rows already written — so every
    search, which filters on it, silently skips those chunks."""
    fake_client = MagicMock()
    fake_client.has_collection.return_value = True
    fake_client.describe_collection.return_value = _described(
        "pk", "org_id", "document_id", "chunk_index", "content"
    )

    with patch("src.milvus_store.get_milvus_client", return_value=fake_client):
        assert missing_collection_fields() == ("knowledge_base_id",)


def test_missing_collection_fields_is_empty_for_a_current_collection():
    fake_client = MagicMock()
    fake_client.has_collection.return_value = True
    fake_client.describe_collection.return_value = _described(
        "pk", "org_id", "knowledge_base_id", "document_id", "chunk_index", "content"
    )

    with patch("src.milvus_store.get_milvus_client", return_value=fake_client):
        assert missing_collection_fields() == ()


def test_missing_collection_fields_says_nothing_when_there_is_no_collection_yet():
    """An absent collection is about to be created with the current schema;
    reporting drift there would be a false alarm on every fresh install."""
    fake_client = MagicMock()
    fake_client.has_collection.return_value = False

    with patch("src.milvus_store.get_milvus_client", return_value=fake_client):
        assert missing_collection_fields() == ()


def test_init_refuses_to_bless_a_drifted_collection():
    fake_client = MagicMock()
    fake_client.has_collection.return_value = True
    fake_client.describe_collection.return_value = _described("pk", "org_id", "content")

    with patch("src.milvus_store.get_milvus_client", return_value=fake_client):
        assert init_milvus_collection() is False
    fake_client.create_collection.assert_not_called()


def test_an_undescribable_collection_is_not_reported_as_drifted():
    """Milvus being unreachable is not a schema verdict — returning a false
    positive here would print the re-index instruction on every outage."""
    fake_client = MagicMock()
    fake_client.has_collection.return_value = True
    fake_client.describe_collection.side_effect = RuntimeError("milvus down")

    with patch("src.milvus_store.get_milvus_client", return_value=fake_client):
        assert missing_collection_fields() == ()


@pytest.mark.asyncio
async def test_chunks_are_stored_without_an_access_list_of_their_own() -> None:
    """Access is the knowledge base's live grants, so no copy of it is stored per chunk."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from src import milvus_store
    from src.indexing.schema import ChunkDocument

    assert "acl" not in ChunkDocument.model_fields

    scope = SimpleNamespace(org_id="org", knowledge_base_id="kb", document_id="doc")
    chunk = SimpleNamespace(
        org_id="org",
        knowledge_base_id="kb",
        document_id="doc",
        chunk_index=0,
        content="c",
        parent_content="p",
        document_title="t",
        embedding=[0.1],
    )
    written: list[list[dict]] = []

    def fake_mutate(client, operation, collection, data):
        written.append(data)
        return {"upsert_count": len(data)}

    with (
        patch("src.milvus_store.get_milvus_client"),
        patch("src.milvus_store.init_milvus_collection"),
        patch("src.milvus_store.mutate", fake_mutate),
        patch("src.lifecycle.begin_vector", AsyncMock(return_value=(scope, "op"))),
        patch("src.lifecycle.settle_vector", AsyncMock()),
    ):
        await milvus_store.bulk_insert_milvus([("doc:0", chunk)])

    assert written[0][0]["acl_json"] == "[]"
    assert asyncio.iscoroutinefunction(milvus_store.bulk_insert_milvus)
