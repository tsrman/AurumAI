"""Operator tool: rebuild knowledge-base vectors from what is already stored.

Run it after changing the embedding model (or the chunking, or to pick up a better parser),
never on a schedule:

    python -m src.reindex --org <org-id> [--kb <kb-id>] [--run-id <label>] [--dry-run]
    python -m src.reindex --all --recreate-collection --yes      # embedding dimension changed

For every document it records an intent, removes the document's vectors, resets the document
to ``pending`` and starts a fresh ``IndexDocumentWorkflow`` for the same document, exactly as
an upload would (same generation fence, same exact-workflow receipts). The workflow reuses the
cached parse, so Docling does not run again unless ``--reparse`` is given.

Safe to repeat: the operation id of a document is derived from ``--run-id`` and its id, so
running again with the same run id skips what was already started and only adopts a start whose
reply was lost. A document is skipped, never forced, while an indexing run is still open, a
Milvus write or delete is unsettled, or its knowledge base is archiving or archived.
"""

import argparse
import asyncio
import logging
import sys
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from src.config import settings
from src.index_execution import verify_execution
from src.lifecycle import ArchiveBlocked, Scope, begin_external, settle_external, tenant
from src.milvus_store import delete_milvus_chunks, get_milvus_client

logger = logging.getLogger("rag.reindex")

STARTED = "started"
ADOPTED = "adopted"
ALREADY = "already"
SKIPPED_RUNNING = "skipped_running"
SKIPPED_UNSETTLED = "skipped_unsettled"
SKIPPED_FROZEN = "skipped_frozen"
SKIPPED_MISSING = "skipped_missing"
FAILED_VECTORS = "failed_vectors"
FAILED_START = "failed_start"
FAILED = {FAILED_VECTORS, FAILED_START}


class ReindexSettings(BaseSettings):
    """Operator credentials: the ``rag_lifecycle`` role, never the ordinary API role."""

    model_config = SettingsConfigDict(env_prefix="RAG_API_", hide_input_in_errors=True)
    reindex_database_url: SecretStr


def operation_id_for(run_id: str, document_id: uuid.UUID) -> uuid.UUID:
    """Stable per run and document, so a repeated run recognises its own work."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"rag-reindex:{run_id}:{document_id}")


async def knowledge_bases(
    sessions: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID | None,
    knowledge_base_id: uuid.UUID | None,
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """(organization, knowledge base) pairs that hold documents, optionally narrowed."""
    query = "SELECT DISTINCT org_id, knowledge_base_id FROM documents WHERE true"
    params: dict[str, Any] = {}
    if org_id is not None:
        query += " AND org_id = :o"
        params["o"] = org_id
    if knowledge_base_id is not None:
        query += " AND knowledge_base_id = :k"
        params["k"] = knowledge_base_id
    async with sessions() as db:
        rows = (await db.execute(text(query + " ORDER BY org_id, knowledge_base_id"), params)).all()
    return [(row[0], row[1]) for row in rows]


async def document_ids(
    sessions: async_sessionmaker[AsyncSession], org_id: uuid.UUID, knowledge_base_id: uuid.UUID
) -> list[uuid.UUID]:
    async with sessions() as db:
        rows = await db.execute(
            text(
                "SELECT id FROM documents WHERE org_id = :o AND knowledge_base_id = :k ORDER BY id"
            ),
            {"o": org_id, "k": knowledge_base_id},
        )
        return [row[0] for row in rows.all()]


def _frozen(exc: DBAPIError) -> bool:
    return getattr(exc.orig, "sqlstate", None) == "55000"


async def reindex_document(
    sessions: async_sessionmaker[AsyncSession],
    client: Client,
    org_id: uuid.UUID,
    knowledge_base_id: uuid.UUID,
    document_id: uuid.UUID,
    run_id: str,
    *,
    reparse: bool = False,
) -> str:
    operation = operation_id_for(run_id, document_id)
    workflow_id = f"rag-index-{document_id}-{operation}"

    async with sessions() as db:
        await tenant(db, org_id)
        try:
            generation = int(
                await db.scalar(
                    text("SELECT rag_writer_fence(:o,:k,NULL)"),
                    {"o": org_id, "k": knowledge_base_id},
                )
            )
        except DBAPIError as exc:
            if _frozen(exc):
                return SKIPPED_FROZEN
            raise
        if await db.scalar(
            text("SELECT NOT EXISTS(SELECT FROM documents WHERE id=:d AND org_id=:o AND knowledge_base_id=:k)"),
            {"d": document_id, "o": org_id, "k": knowledge_base_id},
        ):
            return SKIPPED_MISSING
        existing = (
            (
                await db.execute(
                    text("SELECT * FROM rag_index_executions WHERE operation_id=:p AND org_id=:o"),
                    {"p": operation, "o": org_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            existing_row = dict(existing)
            await db.rollback()
            return await _settle_existing(sessions, client, existing_row)
        open_rows = (
            (
                await db.execute(
                    text(
                        "SELECT * FROM rag_index_executions WHERE org_id=:o AND knowledge_base_id=:k "
                        "AND document_id=:d AND closed_at IS NULL"
                    ),
                    {"o": org_id, "k": knowledge_base_id, "d": document_id},
                )
            )
            .mappings()
            .all()
        )
        unsettled = await db.scalar(
            text(
                "SELECT EXISTS(SELECT FROM rag_external_operations WHERE org_id=:o "
                "AND knowledge_base_id=:k AND document_id=:d AND NOT settled)"
            ),
            {"o": org_id, "k": knowledge_base_id, "d": document_id},
        )
        open_executions = [dict(row) for row in open_rows]
        await db.rollback()

    if unsettled:
        return SKIPPED_UNSETTLED
    for row in open_executions:
        try:
            _, terminal = await verify_execution(client, row)
        except ArchiveBlocked:
            return SKIPPED_RUNNING
        if terminal is None:
            return SKIPPED_RUNNING

    scope = Scope(org_id, knowledge_base_id, generation)
    try:
        intent = await begin_external(sessions, scope, document_id, "vector_delete")
        await delete_milvus_chunks(document_id=str(document_id))
        await settle_external(sessions, scope, intent)
    except Exception:
        # The intent stays unsettled on purpose: the outcome of the Milvus call is unknown,
        # and the platform never guesses. Reconcile before indexing this document again.
        logger.warning("rag_reindex_vectors_unsettled document=%s", document_id)
        return FAILED_VECTORS

    async with sessions() as db:
        await tenant(db, org_id)
        try:
            await db.execute(
                text("SELECT rag_writer_fence(:o,:k,:g)"),
                {"o": org_id, "k": knowledge_base_id, "g": generation},
            )
        except DBAPIError as exc:
            if _frozen(exc):
                return SKIPPED_FROZEN
            raise
        await db.execute(
            text(
                "UPDATE documents SET status='pending', chunk_count=0, error=NULL, "
                "uncontextualized_chunks=0, truncated_chunks=0, "
                + ("parsed_markdown=NULL, " if reparse else "")
                + "updated_at=now() WHERE id=:d AND org_id=:o AND knowledge_base_id=:k"
            ),
            {"d": document_id, "o": org_id, "k": knowledge_base_id},
        )
        await db.execute(
            text("SELECT rag_prepare_index(:p,:o,:k,:g,:d,:w,:q,:n)"),
            {
                "p": operation,
                "o": org_id,
                "k": knowledge_base_id,
                "g": generation,
                "d": document_id,
                "w": workflow_id,
                "q": settings.ingest_task_queue,
                "n": settings.temporal_namespace,
            },
        )
        await db.commit()

    try:
        try:
            handle = await client.start_workflow(
                "IndexDocumentWorkflow",
                {
                    "document_id": str(document_id),
                    "org_id": str(org_id),
                    "knowledge_base_id": str(knowledge_base_id),
                    "operation_id": str(operation),
                    "generation": generation,
                },
                id=workflow_id,
                task_queue=settings.ingest_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
            run_id_started = handle.first_execution_run_id
            if run_id_started is None:
                raise RuntimeError("index_run_identity_missing")
            run = uuid.UUID(run_id_started)
        except WorkflowAlreadyStartedError:
            run, _ = await verify_execution(
                client,
                {
                    "workflow_id": workflow_id,
                    "run_id": None,
                    "document_id": document_id,
                    "operation_id": operation,
                    "org_id": org_id,
                    "knowledge_base_id": knowledge_base_id,
                    "generation": generation,
                    "task_queue": settings.ingest_task_queue,
                    "namespace": settings.temporal_namespace,
                },
            )
        await _register(sessions, org_id, knowledge_base_id, generation, document_id, operation, workflow_id, run)
    except Exception:
        # The execution row is durable; a repeat with the same run id adopts it.
        logger.warning("rag_reindex_start_unsettled document=%s", document_id)
        return FAILED_START
    return STARTED


async def _register(
    sessions: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    knowledge_base_id: uuid.UUID,
    generation: int,
    document_id: uuid.UUID,
    operation: uuid.UUID,
    workflow_id: str,
    run: uuid.UUID,
) -> None:
    async with sessions() as db:
        await db.execute(
            text("SELECT rag_register_index(:p,:o,:k,:g,:d,:w,:r,:q,:n)"),
            {
                "p": operation,
                "o": org_id,
                "k": knowledge_base_id,
                "g": generation,
                "d": document_id,
                "w": workflow_id,
                "r": run,
                "q": settings.ingest_task_queue,
                "n": settings.temporal_namespace,
            },
        )
        await db.commit()


async def _settle_existing(
    sessions: async_sessionmaker[AsyncSession], client: Client, row: dict[str, Any]
) -> str:
    """A repeat of this run: nothing to do, or finish a start whose reply was lost."""
    if row["run_id"] is not None:
        return ALREADY
    try:
        run, _ = await verify_execution(client, row)
        await _register(
            sessions,
            row["org_id"],
            row["knowledge_base_id"],
            row["generation"],
            row["document_id"],
            row["operation_id"],
            row["workflow_id"],
            run,
        )
    except Exception:
        logger.warning("rag_reindex_start_unsettled document=%s", row["document_id"])
        return FAILED_START
    return ADOPTED


def embedding_dimension() -> int | None:
    client = get_milvus_client()
    if not client.has_collection(settings.milvus_collection):
        return None
    description = client.describe_collection(settings.milvus_collection)
    for field in description.get("fields", []):
        if field.get("name") == "dense":
            return int(field["params"]["dim"])
    return None


async def not_active(
    sessions: async_sessionmaker[AsyncSession], pairs: Sequence[tuple[uuid.UUID, uuid.UUID]]
) -> list[tuple[uuid.UUID, uuid.UUID, str]]:
    """Knowledge bases whose lifecycle is not ``active`` (archiving, archived, restoring)."""
    bad: list[tuple[uuid.UUID, uuid.UUID, str]] = []
    for org_id, knowledge_base_id in pairs:
        async with sessions() as db:
            await tenant(db, org_id)
            state = await db.scalar(
                text("SELECT state FROM rag_kb_lifecycle WHERE org_id=:o AND knowledge_base_id=:k"),
                {"o": org_id, "k": knowledge_base_id},
            )
        if state is not None and state != "active":
            bad.append((org_id, knowledge_base_id, str(state)))
    return bad


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.reindex", description="Rebuild knowledge-base vectors."
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true", help="every organization")
    scope.add_argument("--org", type=uuid.UUID, help="one organization")
    parser.add_argument("--kb", type=uuid.UUID, help="one knowledge base (needs --org)")
    parser.add_argument("--run-id", help="label of this run; repeat it to resume (default: new)")
    parser.add_argument("--reparse", action="store_true", help="parse the source files again")
    parser.add_argument("--limit", type=int, help="stop after this many documents")
    parser.add_argument("--dry-run", action="store_true", help="count, change nothing")
    parser.add_argument(
        "--recreate-collection",
        action="store_true",
        help="drop the Milvus collection first (embedding dimension changed); needs --all and --yes",
    )
    parser.add_argument("--yes", action="store_true", help="confirm dropping every vector")
    return parser


def validate(args: argparse.Namespace) -> str | None:
    if args.kb is not None and args.org is None:
        return "--kb needs --org"
    if args.recreate_collection and not args.all:
        return "--recreate-collection needs --all: every knowledge base must be rebuilt"
    if args.recreate_collection and not args.yes:
        return "--recreate-collection drops every vector; add --yes to confirm"
    if args.recreate_collection and args.dry_run:
        return "--recreate-collection cannot be combined with --dry-run"
    if args.limit is not None and args.limit < 1:
        return "--limit must be positive"
    return None


@asynccontextmanager
async def _sessions(url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(url)
    try:
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()


async def run(args: argparse.Namespace, out: Any = sys.stdout) -> int:
    problem = validate(args)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    run_id = args.run_id or uuid.uuid4().hex[:12]
    config = ReindexSettings()  # type: ignore[call-arg]
    async with _sessions(config.reindex_database_url.get_secret_value()) as sessions:
        pairs = await knowledge_bases(sessions, args.org, args.kb)
        blocked = await not_active(sessions, pairs)
        if args.recreate_collection and blocked:
            print(
                f"refusing to drop the collection: {len(blocked)} knowledge base(s) are not active "
                f"(first: {blocked[0][1]} is {blocked[0][2]})",
                file=sys.stderr,
            )
            return 2
        total = 0
        for org_id, knowledge_base_id in pairs:
            total += len(await document_ids(sessions, org_id, knowledge_base_id))
        print(
            f"run {run_id}: {len(pairs)} knowledge base(s), {total} document(s)"
            + (f", {len(blocked)} not active (skipped)" if blocked else ""),
            file=out,
        )
        if args.dry_run:
            return 0

        if args.recreate_collection:
            existing = embedding_dimension()
            print(
                f"dropping collection {settings.milvus_collection!r} "
                f"(dimension {existing}, configured {settings.embedding_dim})",
                file=out,
            )
            get_milvus_client().drop_collection(settings.milvus_collection)

        client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
        outcomes: Counter[str] = Counter()
        done = 0
        for org_id, knowledge_base_id in pairs:
            for document_id in await document_ids(sessions, org_id, knowledge_base_id):
                if args.limit is not None and done >= args.limit:
                    break
                outcome = await reindex_document(
                    sessions,
                    client,
                    org_id,
                    knowledge_base_id,
                    document_id,
                    run_id,
                    reparse=args.reparse,
                )
                outcomes[outcome] += 1
                done += 1
                if outcome in FAILED or outcome.startswith("skipped"):
                    print(f"{outcome} document={document_id} kb={knowledge_base_id}", file=out)
        print(
            "result: " + ", ".join(f"{name}={count}" for name, count in sorted(outcomes.items())),
            file=out,
        )
        print(
            f"re-run with --run-id {run_id} to resume; documents are searchable again as they "
            "reach status 'indexed'",
            file=out,
        )
        return 1 if any(outcomes[name] for name in FAILED) else 0


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    return asyncio.run(run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
