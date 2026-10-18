from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import settings
from src.indexing.ingestion import _embed_texts, ingest_parsed_document
from src.indexing.parser import ParsedDocument

KNOWLEDGE_BASE_ID = "11111111-1111-1111-1111-111111111111"


def _parsed(text: str) -> ParsedDocument:
    return ParsedDocument(title="Doc", text=text, source_type="text", metadata={})


@pytest.mark.asyncio
async def test_ingest_uses_deterministic_chunk_pk_per_document_and_index(monkeypatch) -> None:
    """chunk_id must be derived from (document_id,
    chunk_index), not uuid4() — bulk_insert_milvus upserts by pk, so a
    retried activity must reuse the same pk to overwrite rather than
    duplicate a prior partial attempt's rows."""
    monkeypatch.setattr(settings, "contextual_retrieval_enabled", False)
    document_id = "doc-123"
    text = "First sentence here. Second sentence follows. Third one too."

    captured_docs: list[list[tuple[str, object]]] = []

    async def fake_bulk_insert(docs: list[tuple[str, object]]) -> dict:
        captured_docs.append(docs)
        return {"indexed": len(docs), "errors": 0, "skipped": False}

    with (
        patch("src.indexing.ingestion.activity.heartbeat"),
        patch(
            "src.indexing.ingestion._embed_texts",
            AsyncMock(side_effect=lambda texts: [[0.1] * 1536 for _ in texts]),
        ),
        patch("src.indexing.ingestion.bulk_insert_milvus", fake_bulk_insert),
    ):
        result = await ingest_parsed_document(
            _parsed(text),
            org_id="org-1",
            knowledge_base_id=KNOWLEDGE_BASE_ID,
            document_id=document_id,
        )

    assert result["document_id"] == document_id
    assert captured_docs, "bulk_insert_milvus should have been called"
    docs = captured_docs[0]
    chunk_ids = [chunk_id for chunk_id, _ in docs]

    # Deterministic and matches the documented f"{document_id}:{chunk_index}" scheme.
    expected = [f"{document_id}:{chunk_doc.chunk_index}" for _, chunk_doc in docs]
    assert chunk_ids == expected
    # No duplicates within a single run either.
    assert len(chunk_ids) == len(set(chunk_ids))
    assert {chunk_doc.knowledge_base_id for _, chunk_doc in docs} == {
        KNOWLEDGE_BASE_ID
    }


@pytest.mark.asyncio
async def test_ingest_retry_reuses_identical_chunk_pks(monkeypatch) -> None:
    """A second run over the same document (simulating a Temporal activity
    retry after a partial failure) must produce the exact same pk set, so
    bulk_insert_milvus's upsert overwrites instead of duplicating."""
    monkeypatch.setattr(settings, "contextual_retrieval_enabled", False)
    document_id = "doc-retry"
    text = "Alpha sentence. Beta sentence. Gamma sentence."

    async def fake_bulk_insert(docs: list[tuple[str, object]]) -> dict:
        return {"indexed": len(docs), "errors": 0, "skipped": False}

    async def _run() -> list[str]:
        captured: list[list[tuple[str, object]]] = []

        async def _capture(docs: list[tuple[str, object]]) -> dict:
            captured.append(docs)
            return await fake_bulk_insert(docs)

        with (
            patch("src.indexing.ingestion.activity.heartbeat"),
            patch(
                "src.indexing.ingestion._embed_texts",
                AsyncMock(side_effect=lambda texts: [[0.1] * 1536 for _ in texts]),
            ),
            patch("src.indexing.ingestion.bulk_insert_milvus", _capture),
        ):
            await ingest_parsed_document(
                _parsed(text),
                org_id="org-1",
                knowledge_base_id=KNOWLEDGE_BASE_ID,
                document_id=document_id,
            )
        return [chunk_id for chunk_id, _ in captured[0]]

    first_run_ids = await _run()
    second_run_ids = await _run()

    assert first_run_ids == second_run_ids


@pytest.mark.asyncio
async def test_ingest_truncates_chunks_over_max_chunks_per_document(monkeypatch) -> None:
    """a document that chunks into more pieces than the
    configured cap must be truncated, not drive one contextualization LLM
    call per chunk unbounded."""
    monkeypatch.setattr(settings, "contextual_retrieval_enabled", False)
    monkeypatch.setattr(settings, "max_chunks_per_document", 2)
    document_id = "doc-many-chunks"
    text = "Alpha sentence. Beta sentence. Gamma sentence. Delta sentence."

    captured_docs: list[list[tuple[str, object]]] = []

    async def fake_bulk_insert(docs: list[tuple[str, object]]) -> dict:
        captured_docs.append(docs)
        return {"indexed": len(docs), "errors": 0, "skipped": False}

    with (
        patch("src.indexing.ingestion.activity.heartbeat"),
        patch(
            "src.indexing.ingestion._embed_texts",
            AsyncMock(side_effect=lambda texts: [[0.1] * 1536 for _ in texts]),
        ),
        patch("src.indexing.ingestion.bulk_insert_milvus", fake_bulk_insert),
    ):
        result = await ingest_parsed_document(
            _parsed(text),
            org_id="org-1",
            knowledge_base_id=KNOWLEDGE_BASE_ID,
            document_id=document_id,
        )

    assert result["chunks_created"] == 2
    assert len(captured_docs[0]) == 2


@pytest.mark.asyncio
async def test_embed_texts_batches_requests_by_embed_batch_size(monkeypatch) -> None:
    """embed_batch_size was declared but unused — a
    single request covering every chunk in a large document was the one
    place still likely to blow the 120s embedding-call timeout."""
    monkeypatch.setattr(settings, "embed_batch_size", 2)
    texts = ["a", "b", "c", "d", "e"]

    posted_batches: list[list[str]] = []

    def _fake_post(url: str, json: dict, timeout: float) -> MagicMock:
        batch = json["input"]
        posted_batches.append(batch)
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {
            "data": [{"embedding": [0.0]} for _ in batch]
        }
        return response

    client = MagicMock()
    client.post = AsyncMock(side_effect=_fake_post)
    client.__aenter__.return_value = client
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=client):
        embeddings = await _embed_texts(texts)

    assert len(embeddings) == 5
    assert posted_batches == [["a", "b"], ["c", "d"], ["e"]]
