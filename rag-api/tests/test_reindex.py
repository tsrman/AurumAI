import io
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src import reindex
from src.config import settings
from src.lifecycle import ArchiveBlocked


def _args(*argv: str):
    return reindex.build_parser().parse_args(list(argv))


def test_operation_ids_are_stable_per_run_and_document() -> None:
    doc, other = uuid.uuid4(), uuid.uuid4()
    assert reindex.operation_id_for("run-a", doc) == reindex.operation_id_for("run-a", doc)
    assert reindex.operation_id_for("run-a", doc) != reindex.operation_id_for("run-b", doc)
    assert reindex.operation_id_for("run-a", doc) != reindex.operation_id_for("run-a", other)


def test_a_scope_is_required() -> None:
    with pytest.raises(SystemExit):
        _args()
    with pytest.raises(SystemExit):
        _args("--all", "--org", str(uuid.uuid4()))


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--all", "--kb", str(uuid.uuid4())], "--kb needs --org"),
        (["--org", str(uuid.uuid4()), "--recreate-collection", "--yes"], "needs --all"),
        (["--all", "--recreate-collection"], "add --yes"),
        (["--all", "--recreate-collection", "--yes", "--dry-run"], "dry-run"),
        (["--all", "--limit", "0"], "positive"),
    ],
)
def test_unsafe_or_inconsistent_options_are_refused(argv, message) -> None:
    assert message in (reindex.validate(_args(*argv)) or "")


def test_a_sound_invocation_passes_validation() -> None:
    assert reindex.validate(_args("--org", str(uuid.uuid4()), "--kb", str(uuid.uuid4()))) is None
    assert reindex.validate(_args("--all", "--recreate-collection", "--yes")) is None


@pytest_asyncio.fixture
async def world():
    engine = create_async_engine(settings.database_url)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    org, kb = uuid.uuid4(), uuid.uuid4()
    docs: list[uuid.UUID] = []

    async def add_document(**values) -> uuid.UUID:
        doc = uuid.uuid4()
        docs.append(doc)
        async with sessions() as db:
            await db.execute(
                text(
                    "INSERT INTO documents(id,org_id,knowledge_base_id,title,content,status,chunk_count,"
                    "parsed_markdown,uncontextualized_chunks,truncated_chunks) "
                    "VALUES(:d,:o,:k,'t','body',:status,:chunks,:parsed,3,4)"
                ),
                {
                    "d": doc,
                    "o": org,
                    "k": kb,
                    "status": values.get("status", "indexed"),
                    "chunks": values.get("chunk_count", 5),
                    "parsed": values.get("parsed_markdown", "parsed"),
                },
            )
            await db.commit()
        return doc

    client = MagicMock()
    client.namespace = "default"
    client.start_workflow = AsyncMock(
        side_effect=lambda *a, **k: SimpleNamespace(first_execution_run_id=str(uuid.uuid4()))
    )
    yield SimpleNamespace(
        sessions=sessions, org=org, kb=kb, add_document=add_document, client=client
    )
    async with sessions() as db:
        await db.execute(text("DELETE FROM rag_index_executions WHERE knowledge_base_id=:k"), {"k": kb})
        await db.execute(text("DELETE FROM rag_external_operations WHERE knowledge_base_id=:k"), {"k": kb})
        await db.execute(text("DELETE FROM documents WHERE knowledge_base_id=:k"), {"k": kb})
        await db.execute(text("DELETE FROM rag_kb_lifecycle WHERE knowledge_base_id=:k"), {"k": kb})
        await db.commit()
    await engine.dispose()


async def _row(world, sql: str, **params):
    async with world.sessions() as db:
        return (await db.execute(text(sql), params)).mappings().one_or_none()


async def _reindex(world, doc, run_id="run-1", **kwargs) -> str:
    return await reindex.reindex_document(
        world.sessions, world.client, world.org, world.kb, doc, run_id, **kwargs
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_document_is_reset_and_a_fresh_indexing_run_is_started(world) -> None:
    doc = await world.add_document()
    delete = AsyncMock(return_value={"deleted": 5})

    with patch("src.reindex.delete_milvus_chunks", delete):
        outcome = await _reindex(world, doc)

    assert outcome == reindex.STARTED
    delete.assert_awaited_once_with(document_id=str(doc))
    row = await _row(
        world, "SELECT status,chunk_count,error,uncontextualized_chunks,truncated_chunks,parsed_markdown "
        "FROM documents WHERE id=:d", d=doc,
    )
    assert (row["status"], row["chunk_count"], row["uncontextualized_chunks"], row["truncated_chunks"]) == (
        "pending", 0, 0, 0,
    )
    # The cached parse is kept unless a re-parse is asked for.
    assert row["parsed_markdown"] == "parsed"
    operation = reindex.operation_id_for("run-1", doc)
    execution = await _row(world, "SELECT * FROM rag_index_executions WHERE operation_id=:p", p=operation)
    assert execution["workflow_id"] == f"rag-index-{doc}-{operation}"
    assert execution["run_id"] is not None
    call = world.client.start_workflow.await_args
    assert call.args[0] == "IndexDocumentWorkflow"
    assert call.args[1] == {
        "document_id": str(doc),
        "org_id": str(world.org),
        "knowledge_base_id": str(world.kb),
        "operation_id": str(operation),
        "generation": 1,
    }
    # The vector delete left a settled receipt.
    unsettled = await _row(
        world, "SELECT count(*) AS n FROM rag_external_operations WHERE document_id=:d AND NOT settled", d=doc
    )
    assert unsettled["n"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reparse_forgets_the_cached_parse(world) -> None:
    doc = await world.add_document()
    with patch("src.reindex.delete_milvus_chunks", AsyncMock()):
        await _reindex(world, doc, reparse=True)

    row = await _row(world, "SELECT parsed_markdown FROM documents WHERE id=:d", d=doc)
    assert row["parsed_markdown"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_repeating_a_run_starts_nothing_twice(world) -> None:
    doc = await world.add_document()
    delete = AsyncMock()
    with patch("src.reindex.delete_milvus_chunks", delete):
        assert await _reindex(world, doc) == reindex.STARTED
        assert await _reindex(world, doc) == reindex.ALREADY

    assert world.client.start_workflow.await_count == 1
    assert delete.await_count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_document_with_a_run_still_open_is_left_alone(world) -> None:
    doc = await world.add_document()
    delete = AsyncMock()
    with patch("src.reindex.delete_milvus_chunks", delete):
        assert await _reindex(world, doc, run_id="first") == reindex.STARTED
        # The first run has not finished: a new run must neither delete vectors nor start.
        with patch("src.reindex.verify_execution", AsyncMock(return_value=(uuid.uuid4(), None))):
            assert await _reindex(world, doc, run_id="second") == reindex.SKIPPED_RUNNING

    assert delete.await_count == 1
    assert world.client.start_workflow.await_count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_finished_earlier_run_does_not_block_a_new_one(world) -> None:
    doc = await world.add_document()
    with patch("src.reindex.delete_milvus_chunks", AsyncMock()):
        assert await _reindex(world, doc, run_id="first") == reindex.STARTED
        with patch(
            "src.reindex.verify_execution", AsyncMock(return_value=(uuid.uuid4(), "completed"))
        ):
            assert await _reindex(world, doc, run_id="second") == reindex.STARTED


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unsettled_vector_operation_blocks_the_document(world) -> None:
    doc = await world.add_document()
    async with world.sessions() as db:
        await db.execute(
            text("SELECT rag_begin_external(:p,:o,:k,1,:d,'vector_upsert')"),
            {"p": uuid.uuid4(), "o": world.org, "k": world.kb, "d": doc},
        )
        await db.commit()
    delete = AsyncMock()

    with patch("src.reindex.delete_milvus_chunks", delete):
        assert await _reindex(world, doc) == reindex.SKIPPED_UNSETTLED

    delete.assert_not_awaited()
    world.client.start_workflow.assert_not_awaited()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_knowledge_base_that_is_archiving_is_skipped(world) -> None:
    doc = await world.add_document()
    async with world.sessions() as db:
        await db.execute(
            text(
                "UPDATE rag_kb_lifecycle SET state='archiving',generation=2,archive_id=:a "
                "WHERE org_id=:o AND knowledge_base_id=:k"
            ),
            {"a": uuid.uuid4(), "o": world.org, "k": world.kb},
        )
        await db.commit()
    delete = AsyncMock()

    with patch("src.reindex.delete_milvus_chunks", delete):
        assert await _reindex(world, doc) == reindex.SKIPPED_FROZEN

    delete.assert_not_awaited()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_failed_vector_delete_leaves_an_unsettled_intent_and_changes_nothing_else(world) -> None:
    doc = await world.add_document()

    with patch("src.reindex.delete_milvus_chunks", AsyncMock(side_effect=RuntimeError("milvus down"))):
        assert await _reindex(world, doc) == reindex.FAILED_VECTORS

    row = await _row(world, "SELECT status,chunk_count FROM documents WHERE id=:d", d=doc)
    assert (row["status"], row["chunk_count"]) == ("indexed", 5)
    unsettled = await _row(
        world, "SELECT count(*) AS n FROM rag_external_operations WHERE document_id=:d AND NOT settled", d=doc
    )
    assert unsettled["n"] == 1
    world.client.start_workflow.assert_not_awaited()
    # And the document stays blocked until an operator reconciles the unknown outcome.
    with patch("src.reindex.delete_milvus_chunks", AsyncMock()):
        assert await _reindex(world, doc, run_id="again") == reindex.SKIPPED_UNSETTLED


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_start_whose_reply_was_lost_is_adopted_on_repeat(world) -> None:
    doc = await world.add_document()
    world.client.start_workflow = AsyncMock(side_effect=RuntimeError("reply lost"))
    with patch("src.reindex.delete_milvus_chunks", AsyncMock()):
        assert await _reindex(world, doc) == reindex.FAILED_START

    run = uuid.uuid4()
    with (
        patch("src.reindex.delete_milvus_chunks", AsyncMock()) as delete,
        patch("src.reindex.verify_execution", AsyncMock(return_value=(run, None))),
    ):
        assert await _reindex(world, doc) == reindex.ADOPTED

    delete.assert_not_awaited()
    execution = await _row(
        world, "SELECT run_id FROM rag_index_executions WHERE document_id=:d", d=doc
    )
    assert execution["run_id"] == run


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unverifiable_lost_start_is_reported_not_restarted(world) -> None:
    doc = await world.add_document()
    world.client.start_workflow = AsyncMock(side_effect=RuntimeError("reply lost"))
    with patch("src.reindex.delete_milvus_chunks", AsyncMock()):
        await _reindex(world, doc)
    world.client.start_workflow = AsyncMock()

    with patch("src.reindex.verify_execution", AsyncMock(side_effect=ArchiveBlocked("index_history_unavailable"))):
        assert await _reindex(world, doc) == reindex.FAILED_START

    world.client.start_workflow.assert_not_awaited()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dry_run_counts_and_changes_nothing(world, monkeypatch) -> None:
    doc = await world.add_document()
    monkeypatch.setenv("RAG_API_REINDEX_DATABASE_URL", settings.database_url)
    out = io.StringIO()

    code = await reindex.run(
        _args("--org", str(world.org), "--kb", str(world.kb), "--dry-run", "--run-id", "r"), out
    )

    assert code == 0
    assert "1 knowledge base(s), 1 document(s)" in out.getvalue()
    row = await _row(world, "SELECT status FROM documents WHERE id=:d", d=doc)
    assert row["status"] == "indexed"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_collection_is_not_dropped_while_a_knowledge_base_is_not_active(world, monkeypatch) -> None:
    await world.add_document()
    async with world.sessions() as db:
        await db.execute(
            text(
                "UPDATE rag_kb_lifecycle SET state='archiving',generation=2,archive_id=:a "
                "WHERE org_id=:o AND knowledge_base_id=:k"
            ),
            {"a": uuid.uuid4(), "o": world.org, "k": world.kb},
        )
        await db.commit()
    monkeypatch.setenv("RAG_API_REINDEX_DATABASE_URL", settings.database_url)
    drop = MagicMock()

    with (
        patch("src.reindex.get_milvus_client", return_value=MagicMock(drop_collection=drop)),
        patch("src.reindex.knowledge_bases", AsyncMock(return_value=[(world.org, world.kb)])),
    ):
        code = await reindex.run(_args("--all", "--recreate-collection", "--yes"), io.StringIO())

    assert code == 2
    drop.assert_not_called()
