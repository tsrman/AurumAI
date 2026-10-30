"""Immutable create-only archive objects, single-attempt transport and bounded reads."""

import hashlib
import re
from pathlib import Path
from typing import Protocol, Any
import aioboto3
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from src.services.archive.schemas import MAX_PART, ArchiveFormatError

KEY = re.compile(
    r"^archives/v1/[0-9a-f-]{36}/[0-9a-f-]{36}/(?:[a-z_]+/[0-9]{6}\.parquet|manifest\.json)$"
)
IMAGE_KEY = re.compile(r"^archives/v1/[0-9a-f-]{36}/[0-9a-f-]{36}/(?:images|files)/[0-9a-f-]{36}$")
# The largest image the platform accepts (APP_IMAGES_MAX_BYTES upper bound).
IMAGE_OBJECT_LIMIT = 100 * 1024 * 1024
PLATFORM_IMAGE_KEY = re.compile(
    r"^conversation-images/[0-9a-f-]{36}/[0-9a-f-]{36}/(?:(?:uploads/)?[0-9a-f-]{36}\.(?:png|jpg|webp)"
    r"|files/(?:[0-9a-f-]{36}\.(?:csv|txt|md|json|pdf|xlsx|docx|pptx|zip)"
    r"|uploads/[0-9a-f-]{36}\.(?:csv|txt|md|json|pdf|xlsx|docx|pptx|zip|png|jpg|webp)))$"
)


RAG_READ_KEY = re.compile(
    r"^archives/v1/[0-9a-f-]{36}/[0-9a-f-]{36}/rag/(?:manifest\.json|documents/[0-9a-f-]{36}\.parquet|vectors/[0-9a-f-]{36}/[0-9]+\.parquet|sources/[0-9a-f-]{36})$"
)


def read_limit(key: str) -> int:
    if KEY.fullmatch(key):
        return MAX_PART
    if IMAGE_KEY.fullmatch(key):
        return IMAGE_OBJECT_LIMIT
    if RAG_READ_KEY.fullmatch(key):
        return 1024 * 1024 * 1024 if "/rag/sources/" in key else 16 * 1024 * 1024
    raise ArchiveFormatError("object_invalid")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(65536):
            value.update(block)
    return value.hexdigest()


class ArchiveStorage(Protocol):
    async def upload(self, key: str, path: Path) -> None: ...
    async def download(self, key: str, path: Path) -> None: ...
    async def healthy(self) -> bool: ...


class S3ArchiveStorage:
    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str = "us-east-1",
    ):
        self.endpoint, self.access_key, self.secret_key, self.bucket, self.region = (
            endpoint,
            access_key,
            secret_key,
            bucket,
            region,
        )

    def _client(self) -> Any:
        if not self.endpoint or not self.access_key or not self.secret_key:
            raise ArchiveFormatError("archive_storage_not_configured")
        return aioboto3.Session().client(
            "s3",
            endpoint_url=self.endpoint,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=self.region,
            config=Config(
                connect_timeout=5,
                read_timeout=30,
                retries={"total_max_attempts": 1},
                s3={"addressing_style": "path"},
            ),
        )

    async def upload(self, key: str, path: Path) -> None:
        limit = (
            MAX_PART if KEY.fullmatch(key) else IMAGE_OBJECT_LIMIT if IMAGE_KEY.fullmatch(key) else 0
        )
        if not limit or path.stat().st_size > limit:
            raise ArchiveFormatError("object_invalid")
        # A lost reply can be reconciled only by independent same-hash readback:
        # the delayed request cannot overwrite an existing object (If-None-Match).
        async with self._client() as client:
            try:
                with path.open("rb") as body:
                    await client.put_object(Bucket=self.bucket, Key=key, Body=body, IfNoneMatch="*")
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") not in ("PreconditionFailed", "412"):
                    raise ArchiveFormatError("archive_upload_uncertain") from None
            except Exception:
                raise ArchiveFormatError("archive_upload_uncertain") from None

    async def download(self, key: str, path: Path) -> None:
        limit = read_limit(key)
        try:
            async with self._client() as client:
                result = await client.get_object(Bucket=self.bucket, Key=key)
                if result["ContentLength"] > limit:
                    raise ArchiveFormatError("object_too_large")
                with path.open("xb") as target:
                    path.chmod(0o600)
                    size = 0
                    body = result["Body"]
                    async with body:
                        while block := await body.read(65536):
                            size += len(block)
                            if size > limit:
                                raise ArchiveFormatError("object_too_large")
                            target.write(block)
                if size != result["ContentLength"]:
                    raise ArchiveFormatError("object_truncated")
        except Exception:
            path.unlink(missing_ok=True)
            raise ArchiveFormatError("archive_read_failed") from None

    async def healthy(self) -> bool:
        try:
            async with self._client() as client:
                await client.list_objects_v2(Bucket=self.bucket, Prefix="archives/v1/", MaxKeys=1)
            return True
        except Exception:
            return False


class PlatformImageStorage(Protocol):
    async def size(self, key: str) -> int | None: ...
    async def download(self, key: str, path: Path, limit: int) -> None: ...
    async def upload(self, key: str, path: Path) -> None: ...
    async def delete(self, key: str) -> None: ...


class S3PlatformImageStorage:
    """Conversation image bytes in the platform bucket, reached with separate
    credentials that operators scope to the ``conversation-images/`` prefix."""

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str = "us-east-1",
    ):
        self.endpoint, self.access_key, self.secret_key, self.bucket, self.region = (
            endpoint,
            access_key,
            secret_key,
            bucket,
            region,
        )

    def _client(self) -> Any:
        return aioboto3.Session().client(
            "s3",
            endpoint_url=self.endpoint,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=self.region,
            config=Config(
                connect_timeout=5,
                read_timeout=60,
                retries={"total_max_attempts": 1},
                s3={"addressing_style": "path"},
            ),
        )

    @staticmethod
    def _checked(key: str) -> str:
        if not PLATFORM_IMAGE_KEY.fullmatch(key):
            raise ArchiveFormatError("image_object_invalid")
        return key

    async def size(self, key: str) -> int | None:
        """The object's size, or None when it is missing or unreadable."""
        self._checked(key)
        try:
            async with self._client() as client:
                head = await client.head_object(Bucket=self.bucket, Key=key)
            return int(head["ContentLength"])
        except Exception:
            return None

    async def download(self, key: str, path: Path, limit: int) -> None:
        self._checked(key)
        try:
            async with self._client() as client:
                result = await client.get_object(Bucket=self.bucket, Key=key)
                if result["ContentLength"] > limit:
                    raise ArchiveFormatError("object_too_large")
                with path.open("xb") as target:
                    path.chmod(0o600)
                    size = 0
                    # Read from the streaming body itself; its context manager
                    # yields the raw HTTP response, which has no chunked read.
                    body = result["Body"]
                    async with body:
                        while block := await body.read(65536):
                            size += len(block)
                            if size > limit:
                                raise ArchiveFormatError("object_too_large")
                            target.write(block)
                if size != result["ContentLength"]:
                    raise ArchiveFormatError("object_truncated")
        except Exception:
            path.unlink(missing_ok=True)
            raise ArchiveFormatError("image_read_failed") from None

    async def upload(self, key: str, path: Path) -> None:
        self._checked(key)
        if path.stat().st_size > IMAGE_OBJECT_LIMIT:
            raise ArchiveFormatError("object_invalid")
        try:
            async with self._client() as client:
                with path.open("rb") as body:
                    await client.put_object(Bucket=self.bucket, Key=key, Body=body)
        except Exception:
            raise ArchiveFormatError("image_write_failed") from None

    async def delete(self, key: str) -> None:
        self._checked(key)
        try:
            async with self._client() as client:
                # Deleting a missing key succeeds, so a resumed purge repeats it.
                await client.delete_object(Bucket=self.bucket, Key=key)
        except Exception:
            raise ArchiveFormatError("image_delete_failed") from None
