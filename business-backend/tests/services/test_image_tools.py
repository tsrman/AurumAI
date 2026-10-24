import struct
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.authorization_types import AuthorizationSubject
from src.core.config import settings
from src.models.conversation import Conversation
from src.models.conversation_image import ConversationImage
from src.models.interaction import InteractionExecution, InteractionTurn, ToolOperation
from src.models.organization import Organization
from src.models.organization_settings import OrganizationSettings
from src.models.user import User
from src.repositories.interaction_repository import InteractionRepository
from src.schemas.interaction import TurnLimits
from src.services.image_api_client import ImageApiError
from src.services.image_tools import (
    IMAGE_TOOL_ATTEMPT_DURATION_SECONDS,
    IMAGE_TOOL_ATTEMPTS_TOTAL,
    ImageToolExecutionError,
    ImageToolsService,
)
from src.services.interaction import interaction_service


def png(width: int = 2, height: int = 2) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IEND", b"")


@dataclass(frozen=True)
class ImageFixture:
    sessions: async_sessionmaker[AsyncSession]
    subject: AuthorizationSubject
    conversation_id: UUID
    turn_id: UUID
    execution_id: UUID


@pytest.fixture
async def image_fixture() -> ImageFixture:
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    org_id, owner_id, conversation_id, turn_id, execution_id = (uuid4() for _ in range(5))
    try:
        async with sessions.begin() as db:
            db.add(Organization(id=org_id, name="Image service test", slug=f"img-svc-{org_id}"))
            await db.flush()
            db.add(User(id=owner_id, org_id=org_id, keycloak_id=uuid4(), email=f"{owner_id}@example.test"))
            db.add(
                OrganizationSettings(
                    org_id=org_id,
                    image_generation_model="gen-model",
                    image_edit_model="edit-model",
                )
            )
            await db.flush()
            db.add(Conversation(id=conversation_id, org_id=org_id, user_id=owner_id, model="fixture"))
            await db.flush()
            db.add(
                InteractionTurn(
                    id=turn_id,
                    org_id=org_id,
                    conversation_id=conversation_id,
                    owner_id=owner_id,
                    limits=TurnLimits(image_total=2).model_dump(),
                )
            )
            await db.flush()
            db.add(
                InteractionExecution(
                    id=execution_id,
                    org_id=org_id,
                    turn_id=turn_id,
                    workflow_id=f"conversation-{conversation_id}",
                    run_id=uuid4(),
                    task_queue=f"session-{conversation_id}",
                    phase="ordinary",
                    iteration=1,
                )
            )
        yield ImageFixture(
            sessions,
            AuthorizationSubject(owner_id, org_id, "member"),
            conversation_id,
            turn_id,
            execution_id,
        )
    finally:
        async with sessions.begin() as db:
            await db.execute(delete(InteractionTurn).where(InteractionTurn.id == turn_id))
            await db.execute(delete(Conversation).where(Conversation.id == conversation_id))
            await db.execute(
                text("DELETE FROM conversation_identities WHERE id = :id"), {"id": conversation_id}
            )
            await db.execute(delete(OrganizationSettings).where(OrganizationSettings.org_id == org_id))
            await db.execute(delete(User).where(User.id == owner_id))
            await db.execute(delete(Organization).where(Organization.id == org_id))
        await engine.dispose()


class FakeImages:
    def __init__(self, result: bytes | None = None, errors: list[str] | None = None) -> None:
        self.result = result if result is not None else png()
        self.errors = list(errors or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def generate(self, *, model: str, prompt: str, size: str) -> bytes:
        self.calls.append(("generate", {"model": model, "prompt": prompt, "size": size}))
        if self.errors:
            raise ImageApiError(self.errors.pop(0))
        return self.result

    async def edit(
        self, *, model: str, prompt: str, image: bytes, content_type: str, size: str
    ) -> bytes:
        self.calls.append(
            ("edit", {"model": model, "prompt": prompt, "image": image, "type": content_type})
        )
        if self.errors:
            raise ImageApiError(self.errors.pop(0))
        return self.result


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_bytes(self, key: str, data: bytes) -> None:
        self.objects[key] = data

    async def get_bytes(self, key: str) -> bytes:
        return self.objects[key]


def allow() -> Callable[..., Awaitable[None]]:
    async def allow_all(*args: object) -> None:
        del args

    return allow_all


async def _operations(fixture: ImageFixture) -> list[ToolOperation]:
    async with fixture.sessions() as db:
        return list(
            await db.scalars(
                select(ToolOperation).where(ToolOperation.org_id == fixture.subject.org_id)
            )
        )


async def _images(fixture: ImageFixture) -> list[ConversationImage]:
    async with fixture.sessions() as db:
        return list(
            await db.scalars(
                select(ConversationImage).where(
                    ConversationImage.org_id == fixture.subject.org_id
                )
            )
        )


async def _run(
    fixture: ImageFixture,
    service: ImageToolsService,
    tool: str,
    args: dict[str, Any],
    invocation_id: UUID | None = None,
) -> dict[str, Any]:
    async with fixture.sessions() as db:
        return await service.execute(
            db, fixture.subject, fixture.execution_id, invocation_id or uuid4(), tool, args
        )


async def test_generation_stores_one_image_and_retries_return_it(image_fixture: ImageFixture) -> None:
    images, storage = FakeImages(), FakeStorage()
    service = ImageToolsService(client=images, storage=storage, authorize=allow())
    invocation = uuid4()

    first = await _run(image_fixture, service, "generate_image", {"prompt": "a red fox"}, invocation)
    again = await _run(image_fixture, service, "generate_image", {"prompt": "a red fox"}, invocation)

    assert first == again
    assert first["contentType"] == "image/png" and (first["width"], first["height"]) == (2, 2)
    assert images.calls == [("generate", {"model": "gen-model", "prompt": "a red fox", "size": "auto"})]
    [row] = await _images(image_fixture)
    assert str(row.id) == first["imageId"]
    assert (row.origin, row.model, row.prompt, row.source_image_id) == (
        "generated",
        "gen-model",
        "a red fox",
        None,
    )
    assert row.conversation_id == image_fixture.conversation_id
    assert storage.objects == {row.storage_key: png()}
    assert row.storage_key.startswith(
        f"conversation-images/{image_fixture.subject.org_id}/{image_fixture.conversation_id}/"
    )


async def test_edit_reads_the_source_from_the_same_conversation(image_fixture: ImageFixture) -> None:
    images, storage = FakeImages(), FakeStorage()
    service = ImageToolsService(client=images, storage=storage, authorize=allow())
    source = await _run(image_fixture, service, "generate_image", {"prompt": "a fox"})
    images.result = png(4, 3)

    edited = await _run(
        image_fixture,
        service,
        "edit_image",
        {"image_id": source["imageId"], "prompt": "make it blue", "size": "1024x1024"},
    )

    assert images.calls[1] == (
        "edit",
        {"model": "edit-model", "prompt": "make it blue", "image": png(), "type": "image/png"},
    )
    rows = {str(row.id): row for row in await _images(image_fixture)}
    assert str(rows[edited["imageId"]].source_image_id) == source["imageId"]
    assert (edited["width"], edited["height"]) == (4, 3)


async def test_edit_rejects_images_outside_the_conversation(image_fixture: ImageFixture) -> None:
    service = ImageToolsService(client=FakeImages(), storage=FakeStorage(), authorize=allow())
    with pytest.raises(ImageToolExecutionError, match="image_not_found"):
        await _run(
            image_fixture, service, "edit_image", {"image_id": str(uuid4()), "prompt": "blue"}
        )


async def test_unconfigured_model_disables_the_tool_without_spending_budget(
    image_fixture: ImageFixture,
) -> None:
    async with image_fixture.sessions.begin() as db:
        settings_row = await db.get(OrganizationSettings, image_fixture.subject.org_id)
        assert settings_row is not None
        settings_row.image_edit_model = None
    images = FakeImages()
    service = ImageToolsService(client=images, storage=FakeStorage(), authorize=allow())
    with pytest.raises(ImageToolExecutionError, match="image_disabled"):
        await _run(image_fixture, service, "edit_image", {"image_id": str(uuid4()), "prompt": "x"})
    assert images.calls == [] and await _operations(image_fixture) == []


@pytest.mark.parametrize(
    "args",
    [{"prompt": "   "}, {"prompt": "x", "size": "4096x4096"}, {"prompt": "x", "url": "https://a"}],
)
async def test_invalid_arguments_are_rejected_before_any_call(
    image_fixture: ImageFixture, args: dict[str, Any]
) -> None:
    images = FakeImages()
    service = ImageToolsService(client=images, storage=FakeStorage(), authorize=allow())
    with pytest.raises(ImageToolExecutionError, match="image_rejected"):
        await _run(image_fixture, service, "generate_image", args)
    assert images.calls == []


async def test_non_image_bytes_are_never_stored(image_fixture: ImageFixture) -> None:
    storage = FakeStorage()
    service = ImageToolsService(
        client=FakeImages(result=b"<svg xmlns='http://www.w3.org/2000/svg'/>"),
        storage=storage,
        authorize=allow(),
    )
    with pytest.raises(ImageToolExecutionError, match="image_unavailable"):
        await _run(image_fixture, service, "generate_image", {"prompt": "x"})
    assert storage.objects == {} and await _images(image_fixture) == []
    [operation] = await _operations(image_fixture)
    assert (operation.status, operation.error_code) == ("failed", "image_unavailable")


async def test_provider_rejection_is_not_retried(image_fixture: ImageFixture) -> None:
    images = FakeImages(errors=["image_rejected"])
    service = ImageToolsService(client=images, storage=FakeStorage(), authorize=allow())
    with pytest.raises(ImageToolExecutionError, match="image_rejected"):
        await _run(image_fixture, service, "generate_image", {"prompt": "x"})
    assert len(images.calls) == 1


async def test_transient_outage_is_retried_once(image_fixture: ImageFixture) -> None:
    images = FakeImages(errors=["image_unavailable"])
    service = ImageToolsService(client=images, storage=FakeStorage(), authorize=allow())
    result = await _run(image_fixture, service, "generate_image", {"prompt": "x"})
    assert len(images.calls) == 2 and result["imageId"]
    [operation] = await _operations(image_fixture)
    assert (operation.status, operation.attempts) == ("completed", 2)


async def test_per_turn_image_budget_is_shared_by_generation_and_editing(
    image_fixture: ImageFixture,
) -> None:
    service = ImageToolsService(client=FakeImages(), storage=FakeStorage(), authorize=allow())
    first = await _run(image_fixture, service, "generate_image", {"prompt": "one"})
    await _run(image_fixture, service, "edit_image", {"image_id": first["imageId"], "prompt": "two"})
    with pytest.raises(ImageToolExecutionError, match="image_limit"):
        await _run(image_fixture, service, "generate_image", {"prompt": "three"})


async def test_cancelled_turn_fails_the_operation_as_cancelled(
    image_fixture: ImageFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(interaction_service, "_require_live", AsyncMock())
    monkeypatch.setattr(interaction_service._authorization, "require_capability", AsyncMock())

    class CancelDuringGeneration(FakeImages):
        async def generate(self, *, model: str, prompt: str, size: str) -> bytes:
            async with image_fixture.sessions.begin() as other:
                await other.execute(text("SET LOCAL lock_timeout = '200ms'"))
                await InteractionRepository(image_fixture.subject.org_id).cancel_turn(
                    other, image_fixture.turn_id
                )
            return png()

    service = ImageToolsService(client=CancelDuringGeneration(), storage=FakeStorage())
    with pytest.raises(ImageToolExecutionError, match="image_cancelled"):
        await _run(image_fixture, service, "generate_image", {"prompt": "x"})
    assert await _images(image_fixture) == []
    [operation] = await _operations(image_fixture)
    assert operation.status in {"cancelled", "failed"}


def test_image_metrics_have_only_bounded_non_tenant_dimensions() -> None:
    for metric in (IMAGE_TOOL_ATTEMPTS_TOTAL, IMAGE_TOOL_ATTEMPT_DURATION_SECONDS):
        assert metric._labelnames == ("tool", "status")


async def test_recent_images_are_newest_first_bounded_and_conversation_scoped(
    image_fixture: ImageFixture,
) -> None:
    from src.services.image_tools import RECENT_IMAGE_LIMIT, recent_conversation_images

    service = ImageToolsService(client=FakeImages(), storage=FakeStorage(), authorize=allow())
    first = await _run(image_fixture, service, "generate_image", {"prompt": "x" * 500})
    second = await _run(image_fixture, service, "generate_image", {"prompt": "second"})
    async with image_fixture.sessions() as db:
        recent = await recent_conversation_images(
            db, image_fixture.subject.org_id, image_fixture.conversation_id
        )
        other = await recent_conversation_images(db, image_fixture.subject.org_id, uuid4())
    assert [row["id"] for row in recent] == [second["imageId"], first["imageId"]]
    assert recent[1]["prompt"] == "x" * 200
    assert other == [] and RECENT_IMAGE_LIMIT == 8
