"""Thin async S3/MinIO client wrapper.

Wires up the previously-unused S3_* settings in core/config.py (they existed
as dead config with no consumer — MinIO itself was only ever used by
Langfuse). aioboto3 (not boto3) so uploads/downloads don't block the FastAPI
event loop.
"""

from typing import Any, cast

import aioboto3
from botocore.config import Config  # type: ignore[import-untyped]

from src.core.config import settings


class ObjectStorage:
    def __init__(self) -> None:
        self._session = aioboto3.Session()

    def _client(self) -> Any:
        # aioboto3 ships no py.typed marker, so mypy treats its exports as
        # untyped regardless — `Any` here is the honest boundary, not a gap.
        return self._session.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name=settings.S3_REGION,
            config=Config(retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=15),
        )

    async def put_bytes(self, key: str, data: bytes, *, bucket: str | None = None) -> None:
        async with self._client() as s3:
            await s3.put_object(Bucket=bucket or settings.S3_BUCKET, Key=key, Body=data)

    async def get_bytes(self, key: str, *, bucket: str | None = None) -> bytes:
        async with self._client() as s3:
            response = await s3.get_object(Bucket=bucket or settings.S3_BUCKET, Key=key)
            async with response["Body"] as stream:
                return cast(bytes, await stream.read())

    async def delete(self, key: str, *, bucket: str | None = None) -> None:
        async with self._client() as s3:
            await s3.delete_object(Bucket=bucket or settings.S3_BUCKET, Key=key)


object_storage = ObjectStorage()
