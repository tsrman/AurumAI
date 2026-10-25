"""Read-only MinIO/S3 client — skill-executor only ever fetches bundles that
business-backend already validated and stored (see
business-backend/src/services/object_storage.py, business-backend/src/
services/skill_bundle.py); it never writes here. Hermes workspace snapshots
are the one thing skill-executor writes, through src/hermes_snapshots.py."""

import asyncio
from typing import Any

import aioboto3

from src.config import settings

# Read in bounded chunks rather than one giant .read() so an oversized
# object aborts mid-stream instead of first being fully buffered into
# memory.
_READ_CHUNK_BYTES = 64 * 1024


class ObjectStorageError(Exception):
    pass


class ObjectStorage:
    def __init__(self) -> None:
        self._session = aioboto3.Session()

    def _client(self) -> Any:
        return self._session.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
        )

    async def get_bytes(self, key: str) -> bytes:
        try:
            async with asyncio.timeout(settings.bundle_fetch_timeout_seconds):
                async with self._client() as s3:
                    response = await s3.get_object(Bucket=settings.s3_bucket, Key=key)
                    content_length = response.get("ContentLength")
                    if content_length is not None and content_length > settings.bundle_max_bytes:
                        raise ObjectStorageError(
                            f"Bundle {key} is {content_length} bytes, "
                            f"exceeding the {settings.bundle_max_bytes} byte limit"
                        )
                    chunks: list[bytes] = []
                    total = 0
                    # Read through `stream` itself, never through what
                    # `async with` yields: response["Body"] is an aiobotocore
                    # StreamingBody (a wrapt ObjectProxy) whose __aenter__ is
                    # forwarded to the wrapped aiohttp ClientResponse and so
                    # returns the *raw* response — and ClientResponse.read()
                    # takes no size argument, so `await raw.read(n)` blows up
                    # with "ClientResponse.read() takes 1 positional argument
                    # but 2 were given" on every real fetch. The context
                    # manager is still worth entering: it's what releases the
                    # connection.
                    stream = response["Body"]
                    async with stream:
                        while True:
                            chunk = await stream.read(_READ_CHUNK_BYTES)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > settings.bundle_max_bytes:
                                raise ObjectStorageError(
                                    f"Bundle {key} exceeded the "
                                    f"{settings.bundle_max_bytes} byte limit while streaming"
                                )
                            chunks.append(chunk)
                    return b"".join(chunks)
        except ObjectStorageError:
            raise
        except TimeoutError as exc:
            raise ObjectStorageError(
                f"Timed out fetching bundle {key} after "
                f"{settings.bundle_fetch_timeout_seconds}s"
            ) from exc
        except Exception as exc:
            raise ObjectStorageError(f"Could not fetch bundle {key}: {exc}") from exc


object_storage = ObjectStorage()
