# ruff: noqa: F811
"""Real immutable MinIO writes with accepted transport reply loss; exact key journal."""

import json
import os
from pathlib import Path
from uuid import UUID
import pytest
from aiobotocore.httpsession import AIOHTTPSession
from botocore.exceptions import ReadTimeoutError
from tests.services.test_business_archive_service import archive_fixture  # noqa:F401
from src.services.archive.service import ArchiveService
from src.services.archive.storage import S3ArchiveStorage


@pytest.mark.skipif(
    not os.getenv("ARCHIVE_TEST_S3_ENDPOINT"), reason="isolated archive MinIO required"
)
async def test_real_minio_accepted_put_loss_reconciles_immutable_bytes(
    archive_fixture, tmp_path, monkeypatch
):
    endpoint = os.environ["ARCHIVE_TEST_S3_ENDPOINT"]
    assert endpoint == "http://127.0.0.1:59000"
    sessions, org, conversation = archive_fixture
    keys = []
    journal = Path("/tmp") / f"business-archive-s3-{org}.json"

    def record():
        journal.write_text(json.dumps({"org_id": str(org), "keys": keys}))
        journal.chmod(0o600)

    class Recorded(S3ArchiveStorage):
        async def upload(self, key, path):
            if key not in keys:
                keys.append(key)
                record()
            await super().upload(key, path)

    storage = Recorded(
        endpoint, "lifecycle-archives", "lifecycle-archives-password", "lifecycle-archives"
    )
    assert await storage.healthy()
    original = AIOHTTPSession.send
    counts = {}

    async def accepted_loss(self, request):
        response = await original(self, request)
        if request.method == "PUT":
            counts[request.url] = counts.get(request.url, 0) + 1
            assert response.status_code == 200
            if counts[request.url] == 1:
                raise ReadTimeoutError(endpoint_url=endpoint)
            raise AssertionError("hidden_second_submission")
        return response

    monkeypatch.setattr(AIOHTTPSession, "send", accepted_loss)
    service = ArchiveService(sessions, storage)
    result = await service.run(org, conversation, apply=True)
    assert counts and set(counts.values()) == {1}
    output = tmp_path / "export"
    await service.export(org, result["archive_id"], output)
    assert output.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in output.iterdir())
    await service.restore(org, result["archive_id"], apply=True)
    assert (await service.list(org))[0]["state"] == "restored"
    # Every fault was injected only after the real server replied200. That gives
    # exact completion evidence for fixture cleanup, not a production force-ack.
    monkeypatch.setattr(AIOHTTPSession, "send", original)
    import boto3
    from botocore.config import Config

    admin = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id="lifecycle-minio",
        aws_secret_access_key="lifecycle-minio-password",
        config=Config(retries={"total_max_attempts": 1}),
    )
    for key in keys:
        assert key.startswith(f"archives/v1/{org}/")
        UUID(key.split("/")[3])
        admin.delete_object(Bucket="lifecycle-archives", Key=key)
    journal.unlink()
