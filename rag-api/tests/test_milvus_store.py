import json
from unittest.mock import MagicMock, patch

import pytest

from src.config import settings
from src.milvus_store import (
    init_milvus_collection,
    missing_collection_fields,
    search_milvus_hybrid,
)


def _make_hit(document_id: str, distance: float) -> dict:
    return {
        "id": f"pk-{document_id}",
        "distance": distance,
        "entity": {
            "content": "chunk text",
            "parent_content": "",
            "document_id": document_id,
            "chunk_index": 0,
            "org_id": "org-1",
            "acl_json": json.dumps(["public"]),
            "document_title": "Doc",
        },
    }


@pytest.mark.asyncio
async def test_search_milvus_hybrid_returns_relevance_score_not_score():
    """the skill contract is results[].relevance_score
    (docs/ARCHITECTURE.md) — a "score" key silently
    breaks tool-worker's mapping (always relevance_score=None)."""
    fake_client = MagicMock()
    fake_client.has_collection.return_value = True
    fake_client.hybrid_search.return_value = [[_make_hit("doc-1", 0.42)]]

    with patch("src.milvus_store.get_milvus_client", return_value=fake_client):
        results, warnings = await search_milvus_hybrid(
            query="q",
            embedding=[0.1, 0.2],
            limit=5,
            org_id="org-1",
            knowledge_base_id="base-1",
            acl=None,
        )

    assert warnings == []
    assert len(results) == 1
    assert "relevance_score" in results[0]
    assert results[0]["relevance_score"] == 0.42
    assert "score" not in results[0]


@pytest.mark.asyncio
async def test_search_milvus_hybrid_clamps_limit_to_search_max_limit(monkeypatch):
    """search_milvus_hybrid drives Milvus's fetch_n
    directly — an unclamped limit (e.g. 10**6) would make it fetch millions
    of rows even if a caller forgot to clamp before calling in."""
    monkeypatch.setattr(settings, "search_max_limit", 50)
    fake_client = MagicMock()
    fake_client.has_collection.return_value = True
    fake_client.hybrid_search.return_value = [[]]

    with patch("src.milvus_store.get_milvus_client", return_value=fake_client):
        await search_milvus_hybrid(
            query="q",
            embedding=[0.1, 0.2],
            limit=10**6,
            org_id="org-1",
            knowledge_base_id="base-1",
            acl=None,
        )

    call_kwargs = fake_client.hybrid_search.call_args.kwargs
    assert call_kwargs["limit"] == 200  # fetch_n = max(clamped_limit * 4, clamped_limit)


@pytest.mark.asyncio
async def test_search_milvus_hybrid_filters_every_request_by_org_and_knowledge_base():
    fake_client = MagicMock()
    fake_client.has_collection.return_value = True
    fake_client.hybrid_search.return_value = [[]]

    with (
        patch("src.milvus_store.get_milvus_client", return_value=fake_client),
        patch(
            "src.milvus_store.AnnSearchRequest",
            side_effect=lambda **kwargs: kwargs,
        ) as request_factory,
    ):
        await search_milvus_hybrid(
            query="q",
            embedding=[0.1, 0.2],
            limit=5,
            org_id="org-1",
            knowledge_base_id="base-1",
            acl=None,
        )

    assert [call.kwargs["expr"] for call in request_factory.call_args_list] == [
        'org_id == "org-1" and knowledge_base_id == "base-1"',
        'org_id == "org-1" and knowledge_base_id == "base-1"',
    ]


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
