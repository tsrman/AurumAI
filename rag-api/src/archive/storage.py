"""Bounded private Parquet parts and immutable source copies."""

import hashlib
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from botocore.exceptions import ClientError

from src.lifecycle import ArchiveBlocked

MAX_PART_BYTES = 16 * 1024 * 1024
MAX_CELL_BYTES = 2 * 1024 * 1024
DOCUMENT_SCHEMA = pa.schema(
    [
        *[
            pa.field(n, pa.string())
            for n in (
                "id",
                "org_id",
                "knowledge_base_id",
                "title",
                "content",
                "parsed_markdown",
                "source_key",
                "source_filename",
                "status",
                "error",
            )
        ],
        pa.field("chunk_count", pa.int64()),
        pa.field("created_at", pa.timestamp("us", tz="UTC")),
        pa.field("updated_at", pa.timestamp("us", tz="UTC")),
    ]
)
VECTOR_SCHEMA = pa.schema(
    [
        *[pa.field(n, pa.string()) for n in ("pk", "org_id", "knowledge_base_id", "document_id")],
        pa.field("chunk_index", pa.int64()),
        pa.field("content", pa.string()),
        pa.field("dense", pa.list_(pa.float32())),
        *[pa.field(n, pa.string()) for n in ("parent_content", "acl_json", "document_title")],
    ]
)
SCHEMAS = {"documents": DOCUMENT_SCHEMA, "vectors": VECTOR_SCHEMA}


def encode_rows(rows: list[dict[str, Any]], kind: str) -> bytes:
    if len(rows) > 128 or any(
        isinstance(v, str) and len(v.encode()) > MAX_CELL_BYTES
        for row in rows
        for v in row.values()
    ):
        raise ArchiveBlocked("artifact_size_limit")
    table = pa.Table.from_pylist(rows, schema=SCHEMAS[kind])
    if table.nbytes > MAX_PART_BYTES:
        raise ArchiveBlocked("artifact_size_limit")
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, row_group_size=128, compression="zstd")
    return bytes(sink.getvalue())


def decode_rows(payload: bytes, kind: str) -> list[dict[str, Any]]:
    if len(payload) > MAX_PART_BYTES:
        raise ArchiveBlocked("artifact_size_limit")
    table = pq.read_table(pa.BufferReader(payload))
    if (
        not table.schema.equals(SCHEMAS[kind])
        or table.num_rows > 128
        or table.nbytes > MAX_PART_BYTES
    ):
        raise ArchiveBlocked("artifact_schema_invalid")
    return list(table.to_pylist())


class ArchiveStorage:
    def __init__(self, source: Any, source_bucket: str, archive: Any, archive_bucket: str):
        self.source = source
        self.source_bucket = source_bucket
        self.archive = archive
        self.archive_bucket = archive_bucket

    def read(self, key: str, *, source: bool = False, missing_ok: bool = False) -> bytes | None:
        client, bucket = (
            (self.source, self.source_bucket) if source else (self.archive, self.archive_bucket)
        )
        try:
            response = client.get_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            if missing_ok and exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise ArchiveBlocked("source_missing" if source else "archive_unavailable") from None
        body = response["Body"]
        try:
            if response["ContentLength"] > MAX_PART_BYTES:
                raise ArchiveBlocked("artifact_size_limit")
            data = body.read(MAX_PART_BYTES + 1)
            if len(data) > MAX_PART_BYTES:
                raise ArchiveBlocked("artifact_size_limit")
            return bytes(data)
        finally:
            body.close()

    def put(self, key: str, payload: bytes, *, source: bool = False) -> dict[str, Any]:
        client, bucket = (
            (self.source, self.source_bucket) if source else (self.archive, self.archive_bucket)
        )
        digest = hashlib.sha256(payload).hexdigest()
        previous = self.read(key, source=source, missing_ok=True)
        if previous is None:
            try:
                client.put_object(Bucket=bucket, Key=key, Body=payload, IfNoneMatch="*")
            except ClientError as exc:
                if exc.response["Error"]["Code"] not in (
                    "PreconditionFailed",
                    "412",
                    "ConditionalRequestConflict",
                ):
                    raise ArchiveBlocked("artifact_upload_failed") from None
        actual = self.read(key, source=source)
        if actual is None or hashlib.sha256(actual).hexdigest() != digest:
            raise ArchiveBlocked("artifact_collision")
        return {"key": key, "sha256": digest, "bytes": len(payload)}

    def verify(self, part: dict[str, Any]) -> bytes:
        data = self.read(part["key"])
        if (
            data is None
            or len(data) != part["bytes"]
            or hashlib.sha256(data).hexdigest() != part["sha256"]
        ):
            raise ArchiveBlocked("artifact_corrupt")
        return data

    def delete_source(self, key: str) -> None:
        self.source.delete_object(Bucket=self.source_bucket, Key=key)
        if self.read(key, source=True, missing_ok=True) is not None:
            raise ArchiveBlocked("source_delete_unverified")

    def digest(
        self, key: str, *, source: bool = False, missing_ok: bool = False
    ) -> tuple[str, int] | None:
        client, bucket = (
            (self.source, self.source_bucket) if source else (self.archive, self.archive_bucket)
        )
        try:
            response = client.get_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            if missing_ok and exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise ArchiveBlocked("source_missing" if source else "archive_unavailable") from None
        body = response["Body"]
        digest = hashlib.sha256()
        size = 0
        try:
            while data := body.read(1024 * 1024):
                size += len(data)
                if size > 1024 * 1024 * 1024:
                    raise ArchiveBlocked("source_size_limit")
                digest.update(data)
        finally:
            body.close()
        if size != response["ContentLength"]:
            raise ArchiveBlocked("source_truncated")
        return digest.hexdigest(), size

    def verify_source(self, part: dict[str, Any]) -> None:
        if self.digest(part["key"]) != (part["sha256"], part["bytes"]):
            raise ArchiveBlocked("artifact_corrupt")

    def copy_source(self, source_key: str, archive_key: str) -> dict[str, Any]:
        return self._copy(source_key, archive_key, to_source=False)

    def restore_source(self, part: dict[str, Any]) -> None:
        self.verify_source(part)
        result = self._copy(part["key"], part["source_key"], to_source=True)
        if (result["sha256"], result["bytes"]) != (part["sha256"], part["bytes"]):
            raise ArchiveBlocked("restore_source_changed")

    def _copy(self, source_key: str, destination_key: str, *, to_source: bool) -> dict[str, Any]:
        import tempfile

        origin, origin_bucket = (
            (self.archive, self.archive_bucket) if to_source else (self.source, self.source_bucket)
        )
        target, target_bucket = (
            (self.source, self.source_bucket) if to_source else (self.archive, self.archive_bucket)
        )
        try:
            response = origin.get_object(Bucket=origin_bucket, Key=source_key)
        except ClientError:
            raise ArchiveBlocked(
                "source_missing" if not to_source else "archive_unavailable"
            ) from None
        body = response["Body"]
        digest = hashlib.sha256()
        size = 0
        # TemporaryFile is unlinked and mode0600, including exceptions/cancellation.
        try:
            with tempfile.TemporaryFile("w+b") as file:
                while data := body.read(1024 * 1024):
                    size += len(data)
                    if size > 1024 * 1024 * 1024:
                        raise ArchiveBlocked("source_size_limit")
                    digest.update(data)
                    file.write(data)
                if size != response["ContentLength"]:
                    raise ArchiveBlocked("source_truncated")
                previous = self.digest(destination_key, source=to_source, missing_ok=True)
                expected = (digest.hexdigest(), size)
                if previous is not None and previous != expected:
                    raise ArchiveBlocked("artifact_collision")
                if previous is None:
                    file.seek(0)
                    try:
                        target.put_object(
                            Bucket=target_bucket,
                            Key=destination_key,
                            Body=file,
                            ContentLength=size,
                            IfNoneMatch="*",
                        )
                    except ClientError as exc:
                        if exc.response["Error"]["Code"] not in (
                            "PreconditionFailed",
                            "412",
                            "ConditionalRequestConflict",
                        ):
                            raise ArchiveBlocked("artifact_upload_failed") from None
                if self.digest(destination_key, source=to_source) != expected:
                    raise ArchiveBlocked("artifact_corrupt")
                return {"key": destination_key, "sha256": expected[0], "bytes": size}
        finally:
            body.close()
