"""RAG generation fences. External intents survive process and connection loss."""

from dataclasses import dataclass
from uuid import UUID, uuid4
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class ArchiveBlocked(RuntimeError):
    """Only fixed, content-free codes cross the maintenance boundary."""


@dataclass(frozen=True)
class Scope:
    org_id: UUID
    knowledge_base_id: UUID
    generation: int

    def params(self) -> dict[str, Any]:
        return {"o": self.org_id, "k": self.knowledge_base_id, "g": self.generation}


async def tenant(db: AsyncSession, org_id: UUID) -> None:
    await db.execute(text("SELECT set_config('app.current_org_id',:o,true)"), {"o": str(org_id)})


async def writer_fence(db: AsyncSession, scope: Scope) -> int:
    generation = await db.scalar(text("SELECT rag_writer_fence(:o,:k,:g)"), scope.params())
    return int(generation)


async def begin_external(
    sessions: async_sessionmaker[AsyncSession],
    scope: Scope,
    document_id: UUID,
    kind: str,
) -> UUID:
    operation_id = uuid4()
    async with sessions() as db:
        await db.execute(
            text("SELECT rag_begin_external(:p,:o,:k,:g,:d,:t)"),
            {
                **scope.params(),
                "p": operation_id,
                "d": document_id,
                "t": kind,
            },
        )
        await db.commit()
    return operation_id


async def settle_external(
    sessions: async_sessionmaker[AsyncSession],
    scope: Scope,
    operation_id: UUID,
) -> None:
    async with sessions() as db:
        await db.execute(
            text("SELECT rag_settle_external(:p,:o,:k,:g)"),
            {
                **scope.params(),
                "p": operation_id,
            },
        )
        await db.commit()


async def claim(
    sessions: async_sessionmaker[AsyncSession],
    scope: Scope,
    archive_id: UUID,
) -> dict[str, Any]:
    async with sessions() as db:
        await tenant(db, scope.org_id)
        row = (
            (
                await db.execute(
                    text(
                        "SELECT * FROM rag_kb_lifecycle WHERE org_id=:o AND knowledge_base_id=:k FOR UPDATE"
                    ),
                    scope.params(),
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            # Empty hidden KB still gets a durable fence, preventing a later upload.
            if scope.generation != 2:
                raise ArchiveBlocked("generation_conflict")
            await db.execute(
                text("INSERT INTO rag_kb_lifecycle(org_id,knowledge_base_id) VALUES(:o,:k)"),
                scope.params(),
            )
            row = (
                (
                    await db.execute(
                        text(
                            "SELECT * FROM rag_kb_lifecycle WHERE org_id=:o AND knowledge_base_id=:k FOR UPDATE"
                        ),
                        scope.params(),
                    )
                )
                .mappings()
                .one()
            )
        if row["archive_id"] == archive_id and row["generation"] == scope.generation:
            return dict(row)
        if row["state"] != "active" or row["generation"] + 1 != scope.generation:
            raise ArchiveBlocked("generation_conflict")
        if not row["provenance_complete"]:
            raise ArchiveBlocked("legacy_provenance_unknown")
        if await db.scalar(
            text(
                "SELECT EXISTS(SELECT FROM rag_external_operations WHERE org_id=:o AND knowledge_base_id=:k AND NOT settled)"
            ),
            scope.params(),
        ):
            raise ArchiveBlocked("external_operation_unsettled")
        if await db.scalar(
            text(
                "SELECT EXISTS(SELECT FROM rag_index_executions WHERE org_id=:o AND knowledge_base_id=:k AND closed_at IS NULL)"
            ),
            scope.params(),
        ):
            raise ArchiveBlocked("index_execution_unclosed")
        if await db.scalar(
            text(
                "SELECT EXISTS(SELECT FROM documents d WHERE d.org_id=:o AND d.knowledge_base_id=:k AND NOT EXISTS(SELECT FROM rag_index_executions e WHERE e.document_id=d.id AND e.org_id=d.org_id AND e.knowledge_base_id=d.knowledge_base_id AND e.closed_at IS NOT NULL))"
            ),
            scope.params(),
        ):
            raise ArchiveBlocked("document_provenance_unknown")
        ids = (
            (
                await db.execute(
                    text(
                        "SELECT id FROM documents WHERE org_id=:o AND knowledge_base_id=:k ORDER BY id LIMIT 10001"
                    ),
                    scope.params(),
                )
            )
            .scalars()
            .all()
        )
        # Bounded metadata inventory; content is exported separately in parts.
        if len(ids) > 10000:
            raise ArchiveBlocked("inventory_limit")
        import json

        await db.execute(
            text(
                "INSERT INTO rag_archive_jobs(archive_id,org_id,knowledge_base_id,generation,inventory) VALUES(:a,:o,:k,:g,cast(:i AS jsonb))"
            ),
            {**scope.params(), "a": archive_id, "i": json.dumps([str(i) for i in ids])},
        )
        await db.execute(
            text(
                "UPDATE rag_kb_lifecycle SET state='archiving',generation=:g,archive_id=:a WHERE org_id=:o AND knowledge_base_id=:k"
            ),
            {**scope.params(), "a": archive_id},
        )
        await db.commit()
        return {"state": "archiving", "generation": scope.generation, "archive_id": archive_id}
