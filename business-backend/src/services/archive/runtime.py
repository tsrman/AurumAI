"""Process-owned credentials never enter Temporal workflow arguments."""

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
import httpx
from src.core.config import settings
from src.core.database import async_session
from src.services.rag_archive_client import RagArchiveClient
from src.services.archive.service import ArchiveService
from src.services.archive.storage import S3ArchiveStorage


@asynccontextmanager
async def archive_service() -> AsyncIterator[ArchiveService]:
    storage = S3ArchiveStorage(
        settings.ARCHIVE_S3_ENDPOINT or "",
        settings.ARCHIVE_S3_ACCESS_KEY,
        settings.ARCHIVE_S3_SECRET_KEY.get_secret_value(),
        settings.ARCHIVE_S3_BUCKET,
        settings.ARCHIVE_S3_REGION,
    )
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(120, connect=5), transport=httpx.AsyncHTTPTransport(retries=0)
    ) as http:
        rag = RagArchiveClient(
            http, settings.ARCHIVE_RAG_URL, settings.ARCHIVE_RAG_SERVICE_SECRET.get_secret_value()
        )
        yield ArchiveService(async_session, storage, rag, settings.ARCHIVE_RETENTION_DAYS)
