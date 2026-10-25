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
from src.models.llm_usage import LLMUsageEvent, OrganizationUsageMonthly
from src.services import image_tools
from src.services.image_api_client import ImageApiError, ImageResult, ImageUsage
from src.services.image_tools import (
    IMAGE_TOOL_ATTEMPT_DURATION_SECONDS,
    IMAGE_TOOL_ATTEMPTS_TOTAL,
    ImageToolExecutionError,
    ImageToolsService,
)
from src.services.interaction import interaction_service
from src.services.quota import QuotaExceededError, QuotaService


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


class RecordingQuota:
    def __init__(self, reserve_error: Exception | None = None) -> None:
        self.reserve_error = reserve_error
        self.calls: list[tuple[str, Any]] = []

    async def reserve(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("reserve", {"model": args[3], "kind": kwargs["kind"].value}))
        if self.reserve_error is not None:
            raise self.reserve_error

    async def accept(self, org_id: UUID, request_id: UUID) -> None:
        self.calls.append(("accept", None))

    async def complete(self, org_id: UUID, request_id: UUID, *tokens: int) -> None:
        self.calls.append(("complete", tokens))

    async def cancel(self, org_id: UUID, request_id: UUID) -> None:
        self.calls.append(("cancel", None))


@pytest.fixture(autouse=True)
def quota(monkeypatch: pytest.MonkeyPatch) -> RecordingQuota:
    recording = RecordingQuota()
    monkeypatch.setattr(image_tools, "get_quota_service", lambda: recording)
    return recording


class FakeImages:
    def __init__(
        self,
        result: bytes | None = None,
        errors: list[str | ImageApiError] | None = None,
        usage: ImageUsage | None = None,
    ) -> None:
        self.result = result if result is not None else png()
        self.errors = list(errors or [])
        self.usage = usage
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _next(self) -> ImageResult:
        if self.errors:
            error = self.errors.pop(0)
            raise error if isinstance(error, ImageApiError) else ImageApiError(error)
        return ImageResult(self.result, self.usage)

    async def generate(self, *, model: str, prompt: str, size: str) -> ImageResult:
        self.calls.append(("generate", {"model": model, "prompt": prompt, "size": size}))
        return self._next()

    async def edit(
        self, *, model: str, prompt: str, image: bytes, content_type: str, size: str
    ) -> ImageResult:
        self.calls.append(
            ("edit", {"model": model, "prompt": prompt, "image": image, "type": content_type})
        )
        return self._next()


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
        async def generate(self, *, model: str, prompt: str, size: str) -> ImageResult:
            async with image_fixture.sessions.begin() as other:
                await other.execute(text("SET LOCAL lock_timeout = '200ms'"))
                await InteractionRepository(image_fixture.subject.org_id).cancel_turn(
                    other, image_fixture.turn_id
                )
            return ImageResult(png(), None)

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
    assert {row["origin"] for row in recent} == {"generated"}
    assert other == [] and RECENT_IMAGE_LIMIT == 8


async def test_each_provider_call_is_counted_against_the_quota(
    image_fixture: ImageFixture, quota: RecordingQuota
) -> None:
    usage = ImageUsage(prompt_tokens=50, completion_tokens=4000, total_tokens=4050)
    images = FakeImages(errors=["image_unavailable"], usage=usage)
    service = ImageToolsService(client=images, storage=FakeStorage(), authorize=allow())

    await _run(image_fixture, service, "generate_image", {"prompt": "x"})

    # The failed attempt gives its reservation back; the answered one is
    # accepted and completed with the provider's tokens.
    assert quota.calls == [
        ("reserve", {"model": "gen-model", "kind": "image_generate"}),
        ("cancel", None),
        ("reserve", {"model": "gen-model", "kind": "image_generate"}),
        ("accept", None),
        ("complete", (50, 4000, 4050)),
    ]


async def test_an_answer_without_usage_stays_an_accepted_request(
    image_fixture: ImageFixture, quota: RecordingQuota
) -> None:
    service = ImageToolsService(client=FakeImages(), storage=FakeStorage(), authorize=allow())
    await _run(image_fixture, service, "generate_image", {"prompt": "x"})
    assert [name for name, _ in quota.calls] == ["reserve", "accept"]


async def test_an_answered_but_unusable_result_is_still_counted(
    image_fixture: ImageFixture, quota: RecordingQuota
) -> None:
    usage = ImageUsage(1, 2, 3)
    images = FakeImages(errors=[ImageApiError("image_unavailable", accepted=True, usage=usage)] * 2)
    service = ImageToolsService(client=images, storage=FakeStorage(), authorize=allow())
    with pytest.raises(ImageToolExecutionError, match="image_unavailable"):
        await _run(image_fixture, service, "generate_image", {"prompt": "x"})
    assert [name for name, _ in quota.calls] == [
        "reserve", "accept", "complete", "reserve", "accept", "complete",
    ]


async def test_an_exhausted_quota_fails_the_operation_before_any_call(
    image_fixture: ImageFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import date

    exhausted = RecordingQuota(QuotaExceededError("requests", 10, 10, date(2026, 11, 1)))
    monkeypatch.setattr(image_tools, "get_quota_service", lambda: exhausted)
    images = FakeImages()
    service = ImageToolsService(client=images, storage=FakeStorage(), authorize=allow())
    with pytest.raises(ImageToolExecutionError, match="image_quota_exceeded"):
        await _run(image_fixture, service, "generate_image", {"prompt": "x"})
    assert images.calls == []
    [operation] = await _operations(image_fixture)
    assert (operation.status, operation.error_code) == ("failed", "image_quota_exceeded")


async def test_image_usage_lands_in_the_monthly_quota_counters(image_fixture: ImageFixture) -> None:
    org_id = image_fixture.subject.org_id
    real = QuotaService(image_fixture.sessions)
    service = ImageToolsService(
        client=FakeImages(usage=ImageUsage(10, 90, 100)),
        storage=FakeStorage(),
        authorize=allow(),
        quota=real,
    )
    try:
        await _run(image_fixture, service, "generate_image", {"prompt": "x"})
        async with image_fixture.sessions() as db:
            [event] = list(
                await db.scalars(select(LLMUsageEvent).where(LLMUsageEvent.org_id == org_id))
            )
            aggregate = await db.scalar(
                select(OrganizationUsageMonthly).where(OrganizationUsageMonthly.org_id == org_id)
            )
        assert (event.kind, event.model, event.status, event.total_tokens) == (
            "image_generate", "gen-model", "completed", 100,
        )
        assert event.conversation_id == image_fixture.conversation_id
        assert aggregate is not None
        assert (aggregate.requests_used, aggregate.total_tokens) == (1, 100)
    finally:
        async with image_fixture.sessions.begin() as db:
            await db.execute(delete(LLMUsageEvent).where(LLMUsageEvent.org_id == org_id))
            await db.execute(
                delete(OrganizationUsageMonthly).where(OrganizationUsageMonthly.org_id == org_id)
            )


@pytest.mark.parametrize(
    ("phase", "node_key", "expected"),
    [
        ("node", "draw", {"workflow_phase": "node", "workflow_node_key": "draw"}),
        ("supervisor_synthesize", None, {"workflow_phase": "supervisor_synthesize", "workflow_node_key": None}),
        ("ordinary", None, None),
    ],
)
async def test_workflow_image_spend_is_attributed_to_its_run_and_node(
    phase: str, node_key: str | None, expected: dict[str, Any] | None
) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    run_id = uuid4()
    result = MagicMock()
    result.one.return_value = (phase, node_key)
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    turn = SimpleNamespace(workflow_run_id=run_id)

    correlation = await ImageToolsService._workflow_correlation(session, uuid4(), uuid4(), turn)  # type: ignore[arg-type]

    assert correlation == ({} if expected is None else {"workflow_run_id": run_id, **expected})
    chat_turn = SimpleNamespace(workflow_run_id=None)
    session.execute.reset_mock()
    assert await ImageToolsService._workflow_correlation(session, uuid4(), uuid4(), chat_turn) == {}  # type: ignore[arg-type]
    session.execute.assert_not_awaited()
