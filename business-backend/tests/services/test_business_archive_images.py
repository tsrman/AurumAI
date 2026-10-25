# ruff: noqa: F811
"""Archive format versions and conversation images across archive and restore."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest
from sqlalchemy import text

from src.services.archive import service as service_module
from src.services.archive.catalogue import COLUMNS_V1, CURRENT_VERSION
from src.services.archive.schemas import ArchiveFormatError, read_rows, schema_for
from src.services.archive.service import ArchiveService
from src.services.archive.snapshot import inventory
from tests.services.test_business_archive_service import (  # noqa: F401
    LocalArchiveStorage,
    archive_fixture,
)

GENERATED = b"\x89PNG\r\n\x1a\ngenerated"
EDITED = b"\x89PNG\r\n\x1a\nedited-version"
UPLOADED = b"\xff\xd8\xffuploaded"


class LocalPlatformImages:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.fail_upload = False

    async def size(self, key: str) -> int | None:
        target = self.root / key
        return target.stat().st_size if target.exists() else None

    async def download(self, key: str, path: Path, limit: int) -> None:
        data = (self.root / key).read_bytes()
        assert len(data) <= limit
        path.write_bytes(data)

    async def upload(self, key: str, path: Path) -> None:
        if self.fail_upload:
            raise ArchiveFormatError("image_write_failed")
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())

    async def delete(self, key: str) -> None:
        (self.root / key).unlink(missing_ok=True)

    def put(self, key: str, data: bytes) -> None:
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def exists(self, key: str) -> bool:
        return (self.root / key).exists()


async def _history(sessions, org: UUID, conversation: UUID, *, image_count: int = 0) -> dict:  # noqa: ANN001
    """One finished turn with its execution receipt, as archive eligibility needs."""
    turn, execution, run = uuid4(), uuid4(), uuid4()
    at = datetime.now(timezone.utc) - timedelta(days=181)
    args = dict(o=org, c=conversation, t=turn, e=execution, r=run, at=at, n=image_count)
    async with sessions() as db:
        args["u"] = await db.scalar(text("SELECT user_id FROM conversations WHERE id=:c"), args)
        await db.execute(
            text(
                "INSERT INTO interaction_turns(id,org_id,conversation_id,owner_id,status,limits,image_count,terminal_at,created_at,updated_at) VALUES(:t,:o,:c,:u,'completed','{}',:n,:at,:at,:at)"
            ),
            args,
        )
        await db.execute(
            text(
                "INSERT INTO interaction_executions(id,org_id,turn_id,workflow_id,run_id,task_queue,phase) VALUES(:e,:o,:t,:wf,:r,'test','ordinary')"
            ),
            dict(args, wf=f"archive-test-{run}"),
        )
        await db.execute(
            text(
                "INSERT INTO conversation_executions(id,org_id,conversation_id,generation,namespace,workflow_id,run_id,workflow_type,task_queue,terminal_status,closed_at,observed_at,tree_complete) VALUES(:id,:o,:c,1,'default',:wf,:r,'ConversationWorkflow','test','COMPLETED',:at,:at,true)"
            ),
            dict(args, id=uuid4(), wf=f"archive-test-{run}"),
        )
        await db.execute(
            text("UPDATE conversation_identities SET execution_provenance_complete=true WHERE id=:c"),
            args,
        )
        await db.commit()
    return args


async def _images(sessions, platform: LocalPlatformImages, args: dict) -> dict[str, UUID]:  # noqa: ANN001
    """A generated image, an edit of it and an upload, with their bytes."""
    org, conversation = args["o"], args["c"]
    generate_op, edit_op = uuid4(), uuid4()
    generated, edited, uploaded = uuid4(), uuid4(), uuid4()
    prefix = f"conversation-images/{org}/{conversation}"
    keys = {
        generated: f"{prefix}/{generate_op}.png",
        edited: f"{prefix}/{edit_op}.png",
        uploaded: f"{prefix}/uploads/{uploaded}.jpg",
    }
    async with sessions() as db:
        for op, kind in ((generate_op, "image_generate"), (edit_op, "image_edit")):
            await db.execute(
                text(
                    "INSERT INTO tool_operations(id,org_id,turn_id,execution_id,invocation_id,kind,arguments_hash,status,result) VALUES(:op,:o,:t,:e,:op,:k,:hash,'completed','{\"imageId\":\"x\"}')"
                ),
                dict(args, op=op, k=kind, hash="c" * 64),
            )
        rows = [
            (generated, generate_op, "generated", None, GENERATED, "image/png"),
            # The edit points at its source, which must survive the round trip.
            (edited, edit_op, "generated", generated, EDITED, "image/png"),
            (uploaded, None, "uploaded", None, UPLOADED, "image/jpeg"),
        ]
        for image_id, op, origin, source, data, content_type in rows:
            await db.execute(
                text(
                    "INSERT INTO conversation_images(id,org_id,conversation_id,turn_id,execution_id,operation_id,origin,source_image_id,storage_key,content_type,size_bytes,prompt,created_at,updated_at) VALUES(:id,:o,:c,:t,:ex,:op,:origin,:src,:key,:ct,:size,'a fox',:at,:at)"
                ),
                dict(
                    args,
                    id=image_id,
                    ex=args["e"] if op else None,
                    op=op,
                    origin=origin,
                    src=source,
                    key=keys[image_id],
                    ct=content_type,
                    size=len(data),
                ),
            )
            platform.put(keys[image_id], data)
        await db.commit()
    return {"generated": generated, "edited": edited, "uploaded": uploaded, **{
        f"key:{image_id}": key for image_id, key in keys.items()
    }}


async def _image_rows(sessions, org: UUID) -> list[tuple]:  # noqa: ANN001
    async with sessions() as db:
        return (
            await db.execute(
                text(
                    "SELECT id,origin,source_image_id,storage_key,size_bytes,prompt FROM conversation_images WHERE org_id=:o ORDER BY id"
                ),
                dict(o=org),
            )
        ).all()


async def test_conversation_images_survive_archive_and_restore(archive_fixture, tmp_path):
    sessions, org, conversation = archive_fixture
    platform = LocalPlatformImages(tmp_path / "platform")
    args = await _history(sessions, org, conversation, image_count=2)
    ids = await _images(sessions, platform, args)
    before = await _image_rows(sessions, org)
    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"), images=platform)

    result = await service.run(org, conversation, apply=True)

    assert result["state"] == "purged"
    assert await _image_rows(sessions, org) == []
    keys = [ids[f"key:{ids[name]}"] for name in ("generated", "edited", "uploaded")]
    assert not any(platform.exists(key) for key in keys)
    archived = tmp_path / "objects" / f"archives/v1/{org}/{result['archive_id']}/images"
    assert sorted(path.read_bytes() for path in archived.iterdir()) == sorted(
        [GENERATED, EDITED, UPLOADED]
    )
    [job] = await service.list(org)
    assert job["counts"]["conversation_images"] == 3

    await service.restore(org, result["archive_id"], apply=False)
    await service.restore(org, result["archive_id"], apply=True)

    assert await _image_rows(sessions, org) == before
    restored = {key: (platform.root / key).read_bytes() for key in keys}
    assert restored == dict(zip(keys, [GENERATED, EDITED, UPLOADED]))
    async with sessions() as db:
        assert await db.scalar(
            text("SELECT image_count FROM interaction_turns WHERE id=:t"), args
        ) == 2


async def test_images_block_archiving_until_image_storage_is_configured(archive_fixture, tmp_path):
    sessions, org, conversation = archive_fixture
    platform = LocalPlatformImages(tmp_path / "platform")
    await _images(sessions, platform, await _history(sessions, org, conversation))
    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"))

    for apply in (False, True):
        result = await service.run(org, conversation, apply=apply)
        assert result == {"eligible": False, "blocked_reason": "image_archive_not_configured"}
    assert len(await _image_rows(sessions, org)) == 3


async def test_a_purge_interrupted_after_deleting_image_objects_resumes(
    archive_fixture, tmp_path, monkeypatch
):
    sessions, org, conversation = archive_fixture
    platform = LocalPlatformImages(tmp_path / "platform")
    await _images(sessions, platform, await _history(sessions, org, conversation))
    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"), images=platform)
    original = service._purge

    async def crash(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("test_process_loss")

    monkeypatch.setattr(service, "_purge", crash)
    with pytest.raises(RuntimeError, match="test_process_loss"):
        await service.run(org, conversation, apply=True)
    # Objects are gone, rows still present: the next resume finishes the purge.
    assert len(await _image_rows(sessions, org)) == 3
    monkeypatch.setattr(service, "_purge", original)
    [job] = await service.list(org)
    await service.resume(org, UUID(job["archive_id"]))

    assert (await service.list(org))[0]["state"] == "purged"
    assert await _image_rows(sessions, org) == []


async def test_a_failed_image_put_back_leaves_the_restore_resumable(archive_fixture, tmp_path):
    sessions, org, conversation = archive_fixture
    platform = LocalPlatformImages(tmp_path / "platform")
    ids = await _images(sessions, platform, await _history(sessions, org, conversation))
    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"), images=platform)
    archive = (await service.run(org, conversation, apply=True))["archive_id"]

    platform.fail_upload = True
    with pytest.raises(ArchiveFormatError, match="image_write_failed"):
        await service.restore(org, archive, apply=True)
    async with sessions() as db:
        # Staged rows stay hidden while their bytes are missing.
        assert await db.scalar(
            text("SELECT lifecycle FROM conversation_identities WHERE id=:c"), dict(c=conversation)
        ) == "restore_pending"
    platform.fail_upload = False
    await service.restore(org, archive, apply=True)

    assert (await service.list(org))[0]["state"] == "restored"
    assert platform.exists(ids[f"key:{ids['uploaded']}"])


async def test_an_image_key_outside_the_conversation_blocks_the_archive(archive_fixture, tmp_path):
    sessions, org, conversation = archive_fixture
    platform = LocalPlatformImages(tmp_path / "platform")
    args = await _history(sessions, org, conversation)
    async with sessions() as db:
        await db.execute(
            text(
                "INSERT INTO conversation_images(id,org_id,conversation_id,origin,storage_key,content_type,size_bytes) VALUES(:id,:o,:c,'uploaded',:key,'image/png',4)"
            ),
            dict(args, id=uuid4(), key=f"conversation-images/{org}/{uuid4()}/uploads/{uuid4()}.png"),
        )
        await db.commit()
    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"), images=platform)
    with pytest.raises(ArchiveFormatError, match="image_inventory_unknown"):
        await service.run(org, conversation, apply=False)


def test_version_one_parts_stay_readable_and_differ_from_version_two(tmp_path):
    path = tmp_path / "turns.parquet"
    pq.write_table(pa.Table.from_pylist([], schema=schema_for("interaction_turns", 1)), path)

    assert list(read_rows("interaction_turns", path, 1)) == []
    with pytest.raises(ArchiveFormatError, match="schema_mismatch"):
        list(read_rows("interaction_turns", path))
    assert "image_count" not in [name for name, _, _ in COLUMNS_V1["interaction_turns"]]
    assert "conversation_images" not in COLUMNS_V1 and CURRENT_VERSION == 2


async def test_a_version_one_job_archives_and_restores(archive_fixture, tmp_path, monkeypatch):
    """Jobs frozen before versioning carry no marker and keep the v1 format."""
    sessions, org, conversation = archive_fixture
    args = await _history(sessions, org, conversation)
    original = service_module.inventory

    async def frozen_as_v1(session, org_id, conversation_id, audit_ids=None, version=None):  # noqa: ANN001, ANN202
        return await original(session, org_id, conversation_id, audit_ids, version or 1)

    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"))
    monkeypatch.setattr(service_module, "inventory", frozen_as_v1)
    archive = (await service.run(org, conversation, apply=True))["archive_id"]
    monkeypatch.setattr(service_module, "inventory", original)
    [job] = await service.list(org)
    assert "conversation_images" not in job["counts"]

    await service.restore(org, archive, apply=True)

    async with sessions() as db:
        assert await db.scalar(
            text("SELECT image_count FROM interaction_turns WHERE id=:t"), args
        ) == 0


async def test_a_version_one_inventory_refuses_a_non_default_new_column(archive_fixture):
    sessions, org, conversation = archive_fixture
    await _history(sessions, org, conversation, image_count=1)
    async with sessions() as db:
        await db.execute(text("SET LOCAL ROLE aurum_ai_lifecycle"))
        await db.execute(text("SELECT set_config('app.current_org_id',:o,true)"), {"o": str(org)})
        with pytest.raises(ArchiveFormatError, match="archive_version_incompatible"):
            await inventory(db, org, conversation, version=1)


def test_object_keys_are_bounded_to_their_kind():
    from src.services.archive.storage import (
        IMAGE_OBJECT_LIMIT,
        MAX_PART,
        PLATFORM_IMAGE_KEY,
        read_limit,
    )

    org, archive, image = uuid4(), uuid4(), uuid4()
    assert read_limit(f"archives/v1/{org}/{archive}/images/{image}") == IMAGE_OBJECT_LIMIT
    assert read_limit(f"archives/v1/{org}/{archive}/messages/000000.parquet") == MAX_PART
    with pytest.raises(ArchiveFormatError):
        read_limit(f"archives/v1/{org}/{archive}/images/{image}.png")
    assert PLATFORM_IMAGE_KEY.fullmatch(f"conversation-images/{org}/{archive}/{image}.webp")
    assert PLATFORM_IMAGE_KEY.fullmatch(f"conversation-images/{org}/{archive}/uploads/{image}.jpg")
    for key in (
        f"documents/{org}/{archive}/{image}.png",
        f"conversation-images/{org}/{archive}/../{image}.png",
        f"conversation-images/{org}/{archive}/{image}.svg",
    ):
        assert not PLATFORM_IMAGE_KEY.fullmatch(key)


async def test_archive_storage_refuses_an_image_key_above_its_bound(tmp_path, monkeypatch):
    from src.services.archive import storage as storage_module

    monkeypatch.setattr(storage_module, "IMAGE_OBJECT_LIMIT", 4)
    path = tmp_path / "image"
    path.write_bytes(b"12345")
    s3 = storage_module.S3ArchiveStorage("http://unused", "a", "b", "bucket")
    with pytest.raises(ArchiveFormatError, match="object_invalid"):
        await s3.upload(f"archives/v1/{uuid4()}/{uuid4()}/images/{uuid4()}", path)


@pytest.mark.parametrize("damage", ["missing", "resized"])
async def test_a_missing_or_resized_image_object_blocks_before_the_claim(
    archive_fixture, tmp_path, damage
):
    sessions, org, conversation = archive_fixture
    platform = LocalPlatformImages(tmp_path / "platform")
    ids = await _images(sessions, platform, await _history(sessions, org, conversation))
    key = ids[f"key:{ids['uploaded']}"]
    if damage == "missing":
        await platform.delete(key)
    else:
        platform.put(key, b"different size")
    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"), images=platform)

    for apply in (False, True):
        result = await service.run(org, conversation, apply=apply)
        assert result == {"eligible": False, "blocked_reason": "image_bytes_unavailable"}
    assert await service.list(org) == []
    async with sessions() as db:
        assert await db.scalar(
            text("SELECT lifecycle FROM conversation_identities WHERE id=:c"), dict(c=conversation)
        ) == "active"
