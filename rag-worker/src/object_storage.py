"""Read document source files from object storage.

business-backend uploads through POST /v1/documents/upload. This worker
reads the resulting key and does not need storage-write authority.
"""

from typing import Any, cast

import aioboto3

from src.config import settings


class ObjectStorageError(RuntimeError):
    pass


class ObjectStorage:
    def __init__(self) -> None:
        self._session = aioboto3.Session()

    def _client(self) -> Any:
        # aioboto3 does not ship py.typed, so Any marks an explicit external
        # typing boundary rather than an unchecked internal escape.
        return self._session.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
        )

    async def get_bytes(self, key: str) -> bytes:
        if not settings.s3_endpoint:
            raise ObjectStorageError(
                "RAG_WORKER_S3_ENDPOINT is not set; an uploaded document "
                "cannot be read"
            )
        async with self._client() as s3:
            response = await s3.get_object(Bucket=settings.s3_bucket, Key=key)
            async with response["Body"] as stream:
                return cast(bytes, await stream.read())


object_storage = ObjectStorage()
