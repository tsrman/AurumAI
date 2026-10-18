import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src import main
from src.db import Document


class _FakeSession:
    """Stands in for AsyncSessionLocal()'s async-context-managed session —
    only implements the calls delete_document/search_documents make."""

    def __init__(self, doc: Document | None = None, live_ids: set[uuid.UUID] | None = None) -> None:
        self._doc = doc
        self._live_ids = live_ids or set()
        self.deleted: list[Document] = []
        self.committed = False
        self.added: list[Document] = []

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, model: type, id: uuid.UUID) -> Document | None:
        return self._doc

    async def delete(self, obj: Document) -> None:
        self.deleted.append(obj)

    async def commit(self) -> None:
        self.committed = True

    def add(self, obj: Document) -> None:
        self.added.append(obj)

    async def scalar(self,stmt: object,params: object=None) -> object:
        return 'active' if 'SELECT state' in str(stmt) else 1

    async def execute(self, stmt: object,params: object=None) -> MagicMock:
        result = MagicMock()
        result.mappings.return_value.all.return_value = []
        result.all.return_value = [(uid,) for uid in self._live_ids]
        return result


def _make_doc(org_id: uuid.UUID, doc_id: uuid.UUID) -> Document:
    return Document(
        id=doc_id,
        org_id=org_id,
        knowledge_base_id=uuid.uuid4(),
        title="t",
        status="indexed",
    )


@pytest.mark.asyncio
async def test_delete_document_removes_closed_index_document() -> None:
    """delete must try to stop a concurrently running
    IndexDocumentWorkflow before touching Milvus, so bulk_insert_milvus
    can't land chunks after delete_milvus_chunks already ran."""
    org_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    doc = _make_doc(org_id, doc_id)
    session = _FakeSession(doc=doc)

    fake_handle = MagicMock()
    fake_handle.cancel = AsyncMock()
    fake_temporal_client = MagicMock()
    fake_temporal_client.get_workflow_handle = MagicMock(return_value=fake_handle)

    with (
        patch("src.main.AsyncSessionLocal", return_value=session),
        patch("src.main.temporal_client", fake_temporal_client),
        patch("src.main.delete_milvus_chunks", AsyncMock(return_value={"deleted": 3})),
    ):
        await main.delete_document(str(doc_id), str(org_id), str(doc.knowledge_base_id))

    assert session.deleted == [doc]
    assert session.committed is True


@pytest.mark.asyncio
async def test_delete_document_tolerates_no_running_workflow_to_cancel() -> None:
    """Cancelling a workflow that already finished or never started raises
    from the SDK — that's expected, not a failure of the delete itself."""
    org_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    doc = _make_doc(org_id, doc_id)
    session = _FakeSession(doc=doc)

    fake_temporal_client = MagicMock()
    fake_temporal_client.get_workflow_handle = MagicMock(side_effect=RuntimeError("not found"))

    with (
        patch("src.main.AsyncSessionLocal", return_value=session),
        patch("src.main.temporal_client", fake_temporal_client),
        patch("src.main.delete_milvus_chunks", AsyncMock(return_value={"deleted": 0})),
    ):
        await main.delete_document(str(doc_id), str(org_id), str(doc.knowledge_base_id))

    assert session.deleted == [doc]
    assert session.committed is True


@pytest.mark.asyncio
async def test_delete_document_does_not_delete_row_when_milvus_delete_fails() -> None:
    """a failed Milvus delete must not be silently
    swallowed while the Postgres row disappears anyway — that permanently
    orphans whatever chunks didn't get removed. The row must survive so the
    client can retry the DELETE."""
    org_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    doc = _make_doc(org_id, doc_id)
    session = _FakeSession(doc=doc)

    fake_handle = MagicMock()
    fake_handle.cancel = AsyncMock()
    fake_temporal_client = MagicMock()
    fake_temporal_client.get_workflow_handle = MagicMock(return_value=fake_handle)

    with (
        patch("src.main.AsyncSessionLocal", return_value=session),
        patch("src.main.temporal_client", fake_temporal_client),
        patch(
            "src.main.delete_milvus_chunks",
            AsyncMock(side_effect=RuntimeError("milvus unreachable")),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await main.delete_document(str(doc_id), str(org_id), str(doc.knowledge_base_id))

    assert exc_info.value.status_code == 503
    assert session.deleted == []
    assert session.committed is True  # Durable RPC intent survives the failure.


@pytest.mark.asyncio
async def test_filter_results_to_live_documents_drops_deleted_document_hits() -> None:
    """a hit for a document that no longer exists in
    Postgres (e.g. deleted mid-race with an in-flight index run) must not
    reach the caller, even though Milvus itself only filtered by org_id."""
    org_id = uuid.uuid4()
    live_doc_id = uuid.uuid4()
    deleted_doc_id = uuid.uuid4()
    results = [
        {"id": "1", "content": "a", "metadata": {"document_id": str(live_doc_id)}},
        {"id": "2", "content": "b", "metadata": {"document_id": str(deleted_doc_id)}},
    ]
    session = _FakeSession(live_ids={live_doc_id})

    with patch("src.main.AsyncSessionLocal", return_value=session):
        filtered = await main._filter_results_to_live_documents(
            results,
            org_id,
            uuid.uuid4(),
        )

    assert [r["id"] for r in filtered] == ["1"]


@pytest.mark.asyncio
async def test_filter_results_to_live_documents_skips_db_round_trip_when_no_hits() -> None:
    session = _FakeSession()
    with patch("src.main.AsyncSessionLocal", return_value=session) as mock_session_factory:
        filtered = await main._filter_results_to_live_documents(
            [],
            uuid.uuid4(),
            uuid.uuid4(),
        )

    assert filtered == []
    mock_session_factory.assert_not_called()


def _make_embedding_httpx_client() -> MagicMock:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"data": [{"embedding": [0.1, 0.2]}]}

    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__.return_value = client
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
async def test_search_documents_clamps_limit_to_search_max_limit() -> None:
    """a caller-supplied limit (including an
    LLM-formed skill arg, via tool-worker's execute_hybrid_search) must
    never reach Milvus unclamped — settings.search_max_limit is the ceiling."""
    org_id = uuid.uuid4()
    req = main.SearchRequest(query="q", limit=10**6)
    fake_search = AsyncMock(return_value=([], []))
    session = _FakeSession()

    with (
        patch("httpx.AsyncClient", return_value=_make_embedding_httpx_client()),
        patch("src.main.search_milvus_hybrid", fake_search),
        patch("src.main.AsyncSessionLocal", return_value=session),
        patch("src.main.settings.search_max_limit", 50),
    ):
        await main.search_documents(req, str(org_id), str(uuid.uuid4()))

    assert fake_search.call_args.kwargs["limit"] == 50


@pytest.mark.asyncio
async def test_search_embedding_request_uses_configured_timeout() -> None:
    """A CPU-only embedding model can need more than the default to answer
    the first query after idling, so operators can raise the bound."""
    client = _make_embedding_httpx_client()
    req = main.SearchRequest(query="q")

    with (
        patch("httpx.AsyncClient", return_value=client),
        patch("src.main.search_milvus_hybrid", AsyncMock(return_value=([], []))),
        patch("src.main.AsyncSessionLocal", return_value=_FakeSession()),
        patch("src.main.settings.search_embedding_timeout_seconds", 42.5),
    ):
        await main.search_documents(req, str(uuid.uuid4()), str(uuid.uuid4()))

    assert client.post.call_args.kwargs["timeout"] == 42.5


def test_search_embedding_timeout_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.config import Settings

    monkeypatch.setenv("RAG_API_DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    assert Settings().search_embedding_timeout_seconds == 10.0
    monkeypatch.setenv("RAG_API_SEARCH_EMBEDDING_TIMEOUT_SECONDS", "45")
    assert Settings().search_embedding_timeout_seconds == 45.0
    monkeypatch.setenv("RAG_API_SEARCH_EMBEDDING_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.asyncio
async def test_create_document_rejects_oversized_content() -> None:
    """an unbounded document turns into thousands of
    per-chunk LLM calls plus one giant embedding request in rag-worker —
    reject it here, before it's ever queued, and don't insert a row."""
    org_id = uuid.uuid4()
    session = _FakeSession()
    fake_temporal_client = MagicMock()

    with (
        patch("src.main.AsyncSessionLocal", return_value=session),
        patch("src.main.temporal_client", fake_temporal_client),
        patch("src.main.settings.document_max_content_bytes", 10),
        pytest.raises(HTTPException) as exc_info,
    ):
        knowledge_base_id = uuid.uuid4()
        await main.create_document(
            main.DocumentCreate(
                title="t",
                content="x" * 1000,
                knowledge_base_id=knowledge_base_id,
            ),
            str(org_id),
            str(knowledge_base_id),
        )

    assert exc_info.value.status_code == 413
    fake_temporal_client.start_workflow.assert_not_called()


def test_document_contract_requires_canonical_knowledge_base_id() -> None:
    with pytest.raises(ValidationError):
        main.DocumentCreate(title="t", content="body")

    knowledge_base_id = uuid.uuid4()
    payload = main.DocumentCreate(
        title="t",
        content="body",
        knowledge_base_id=knowledge_base_id,
    )

    assert payload.knowledge_base_id == knowledge_base_id
    assert Document.__table__.c.knowledge_base_id.nullable is False


@pytest.mark.asyncio
async def test_create_document_rejects_mismatched_trusted_base_id() -> None:
    org_id = uuid.uuid4()
    body_base_id = uuid.uuid4()
    trusted_base_id = uuid.uuid4()

    with pytest.raises(HTTPException) as exc_info:
        await main.create_document(
            main.DocumentCreate(
                title="t",
                content="body",
                knowledge_base_id=body_base_id,
            ),
            str(org_id),
            str(trusted_base_id),
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "Knowledge base ID mismatch"


@pytest.mark.asyncio
async def test_delete_document_hides_document_from_another_knowledge_base() -> None:
    org_id = uuid.uuid4()
    requested_base_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    doc = _make_doc(org_id, doc_id)
    doc.knowledge_base_id = uuid.uuid4()
    session = _FakeSession(doc=doc)

    with (
        patch("src.main.AsyncSessionLocal", return_value=session),
        pytest.raises(HTTPException) as exc_info,
    ):
        await main.delete_document(
            str(doc_id),
            str(org_id),
            str(requested_base_id),
        )

    assert exc_info.value.status_code == 404
    assert session.deleted == []


@pytest.mark.asyncio
async def test_search_scopes_milvus_and_live_document_filter_to_knowledge_base() -> None:
    org_id = uuid.uuid4()
    knowledge_base_id = uuid.uuid4()
    req = main.SearchRequest(query="q", limit=5)
    fake_search = AsyncMock(return_value=([], []))
    session = _FakeSession()

    with (
        patch("httpx.AsyncClient", return_value=_make_embedding_httpx_client()),
        patch("src.main.search_milvus_hybrid", fake_search),
        patch("src.main.AsyncSessionLocal", return_value=session),
    ):
        await main.search_documents(req, str(org_id), str(knowledge_base_id))

    assert fake_search.call_args.kwargs["org_id"] == str(org_id)
    assert fake_search.call_args.kwargs["knowledge_base_id"] == str(knowledge_base_id)
