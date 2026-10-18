"""Narrow ordinary-worker fence client; never operator archive credentials."""

from contextvars import ContextVar
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.exceptions import ApplicationError

from src.workflows.index_document import IndexDocumentInput

current_scope: ContextVar[IndexDocumentInput | None] = ContextVar("rag_index_scope", default=None)


@asynccontextmanager
async def fenced_session(
    sessions: async_sessionmaker[AsyncSession], scope: IndexDocumentInput
) -> AsyncIterator[AsyncSession]:
    async with sessions() as db:
        await db.execute(
            text("SELECT rag_writer_fence(:o,:k,:g)"),
            dict(o=UUID(scope.org_id), k=UUID(scope.knowledge_base_id), g=scope.generation),
        )
        yield db


async def begin_vector(
    sessions: async_sessionmaker[AsyncSession],
) -> tuple[IndexDocumentInput, UUID]:
    scope = current_scope.get()
    if scope is None:
        raise ApplicationError("rag_index_scope_required", non_retryable=True)
    operation = uuid4()
    async with sessions() as db:
        await db.execute(
            text("SELECT rag_begin_external(:p,:o,:k,:g,:d,'vector_upsert')"),
            dict(
                p=operation,
                o=UUID(scope.org_id),
                k=UUID(scope.knowledge_base_id),
                g=scope.generation,
                d=UUID(scope.document_id),
            ),
        )
        await db.commit()
    return scope, operation


async def settle_vector(
    sessions: async_sessionmaker[AsyncSession], scope: IndexDocumentInput, operation: UUID
) -> None:
    async with sessions() as db:
        await db.execute(
            text("SELECT rag_settle_external(:p,:o,:k,:g)"),
            dict(
                p=operation,
                o=UUID(scope.org_id),
                k=UUID(scope.knowledge_base_id),
                g=scope.generation,
            ),
        )
        await db.commit()
