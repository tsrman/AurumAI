import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from datetime import datetime, timezone
import uuid
import logging
from typing import Any, List, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from src.config import settings
from src.db import Document
from src.lifecycle import (
    ArchiveBlocked,
    Scope,
    begin_external,
    settle_external,
    writer_fence,
    tenant,
)
from src.metrics import MetricsMiddleware, metrics_endpoint
from src.milvus_store import (
    delete_milvus_chunks,
    missing_collection_fields,
    search_milvus_hybrid,
)
from src.telemetry import init_telemetry

logger = logging.getLogger(__name__)

tracer = init_telemetry("rag-api")

engine = create_async_engine(settings.database_url, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
HTTPXClientInstrumentor().instrument()
SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)

temporal_client: Optional[Client] = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global temporal_client
    temporal_client = await Client.connect(
        settings.temporal_host, namespace=settings.temporal_namespace
    )
    # Report a collection that predates knowledge_base_id once, at boot, rather
    # than letting every search quietly return nothing for pre-existing
    # documents. Never fatal: Milvus lives behind the opt-in `rag` profile and
    # may simply not be up yet.
    try:
        missing = await asyncio.to_thread(missing_collection_fields)
        if missing:
            logger.error(
                "Milvus collection is missing %s — existing chunks are unsearchable "
                "until the collection is dropped and every document re-indexed.",
                ", ".join(missing),
            )
    except Exception:
        logger.warning("Could not verify the Milvus collection schema", exc_info=True)
    yield
    await engine.dispose()


app = FastAPI(lifespan=lifespan)
app.add_middleware(MetricsMiddleware)
app.add_api_route("/metrics", metrics_endpoint, methods=["GET"])
FastAPIInstrumentor.instrument_app(app)


@app.exception_handler(DBAPIError)
async def database_error(request: Request, exc: DBAPIError) -> JSONResponse:
    if getattr(exc.orig, "sqlstate", None) == "55000":
        return JSONResponse(status_code=409, content={"detail": "rag_frozen_or_unsettled"})
    return JSONResponse(status_code=503, content={"detail": "rag_database_unavailable"})


async def active_knowledge_base(
    session: AsyncSession, org_id: uuid.UUID, knowledge_base_id: uuid.UUID
) -> bool:
    await tenant(session, org_id)
    state = await session.scalar(
        text("SELECT state FROM rag_kb_lifecycle WHERE org_id=:o AND knowledge_base_id=:k"),
        dict(o=org_id, k=knowledge_base_id),
    )
    return bool(state == "active")


class DocumentCreate(BaseModel):
    operation_id: uuid.UUID | None = None
    title: str
    knowledge_base_id: uuid.UUID
    # Require exactly one of inline document content or a stored source key.
    # create_document checks this; the schema permits both absent so the route
    # can return a clear422 instead of a generic Pydantic ValidationError.
    content: Optional[str] = None
    source_key: Optional[str] = None
    source_filename: Optional[str] = None


class DocumentResponse(BaseModel):
    indexing_operation_id: str | None = None
    generation: int | None = None
    id: str
    knowledge_base_id: str
    title: Optional[str]
    source_filename: Optional[str] = None
    status: str
    chunk_count: int
    error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


@app.post("/v1/documents", response_model=DocumentResponse, status_code=202)
async def create_document(
    doc_in: DocumentCreate,
    x_org_id: str = Header(...),
    x_knowledge_base_id: str = Header(...),
) -> dict[str, Any]:
    try:
        org_id = uuid.UUID(x_org_id)
        trusted_knowledge_base_id = uuid.UUID(x_knowledge_base_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid scope UUID format")
    if doc_in.knowledge_base_id != trusted_knowledge_base_id:
        raise HTTPException(status_code=422, detail="Knowledge base ID mismatch")
    if doc_in.source_key:
        from src.source_ownership import owns_source

        if not owns_source(doc_in.source_key, org_id, trusted_knowledge_base_id):
            raise HTTPException(status_code=422, detail="rag_source_owner_invalid")

    if bool(doc_in.content) == bool(doc_in.source_key):
        raise HTTPException(
            status_code=422,
            detail="Exactly one of `content` or `source_key` must be provided",
        )

    # cap here, before the document is ever queued for
    # indexing — rag-worker has no size check of its own on the content it
    # reads back out of this row. The uploader (business-backend) bounds file
    # size; this service does not yet hold the source bytes.
    if doc_in.content:
        content_bytes = len(doc_in.content.encode("utf-8"))
        if content_bytes > settings.document_max_content_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Document content is {content_bytes} bytes, exceeding the "
                    f"{settings.document_max_content_bytes} byte limit"
                ),
            )

    doc_id = uuid.uuid4()
    operation_id = doc_in.operation_id or uuid.uuid4()
    workflow_id = f"rag-index-{doc_id}-{operation_id}"
    async with AsyncSessionLocal() as session:
        generation = int(
            await session.scalar(
                text("SELECT rag_writer_fence(:o,:k,NULL)"),
                dict(o=org_id, k=trusted_knowledge_base_id),
            )
        )
        existing = (
            (
                await session.execute(
                    text(
                        "SELECT * FROM rag_index_executions WHERE operation_id=:p AND org_id=:o AND knowledge_base_id=:k"
                    ),
                    dict(p=operation_id, o=org_id, k=trusted_knowledge_base_id),
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if existing["generation"] != generation:
                raise HTTPException(409, "rag_operation_generation_conflict")
            doc_id = existing["document_id"]
            workflow_id = existing["workflow_id"]
            original = await session.get(Document, doc_id)
            if original is None or (
                original.title,
                original.content,
                original.source_key,
                original.source_filename,
            ) != (doc_in.title, doc_in.content, doc_in.source_key, doc_in.source_filename):
                raise HTTPException(409, "rag_operation_payload_conflict")
        else:
            new_doc = Document(
                id=doc_id,
                org_id=org_id,
                knowledge_base_id=trusted_knowledge_base_id,
                title=doc_in.title,
                content=doc_in.content,
                source_key=doc_in.source_key,
                source_filename=doc_in.source_filename,
                status="pending",
            )
            session.add(new_doc)
            await session.flush()
            await session.execute(
                text("SELECT rag_prepare_index(:p,:o,:k,:g,:d,:w,:q,:n)"),
                dict(
                    p=operation_id,
                    o=org_id,
                    k=trusted_knowledge_base_id,
                    g=generation,
                    d=doc_id,
                    w=workflow_id,
                    q=settings.ingest_task_queue,
                    n=settings.temporal_namespace,
                ),
            )
        await session.commit()

    assert temporal_client is not None
    try:
        if existing is not None:
            if existing["run_id"] is not None:
                # Never call start again for established history: retention may
                # have removed it, making REJECT_DUPLICATE permit a new run.
                run_id = str(existing["run_id"])
            else:
                # A committed but unestablished start is uncertain. Reconcile
                # existing exact history; missing history never means restart.
                from src.index_execution import verify_execution

                verified_run, _ = await verify_execution(temporal_client, dict(existing))
                run_id = str(verified_run)
        else:
            try:
                handle = await temporal_client.start_workflow(
                    "IndexDocumentWorkflow",
                    {
                        "document_id": str(doc_id),
                        "org_id": str(org_id),
                        "knowledge_base_id": str(trusted_knowledge_base_id),
                        "operation_id": str(operation_id),
                        "generation": generation,
                    },
                    id=workflow_id,
                    task_queue=settings.ingest_task_queue,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )
                started_run_id = handle.first_execution_run_id
                if started_run_id is None:
                    raise RuntimeError("index_run_identity_missing")
                run_id = started_run_id
            except WorkflowAlreadyStartedError:
                from src.index_execution import verify_execution

                verified_run, _ = await verify_execution(temporal_client, dict(
                    workflow_id=workflow_id, run_id=None, document_id=doc_id,
                    operation_id=operation_id, org_id=org_id,
                    knowledge_base_id=trusted_knowledge_base_id, generation=generation,
                    task_queue=settings.ingest_task_queue, namespace=settings.temporal_namespace,
                ))
                run_id = str(verified_run)
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("SELECT rag_register_index(:p,:o,:k,:g,:d,:w,:r,:q,:n)"),
                dict(
                    p=operation_id,
                    o=org_id,
                    k=trusted_knowledge_base_id,
                    g=generation,
                    d=doc_id,
                    w=workflow_id,
                    r=uuid.UUID(run_id),
                    q=settings.ingest_task_queue,
                    n=settings.temporal_namespace,
                ),
            )
            await session.commit()
    except Exception:
        # Uncertain start stays durable for exact history reconciliation.
        logger.error("rag_index_start_unsettled")
        raise HTTPException(status_code=503, detail="rag_index_start_unsettled") from None

    return {
        "indexing_operation_id": str(operation_id),
        "generation": generation,
        "id": str(doc_id),
        "knowledge_base_id": str(trusted_knowledge_base_id),
        "title": doc_in.title,
        "source_filename": doc_in.source_filename,
        "status": "pending",
        "chunk_count": 0,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }


@app.get("/v1/documents", response_model=List[DocumentResponse])
async def list_documents(
    x_org_id: str = Header(...),
    x_knowledge_base_id: str = Header(...),
) -> list[dict[str, Any]]:
    try:
        org_id = uuid.UUID(x_org_id)
        knowledge_base_id = uuid.UUID(x_knowledge_base_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid scope UUID format")

    async with AsyncSessionLocal() as session:
        if not await active_knowledge_base(session, org_id, knowledge_base_id):
            return []
        result = await session.execute(
            select(Document).where(
                Document.org_id == org_id,
                Document.knowledge_base_id == knowledge_base_id,
            )
        )
        docs = result.scalars().all()
        return [
            {
                "id": str(d.id),
                "knowledge_base_id": str(d.knowledge_base_id),
                "title": d.title,
                "source_filename": d.source_filename,
                "status": d.status,
                "chunk_count": d.chunk_count,
                "error": d.error,
                "created_at": d.created_at,
                "updated_at": d.updated_at,
            }
            for d in docs
        ]


@app.get("/v1/documents/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: str,
    x_org_id: str = Header(...),
    x_knowledge_base_id: str = Header(...),
) -> dict[str, Any]:
    try:
        org_id = uuid.UUID(x_org_id)
        knowledge_base_id = uuid.UUID(x_knowledge_base_id)
        doc_uuid = uuid.UUID(document_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid UUID format")

    async with AsyncSessionLocal() as session:
        if not await active_knowledge_base(session, org_id, knowledge_base_id):
            raise HTTPException(404, "Document not found")
        doc = await session.get(Document, doc_uuid)
        if not doc or doc.org_id != org_id or doc.knowledge_base_id != knowledge_base_id:
            raise HTTPException(status_code=404, detail="Document not found")
        return {
            "id": str(doc.id),
            "knowledge_base_id": str(doc.knowledge_base_id),
            "title": doc.title,
            "source_filename": doc.source_filename,
            "status": doc.status,
            "chunk_count": doc.chunk_count,
            "error": doc.error,
            "created_at": doc.created_at,
            "updated_at": doc.updated_at,
        }


@app.delete("/v1/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: str,
    x_org_id: str = Header(...),
    x_knowledge_base_id: str = Header(...),
) -> None:
    try:
        org_id = uuid.UUID(x_org_id)
        knowledge_base_id = uuid.UUID(x_knowledge_base_id)
        doc_uuid = uuid.UUID(document_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid UUID format")

    async with AsyncSessionLocal() as session:
        doc = await session.get(Document, doc_uuid)
        if not doc or doc.org_id != org_id or doc.knowledge_base_id != knowledge_base_id:
            raise HTTPException(status_code=404, detail="Document not found")

        generation = int(
            await session.scalar(
                text("SELECT rag_writer_fence(:o,:k,NULL)"), dict(o=org_id, k=knowledge_base_id)
            )
        )
        executions = (
            (
                await session.execute(
                    text(
                        "SELECT * FROM rag_index_executions WHERE org_id=:o AND knowledge_base_id=:k AND document_id=:d AND closed_at IS NULL"
                    ),
                    dict(o=org_id, k=knowledge_base_id, d=doc_uuid),
                )
            )
            .mappings()
            .all()
        )
        from src.index_execution import verify_execution

        assert temporal_client is not None
        for execution in executions:
            try:
                if (await verify_execution(temporal_client, dict(execution)))[1] is None:
                    raise HTTPException(409, "rag_index_execution_unclosed")
            except ArchiveBlocked:
                raise HTTPException(409, "rag_index_execution_unclosed") from None
        await session.commit()
        scope = Scope(org_id, knowledge_base_id, generation)
        operation = await begin_external(AsyncSessionLocal, scope, doc_uuid, "vector_delete")
        try:
            await delete_milvus_chunks(document_id=document_id)
            await settle_external(AsyncSessionLocal, scope, operation)
        except Exception:
            raise HTTPException(503, "rag_vector_delete_unsettled") from None
        await writer_fence(session, scope)
        await session.delete(doc)
        await session.commit()
    return None


class SearchRequest(BaseModel):
    query: str
    limit: int = 5


@app.post("/v1/search")
async def search_documents(
    req: SearchRequest,
    x_org_id: str = Header(...),
    x_knowledge_base_id: str = Header(...),
) -> dict[str, Any]:
    # rag-api is an internal trust boundary. The public backend validates the
    # caller's live knowledge-base ACL, then supplies this canonical org/KB
    # pair; rag-api must apply both dimensions to every SQL/Milvus lookup.
    try:
        org_id = uuid.UUID(x_org_id)
        knowledge_base_id = uuid.UUID(x_knowledge_base_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid X-Org-Id format")

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{settings.reranker_url}/embeddings",
                json={"model": settings.embedding_model, "input": [req.query]},
                timeout=settings.search_embedding_timeout_seconds,
            )
            response.raise_for_status()
            embedding = response.json()["data"][0]["embedding"]
        except Exception as e:
            logger.error(f"Embedding failed: {e}")
            raise HTTPException(status_code=500, detail="Embedding service unavailable")

    # req.limit is caller-controlled (including via a
    # skill's LLM-formed args, see tool-worker/src/activities.py's
    # execute_hybrid_search branch) — clamp to search_max_limit so a huge
    # value can't force Milvus to fetch millions of rows.
    limit = max(1, min(req.limit, settings.search_max_limit))
    results, warnings = await search_milvus_hybrid(
        query=req.query,
        embedding=embedding,
        limit=limit,
        org_id=str(org_id),
        knowledge_base_id=str(knowledge_base_id),
        acl=None,
    )
    results = await _filter_results_to_live_documents(
        results,
        org_id,
        knowledge_base_id,
    )
    return {"results": results, "warnings": warnings}


async def _filter_results_to_live_documents(
    results: list[dict[str, Any]],
    org_id: uuid.UUID,
    knowledge_base_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Drop hits whose document no longer exists in Postgres — closes the
    residual race window between a concurrent delete and an in-flight
    IndexDocumentWorkflow: Milvus filters search only
    by org_id, so a cancelled-but-not-yet-stopped indexing run can still
    land a few chunks after delete_milvus_chunks already ran."""
    doc_ids: set[uuid.UUID] = set()
    for r in results:
        raw_id = (r.get("metadata") or {}).get("document_id")
        if not raw_id:
            continue
        try:
            doc_ids.add(uuid.UUID(str(raw_id)))
        except ValueError:
            continue
    if not doc_ids:
        return []

    async with AsyncSessionLocal() as session:
        await tenant(session, org_id)
        state = await session.scalar(
            text("SELECT state FROM rag_kb_lifecycle WHERE org_id=:o AND knowledge_base_id=:k"),
            dict(o=org_id, k=knowledge_base_id),
        )
        if state != "active":
            return []
        rows = await session.execute(
            select(Document.id).where(
                Document.org_id == org_id,
                Document.knowledge_base_id == knowledge_base_id,
                Document.id.in_(doc_ids),
            )
        )
        live_ids = {str(row[0]) for row in rows.all()}

    return [r for r in results if (r.get("metadata") or {}).get("document_id") in live_ids]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
