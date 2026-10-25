"""Explicit versioned Parquet schemas. No inferred Arrow types or floating point money."""

import json
import os
from enum import Enum
from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
from src.services.archive.catalogue import CURRENT_VERSION, catalogue


class JsonNull(Enum):
    VALUE = "json_null"


JSON_NULL = JsonNull.VALUE

MAX_CELL = 1024 * 1024
MAX_PART = 8 * 1024 * 1024
ROWS_PER_PART = 128


class ArchiveFormatError(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def schema_for(table: str, version: int = CURRENT_VERSION) -> Any:
    columns = catalogue(version)
    if table not in columns:
        raise ArchiveFormatError("table_not_allowed")
    types = {
        "uuid": pa.string(),
        "str": pa.string(),
        "inet": pa.string(),
        "utc": pa.timestamp("us", tz="UTC"),
        "json": pa.string(),
        "bool": pa.bool_(),
        "int": pa.int64(),
        "float": pa.float64(),
    }
    fields = []
    for name, kind, nullable in columns[table]:
        datatype = (
            pa.decimal128(*map(int, kind.split(":")[1:]))
            if kind.startswith("decimal:")
            else types[kind]
        )
        fields.append(pa.field(name, datatype, nullable=nullable))
    return pa.schema(
        fields, metadata={b"archive_version": str(version).encode(), b"table": table.encode()}
    )


def encode_rows(
    table: str,
    rows: Iterable[Mapping[str, Any]],
    path: Path,
    version: int = CURRENT_VERSION,
) -> None:
    normalized = []
    size = 0
    for row in rows:
        result = {}
        for name, kind, _ in catalogue(version)[table]:
            value = row.get(name)
            if value is not None:
                if kind == "json":
                    value = canonical(None if value is JSON_NULL else value)
                elif kind == "inet":
                    value = str(value)
                elif kind == "uuid":
                    value = str(UUID(str(value)))
                elif kind == "utc":
                    if not isinstance(value, datetime) or value.tzinfo is None:
                        raise ArchiveFormatError("timestamp_not_utc")
                    value = value.astimezone(timezone.utc)
                if isinstance(value, str):
                    width = len(value.encode())
                    if width > MAX_CELL:
                        raise ArchiveFormatError("cell_too_large")
                    size += width
            result[name] = value
        normalized.append(result)
        if size > MAX_PART or len(normalized) > ROWS_PER_PART:
            raise ArchiveFormatError("part_too_large")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        pq.write_table(
            pa.Table.from_pylist(normalized, schema=schema_for(table, version)),
            path,
            compression="zstd",
            row_group_size=ROWS_PER_PART,
        )
        if path.stat().st_size > MAX_PART:
            raise ArchiveFormatError("part_too_large")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def read_rows(
    table: str, path: Path, version: int = CURRENT_VERSION
) -> Iterator[dict[str, Any]]:
    if path.stat().st_size > MAX_PART:
        raise ArchiveFormatError("part_too_large")
    parquet = pq.ParquetFile(path)
    if not parquet.schema_arrow.equals(schema_for(table, version), check_metadata=True):
        raise ArchiveFormatError("schema_mismatch")
    if parquet.metadata.num_rows > ROWS_PER_PART:
        raise ArchiveFormatError("part_too_large")
    for batch in parquet.iter_batches(batch_size=ROWS_PER_PART):
        if batch.nbytes > MAX_PART * 2:
            raise ArchiveFormatError("part_too_large")
        for row in batch.to_pylist():
            for name, kind, _ in catalogue(version)[table]:
                value = row[name]
                if value is not None:
                    if isinstance(value, str) and len(value.encode()) > MAX_CELL:
                        raise ArchiveFormatError("cell_too_large")
                    if kind == "uuid":
                        row[name] = UUID(value)
                    elif kind == "json":
                        parsed = json.loads(value)
                        row[name] = JSON_NULL if parsed is None else parsed
            yield row
