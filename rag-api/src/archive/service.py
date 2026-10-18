"""Exact, generation-bound attachment archive saga with durable side-effect receipts."""

import asyncio
import hashlib
import json
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Table, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.lifecycle import ArchiveBlocked, Scope, tenant
from src.archive.storage import ArchiveStorage, encode_rows, decode_rows
from src.archive.vector_snapshot import VectorSnapshot


class ArchiveService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        storage: ArchiveStorage,
        vectors: VectorSnapshot,
    ):
        self.sessions = sessions
        self.storage = storage
        self.vectors = vectors

    async def _job(self, db: AsyncSession, scope: Scope, archive_id: UUID) -> dict[str, Any]:
        await tenant(db, scope.org_id)
        kb = (
            (
                await db.execute(
                    text(
                        "SELECT * FROM rag_kb_lifecycle WHERE org_id=:o AND knowledge_base_id=:k FOR UPDATE"
                    ),
                    scope.params(),
                )
            )
            .mappings()
            .one_or_none()
        )
        if kb is None or kb["archive_id"] != archive_id or kb["generation"] != scope.generation:
            raise ArchiveBlocked("generation_conflict")
        job = (
            (
                await db.execute(
                    text(
                        "SELECT * FROM rag_archive_jobs WHERE archive_id=:a AND org_id=:o AND knowledge_base_id=:k AND (generation=:g OR restore_generation=:g) FOR UPDATE"
                    ),
                    {**scope.params(), "a": archive_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if job is None:
            raise ArchiveBlocked("archive_unknown")
        return {
            **dict(job),
            "kb_state": kb["state"],
            "archive_generation": job["generation"],
            "generation": scope.generation,
        }

    @staticmethod
    def _public(job: dict[str, Any]) -> dict[str, Any]:
        result = {k: job[k] for k in ("state", "receipt_hash", "archive_id", "generation")}
        result["archive_generation"] = job["archive_generation"]
        result["next_restore_generation"] = (
            job["generation"] + 1 if job["state"] == "purged" else None
        )
        return result

    async def status(self, scope: Scope, archive_id: UUID) -> dict[str, Any]:
        async with self.sessions() as db:
            result = self._public(await self._job(db, scope, archive_id))
            pending = (
                (
                    await db.execute(
                        text(
                            "SELECT step,resource FROM rag_archive_steps WHERE archive_id=:a AND state='intent' ORDER BY step,resource LIMIT 100"
                        ),
                        dict(a=archive_id),
                    )
                )
                .mappings()
                .all()
            )
            result["pending_steps"] = [dict(row) for row in pending]
            return result

    async def export(self, scope: Scope, archive_id: UUID) -> dict[str, Any]:
        async with self.sessions() as db:
            job = await self._job(db, scope, archive_id)
            if job["state"] != "claimed":
                await self._verify(job, scope)
                return self._public(job)
            schema = await asyncio.to_thread(self.vectors.schema)
            manifest: dict[str, Any] = {
                "version": 1,
                "org_id": str(scope.org_id),
                "knowledge_base_id": str(scope.knowledge_base_id),
                "archive_id": str(archive_id),
                "generation": scope.generation,
                "vector_schema": schema,
                "documents": [],
            }
            prefix = f"archives/v1/{scope.org_id}/{archive_id}/rag"
            total_vectors = 0
            manifest_bytes = 0
            for document_id in job["inventory"]:
                size = await db.scalar(
                    text(
                        "SELECT greatest(coalesce(octet_length(content),0),coalesce(octet_length(parsed_markdown),0),coalesce(octet_length(error),0)) FROM documents WHERE id=:d AND org_id=:o AND knowledge_base_id=:k"
                    ),
                    {**scope.params(), "d": UUID(document_id)},
                )
                if size is not None and size > 2 * 1024 * 1024:
                    raise ArchiveBlocked("artifact_size_limit")
                row = (
                    (
                        await db.execute(
                            text(
                                "SELECT * FROM documents WHERE id=:d AND org_id=:o AND knowledge_base_id=:k"
                            ),
                            {**scope.params(), "d": UUID(document_id)},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    raise ArchiveBlocked("document_inventory_changed")
                data = dict(row)
                if data["source_key"]:
                    await self._source_owner(db, scope, job, data["source_key"])
                for field in ("id", "org_id", "knowledge_base_id"):
                    data[field] = str(data[field])
                # Raw indexing diagnostics may contain provider content/credentials.
                data["error"] = None
                document = {
                    "id": document_id,
                    "metadata": await asyncio.to_thread(
                        self.storage.put,
                        f"{prefix}/documents/{document_id}.parquet",
                        encode_rows([data], "documents"),
                    ),
                    "vectors": [],
                    "source": None,
                }
                if data["source_key"]:
                    document["source"] = {
                        **await asyncio.to_thread(
                            self.storage.copy_source,
                            data["source_key"],
                            f"{prefix}/sources/{document_id}",
                        ),
                        "source_key": data["source_key"],
                    }
                # Iterator advancement itself is synchronous; never accumulate all vectors.
                iterator = self.vectors.rows(scope, UUID(document_id), schema["dimension"])
                index = 0
                count = 0
                while True:
                    batch = await asyncio.to_thread(next, iterator, None)
                    if batch is None:
                        break
                    if index >= 10000:
                        raise ArchiveBlocked("vector_inventory_limit")
                    part = await asyncio.to_thread(
                        self.storage.put,
                        f"{prefix}/vectors/{document_id}/{index}.parquet",
                        encode_rows(batch, "vectors"),
                    )
                    part["count"] = len(batch)
                    document["vectors"].append(part)
                    count += len(batch)
                    index += 1
                if count != (data["chunk_count"] or 0):
                    raise ArchiveBlocked("vector_count_mismatch")
                total_vectors += count
                manifest_bytes += len(json.dumps(document, separators=(",", ":")).encode())
                if manifest_bytes > 8 * 1024 * 1024:
                    raise ArchiveBlocked("manifest_size_limit")
                manifest["documents"].append(document)
            if await asyncio.to_thread(self.vectors.knowledge_base_count, scope) != total_vectors:
                raise ArchiveBlocked("orphan_vector_inventory")
            serialized = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
            receipt = hashlib.sha256(serialized.encode()).hexdigest()
            await asyncio.to_thread(
                self.storage.put,
                f"archives/v1/{scope.org_id}/{archive_id}/rag/manifest.json",
                serialized.encode(),
            )
            job.update(manifest=manifest, receipt_hash=receipt, state="verified")
            await self._verify(job, scope)
            await db.execute(
                text(
                    "UPDATE rag_archive_jobs SET manifest=cast(:m AS jsonb),receipt_hash=:h,state='verified' WHERE archive_id=:a"
                ),
                dict(m=serialized, h=receipt, a=archive_id),
            )
            await db.commit()
            return self._public(job)

    async def _verify(self, job: dict[str, Any], scope: Scope) -> None:
        manifest = job["manifest"]
        if (
            not manifest
            or hashlib.sha256(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            != job["receipt_hash"]
        ):
            raise ArchiveBlocked("manifest_corrupt")
        if (
            manifest["org_id"],
            manifest["knowledge_base_id"],
            manifest["archive_id"],
            manifest["generation"],
        ) != (
            str(scope.org_id),
            str(scope.knowledge_base_id),
            str(job["archive_id"]),
            job["archive_generation"],
        ):
            raise ArchiveBlocked("manifest_scope_invalid")
        if [d["id"] for d in manifest["documents"]] != job["inventory"]:
            raise ArchiveBlocked("manifest_inventory_invalid")
        if await asyncio.to_thread(self.vectors.schema) != manifest["vector_schema"]:
            raise ArchiveBlocked("vector_schema_changed")
        archived_manifest = await asyncio.to_thread(
            self.storage.read, f'archives/v1/{scope.org_id}/{job["archive_id"]}/rag/manifest.json'
        )
        if (
            archived_manifest is None
            or hashlib.sha256(archived_manifest).hexdigest() != job["receipt_hash"]
        ):
            raise ArchiveBlocked("manifest_corrupt")
        from src.archive.vector_snapshot import validate_row

        for document in manifest["documents"]:
            metadata = decode_rows(
                await asyncio.to_thread(self.storage.verify, document["metadata"]), "documents"
            )
            if len(metadata) != 1 or (
                metadata[0]["id"],
                metadata[0]["org_id"],
                metadata[0]["knowledge_base_id"],
            ) != (document["id"], str(scope.org_id), str(scope.knowledge_base_id)):
                raise ArchiveBlocked("metadata_scope_invalid")
            count = 0
            for part in document["vectors"]:
                rows = decode_rows(await asyncio.to_thread(self.storage.verify, part), "vectors")
                if len(rows) != part["count"]:
                    raise ArchiveBlocked("vector_count_mismatch")
                for row in rows:
                    validate_row(
                        row, scope, UUID(document["id"]), manifest["vector_schema"]["dimension"]
                    )
                count += len(rows)
            if count != (metadata[0]["chunk_count"] or 0):
                raise ArchiveBlocked("vector_count_mismatch")
            if document["source"]:
                async with self.sessions() as db:
                    await tenant(db, scope.org_id)
                    await self._source_owner(db, scope, job, document["source"]["source_key"])
                if metadata[0]["source_key"] != document["source"]["source_key"]:
                    raise ArchiveBlocked("source_inventory_invalid")
                await asyncio.to_thread(self.storage.verify_source, document["source"])
            elif metadata[0]["source_key"]:
                raise ArchiveBlocked("source_inventory_invalid")

    async def _source_owner(
        self, db: AsyncSession, scope: Scope, job: dict[str, Any], key: str
    ) -> None:
        from src.source_ownership import owns_source

        if not owns_source(key, scope.org_id, scope.knowledge_base_id) or await db.scalar(
            text(
                "SELECT EXISTS(SELECT FROM documents WHERE source_key=:s AND "
                "(org_id<>:o OR knowledge_base_id<>:k OR NOT(id=ANY(:ids))))"
            ),
            {**scope.params(), "s": key, "ids": [UUID(d) for d in job["inventory"]]},
        ):
            raise ArchiveBlocked("source_owner_unproven")

    async def _external_step(
        self, scope: Scope, archive_id: UUID, step: str, resource: str, call: Any
    ) -> None:
        async with self.sessions() as db:
            job = await self._job(db, scope, archive_id)
            state = await db.scalar(
                text(
                    "SELECT state FROM rag_archive_steps WHERE archive_id=:a AND step=:s AND resource=:r"
                ),
                dict(a=archive_id, s=step, r=resource),
            )
            if state == "done":
                return
            if state is not None:
                # A lost RPC reply is not proof of completion. No expiry/takeover
                # clears this record: a late delete must never race restore.
                raise ArchiveBlocked("archive_external_uncertain")
            required_phase = "restore_pending" if step.endswith("restore") else "purging"
            if job["state"] != required_phase:
                raise ArchiveBlocked("archive_phase_conflict")
            await db.execute(
                text(
                    "INSERT INTO rag_archive_steps(archive_id,org_id,step,resource,state) VALUES(:a,:o,:s,:r,'intent')"
                ),
                dict(a=archive_id, o=scope.org_id, s=step, r=resource),
            )
            await db.commit()
        await asyncio.to_thread(call)
        async with self.sessions() as db:
            await self._job(db, scope, archive_id)
            await db.execute(
                text(
                    "UPDATE rag_archive_steps SET state='done' WHERE archive_id=:a AND step=:s AND resource=:r"
                ),
                dict(a=archive_id, s=step, r=resource),
            )
            await db.commit()

    async def purge(self, scope: Scope, archive_id: UUID) -> dict[str, Any]:
        async with self.sessions() as db:
            job = await self._job(db, scope, archive_id)
            if job["state"] == "purged":
                return self._public(job)
            if job["state"] not in ("verified", "purging"):
                raise ArchiveBlocked("archive_not_verified")
            await self._verify(job, scope)
            await db.execute(
                text("UPDATE rag_archive_jobs SET state='purging' WHERE archive_id=:a"),
                dict(a=archive_id),
            )
            await db.commit()
        for document in job["manifest"]["documents"]:
            await self._external_step(
                scope,
                archive_id,
                "vector_delete",
                document["id"],
                lambda: self._delete_vectors(
                    scope, document, job["manifest"]["vector_schema"]["dimension"]
                ),
            )
        async with self.sessions() as db:
            await self._job(db, scope, archive_id)
            done = await db.scalar(
                text(
                    "SELECT state FROM rag_archive_steps WHERE archive_id=:a AND step='sql_delete' AND resource='documents'"
                ),
                dict(a=archive_id),
            )
            if done is None:
                rows = (
                    (
                        await db.execute(
                            text(
                                "DELETE FROM documents WHERE org_id=:o AND knowledge_base_id=:k AND id=ANY(:ids) RETURNING id"
                            ),
                            {**scope.params(), "ids": [UUID(d) for d in job["inventory"]]},
                        )
                    )
                    .scalars()
                    .all()
                )
                if set(map(str, rows)) != set(job["inventory"]):
                    raise ArchiveBlocked("sql_inventory_changed")
                await db.execute(
                    text(
                        "INSERT INTO rag_archive_steps VALUES(:a,:o,'sql_delete','documents','done')"
                    ),
                    dict(a=archive_id, o=scope.org_id),
                )
            await db.commit()
        for document in job["manifest"]["documents"]:
            if document["source"]:
                await self._external_step(
                    scope,
                    archive_id,
                    "source_delete",
                    hashlib.sha256(document["source"]["source_key"].encode()).hexdigest(),
                    lambda: self._delete_source(document["source"]),
                )
        await self._finish_purge(scope, archive_id)
        return await self.status(scope, archive_id)

    async def _finish_purge(self, scope: Scope, archive_id: UUID) -> None:
        async with self.sessions() as db:
            job = await self._job(db, scope, archive_id)
            if job["state"] == "purged" and job["kb_state"] == "archived":
                return
            if job["state"] != "purging" or job["kb_state"] != "archiving":
                raise ArchiveBlocked("archive_phase_conflict")
            await self._completion_gate(db, scope, job, restore=False)
            await db.execute(
                text(
                    "UPDATE rag_kb_lifecycle SET state='archived' WHERE org_id=:o AND knowledge_base_id=:k"
                ),
                scope.params(),
            )
            await db.execute(
                text("UPDATE rag_archive_jobs SET state='purged' WHERE archive_id=:a"),
                dict(a=archive_id),
            )
            await db.commit()

    async def _completion_gate(
        self, db: AsyncSession, scope: Scope, job: dict[str, Any], *, restore: bool
    ) -> None:
        rows = (
            await db.execute(
                text("SELECT step,resource,state FROM rag_archive_steps WHERE archive_id=:a"),
                dict(a=job["archive_id"]),
            )
        ).all()
        if any(row.state != "done" for row in rows) or await db.scalar(
            text(
                "SELECT EXISTS(SELECT FROM rag_external_operations WHERE org_id=:o AND knowledge_base_id=:k AND NOT settled)"
            ),
            scope.params(),
        ):
            raise ArchiveBlocked("archive_external_uncertain")
        expected = {("sql_restore" if restore else "sql_delete", "documents")}
        for document in job["manifest"]["documents"]:
            if document["source"]:
                expected.add(
                    (
                        "source_restore" if restore else "source_delete",
                        hashlib.sha256(document["source"]["source_key"].encode()).hexdigest(),
                    )
                )
            if restore:
                expected.update(
                    ("vector_restore", f"{document['id']}:{index}")
                    for index, _ in enumerate(document["vectors"])
                )
            else:
                expected.add(("vector_delete", document["id"]))
        if not expected.issubset({(row.step, row.resource) for row in rows}):
            raise ArchiveBlocked("archive_receipts_incomplete")

    def _delete_vectors(self, scope: Scope, document: dict[str, Any], dimension: int) -> None:
        expected = {}
        for part in document["vectors"]:
            for row in decode_rows(self.storage.verify(part), "vectors"):
                expected[row["pk"]] = hashlib.sha256(encode_rows([row], "vectors")).hexdigest()
        actual = {}
        for batch in self.vectors.rows(scope, UUID(document["id"]), dimension):
            for row in batch:
                actual[row["pk"]] = hashlib.sha256(encode_rows([row], "vectors")).hexdigest()
        if actual != expected:
            raise ArchiveBlocked("vector_delete_inventory_changed")
        self.vectors.delete(scope, UUID(document["id"]))

    def _delete_source(self, source: dict[str, Any]) -> None:
        payload = self.storage.digest(source["source_key"], source=True, missing_ok=True)
        if payload != (source["sha256"], source["bytes"]):
            raise ArchiveBlocked("source_delete_inventory_changed")
        self.storage.delete_source(source["source_key"])

    async def restore(self, scope: Scope, archive_id: UUID, *, dry_run: bool) -> dict[str, Any]:
        async with self.sessions() as db:
            await tenant(db, scope.org_id)
            current = (
                (
                    await db.execute(
                        text(
                            "SELECT state,generation,archive_id FROM rag_kb_lifecycle WHERE org_id=:o AND knowledge_base_id=:k FOR UPDATE"
                        ),
                        scope.params(),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                current is not None
                and current["state"] == "archived"
                and current["archive_id"] == archive_id
                and current["generation"] + 1 == scope.generation
            ):
                archived_scope = Scope(scope.org_id, scope.knowledge_base_id, current["generation"])
                job = await self._job(db, archived_scope, archive_id)
            else:
                job = await self._job(db, scope, archive_id)
                if job["restore_generation"] != scope.generation or job["state"] not in (
                    "restore_pending",
                    "restored",
                ):
                    raise ArchiveBlocked("generation_conflict")
            if job["state"] == "restored":
                return self._public(job)
            if job["state"] not in ("purged", "restore_pending"):
                raise ArchiveBlocked("archive_not_purged")
            await self._verify(job, scope)
            if job["state"] == "purged":
                if await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT FROM documents WHERE id=ANY(:ids) OR (org_id=:o AND knowledge_base_id=:k))"
                    ),
                    {**scope.params(), "ids": [UUID(i) for i in job["inventory"]]},
                ):
                    raise ArchiveBlocked("restore_document_collision")
                for document in job["manifest"]["documents"]:
                    if document["source"]:
                        previous = await asyncio.to_thread(
                            self.storage.digest,
                            document["source"]["source_key"],
                            source=True,
                            missing_ok=True,
                        )
                        if previous is not None and previous != (
                            document["source"]["sha256"],
                            document["source"]["bytes"],
                        ):
                            raise ArchiveBlocked("restore_source_collision")
                    for part in document["vectors"]:
                        stored = decode_rows(
                            await asyncio.to_thread(self.storage.verify, part), "vectors"
                        )
                        await asyncio.to_thread(self.vectors.check_restore, stored)
                    rows = await asyncio.to_thread(
                        lambda: next(
                            self.vectors.rows(
                                scope,
                                UUID(document["id"]),
                                job["manifest"]["vector_schema"]["dimension"],
                            ),
                            None,
                        )
                    )
                    if rows:
                        raise ArchiveBlocked("restore_vector_collision")
            if dry_run:
                return {**self._public(job), "generation": scope.generation}
            await db.execute(
                text(
                    "UPDATE rag_kb_lifecycle SET state='restore_pending',generation=:g WHERE org_id=:o AND knowledge_base_id=:k"
                ),
                scope.params(),
            )
            await db.execute(
                text(
                    "UPDATE rag_archive_jobs SET state='restore_pending',restore_generation=:g WHERE archive_id=:a"
                ),
                dict(a=archive_id, g=scope.generation),
            )
            await db.commit()
        for document in job["manifest"]["documents"]:
            if document["source"]:
                source = document["source"]
                await self._external_step(
                    scope,
                    archive_id,
                    "source_restore",
                    hashlib.sha256(source["source_key"].encode()).hexdigest(),
                    lambda: self.storage.restore_source(source),
                )
            for index, part in enumerate(document["vectors"]):
                rows = decode_rows(await asyncio.to_thread(self.storage.verify, part), "vectors")
                await self._external_step(
                    scope,
                    archive_id,
                    "vector_restore",
                    f"{document['id']}:{index}",
                    lambda: self.vectors.restore(rows),
                )
        async with self.sessions() as db:
            current_job = await self._job(db, scope, archive_id)
            if (
                current_job["state"] != "restore_pending"
                or current_job["kb_state"] != "restore_pending"
            ):
                raise ArchiveBlocked("archive_phase_conflict")
            done = await db.scalar(
                text(
                    "SELECT state FROM rag_archive_steps WHERE archive_id=:a AND step='sql_restore' AND resource='documents'"
                ),
                dict(a=archive_id),
            )
            if done is None:
                from src.db import Document

                for document in job["manifest"]["documents"]:
                    data = decode_rows(
                        await asyncio.to_thread(self.storage.verify, document["metadata"]),
                        "documents",
                    )[0]
                    for name in ("id", "org_id", "knowledge_base_id"):
                        data[name] = UUID(data[name])
                    # Core INSERT preserves explicit legacy NULLs; ORM defaults
                    # would silently replace NULL status/count/timestamps.
                    await db.execute(cast(Table, Document.__table__).insert().values(**data))
                await db.flush()
                await db.execute(
                    text(
                        "INSERT INTO rag_archive_steps VALUES(:a,:o,'sql_restore','documents','done')"
                    ),
                    dict(a=archive_id, o=scope.org_id),
                )
            await db.commit()
        await self._verify_restored(scope, job)
        return await self.status(scope, archive_id)

    async def _verify_restored(self, scope: Scope, job: dict[str, Any]) -> None:
        async with self.sessions() as db:
            await self._job(db, scope, job["archive_id"])
            for document in job["manifest"]["documents"]:
                expected = decode_rows(
                    await asyncio.to_thread(self.storage.verify, document["metadata"]), "documents"
                )[0]
                actual = (
                    (
                        await db.execute(
                            text(
                                "SELECT * FROM documents WHERE id=:d AND org_id=:o AND knowledge_base_id=:k"
                            ),
                            {**scope.params(), "d": UUID(document["id"])},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if actual is None:
                    raise ArchiveBlocked("restore_metadata_missing")
                normalized = dict(actual)
                for name in ("id", "org_id", "knowledge_base_id"):
                    normalized[name] = str(normalized[name])
                if normalized != expected:
                    raise ArchiveBlocked("restore_metadata_changed")
                if document["source"]:
                    data = await asyncio.to_thread(
                        self.storage.digest, document["source"]["source_key"], source=True
                    )
                    if data != (document["source"]["sha256"], document["source"]["bytes"]):
                        raise ArchiveBlocked("restore_source_changed")
                # Compare bounded canonical row digests, including actual float32 dense values.
                expected_hashes = {}
                for part in document["vectors"]:
                    for row in decode_rows(
                        await asyncio.to_thread(self.storage.verify, part), "vectors"
                    ):
                        expected_hashes[row["pk"]] = hashlib.sha256(
                            encode_rows([row], "vectors")
                        ).hexdigest()
                actual_hashes = {}
                iterator = self.vectors.rows(
                    scope, UUID(document["id"]), job["manifest"]["vector_schema"]["dimension"]
                )
                while (batch := await asyncio.to_thread(next, iterator, None)) is not None:
                    for row in batch:
                        actual_hashes[row["pk"]] = hashlib.sha256(
                            encode_rows([row], "vectors")
                        ).hexdigest()
                if actual_hashes != expected_hashes:
                    raise ArchiveBlocked("restore_vectors_changed")

    async def publish(self, scope: Scope, archive_id: UUID) -> dict[str, Any]:
        async with self.sessions() as db:
            job = await self._job(db, scope, archive_id)
            if job["state"] == "restored":
                return self._public(job)
            if job["state"] != "restore_pending":
                raise ArchiveBlocked("restore_not_staged")
        await self._verify_restored(scope, job)
        async with self.sessions() as db:
            current_job = await self._job(db, scope, archive_id)
            if current_job["state"] == "restored" and current_job["kb_state"] == "active":
                return self._public(current_job)
            if (
                current_job["state"] != "restore_pending"
                or current_job["kb_state"] != "restore_pending"
            ):
                raise ArchiveBlocked("archive_phase_conflict")
            await self._completion_gate(db, scope, current_job, restore=True)
            await db.execute(
                text(
                    "UPDATE rag_kb_lifecycle SET state='active' WHERE org_id=:o AND knowledge_base_id=:k"
                ),
                scope.params(),
            )
            await db.execute(
                text("UPDATE rag_archive_jobs SET state='restored' WHERE archive_id=:a"),
                dict(a=archive_id),
            )
            await db.commit()
        return await self.status(scope, archive_id)

    async def release(self, scope: Scope, archive_id: UUID) -> dict[str, Any]:
        async with self.sessions() as db:
            job = await self._job(db, scope, archive_id)
            if job["state"] not in ("claimed", "verified"):
                raise ArchiveBlocked("archive_release_unsafe")
            await db.execute(
                text(
                    "UPDATE rag_kb_lifecycle SET state='active' WHERE org_id=:o AND knowledge_base_id=:k"
                ),
                scope.params(),
            )
            await db.execute(
                text("UPDATE rag_archive_jobs SET state='released' WHERE archive_id=:a"),
                dict(a=archive_id),
            )
            await db.commit()
        return await self.status(scope, archive_id)
