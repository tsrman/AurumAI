"""Separate private archive process. Never mounted in ordinary main.app."""

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Any, Literal
from uuid import UUID

import boto3
from botocore.config import Config
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from pymilvus import MilvusClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from temporalio.client import Client

from src.archive.config import ArchiveSettings
from src.archive.service import ArchiveService
from src.archive.storage import ArchiveStorage
from src.archive.vector_snapshot import VectorSnapshot
from src.lifecycle import ArchiveBlocked, Scope, claim, tenant
from src.index_execution import observe_scope, observe_batch
from src.mutation_transport import GRPC_OPTIONS


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org_id: UUID
    knowledge_base_id: UUID
    archive_id: UUID
    generation: int = Field(gt=0)
    dry_run: bool = False


class InspectCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org_id: UUID
    knowledge_base_id: UUID


def create_app(config: ArchiveSettings) -> FastAPI:
    # The SDK otherwise logs raw RPC exception payloads before our safe handler.
    logging.getLogger("pymilvus").disabled = True
    engine = create_async_engine(
        config.archive_database_url.get_secret_value(), hide_parameters=True
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    source = boto3.client(
        "s3",
        endpoint_url=config.s3_endpoint,
        aws_access_key_id=config.s3_access_key,
        aws_secret_access_key=config.s3_secret_key.get_secret_value(),
        region_name=config.s3_region,
        config=Config(retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=15),
    )
    archive = boto3.client(
        "s3",
        endpoint_url=config.archive_s3_endpoint,
        aws_access_key_id=config.archive_s3_access_key,
        aws_secret_access_key=config.archive_s3_secret_key.get_secret_value(),
        region_name=config.archive_s3_region,
        config=Config(retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=15),
    )
    service = ArchiveService(
        sessions,
        ArchiveStorage(source, config.s3_bucket, archive, config.archive_s3_bucket),
        VectorSnapshot(MilvusClient(uri=config.milvus_uri, grpc_options=GRPC_OPTIONS), config.milvus_collection),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.temporal = await Client.connect(
            config.temporal_host, namespace=config.temporal_namespace
        )

        async def observe() -> None:
            cursor = None
            while True:
                try:
                    cursor = await observe_batch(app.state.temporal, sessions, cursor)
                except Exception:
                    logging.getLogger(__name__).warning("rag_index_observer_unavailable")
                await asyncio.sleep(1 if cursor is not None else 60)

        observer = asyncio.create_task(observe())
        try:
            yield
        finally:
            observer.cancel()
            try:
                await observer
            except asyncio.CancelledError:
                pass
            await engine.dispose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/internal/archive/inspect")
    async def inspect(
        body: InspectCommand, x_archive_service_secret: str = Header(default="")
    ) -> dict[str, Any]:
        secret = config.archive_service_secret.get_secret_value()
        if not secret or not hmac.compare_digest(
            secret.encode(), x_archive_service_secret.encode()
        ):
            raise HTTPException(403, "archive_service_denied")
        async with sessions() as db:
            await tenant(db, body.org_id)
            row = (
                (
                    await db.execute(
                        text(
                            "SELECT state,generation,archive_id FROM rag_kb_lifecycle WHERE org_id=:o AND knowledge_base_id=:k"
                        ),
                        dict(o=body.org_id, k=body.knowledge_base_id),
                    )
                )
                .mappings()
                .one_or_none()
            )
            return dict(row) if row else {"state": "active", "generation": 1, "archive_id": None}

    @app.post("/internal/archive/{action}")
    async def command(
        action: Literal["claim", "export", "purge", "restore", "publish", "release", "status"],
        body: Command,
        x_archive_service_secret: str = Header(default=""),
    ) -> dict[str, Any]:
        secret = config.archive_service_secret.get_secret_value()
        if not secret or not hmac.compare_digest(
            secret.encode(), x_archive_service_secret.encode()
        ):
            raise HTTPException(403, "archive_service_denied")
        scope = Scope(body.org_id, body.knowledge_base_id, body.generation)
        try:
            if action == "claim":
                await observe_scope(app.state.temporal, sessions, scope)
                await claim(sessions, scope, body.archive_id)
                return await service.status(scope, body.archive_id)
            if action == "restore":
                return await service.restore(scope, body.archive_id, dry_run=body.dry_run)
            result: dict[str, Any] = await getattr(service, action)(scope, body.archive_id)
            return result
        except ArchiveBlocked as exc:
            raise HTTPException(409, str(exc)) from None
        except Exception:
            raise HTTPException(503, "archive_dependency_unavailable") from None

    return app


# Uvicorn factory avoids importing/validating private settings in ordinary tests.
def app_factory() -> FastAPI:
    return create_app(ArchiveSettings())  # type: ignore[call-arg]
