"""Stored vector export; never embeddings, reindexing or collection creation."""

import hashlib
import json
import math
from typing import Any, Iterator
from uuid import UUID

from src.lifecycle import ArchiveBlocked, Scope
from src.mutation_transport import mutate

FIELDS = (
    "pk",
    "org_id",
    "knowledge_base_id",
    "document_id",
    "chunk_index",
    "content",
    "dense",
    "parent_content",
    "acl_json",
    "document_title",
)


def validate_row(
    row: dict[str, Any], scope: Scope, document_id: UUID, dimension: int
) -> dict[str, Any]:
    if (row.get("org_id"), row.get("knowledge_base_id"), row.get("document_id")) != (
        str(scope.org_id),
        str(scope.knowledge_base_id),
        str(document_id),
    ):
        raise ArchiveBlocked("vector_scope_invalid")
    if (
        set(row) - set(FIELDS)
        or row.get("pk") != f"{document_id}:{row.get('chunk_index')}"
        or not isinstance(row.get("chunk_index"), int)
    ):
        raise ArchiveBlocked("vector_schema_invalid")
    dense = row.get("dense")
    if dense is None or len(dense) != dimension or not all(math.isfinite(float(v)) for v in dense):
        raise ArchiveBlocked("vector_schema_invalid")
    return {**row, "dense": [float(v) for v in dense]}


class VectorSnapshot:
    def __init__(self, client: Any, collection: str):
        self.client = client
        self.collection = collection

    def schema(self) -> dict[str, Any]:
        description = self.client.describe_collection(self.collection)
        fields = description.get("fields", [])
        required = {
            "pk",
            "org_id",
            "knowledge_base_id",
            "document_id",
            "chunk_index",
            "content",
            "dense",
            "sparse",
        }
        if {f["name"] for f in fields} != required:
            raise ArchiveBlocked("vector_schema_invalid")
        dimensions = [int(f["params"]["dim"]) for f in fields if f["name"] == "dense"]
        functions = description.get("functions", [])
        if len(dimensions) != 1 or not any(
            f.get("input_field_names") == ["content"]
            and f.get("output_field_names") == ["sparse"]
            and int(f.get("type", f.get("function_type", 0))) == 1
            for f in functions
        ):
            raise ArchiveBlocked("vector_schema_invalid")
        stable = {
            "fields": fields,
            "functions": functions,
            "enable_dynamic_field": description.get("enable_dynamic_field"),
        }
        return {
            "dimension": dimensions[0],
            "sha256": hashlib.sha256(
                json.dumps(stable, sort_keys=True, default=str).encode()
            ).hexdigest(),
        }

    @staticmethod
    def expression(scope: Scope, document_id: UUID) -> str:
        return f'org_id == "{scope.org_id}" and knowledge_base_id == "{scope.knowledge_base_id}" and document_id == "{document_id}"'

    def rows(
        self, scope: Scope, document_id: UUID, dimension: int
    ) -> Iterator[list[dict[str, Any]]]:
        # The SDK iterator's MVCC snapshot can lag even with Strong. Use it
        # only to page PKs; each actual row and the count use strong reads.
        counts = self.client.query(
            collection_name=self.collection,
            filter=self.expression(scope, document_id),
            output_fields=["count(*)"],
            consistency_level="Strong",
        )
        count = int(counts[0]["count(*)"])
        if count > 10000:
            raise ArchiveBlocked("vector_inventory_limit")
        if count == 0:
            return
        # pymilvus marks iterator requests use_default_consistency even when
        # passed Strong. Gather only bounded PK metadata and retry stale
        # snapshots before yielding any immutable part, then Strong-get rows.
        import time

        keys: set[str] = set()
        for attempt in range(12):
            keys = set()
            iterator = self.client.query_iterator(
                collection_name=self.collection,
                filter=self.expression(scope, document_id),
                output_fields=["pk"],
                batch_size=128,
                consistency_level="Strong",
                timeout=15,
            )
            try:
                while batch := iterator.next():
                    for row in batch:
                        if row["pk"] in keys or len(keys) >= 10000:
                            raise ArchiveBlocked("vector_snapshot_unstable")
                        keys.add(row["pk"])
            finally:
                iterator.close()
            if len(keys) == count:
                break
            if attempt == 11:
                raise ArchiveBlocked("vector_snapshot_unstable")
            time.sleep(0.5)
        ordered = sorted(keys)
        for offset in range(0, len(ordered), 128):
            wanted = ordered[offset : offset + 128]
            current = self.client.get(
                collection_name=self.collection,
                ids=wanted,
                output_fields=["*"],
                consistency_level="Strong",
                timeout=15,
            )
            if {row["pk"] for row in current} != set(wanted):
                raise ArchiveBlocked("vector_snapshot_unstable")
            result = [
                validate_row(
                    {k: v for k, v in row.items() if k != "sparse"}, scope, document_id, dimension
                )
                for row in current
            ]
            yield sorted(result, key=lambda row: row["pk"])

    def knowledge_base_count(self, scope: Scope) -> int:
        result = self.client.query(
            collection_name=self.collection,
            filter=f'org_id == "{scope.org_id}" and knowledge_base_id == "{scope.knowledge_base_id}"',
            output_fields=["count(*)"],
            consistency_level="Strong",
        )
        return int(result[0]["count(*)"])

    def delete(self, scope: Scope, document_id: UUID) -> None:
        mutate(self.client, "delete", self.collection, self.expression(scope, document_id))
        rows = self.client.query(
            collection_name=self.collection,
            filter=self.expression(scope, document_id),
            output_fields=["pk"],
            limit=1,
            consistency_level="Strong",
        )
        if rows:
            raise ArchiveBlocked("vector_delete_unverified")

    def check_restore(self, rows: list[dict[str, Any]]) -> None:
        if rows:
            existing = self.client.get(
                collection_name=self.collection,
                ids=[r["pk"] for r in rows],
                output_fields=["*"],
                consistency_level="Strong",
            )
            expected = {r["pk"]: r for r in rows}
            for actual in existing:
                normalized = {k: v for k, v in actual.items() if k != "sparse"}
                if normalized != expected.get(actual["pk"]):
                    raise ArchiveBlocked("restore_vector_collision")

    def restore(self, rows: list[dict[str, Any]]) -> None:
        self.check_restore(rows)
        if rows:
            mutate(self.client, "upsert", self.collection, rows)
