"""Resumable operator saga: frozen inventory → immutable verification → exact purge.

Each call holds a session advisory lock for one immutable archive UUID. SQL state
transitions commit separately from external calls. RAG owns its own durable RPC
intents. Archive create-only uploads are the sole retryable uncertain mutation.
"""

from __future__ import annotations
import builtins
import hashlib
import json
import tempfile
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4
from sqlalchemy import select, text, delete, insert, update, bindparam, JSON
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from src.services.archive.collisions import unique_collision
from src.models.archive_manifest import ArchiveJob
from src.models.data_identity import ConversationIdentity, AuditLogIdentity
from src.models.knowledge_base import KnowledgeBase
from src.services.conversation_lifecycle import (
    claim_conversation,
    eligibility_blocker,
    lock_conversation_identity,
    ArchiveBlocked,
)
from src.services.rag_archive_client import RagArchiveClient, Action
from src.services.archive.schemas import (
    canonical,
    encode_rows,
    read_rows,
    ArchiveFormatError,
    JSON_NULL,
)
from src.services.archive.catalogue import catalogue
from src.services.archive.snapshot import (
    TABLES,
    check_image_key,
    conversation_tables,
    inventory,
    inventory_version,
    pages,
    selection,
    row_json,
    decode_record,
)
from src.services.archive.storage import (
    IMAGE_OBJECT_LIMIT,
    ArchiveStorage,
    PlatformImageStorage,
    digest,
)
from src.models.conversation_image import ConversationImage

Sessions = Callable[[], AsyncSession]


class ArchiveService:
    def __init__(
        self,
        sessions: Sessions,
        storage: ArchiveStorage,
        rag: RagArchiveClient | None = None,
        retention_days: int = 180,
        images: PlatformImageStorage | None = None,
    ):
        if retention_days <= 0:
            raise ValueError("retention_must_be_positive")
        self.sessions, self.storage, self.rag, self.retention_days = (
            sessions,
            storage,
            rag,
            retention_days,
        )
        # Platform-bucket access for conversation image bytes; without it,
        # conversations that hold images are not archived.
        self.images = images

    @staticmethod
    def _version(job: ArchiveJob) -> int:
        return inventory_version(job.inventory)

    def _tables(self, job: ArchiveJob) -> builtins.list[str]:
        return conversation_tables(self._version(job)) if job.kind == "conversation" else [
            "audit_logs"
        ]

    async def _images_blocker(self, session: AsyncSession, org: UUID, conversation: UUID) -> str | None:
        if self.images is not None:
            return None
        present = await session.scalar(
            select(ConversationImage.id)
            .where(ConversationImage.org_id == org, ConversationImage.conversation_id == conversation)
            .limit(1)
        )
        return "image_archive_not_configured" if present is not None else None

    @asynccontextmanager
    async def session(self, org: UUID) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            # Connect as a dedicated lifecycle member. SET ROLE drops superuser
            # bypass in tests too; never elevate an ordinary app connection.
            await session.execute(text("SET LOCAL ROLE aurum_ai_lifecycle"))
            await session.execute(
                text("SELECT set_config('app.current_org_id',:o,true)"), {"o": str(org)}
            )
            yield session

    @asynccontextmanager
    async def exclusive(self, org: UUID, archive: UUID) -> AsyncIterator[None]:
        # Session-level advisory lock must use one pinned physical connection,
        # never return it to a pool while locked.
        async with self.sessions() as session:
            connection = await session.connection()
            key = int.from_bytes(hashlib.sha256(archive.bytes).digest()[:8], signed=True)
            locked = await connection.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
            if not locked:
                raise ArchiveBlocked("archive_busy")
            try:
                yield
            finally:
                await connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})

    async def list(self, org: UUID) -> list[dict[str, Any]]:
        async with self.session(org) as session:
            jobs = (
                await session.scalars(
                    select(ArchiveJob).order_by(ArchiveJob.created_at).limit(1000)
                )
            ).all()
            return [
                dict(
                    archive_id=str(j.id),
                    kind=j.kind,
                    state=j.state,
                    generation=j.generation,
                    cutoff=j.cutoff.isoformat(),
                    counts={
                        table: value["count"]
                        for table, value in j.inventory.items()
                        if not table.startswith("_")
                    },
                    manifest_hash=j.manifest_hash,
                    rag={
                        key: value
                        for key, value in (j.rag or {}).items()
                        if key
                        in (
                            "knowledge_base_id",
                            "archive_id",
                            "generation",
                            "archive_generation",
                            "restore_generation",
                            "state",
                            "receipt_hash",
                        )
                    },
                    conversation_id=str(j.conversation_id) if j.conversation_id else None,
                )
                for j in jobs
            ]

    async def _requested_job(
        self, org: UUID, archive_id: UUID | None, conversation: UUID | None, apply: bool
    ) -> dict[str, Any] | None:
        if archive_id is None:
            return None
        async with self.session(org) as session:
            job = await session.scalar(
                select(ArchiveJob).where(ArchiveJob.id == archive_id, ArchiveJob.org_id == org)
            )
        if job is None:
            return None
        if job.conversation_id != conversation or job.kind != (
            "conversation" if conversation else "audit"
        ):
            raise ArchiveBlocked("archive_request_identity_changed")
        if apply:
            await self.resume(org, archive_id)
            job = await self._job(org, archive_id)
        return {"archive_id": archive_id, "state": job.state, "eligible": True}

    async def resume_existing(self, org: UUID, archive_id: UUID) -> bool:
        async with self.session(org) as session:
            present = await session.scalar(
                select(ArchiveJob.id).where(ArchiveJob.id == archive_id, ArchiveJob.org_id == org)
            )
        if present is None:
            return False
        await self.resume(org, archive_id)
        return True

    async def run(
        self, org: UUID, conversation: UUID, *, apply: bool = False, archive_id: UUID | None = None
    ) -> dict[str, Any]:
        existing = await self._requested_job(org, archive_id, conversation, apply)
        if existing is not None:
            return existing
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        # Capture only the local admission token under short payload-first locks.
        # No external RPC may hold these ordinary-writer locks.
        async with self.session(org) as session:
            identity, payload = await lock_conversation_identity(session, org, conversation)
            if identity.lifecycle != "active" or payload is None:
                return {
                    "eligible": False,
                    "blocked_reason": "not_active",
                    "archive_id": identity.archive_id,
                }
            blocker = await eligibility_blocker(
                session, identity, payload, cutoff
            ) or await self._images_blocker(session, org, conversation)
            if blocker:
                return {"eligible": False, "blocked_reason": blocker}
            observed_generation = identity.generation
            kb = await session.scalar(
                select(KnowledgeBase.id).where(
                    KnowledgeBase.org_id == org,
                    KnowledgeBase.owning_conversation_id == conversation,
                )
            )
        # Like the RAG inspection, outside any lock: a missing or resized image
        # object must stop the archive before the conversation is claimed.
        blocker = await self._image_objects_blocker(org, conversation)
        if blocker:
            return {"eligible": False, "blocked_reason": blocker}
        archive, lease = archive_id or uuid4(), uuid4()
        rag_link = None
        if kb is not None:
            if self.rag is None:
                raise ArchiveBlocked("rag_archive_not_configured")
            inspection = await self.rag.inspect(org_id=org, knowledge_base_id=kb)
            if inspection.state != "active":
                raise ArchiveBlocked("rag_not_active")
            rag_link = {
                "knowledge_base_id": str(kb),
                "archive_id": str(uuid4()),
                "generation": inspection.generation + 1,
            }
        async with self.session(org) as session:
            identity, payload = await lock_conversation_identity(session, org, conversation)
            if (
                identity.lifecycle != "active"
                or payload is None
                or identity.generation != observed_generation
            ):
                return {"eligible": False, "blocked_reason": "lifecycle_changed"}
            current_kb = await session.scalar(
                select(KnowledgeBase.id).where(
                    KnowledgeBase.org_id == org,
                    KnowledgeBase.owning_conversation_id == conversation,
                )
            )
            if current_kb != kb:
                return {"eligible": False, "blocked_reason": "attachment_inventory_changed"}
            blocker = await eligibility_blocker(
                session, identity, payload, cutoff
            ) or await self._images_blocker(session, org, conversation)
            if blocker:
                return {"eligible": False, "blocked_reason": blocker}
            if not apply:
                await inventory(session, org, conversation)
                return {
                    "eligible": True,
                    "rag_eligibility": "checked_on_apply" if kb else "not_required",
                }
            identity = await claim_conversation(
                session, org, conversation, archive_id=archive, lease_id=lease, cutoff=cutoff
            )
            frozen = await inventory(session, org, conversation)
            session.add(
                ArchiveJob(
                    id=archive,
                    org_id=org,
                    kind="conversation",
                    conversation_id=conversation,
                    generation=identity.generation,
                    cutoff=cutoff,
                    state="claimed",
                    inventory=frozen,
                    rag=rag_link,
                )
            )
            await session.commit()
        await self.resume(org, archive)
        return {"eligible": True, "archive_id": archive, "state": "purged"}

    async def run_audit(
        self,
        org: UUID,
        *,
        apply: bool = False,
        batch_size: int = 100,
        archive_id: UUID | None = None,
    ) -> dict[str, Any]:
        existing = await self._requested_job(org, archive_id, None, apply)
        if existing is not None:
            return existing
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        async with self.session(org) as session:
            # Payload-first audit locks serialize updates before the identity CAS.
            rows = (
                await session.execute(
                    text(
                        "SELECT a.id FROM audit_logs a JOIN audit_log_identities i ON i.id=a.id AND i.org_id=a.org_id WHERE a.org_id=:o AND a.created_at<:cut AND i.lifecycle='active' ORDER BY a.created_at,a.id LIMIT :n FOR UPDATE OF a SKIP LOCKED"
                    ),
                    dict(o=org, cut=cutoff, n=min(max(batch_size, 1), 1000)),
                )
            ).all()
            ids = [row[0] for row in rows]
            if not apply or not ids:
                return {"eligible_count": len(ids)}
            archive = archive_id or uuid4()
            await session.execute(
                text(
                    "UPDATE audit_log_identities SET lifecycle='archiving',generation=generation+1,archive_id=:a WHERE org_id=:o AND id=ANY(:ids) AND lifecycle='active'"
                ),
                dict(a=archive, o=org, ids=ids),
            )
            # Audit rows never changed shape; their jobs stay version 1.
            frozen = await inventory(session, org, None, ids, version=1)
            session.add(
                ArchiveJob(
                    id=archive,
                    org_id=org,
                    kind="audit",
                    conversation_id=None,
                    generation=1,
                    cutoff=cutoff,
                    state="claimed",
                    inventory=frozen,
                )
            )
            await session.commit()
        await self.resume(org, archive)
        return {"archive_id": archive, "state": "purged", "eligible_count": len(ids)}

    async def _job(self, org: UUID, archive: UUID) -> ArchiveJob:
        async with self.session(org) as session:
            job = await session.scalar(
                select(ArchiveJob).where(ArchiveJob.id == archive, ArchiveJob.org_id == org)
            )
            if job is None:
                raise ArchiveBlocked("archive_not_found")
            return job

    def _ids(self, job: ArchiveJob) -> builtins.list[UUID] | None:
        return (
            [UUID(value) for value in job.inventory["_audit_ids"]] if job.kind == "audit" else None
        )

    async def _rag(self, org: UUID, job: ArchiveJob, action: Action) -> None:
        if not job.rag:
            return
        if self.rag is None:
            raise ArchiveBlocked("rag_archive_not_configured")
        link = dict(job.rag)
        kb, archive = UUID(link["knowledge_base_id"]), UUID(link["archive_id"])
        if "generation" not in link:
            inspection = await self.rag.inspect(org_id=org, knowledge_base_id=kb)
            if inspection.state != "active":
                raise ArchiveBlocked("rag_claim_state_unknown")
            link["generation"] = inspection.generation + 1
            async with self.session(org) as session:
                stored = await session.get(ArchiveJob, job.id)
                assert stored is not None
                stored.rag = link
                await session.commit()
        generation = (
            link.get("restore_generation", link["generation"])
            if action in ("restore", "publish")
            else link["generation"]
        )
        receipt = await self.rag.command(
            action, org_id=org, knowledge_base_id=kb, archive_id=archive, generation=generation
        )
        if receipt.pending_steps:
            raise ArchiveBlocked("rag_operation_unsettled")
        if action == "purge":
            if receipt.state != "purged":
                raise ArchiveBlocked("rag_purge_incomplete")
            link["restore_generation"] = receipt.next_restore_generation
        if receipt.receipt_hash:
            if link.get("receipt_hash") not in (None, receipt.receipt_hash):
                raise ArchiveBlocked("rag_manifest_changed")
            link["receipt_hash"] = receipt.receipt_hash
            link["archive_generation"] = receipt.archive_generation
        link["state"] = receipt.state
        async with self.session(org) as session:
            stored = await session.get(ArchiveJob, job.id)
            assert stored is not None
            stored.rag = link
            await session.commit()
        job.rag = link

    async def resume(self, org: UUID, archive: UUID) -> None:
        async with self.exclusive(org, archive):
            job = await self._job(org, archive)
            if job.state == "claimed":
                await self._rag(org, job, "claim")
                await self._rag(org, job, "export")
                await self._seal(org, job)
                job = await self._job(org, archive)
            if job.state == "verified":
                with tempfile.TemporaryDirectory(prefix="platform-archive-") as temporary:
                    manifest = await self._verified_files(org, job, Path(temporary))
                await self._rag(org, job, "purge")
                # Like the RAG purge, platform image copies go only after the
                # archive copies are verified and before the rows.
                await self._purge_images(org, job, manifest)
                await self._purge(org, job)

    async def _seal(self, org: UUID, job: ArchiveJob) -> None:
        parts = []
        images: builtins.list[dict[str, Any]] = []
        pending_images: builtins.list[dict[str, Any]] = []
        version = self._version(job)
        prefix = f"archives/v1/{org}/{job.id}"
        with tempfile.TemporaryDirectory(prefix="platform-archive-") as temporary:
            directory = Path(temporary)
            async with self.session(org) as session:
                if (
                    await inventory(session, org, job.conversation_id, self._ids(job), version)
                    != job.inventory
                ):
                    raise ArchiveBlocked("inventory_changed")
                for table in self._tables(job):
                    index = 0
                    async for rows in pages(
                        session, table, org, job.conversation_id, self._ids(job), version
                    ):
                        if table == "conversation_images":
                            pending_images.extend(rows)
                        path = directory / f"{table}-{index}.parquet"
                        encode_rows(table, rows, path, version)
                        key = f"{prefix}/{table}/{index:06d}.parquet"
                        part = dict(
                            table=table,
                            key=key,
                            sha256=digest(path),
                            size=path.stat().st_size,
                            count=len(rows),
                        )
                        await self._upload_verified(key, path, part, directory, version)
                        parts.append(part)
                        path.unlink()
                        index += 1
                        if len(parts) > 4096:
                            raise ArchiveBlocked("archive_too_many_parts")
            # Image bytes are large: copy them only after the snapshot
            # transaction ends. The rows were checked against the frozen
            # inventory above, their keys are immutable and the conversation
            # is fenced while it is archiving.
            for row in pending_images:
                images.append(await self._seal_image(prefix, row, directory, version))
            manifest = dict(
                version=version,
                org_id=str(org),
                archive_id=str(job.id),
                conversation_id=str(job.conversation_id) if job.conversation_id else None,
                generation=job.generation,
                kind=job.kind,
                inventory=job.inventory,
                parts=parts,
                rag=job.rag,
                exclusions=[
                    "web_sources.content",
                    "web tool results",
                    "workflow internal errors",
                    "expiring node output",
                ],
            )
            if version >= 2:
                manifest["images"] = images
            path = directory / "manifest.json"
            path.write_text(canonical(manifest))
            path.chmod(0o600)
            if path.stat().st_size > 1024 * 1024:
                raise ArchiveBlocked("manifest_too_large")
            await self._upload_verified(
                f"{prefix}/manifest.json",
                path,
                dict(sha256=digest(path), size=path.stat().st_size),
                directory,
                version,
            )
            async with self.session(org) as session:
                stored = await session.get(ArchiveJob, job.id, with_for_update=True)
                assert stored is not None
                if stored.state != "claimed" or stored.inventory != job.inventory:
                    raise ArchiveBlocked("archive_phase_changed")
                stored.manifest_hash = digest(path)
                stored.state = "verified"
                await session.commit()

    async def _image_objects_blocker(self, org: UUID, conversation: UUID) -> str | None:
        if self.images is None:
            return None
        async with self.session(org) as session:
            rows = (
                await session.execute(
                    select(
                        ConversationImage.id,
                        ConversationImage.operation_id,
                        ConversationImage.storage_key,
                        ConversationImage.size_bytes,
                    ).where(
                        ConversationImage.org_id == org,
                        ConversationImage.conversation_id == conversation,
                    )
                )
            ).mappings().all()
        for row in rows:
            # The same ownership check as the inventory, before any request.
            check_image_key(dict(row), org, conversation)
            if await self.images.size(row["storage_key"]) != row["size_bytes"]:
                return "image_bytes_unavailable"
        return None

    async def _seal_image(
        self, prefix: str, row: dict[str, Any], directory: Path, version: int
    ) -> dict[str, Any]:
        """Copy one image's bytes into the archive as its own immutable object."""
        if self.images is None:
            raise ArchiveBlocked("image_archive_not_configured")
        path = directory / f"{row['id']}.image"
        await self.images.download(row["storage_key"], path, IMAGE_OBJECT_LIMIT)
        try:
            if path.stat().st_size != row["size_bytes"]:
                raise ArchiveBlocked("image_bytes_mismatch")
            key = f"{prefix}/images/{row['id']}"
            entry: dict[str, Any] = dict(
                id=str(row["id"]), key=key, sha256=digest(path), size=path.stat().st_size
            )
            await self._upload_verified(key, path, entry, directory, version)
            return entry
        finally:
            path.unlink(missing_ok=True)

    async def _upload_verified(
        self, key: str, path: Path, part: dict[str, Any], directory: Path, version: int
    ) -> None:
        try:
            await self.storage.upload(key, path)
        except ArchiveFormatError:
            # Only immutable create-only artifacts may reconcile accepted loss.
            pass
        check = directory / f"{uuid4()}.readback"
        try:
            await self.storage.download(key, check)
            if digest(check) != part["sha256"] or check.stat().st_size != part["size"]:
                raise ArchiveBlocked("archive_readback_mismatch")
            if (
                "table" in part
                and len(list(read_rows(part["table"], check, version))) != part["count"]
            ):
                raise ArchiveBlocked("archive_count_mismatch")
        finally:
            check.unlink(missing_ok=True)

    async def _verified_files(self, org: UUID, job: ArchiveJob, directory: Path) -> dict[str, Any]:
        manifest_path = directory / "manifest.json"
        await self.storage.download(f"archives/v1/{org}/{job.id}/manifest.json", manifest_path)
        if digest(manifest_path) != job.manifest_hash or manifest_path.stat().st_size > 1024 * 1024:
            raise ArchiveBlocked("manifest_mismatch")
        manifest: dict[str, Any] = json.loads(manifest_path.read_text())
        version = self._version(job)
        if (
            manifest["version"] != version
            or manifest["org_id"] != str(org)
            or manifest["archive_id"] != str(job.id)
            or manifest["generation"] != job.generation
            or manifest["inventory"] != job.inventory
            or manifest["conversation_id"]
            != (str(job.conversation_id) if job.conversation_id else None)
        ):
            raise ArchiveBlocked("manifest_owner_mismatch")
        seen = set()
        totals: dict[str, Any] = {
            table: {"count": 0, "hash": hashlib.sha256()}
            for table in job.inventory
            if not table.startswith("_")
        }
        if len(manifest["parts"]) > 4096:
            raise ArchiveBlocked("archive_too_many_parts")
        image_rows: dict[str, dict[str, Any]] = {}
        for number, part in enumerate(manifest["parts"]):
            table = part["table"]
            if (
                table not in totals
                or part["key"] in seen
                or not part["key"].startswith(f"archives/v1/{org}/{job.id}/{table}/")
            ):
                raise ArchiveBlocked("part_owner_mismatch")
            seen.add(part["key"])
            path = directory / f"{number:06d}.parquet"
            await self.storage.download(part["key"], path)
            if digest(path) != part["sha256"] or path.stat().st_size != part["size"]:
                raise ArchiveBlocked("part_hash_mismatch")
            count = 0
            for row in read_rows(table, path, version):
                if row["org_id"] != org:
                    raise ArchiveBlocked("part_tenant_mismatch")
                if table == "conversation_images":
                    image_rows[str(row["id"])] = row
                totals[table]["hash"].update(row_json(row, table, version).encode() + b"\n")
                totals[table]["count"] += 1
                count += 1
            if count != part["count"]:
                raise ArchiveBlocked("part_count_mismatch")
        for table, total in totals.items():
            if {"count": total["count"], "sha256": total["hash"].hexdigest()} != job.inventory[
                table
            ]:
                raise ArchiveBlocked("inventory_hash_mismatch")
        await self._verified_images(org, job, manifest, image_rows, directory)
        if job.rag:
            from src.services.archive.rag_artifacts import verify_rag

            linked = manifest.get("rag") or {}
            for name in (
                "archive_id",
                "knowledge_base_id",
                "generation",
                "archive_generation",
                "receipt_hash",
            ):
                if linked.get(name) != job.rag.get(name):
                    raise ArchiveBlocked("rag_manifest_binding_changed")
            documents = await verify_rag(self.storage, org, linked, directory)
            for number, part in enumerate(manifest["parts"]):
                if part["table"] == "file_metadata":
                    for row in read_rows(
                        "file_metadata", directory / f"{number:06d}.parquet", version
                    ):
                        document = documents.get(str(row["document_id"]))
                        if (
                            document is None
                            or document["source_key"] != row["storage_key"]
                            or document["size"] != row["size_bytes"]
                        ):
                            raise ArchiveBlocked("attachment_inventory_mismatch")
        return manifest

    async def _verified_images(
        self,
        org: UUID,
        job: ArchiveJob,
        manifest: dict[str, Any],
        image_rows: dict[str, dict[str, Any]],
        directory: Path,
    ) -> None:
        """Every archived image row has exactly one verified object, kept in
        ``directory`` for a restore."""
        if self._version(job) < 2:
            if "images" in manifest:
                raise ArchiveBlocked("image_manifest_unexpected")
            return
        entries = manifest.get("images")
        if not isinstance(entries, builtins.list) or len(entries) > 100_000:
            raise ArchiveBlocked("image_manifest_invalid")
        if {entry.get("id") for entry in entries} != set(image_rows) or len(entries) != len(
            image_rows
        ):
            raise ArchiveBlocked("image_inventory_mismatch")
        for entry in entries:
            row = image_rows[entry["id"]]
            if entry["key"] != f"archives/v1/{org}/{job.id}/images/{entry['id']}" or entry[
                "size"
            ] != row["size_bytes"]:
                raise ArchiveBlocked("image_owner_mismatch")
            path = directory / f"image-{entry['id']}"
            path.unlink(missing_ok=True)
            await self.storage.download(entry["key"], path)
            if digest(path) != entry["sha256"] or path.stat().st_size != entry["size"]:
                raise ArchiveBlocked("image_hash_mismatch")
        # In memory only: platform keys come from the verified archived rows,
        # never from the manifest's own fields.
        manifest["_image_keys"] = {key: row["storage_key"] for key, row in image_rows.items()}

    async def _purge_images(self, org: UUID, job: ArchiveJob, manifest: dict[str, Any]) -> None:
        """Remove the platform copies of verified archived images. Idempotent,
        so a resume after a crash simply repeats it before the row purge."""
        entries = manifest.get("images") or []
        if not entries:
            return
        if self.images is None:
            raise ArchiveBlocked("image_archive_not_configured")
        keys = manifest["_image_keys"]
        for entry in entries:
            await self.images.delete(keys[entry["id"]])

    async def _restore_images(
        self, org: UUID, job: ArchiveJob, manifest: dict[str, Any], directory: Path
    ) -> None:
        """Put verified bytes back at their original keys, then read them back.
        Runs while the conversation is still hidden; repeating it is harmless."""
        entries = manifest.get("images") or []
        if not entries:
            return
        if self.images is None:
            raise ArchiveBlocked("image_archive_not_configured")
        keys = manifest["_image_keys"]
        for entry in entries:
            source = directory / f"image-{entry['id']}"
            await self.images.upload(keys[entry["id"]], source)
            check = directory / f"{uuid4()}.readback"
            try:
                await self.images.download(keys[entry["id"]], check, IMAGE_OBJECT_LIMIT)
                if digest(check) != entry["sha256"]:
                    raise ArchiveBlocked("image_restore_mismatch")
            finally:
                check.unlink(missing_ok=True)

    async def _lock_audit_claim(
        self,
        session: AsyncSession,
        org: UUID,
        job: ArchiveJob,
        *,
        lifecycle: str,
        payload: bool = False,
    ) -> None:
        ids = self._ids(job)
        assert ids is not None
        if payload:
            rows = (
                await session.execute(
                    select(TABLES["audit_logs"].c.id, TABLES["audit_logs"].c.created_at)
                    .where(TABLES["audit_logs"].c.org_id == org, TABLES["audit_logs"].c.id.in_(ids))
                    .order_by(TABLES["audit_logs"].c.id)
                    .with_for_update()
                )
            ).all()
            if len(rows) != len(ids) or any(row.created_at >= job.cutoff for row in rows):
                raise ArchiveBlocked("audit_payload_changed")
        identities = (
            await session.scalars(
                select(AuditLogIdentity)
                .where(AuditLogIdentity.org_id == org, AuditLogIdentity.id.in_(ids))
                .order_by(AuditLogIdentity.id)
                .with_for_update()
            )
        ).all()
        generations = job.inventory.get("_audit_generations", {})
        if len(identities) != len(ids) or any(
            identity.lifecycle != lifecycle
            or identity.archive_id != job.id
            or identity.generation != generations.get(str(identity.id))
            for identity in identities
        ):
            raise ArchiveBlocked("audit_identity_changed")

    async def _purge(self, org: UUID, job: ArchiveJob) -> None:
        async with self.session(org) as session:
            if job.conversation_id:
                identity, payload = await lock_conversation_identity(
                    session, org, job.conversation_id
                )
                if (
                    identity.lifecycle != "archiving"
                    or identity.archive_id != job.id
                    or identity.generation != job.generation
                    or payload is None
                ):
                    raise ArchiveBlocked("lifecycle_changed")
                blocker = await eligibility_blocker(session, identity, payload, job.cutoff)
                if blocker:
                    raise ArchiveBlocked(blocker)
            else:
                await self._lock_audit_claim(session, org, job, lifecycle="archiving", payload=True)
            stored = await session.get(ArchiveJob, job.id, with_for_update=True)
            assert stored is not None
            if stored.state == "purged":
                return
            if stored.state != "verified" or stored.manifest_hash != job.manifest_hash:
                raise ArchiveBlocked("archive_not_verified")
            version = self._version(job)
            if (
                await inventory(session, org, job.conversation_id, self._ids(job), version)
                != job.inventory
            ):
                raise ArchiveBlocked("inventory_changed")
            for table in reversed(self._tables(job)):
                query = selection(table, org, job.conversation_id, self._ids(job), version)
                ids = query.with_only_columns(TABLES[table].c.id)
                result = await session.execute(
                    delete(TABLES[table]).where(
                        TABLES[table].c.org_id == org, TABLES[table].c.id.in_(ids)
                    )
                )
                if cast(Any, result).rowcount != job.inventory[table]["count"]:
                    raise ArchiveBlocked("purge_count_mismatch")
            if job.conversation_id:
                identity.lifecycle = "archived"
                identity.lease_id = None
                identity.lease_until = None
            else:
                await session.execute(
                    text(
                        "UPDATE audit_log_identities SET lifecycle='archived' WHERE org_id=:o AND archive_id=:a AND lifecycle='archiving'"
                    ),
                    dict(o=org, a=job.id),
                )
            stored.state = "purged"
            await session.commit()

    async def export(self, org: UUID, archive: UUID, output: Path) -> None:
        output.mkdir(mode=0o700, parents=False, exist_ok=False)
        job = await self._job(org, archive)
        await self._verified_files(org, job, output)

    async def restore(self, org: UUID, archive: UUID, *, apply: bool = False) -> dict[str, Any]:
        async with self.exclusive(org, archive):
            job = await self._job(org, archive)
            if job.state == "restored":
                return {"state": "restored"}
            if job.state not in ("purged", "restore_pending"):
                raise ArchiveBlocked("archive_not_purged")
            with tempfile.TemporaryDirectory(prefix="platform-restore-") as temporary:
                directory = Path(temporary)
                manifest = await self._verified_files(org, job, directory)
                await self._dependencies(org, job, manifest, directory)
                if manifest.get("images") and self.images is None:
                    raise ArchiveBlocked("image_archive_not_configured")
                if not apply:
                    if job.rag:
                        if self.rag is None:
                            raise ArchiveBlocked("rag_archive_not_configured")
                        await self.rag.command(
                            "restore",
                            org_id=org,
                            knowledge_base_id=UUID(job.rag["knowledge_base_id"]),
                            archive_id=UUID(job.rag["archive_id"]),
                            generation=job.rag["restore_generation"],
                            dry_run=True,
                        )
                    return {"state": job.state, "restorable": True}
                if job.state == "purged":
                    async with self.session(org) as session:
                        stored = await session.get(ArchiveJob, archive, with_for_update=True)
                        assert stored is not None
                        if stored.state != "purged":
                            raise ArchiveBlocked("archive_phase_changed")
                        if job.conversation_id:
                            identity = await session.get(
                                ConversationIdentity, job.conversation_id, with_for_update=True
                            )
                            if (
                                identity is None
                                or identity.lifecycle != "archived"
                                or identity.archive_id != job.id
                                or identity.generation != job.generation
                            ):
                                raise ArchiveBlocked("lifecycle_changed")
                            identity.lifecycle = "restore_pending"
                            identity.generation += 1
                            stored.restore_generation = identity.generation
                        else:
                            await self._lock_audit_claim(session, org, job, lifecycle="archived")
                        stored.state = "restore_pending"
                        await session.commit()
                    job = await self._job(org, archive)
                # Stage hidden business rows first: real constraints settle a
                # concurrent alternate-key collision before any RAG mutation.
                if not job.staged:
                    try:
                        await self._stage(org, job, manifest, directory)
                    except IntegrityError:
                        raise ArchiveBlocked("restore_payload_collision") from None
                # Bytes return before the conversation becomes visible again.
                await self._restore_images(org, job, manifest, directory)
                await self._rag(org, job, "restore")
                await self._rag(org, job, "publish")
                async with self.session(org) as session:
                    if job.kind == "audit":
                        await self._lock_audit_claim(
                            session, org, job, lifecycle="archived", payload=True
                        )
                    stored = await session.get(ArchiveJob, archive, with_for_update=True)
                    assert stored is not None
                    if stored.state != "restore_pending" or not stored.staged:
                        raise ArchiveBlocked("restore_stage_incomplete")
                    if (
                        await inventory(
                            session, org, job.conversation_id, self._ids(job), self._version(job)
                        )
                        != job.inventory
                    ):
                        raise ArchiveBlocked("restore_inventory_mismatch")
                    if job.conversation_id:
                        identity = await session.get(
                            ConversationIdentity, job.conversation_id, with_for_update=True
                        )
                        if (
                            identity is None
                            or identity.lifecycle != "restore_pending"
                            or identity.generation != stored.restore_generation
                            or identity.archive_id != job.id
                        ):
                            raise ArchiveBlocked("lifecycle_changed")
                        identity.lifecycle = "active"
                        identity.restored_at = datetime.now(timezone.utc)
                    else:
                        await session.execute(
                            text(
                                "UPDATE audit_log_identities SET lifecycle='active',generation=generation+1 WHERE org_id=:o AND archive_id=:a AND lifecycle='archived'"
                            ),
                            dict(o=org, a=job.id),
                        )
                    stored.state = "restored"
                    await session.commit()
            return {"state": "restored"}

    async def _dependencies(
        self, org: UUID, job: ArchiveJob, manifest: dict[str, Any], directory: Path
    ) -> None:
        # Verify all retained FK dependencies without executing inserts in dry-run.
        present = {part["table"] for part in manifest["parts"]}
        version = self._version(job)
        async with self.session(org) as session:
            if job.kind == "audit":
                await self._lock_audit_claim(session, org, job, lifecycle="archived")
            for number, part in enumerate(manifest["parts"]):
                table = TABLES[part["table"]]
                for row in read_rows(
                    part["table"], directory / f"{number:06d}.parquet", version
                ):
                    existing = (
                        (
                            await session.execute(
                                selection(
                                    part["table"], org, job.conversation_id, self._ids(job), version
                                ).where(table.c.id == row["id"])
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if existing is not None and (
                        not job.staged
                        or row_json(
                            decode_record(part["table"], dict(existing), version),
                            part["table"],
                            version,
                        )
                        != row_json(row, part["table"], version)
                    ):
                        raise ArchiveBlocked("restore_payload_collision")
                    if existing is None and await session.scalar(
                        text("SELECT platform_archive_payload_exists(:t,:id)"),
                        {"t": part["table"], "id": row["id"]},
                    ):
                        raise ArchiveBlocked("restore_payload_collision")
                    if await unique_collision(session, part["table"], row):
                        raise ArchiveBlocked("restore_payload_collision")
                    for constraint in table.foreign_key_constraints:
                        elements = list(constraint.elements)
                        if elements[0].column.table.name in present:
                            continue
                        values = [row[element.parent.name] for element in elements]
                        if any(value is None for value in values):
                            continue
                        query = select(elements[0].column).where(
                            *(element.column == value for element, value in zip(elements, values))
                        )
                        if await session.scalar(query.limit(1)) is None:
                            raise ArchiveBlocked("restore_dependency_missing")

    async def _stage(
        self, org: UUID, job: ArchiveJob, manifest: dict[str, Any], directory: Path
    ) -> None:
        async with self.session(org) as session:
            stored = await session.get(ArchiveJob, job.id, with_for_update=True)
            assert stored is not None
            if stored.state != "restore_pending" or stored.staged:
                raise ArchiveBlocked("restore_phase_changed")
            if job.kind == "audit":
                await self._lock_audit_claim(session, org, job, lifecycle="archived")
            version = self._version(job)
            # An edited image may reference a source in a later part, so the
            # self-reference is set once every image row exists.
            image_sources: builtins.list[dict[str, Any]] = []
            for number, part in enumerate(manifest["parts"]):
                table = part["table"]
                rows = list(read_rows(table, directory / f"{number:06d}.parquet", version))
                if table == "conversation_images":
                    for row in rows:
                        if row["source_image_id"] is not None:
                            image_sources.append(
                                {
                                    "image_id": row["id"],
                                    "source_id": row["source_image_id"],
                                    "archived_updated_at": row["updated_at"],
                                }
                            )
                            row["source_image_id"] = None
                if table in ("conversations", "messages", "audit_logs"):
                    for at in {
                        row["created_at"].replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                        for row in rows
                    }:
                        await session.execute(
                            text("SELECT platform_ensure_partition(:t,:at)"), dict(t=table, at=at)
                        )
                if rows:
                    # Core INSERT preserves explicit SQL NULL despite ORM defaults.
                    bindings = {
                        name: bindparam(
                            name,
                            type_=cast(Any, TABLES[table].c[name].type).__class__(
                                none_as_null=True
                            ),
                        )
                        for name, kind, _ in catalogue(version)[table]
                        if kind == "json"
                    }
                    for row in rows:
                        for name in bindings:
                            if row[name] is JSON_NULL:
                                row[name] = JSON.NULL
                    await session.execute(insert(TABLES[table]).values(**bindings), rows)
            if image_sources:
                images = TABLES["conversation_images"]
                await session.execute(
                    update(images)
                    .where(
                        images.c.org_id == org,
                        images.c.id == bindparam("image_id"),
                    )
                    # Explicit updated_at: the column's onupdate would
                    # otherwise change the archived row.
                    .values(
                        source_image_id=bindparam("source_id"),
                        updated_at=bindparam("archived_updated_at"),
                    ),
                    image_sources,
                )
            if (
                await inventory(session, org, job.conversation_id, self._ids(job), version)
                != job.inventory
            ):
                raise ArchiveBlocked("restore_inventory_mismatch")
            stored.staged = True
            await session.commit()
