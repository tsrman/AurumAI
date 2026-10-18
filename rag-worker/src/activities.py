from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError
import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession

from src.config import settings
from src.lifecycle import fenced_session, current_scope
from src.workflows.index_document import IndexDocumentInput

logger = logging.getLogger(__name__)

engine = create_async_engine(settings.database_url, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@activity.defn(name="mark_document_failed")
async def mark_document_failed(
    document_id: str,
    org_id: str,
    knowledge_base_id: str,
    error_msg: str,
    generation: int = 1,
) -> None:
    scope = IndexDocumentInput(
        document_id=document_id,
        org_id=org_id,
        knowledge_base_id=knowledge_base_id,
        generation=generation,
    )
    async with fenced_session(AsyncSessionLocal, scope) as session:
        await session.execute(
            # raw SQL bypasses the ORM's onupdate=
            # (see db.py's Document.updated_at) — must set it explicitly or
            # any polling/sort-by-updated_at on the documents table breaks.
            text(
                "UPDATE documents SET status = 'failed', error = :error, "
                "updated_at = now() WHERE id = :doc_id AND org_id = :org_id "
                "AND knowledge_base_id = :knowledge_base_id"
            ),
            {
                "error": error_msg,
                "doc_id": document_id,
                "org_id": org_id,
                "knowledge_base_id": knowledge_base_id,
            },
        )
        await session.commit()


async def _parse_source_file(
    *,
    document_id: str,
    org_id: str,
    knowledge_base_id: str,
    source_key: str,
    source_filename: str | None,
    title: str | None,
    generation: int = 1,
) -> tuple[str, str | None]:
    """Download/parse a source file into markdown and title.

    Store parsed content in documents.parsed_markdown so activity retries or
    Milvus reindexing do not repeat expensive downloading and Docling parsing.
    """
    from src.indexing.parser import parse_upload
    from src.object_storage import ObjectStorageError, object_storage

    filename = source_filename or Path(source_key).name

    try:
        raw = await object_storage.get_bytes(source_key)
    except ObjectStorageError as exc:
        # Storage is unconfigured; retrying will not repair that prerequisite.
        raise ApplicationError(str(exc), non_retryable=True) from exc

    if not raw:
        raise ApplicationError(f"Source file {source_key} is empty", non_retryable=True)

    parsed = parse_upload(filename=filename, content=raw)
    if not parsed.text.strip():
        raise ApplicationError(f"Parser produced no text for {filename}", non_retryable=True)

    scope = IndexDocumentInput(
        document_id=document_id,
        org_id=org_id,
        knowledge_base_id=knowledge_base_id,
        generation=generation,
    )
    async with fenced_session(AsyncSessionLocal, scope) as session:
        await session.execute(
            text(
                "UPDATE documents SET parsed_markdown = :md, updated_at = now() "
                "WHERE id = :id AND org_id = :org_id "
                "AND knowledge_base_id = :knowledge_base_id"
            ),
            {
                "md": parsed.text,
                "id": document_id,
                "org_id": org_id,
                "knowledge_base_id": knowledge_base_id,
            },
        )
        await session.commit()

    return parsed.text, title or parsed.title


@activity.defn(name="index_document")
async def index_document(input: IndexDocumentInput) -> None:
    async with fenced_session(AsyncSessionLocal, input) as session:
        result = await session.execute(
            text(
                "SELECT content, parsed_markdown, title, source_key, source_filename "
                "FROM documents WHERE id = :id AND org_id = :org_id "
                "AND knowledge_base_id = :knowledge_base_id"
            ),
            {
                "id": input.document_id,
                "org_id": input.org_id,
                "knowledge_base_id": input.knowledge_base_id,
            },
        )
        row = result.fetchone()

        if not row:
            raise ApplicationError(f"Document {input.document_id} not found", non_retryable=True)

        content, parsed_markdown, title, source_key, source_filename = row

    if not parsed_markdown and source_key:
        # Download uploaded source files and parse them with Docling
        # (PDF/DOCX/PPTX/HTML/MD), falling back to plain text for other formats
        # as defined by indexing/parser.py.
        parsed_markdown, title = await _parse_source_file(
            document_id=input.document_id,
            org_id=input.org_id,
            knowledge_base_id=input.knowledge_base_id,
            source_key=source_key,
            source_filename=source_filename,
            title=title,
            generation=input.generation,
        )
    elif not parsed_markdown and content:
        # Inline content needs no source-file parsing.
        parsed_markdown = content

    if not parsed_markdown:
        raise ApplicationError("Document content is empty", non_retryable=True)

    async with fenced_session(AsyncSessionLocal, input) as session:
        await session.execute(
            text(
                "UPDATE documents SET status = 'indexing', updated_at = now() "
                "WHERE id = :id AND org_id = :org_id "
                "AND knowledge_base_id = :knowledge_base_id"
            ),
            {
                "id": input.document_id,
                "org_id": input.org_id,
                "knowledge_base_id": input.knowledge_base_id,
            },
        )
        await session.commit()

    from src.indexing.ingestion import ingest_parsed_document
    from src.indexing.parser import ParsedDocument

    parsed = ParsedDocument(
        text=parsed_markdown, title=title or "Document", source_type="text", metadata={}
    )

    token = current_scope.set(input)
    try:
        res = await ingest_parsed_document(
            parsed,
            org_id=input.org_id,
            knowledge_base_id=input.knowledge_base_id,
            document_id=input.document_id,
            persist_to_postgres=False,
        )
    finally:
        current_scope.reset(token)

    # ingest_parsed_document already catches Milvus insert failures and
    # reports them as status="partial" (see indexing/ingestion.py) instead
    # of raising — this activity used to ignore that and unconditionally
    # write status='indexed', chunk_count=len(docs) (chunks *built*, not
    # actually inserted), so a Milvus outage produced a document that
    # looked indexed but was unsearchable, with no retry and no failure
    # signal anywhere. Raising here lets this activity's own retry_policy
    # (IndexDocumentWorkflow, up to 3 attempts) retry a transient Milvus
    # failure, then fall through to mark_document_failed if it still fails.
    milvus_stats = res.get("milvus") or {}
    if res.get("status") == "partial":
        raise ApplicationError(
            f"Milvus indexing failed for {milvus_stats.get('errors', 0)} of "
            f"{res.get('chunks_created', 0)} chunks (document {input.document_id})"
        )

    async with fenced_session(AsyncSessionLocal, input) as session:
        await session.execute(
            text(
                "UPDATE documents SET status = 'indexed', chunk_count = :chunk_count, "
                "updated_at = now() WHERE id = :id AND org_id = :org_id "
                "AND knowledge_base_id = :knowledge_base_id"
            ),
            {
                "chunk_count": milvus_stats.get("indexed", res.get("chunks_created", 0)),
                "id": input.document_id,
                "org_id": input.org_id,
                "knowledge_base_id": input.knowledge_base_id,
            },
        )
        await session.commit()


@activity.defn(name="register_index_execution")
async def register_index_execution(input: IndexDocumentInput) -> None:
    if input.operation_id is None:
        raise ApplicationError("legacy_index_provenance_unknown", non_retryable=True)
    info = activity.info()
    from uuid import UUID

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT rag_register_index(:p,:o,:k,:g,:d,:w,:r,:q,:n)"),
            dict(
                p=UUID(input.operation_id),
                o=UUID(input.org_id),
                k=UUID(input.knowledge_base_id),
                g=input.generation,
                d=UUID(input.document_id),
                w=info.workflow_id,
                r=UUID(info.workflow_run_id),
                q=info.task_queue,
                n=info.workflow_namespace,
            ),
        )
        await session.commit()
