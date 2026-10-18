"""Independent validation/export of the reviewed v1 private RAG artifact contract."""

import json
import math
import re
from pathlib import Path
from typing import Any
from uuid import UUID
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
from src.services.archive.storage import ArchiveStorage, digest
from src.services.archive.schemas import ArchiveFormatError

DOCUMENT_SCHEMA = pa.schema(
    [
        *[
            pa.field(name, pa.string())
            for name in (
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
        *[
            pa.field(name, pa.string())
            for name in ("pk", "org_id", "knowledge_base_id", "document_id")
        ],
        pa.field("chunk_index", pa.int64()),
        pa.field("content", pa.string()),
        pa.field("dense", pa.list_(pa.float32())),
        *[pa.field(name, pa.string()) for name in ("parent_content", "acl_json", "document_title")],
    ]
)


def parquet_rows(path: Path, kind: str) -> list[dict[str, Any]]:
    parquet = pq.ParquetFile(path)
    schema = DOCUMENT_SCHEMA if kind == "documents" else VECTOR_SCHEMA
    if not parquet.schema_arrow.equals(schema) or parquet.metadata.num_rows > 128:
        raise ArchiveFormatError("rag_artifact_schema_mismatch")
    table = parquet.read()
    if table.nbytes > 16 * 1024 * 1024:
        raise ArchiveFormatError("rag_artifact_too_large")
    result: list[dict[str, Any]] = table.to_pylist()
    for row in result:
        if any(
            isinstance(value, str) and len(value.encode()) > 2 * 1024 * 1024
            for value in row.values()
        ):
            raise ArchiveFormatError("rag_artifact_too_large")
    return result


async def verify_rag(
    storage: ArchiveStorage, org: UUID, link: dict[str, Any], directory: Path
) -> dict[str, dict[str, Any]]:
    archive = UUID(link["archive_id"])
    kb = UUID(link["knowledge_base_id"])
    prefix = f"archives/v1/{org}/{archive}/rag/"
    root = directory / "rag"
    root.mkdir(mode=0o700)
    manifest_path = root / "manifest.json"
    await storage.download(prefix + "manifest.json", manifest_path)
    if (
        manifest_path.stat().st_size > 8 * 1024 * 1024
        or digest(manifest_path) != link["receipt_hash"]
    ):
        raise ArchiveFormatError("rag_manifest_hash_mismatch")
    manifest = json.loads(manifest_path.read_text())
    if (
        manifest.get("version"),
        manifest.get("org_id"),
        manifest.get("archive_id"),
        manifest.get("knowledge_base_id"),
        manifest.get("generation"),
    ) != (1, str(org), str(archive), str(kb), link["archive_generation"]):
        raise ArchiveFormatError("rag_manifest_owner_mismatch")
    dimension = manifest["vector_schema"]["dimension"]
    if (
        not isinstance(dimension, int)
        or not 1 <= dimension <= 65536
        or not re.fullmatch("[a-f0-9]{64}", manifest["vector_schema"]["sha256"])
    ):
        raise ArchiveFormatError("rag_vector_schema_invalid")
    documents = manifest["documents"]
    if not isinstance(documents, list) or len(documents) > 10000:
        raise ArchiveFormatError("rag_inventory_too_large")
    result = {}

    async def download(part: dict[str, Any], relative: str) -> Path:
        if part.get("key") != prefix + relative:
            raise ArchiveFormatError("rag_part_owner_mismatch")
        path = root / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        await storage.download(part["key"], path)
        if path.stat().st_size != part["bytes"] or digest(path) != part["sha256"]:
            raise ArchiveFormatError("rag_part_hash_mismatch")
        return path

    for document in documents:
        document_id = str(UUID(document["id"]))
        if document_id in result:
            raise ArchiveFormatError("rag_duplicate_document")
        metadata = parquet_rows(
            await download(document["metadata"], f"documents/{document_id}.parquet"), "documents"
        )
        if len(metadata) != 1 or (
            metadata[0]["id"],
            metadata[0]["org_id"],
            metadata[0]["knowledge_base_id"],
        ) != (document_id, str(org), str(kb)):
            raise ArchiveFormatError("rag_document_owner_mismatch")
        row = metadata[0]
        if row["error"] is not None:
            raise ArchiveFormatError("rag_raw_diagnostic_forbidden")
        count = 0
        keys = set()
        if len(document["vectors"]) > 10000:
            raise ArchiveFormatError("rag_inventory_too_large")
        for index, part in enumerate(document["vectors"]):
            vectors = parquet_rows(
                await download(part, f"vectors/{document_id}/{index}.parquet"), "vectors"
            )
            if len(vectors) != part["count"]:
                raise ArchiveFormatError("rag_vector_count_mismatch")
            for vector in vectors:
                if (vector["org_id"], vector["knowledge_base_id"], vector["document_id"]) != (
                    str(org),
                    str(kb),
                    document_id,
                ):
                    raise ArchiveFormatError("rag_vector_owner_mismatch")
                if (
                    vector["pk"] != f"{document_id}:{vector['chunk_index']}"
                    or not isinstance(vector["chunk_index"], int)
                    or vector["pk"] in keys
                    or len(vector["dense"]) != dimension
                    or not all(math.isfinite(value) for value in vector["dense"])
                ):
                    raise ArchiveFormatError("rag_vector_invalid")
                keys.add(vector["pk"])
            count += len(vectors)
        if count != (row["chunk_count"] or 0):
            raise ArchiveFormatError("rag_vector_count_mismatch")
        source = document["source"]
        if source:
            key = source["source_key"]
            if key != row["source_key"] or not re.fullmatch(
                rf"documents/{org}/{kb}/[0-9a-f-]{{36}}(?:\.[A-Za-z0-9]+)?", key
            ):
                raise ArchiveFormatError("rag_source_owner_mismatch")
            await download(source, f"sources/{document_id}")
        elif row["source_key"] is not None:
            raise ArchiveFormatError("rag_source_missing")
        result[document_id] = {
            "source_key": row["source_key"],
            "size": source["bytes"] if source else None,
        }
    return result
