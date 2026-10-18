"""Private service authorization and schema boundary without external effects."""

import pytest
import httpx
from pydantic import SecretStr
from src.archive.config import ArchiveSettings
from src.main_archive import create_app


@pytest.mark.asyncio
async def test_private_api_disabled_secret_denies_even_typed_commands(monkeypatch):
    from src import main_archive

    monkeypatch.setattr(main_archive, "MilvusClient", lambda **kwargs: object())
    monkeypatch.setattr(main_archive.boto3, "client", lambda *args, **kwargs: object())
    config = ArchiveSettings(
        archive_database_url=SecretStr("postgresql+asyncpg://test:test@localhost/test"),
        archive_service_secret=SecretStr(""),
        archive_s3_endpoint="http://archive",
        archive_s3_access_key="fixture",
        archive_s3_secret_key=SecretStr("fixture"),
        archive_s3_bucket="archive",
        s3_endpoint="http://source",
        s3_access_key="fixture",
        s3_secret_key=SecretStr("fixture"),
        s3_bucket="source",
    )
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    body = {
        "org_id": "11111111-1111-1111-1111-111111111111",
        "knowledge_base_id": "22222222-2222-2222-2222-222222222222",
        "archive_id": "33333333-3333-3333-3333-333333333333",
        "generation": 2,
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://private") as client:
        assert (await client.get("/health")).json() == {"status": "ok"}
        assert (await client.post("/internal/archive/claim", json=body)).status_code == 403
        config.archive_service_secret = SecretStr("correct-fixture")
        assert (
            await client.post(
                "/internal/archive/purge", json=body, headers={"X-Archive-Service-Secret": "wrong"}
            )
        ).status_code == 403
        assert (
            await client.post(
                "/internal/archive/purge",
                json={**body, "source_key": "arbitrary"},
                headers={"X-Archive-Service-Secret": "correct-fixture"},
            )
        ).status_code == 422
