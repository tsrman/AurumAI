from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from temporalio.exceptions import ApplicationError

from src import activities
from src.workflows.index_document import IndexDocumentInput

KNOWLEDGE_BASE_ID = "11111111-1111-1111-1111-111111111111"


class _FakeResult:
    def __init__(self, row: tuple | None) -> None:
        self._row = row

    def fetchone(self) -> tuple | None:
        return self._row


class _FakeSession:
    def __init__(self, select_row: tuple | None = None) -> None:
        self.select_row = select_row
        self.executed: list[tuple[str, dict]] = []
        self.committed = False

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, stmt: object, params: dict | None = None) -> _FakeResult:
        sql = str(stmt)
        self.executed.append((sql, params or {}))
        if sql.strip().upper().startswith("SELECT"):
            return _FakeResult(self.select_row)
        return _FakeResult(None)

    async def commit(self) -> None:
        self.committed = True


@pytest.mark.asyncio
async def test_mark_document_failed_bumps_updated_at() -> None:
    """raw SQL bypasses the ORM's onupdate=, so
    updated_at must be set explicitly or polling/sort-by-updated_at on the
    documents table silently breaks for worker-driven status changes."""
    session = _FakeSession()
    with patch("src.activities.AsyncSessionLocal", return_value=session):
        await activities.mark_document_failed(
            "22222222-2222-2222-2222-222222222222", "33333333-3333-3333-3333-333333333333", KNOWLEDGE_BASE_ID, "boom"
        )

    assert len(session.executed) == 2
    sql, params = session.executed[1]
    assert "updated_at = now()" in sql
    assert "org_id = :org_id" in sql
    assert "knowledge_base_id = :knowledge_base_id" in sql
    assert params == {
        "error": "boom",
        "doc_id": "22222222-2222-2222-2222-222222222222",
        "org_id": "33333333-3333-3333-3333-333333333333",
        "knowledge_base_id": KNOWLEDGE_BASE_ID,
    }
    assert session.committed is True


@pytest.mark.asyncio
async def test_index_document_indexing_transition_bumps_updated_at() -> None:
    session_select = _FakeSession(select_row=("content text", None, "Title", None, None))
    session_indexing = _FakeSession()
    session_final = _FakeSession()
    sessions = iter([session_select, session_indexing, session_final])

    with (
        patch("src.activities.AsyncSessionLocal", side_effect=lambda: next(sessions)),
        patch(
            "src.indexing.ingestion.ingest_parsed_document",
            AsyncMock(return_value={"status": "ok", "chunks_created": 2, "milvus": {"indexed": 2}}),
        ),
    ):
        await activities.index_document(
            IndexDocumentInput(
                document_id="22222222-2222-2222-2222-222222222222",
                org_id="33333333-3333-3333-3333-333333333333",
                knowledge_base_id=KNOWLEDGE_BASE_ID,
            )
        )

    select_sql, select_params = session_select.executed[1]
    assert "knowledge_base_id = :knowledge_base_id" in select_sql
    assert select_params["knowledge_base_id"] == KNOWLEDGE_BASE_ID
    indexing_sql, _ = session_indexing.executed[1]
    assert "status = 'indexing'" in indexing_sql
    assert "updated_at = now()" in indexing_sql

    final_sql, final_params = session_final.executed[1]
    assert "status = 'indexed'" in final_sql
    assert "updated_at = now()" in final_sql
    assert final_params["chunk_count"] == 2


@pytest.mark.asyncio
async def test_index_document_raises_when_not_found() -> None:
    session = _FakeSession(select_row=None)
    with patch("src.activities.AsyncSessionLocal", return_value=session):
        with pytest.raises(ApplicationError, match="not found"):
            await activities.index_document(
                IndexDocumentInput(
                    document_id="44444444-4444-4444-4444-444444444444",
                    org_id="33333333-3333-3333-3333-333333333333",
                    knowledge_base_id=KNOWLEDGE_BASE_ID,
                )
            )


@pytest.mark.asyncio
async def test_index_document_parses_uploaded_file_with_docling() -> None:
    """Uploaded-file records have a source key rather than inline content;
    the worker must download and parse that object."""
    session_select = _FakeSession(
        select_row=(None, None, None, "documents/33333333-3333-3333-3333-333333333333/abc.pdf", "résumé.pdf")
    )
    session_persist = _FakeSession()
    session_indexing = _FakeSession()
    session_final = _FakeSession()
    sessions = iter([session_select, session_persist, session_indexing, session_final])

    parsed = SimpleNamespace(text="# Report\n\nContent", title="résumé")

    with (
        patch("src.activities.AsyncSessionLocal", side_effect=lambda: next(sessions)),
        patch(
            "src.object_storage.object_storage.get_bytes",
            AsyncMock(return_value=b"%PDF-1.4 ..."),
        ) as get_bytes,
        patch("src.indexing.parser.parse_upload", return_value=parsed) as parse_upload,
        patch(
            "src.indexing.ingestion.ingest_parsed_document",
            AsyncMock(return_value={"status": "ok", "chunks_created": 1, "milvus": {"indexed": 1}}),
        ) as ingest,
    ):
        await activities.index_document(
            IndexDocumentInput(
                document_id="55555555-5555-5555-5555-555555555555",
                org_id="33333333-3333-3333-3333-333333333333",
                knowledge_base_id=KNOWLEDGE_BASE_ID,
            )
        )

    get_bytes.assert_awaited_once_with("documents/33333333-3333-3333-3333-333333333333/abc.pdf")
    # The filename extension selects Docling versus plain-text parsing.
    assert parse_upload.call_args.kwargs["filename"] == "résumé.pdf"
    # Cache parsed content so reindexing does not download the source again.
    persist_sql, persist_params = session_persist.executed[1]
    assert "parsed_markdown" in persist_sql
    assert persist_params["md"] == "# Report\n\nContent"
    # Index exactly that parsed content.
    assert ingest.await_args.args[0].text == "# Report\n\nContent"
    assert ingest.await_args.kwargs["knowledge_base_id"] == KNOWLEDGE_BASE_ID


@pytest.mark.asyncio
async def test_index_document_fails_without_content_and_without_file() -> None:
    session_select = _FakeSession(select_row=(None, None, "Title", None, None))

    with patch("src.activities.AsyncSessionLocal", side_effect=lambda: session_select):
        with pytest.raises(ApplicationError, match="empty"):
            await activities.index_document(
                IndexDocumentInput(
                    document_id="66666666-6666-6666-6666-666666666666",
                    org_id="33333333-3333-3333-3333-333333333333",
                    knowledge_base_id=KNOWLEDGE_BASE_ID,
                )
            )


def test_index_document_input_requires_knowledge_base_id() -> None:
    with pytest.raises(ValueError, match="knowledge_base_id"):
        IndexDocumentInput(document_id="22222222-2222-2222-2222-222222222222", org_id="33333333-3333-3333-3333-333333333333")
