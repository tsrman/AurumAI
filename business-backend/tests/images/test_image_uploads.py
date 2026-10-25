"""Uploaded conversation images: storage, claiming and inlining for vision models."""

import base64
import struct
import uuid
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user, get_current_user_or_session
from src.core.dependencies import get_org_db_session, get_service_db_session
from src.models.conversation import Conversation
from src.models.conversation_image import ConversationImage
from src.models.organization import Organization
from src.models.organization_settings import OrganizationSettings
from src.models.user import User
from src.routers import chat
from src.services import image_uploads
from src.services.image_content import ImageContentError
from src.services.image_uploads import (
    MAX_VISION_IMAGES_PER_REQUEST,
    OMITTED_IMAGE_NOTE,
    ImageUploadError,
    VisionImage,
    check_unclaimed_uploads,
    claim_uploads,
    select_vision_images,
    store_uploaded_image,
    vision_llm_messages,
)
from src.services.quota import get_quota_service
from tests.routers.test_chat_completions import RecordingQuotaService


def _png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    row = b"\x00" + b"\xff\x00\x00" * 2
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * 2))
        + chunk(b"IEND", b"")
    )


PNG = _png()


class _Storage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_bytes(self, key: str, data: bytes) -> None:
        self.objects[key] = data

    async def get_bytes(self, key: str) -> bytes:
        return self.objects[key]


@pytest.fixture
def images_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_uploads.settings, "IMAGES_ENABLED", True)


async def _conversation(db: AsyncSession, org: Organization, user: User) -> Conversation:
    conversation = Conversation(org_id=org.id, user_id=user.id, model="vision-model")
    db.add(conversation)
    await db.commit()
    return conversation


async def _mark_vision(db: AsyncSession, org: Organization, *models: str) -> None:
    db.add(OrganizationSettings(org_id=org.id, vision_models=list(models)))
    await db.commit()


async def test_upload_is_validated_and_stored_under_the_conversation(db, test_org, test_user) -> None:  # noqa: ANN001
    conversation = await _conversation(db, test_org, test_user)
    storage = _Storage()

    image = await store_uploaded_image(db, storage, test_org.id, conversation.id, PNG)

    assert image.origin == "uploaded" and image.turn_id is None and image.operation_id is None
    assert (image.content_type, image.width, image.height) == ("image/png", 2, 2)
    assert image.storage_key == (
        f"conversation-images/{test_org.id}/{conversation.id}/uploads/{image.id}.png"
    )
    assert storage.objects == {image.storage_key: PNG}
    with pytest.raises(ImageContentError):
        await store_uploaded_image(db, storage, test_org.id, conversation.id, b"<svg/>")


async def test_upload_size_limit_is_enforced(db, test_org, test_user, monkeypatch) -> None:  # noqa: ANN001
    conversation = await _conversation(db, test_org, test_user)
    monkeypatch.setattr(image_uploads.settings, "IMAGES_UPLOAD_MAX_BYTES", len(PNG) - 1)
    with pytest.raises(ImageContentError):
        await store_uploaded_image(db, _Storage(), test_org.id, conversation.id, PNG)


async def test_an_upload_is_claimed_once_by_a_turn_of_its_conversation(db, test_org, test_user) -> None:  # noqa: ANN001
    conversation = await _conversation(db, test_org, test_user)
    other = await _conversation(db, test_org, test_user)
    storage = _Storage()
    image = await store_uploaded_image(db, storage, test_org.id, conversation.id, PNG)
    foreign = await store_uploaded_image(db, storage, test_org.id, other.id, PNG)
    turn_id = uuid.uuid4()

    await check_unclaimed_uploads(db, test_org.id, conversation.id, [image.id, image.id])
    with pytest.raises(ImageUploadError):
        await check_unclaimed_uploads(db, test_org.id, conversation.id, [foreign.id])
    assert await claim_uploads(db, test_org.id, conversation.id, turn_id, [image.id]) == [
        str(image.id)
    ]
    await db.refresh(image)
    assert image.turn_id == turn_id
    with pytest.raises(ImageUploadError):
        await check_unclaimed_uploads(db, test_org.id, conversation.id, [image.id])
    with pytest.raises(ImageUploadError):
        await claim_uploads(db, test_org.id, conversation.id, uuid.uuid4(), [image.id])


async def test_vision_selection_needs_the_switch_a_vision_model_and_the_conversation(
    db, test_org, test_user, monkeypatch  # noqa: ANN001
) -> None:
    conversation = await _conversation(db, test_org, test_user)
    other = await _conversation(db, test_org, test_user)
    storage = _Storage()
    image = await store_uploaded_image(db, storage, test_org.id, conversation.id, PNG)
    foreign = await store_uploaded_image(db, storage, test_org.id, other.id, PNG)
    await _mark_vision(db, test_org, "vision-model")
    ids = [[image.id, foreign.id]]

    monkeypatch.setattr(image_uploads.settings, "IMAGES_ENABLED", False)
    assert await select_vision_images(db, test_org.id, conversation.id, "vision-model", ids) == {}
    monkeypatch.setattr(image_uploads.settings, "IMAGES_ENABLED", True)
    assert await select_vision_images(db, test_org.id, conversation.id, "text-model", ids) == {}
    assert await select_vision_images(db, test_org.id, None, "vision-model", ids) == {}
    selected = await select_vision_images(db, test_org.id, conversation.id, "vision-model", ids)
    assert selected == {0: [VisionImage(image.storage_key, "image/png")]}


async def test_only_the_newest_images_are_selected(db, test_org, test_user, images_enabled) -> None:  # noqa: ANN001
    conversation = await _conversation(db, test_org, test_user)
    storage = _Storage()
    await _mark_vision(db, test_org, "vision-model")
    stored = [
        await store_uploaded_image(db, storage, test_org.id, conversation.id, PNG)
        for _ in range(MAX_VISION_IMAGES_PER_REQUEST + 2)
    ]
    by_message = [[stored[0].id, stored[1].id], [], [image.id for image in stored[2:]]]

    selected = await select_vision_images(
        db, test_org.id, conversation.id, "vision-model", by_message
    )

    assert set(selected) == {2}
    assert [image.storage_key for image in selected[2]] == [image.storage_key for image in stored[2:]]


async def test_messages_get_image_parts_or_a_note_for_left_out_images() -> None:
    storage = _Storage()
    storage.objects["a"] = PNG
    first, second, missing = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    messages = [
        ("system", "rules", []),
        ("user", "old", [first]),
        ("user", "", [missing]),
        ("user", "look", [second, missing]),
        ("user", "", [second]),
    ]
    selected = {
        3: [VisionImage("a", "image/png"), VisionImage("gone", "image/png")],
        4: [VisionImage("a", "image/png")],
    }

    result = await vision_llm_messages(messages, selected, storage)

    encoded = base64.b64encode(PNG).decode()
    assert result[0] == {"role": "system", "content": "rules"}
    assert result[1] == {"role": "user", "content": f"old\n\n{OMITTED_IMAGE_NOTE}"}
    assert result[2] == {"role": "user", "content": OMITTED_IMAGE_NOTE}
    assert result[3] == {
        "role": "user",
        "content": [
            {"type": "text", "text": f"look\n\n{OMITTED_IMAGE_NOTE}"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
        ],
    }
    # No empty text block for an image-only message.
    assert result[4] == {
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
        ],
    }


def _upload_app(db: AsyncSession, user: UserInfo) -> FastAPI:
    app = FastAPI()
    app.include_router(chat.router, prefix="/v1")

    async def session_override() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_org_db_session] = session_override
    app.dependency_overrides[get_current_user] = lambda: user
    return app


async def test_upload_route_stores_owned_images_only_when_enabled(
    db, test_org, test_user, monkeypatch  # noqa: ANN001
) -> None:
    conversation = await _conversation(db, test_org, test_user)
    storage = _Storage()
    monkeypatch.setattr(chat, "object_storage", storage)
    monkeypatch.setattr(
        "src.core.authorization_dependencies.authorization_service.has_capability",
        AsyncMock(return_value=True),
    )
    owner = UserInfo(user_id=str(test_user.id), org_id=str(test_org.id), role="member")
    stranger = User(
        keycloak_id=uuid.uuid4(), email=f"s-{uuid.uuid4().hex[:8]}@example.com", org_id=test_org.id
    )
    db.add(stranger)
    await db.commit()
    url = f"/v1/conversations/{conversation.id}/images"
    files = {"file": ("photo.png", PNG, "image/png")}

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_upload_app(db, owner)), base_url="http://test"
    ) as client:
        monkeypatch.setattr(chat.settings, "IMAGES_ENABLED", False)
        disabled = await client.post(url, files=files)
        monkeypatch.setattr(chat.settings, "IMAGES_ENABLED", True)
        uploaded = await client.post(url, files=files)
        rejected = await client.post(url, files={"file": ("x.svg", b"<svg/>", "image/svg+xml")})
    async with httpx.AsyncClient(
        transport=ASGITransport(
            app=_upload_app(
                db, UserInfo(user_id=str(stranger.id), org_id=str(test_org.id), role="member")
            )
        ),
        base_url="http://test",
    ) as client:
        foreign = await client.post(url, files=files)

    assert disabled.status_code == 403
    assert uploaded.status_code == 201
    body = uploaded.json()["data"]
    assert (body["contentType"], body["width"], body["height"]) == ("image/png", 2, 2)
    image = await db.get(ConversationImage, uuid.UUID(body["imageId"]))
    assert image is not None and image.origin == "uploaded"
    assert rejected.status_code == 422
    assert foreign.status_code == 403
    assert len(storage.objects) == 1


async def test_completions_proxy_inlines_images_for_vision_models(
    db, test_org, test_user, images_enabled, monkeypatch  # noqa: ANN001
) -> None:
    conversation = await _conversation(db, test_org, test_user)
    storage = _Storage()
    image = await store_uploaded_image(db, storage, test_org.id, conversation.id, PNG)
    await db.commit()
    await _mark_vision(db, test_org, "vision-model")
    monkeypatch.setattr(chat, "object_storage", storage)
    monkeypatch.setattr(chat, "ensure_model_allowed", AsyncMock())
    monkeypatch.setattr(
        chat.authorization_service, "has_capability", AsyncMock(return_value=True)
    )
    sent: list[dict[str, Any]] = []

    async def fake_stream_chat(
        request: dict[str, Any], *, on_accepted: Callable[[], Awaitable[None]] | None = None
    ) -> AsyncIterator[str]:
        sent.append(request)
        if on_accepted is not None:
            await on_accepted()
        yield '{"choices":[{"delta":{"content":"ok"}}]}'

    async def session_override() -> AsyncIterator[AsyncSession]:
        yield db

    app = FastAPI()
    app.include_router(chat.router, prefix="/v1")
    app.dependency_overrides[get_service_db_session] = session_override
    app.dependency_overrides[get_current_user_or_session] = lambda: UserInfo(
        user_id=str(test_user.id),
        org_id=str(test_org.id),
        role="member",
        raw_claims={"type": "session", "conversation_id": str(conversation.id)},
    )
    app.dependency_overrides[get_quota_service] = RecordingQuotaService
    body = {
        "messages": [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "what is this", "image_ids": [str(image.id)]},
        ]
    }
    with patch.object(chat.llm_client, "stream_chat", side_effect=fake_stream_chat):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            vision = await client.post(
                "/v1/chat/completions", json={"model": "vision-model", **body}
            )
            text_only = await client.post(
                "/v1/chat/completions", json={"model": "text-model", **body}
            )
            assistant_images = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "vision-model",
                    "messages": [
                        {"role": "assistant", "content": "x", "image_ids": [str(image.id)]}
                    ],
                },
            )

    assert vision.status_code == text_only.status_code == 200
    parts = sent[0]["messages"][1]["content"]
    assert parts[0] == {"type": "text", "text": "what is this"}
    assert parts[1]["image_url"]["url"] == (
        "data:image/png;base64," + base64.b64encode(PNG).decode()
    )
    assert sent[0]["messages"][0] == {"role": "system", "content": "rules"}
    assert sent[1]["messages"][1] == {
        "role": "user",
        "content": f"what is this\n\n{OMITTED_IMAGE_NOTE}",
    }
    assert assistant_images.status_code == 422
    assert len(sent) == 2


async def test_only_an_unsent_upload_can_be_deleted(db, test_org, test_user, monkeypatch) -> None:  # noqa: ANN001
    conversation = await _conversation(db, test_org, test_user)
    storage = _Storage()
    deleted: list[str] = []

    async def delete_object(key: str) -> None:
        deleted.append(key)

    storage.delete = delete_object  # type: ignore[attr-defined]
    monkeypatch.setattr(chat, "object_storage", storage)
    unsent = await store_uploaded_image(db, storage, test_org.id, conversation.id, PNG)
    sent = await store_uploaded_image(db, storage, test_org.id, conversation.id, PNG)
    await claim_uploads(db, test_org.id, conversation.id, uuid.uuid4(), [sent.id])
    await db.commit()
    owner = UserInfo(user_id=str(test_user.id), org_id=str(test_org.id), role="member")
    base = f"/v1/conversations/{conversation.id}/images"

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_upload_app(db, owner)), base_url="http://test"
    ) as client:
        removed = await client.delete(f"{base}/{unsent.id}")
        again = await client.delete(f"{base}/{unsent.id}")
        kept = await client.delete(f"{base}/{sent.id}")

    assert (removed.status_code, again.status_code, kept.status_code) == (204, 404, 404)
    assert deleted == [unsent.storage_key]
    assert await db.get(ConversationImage, unsent.id) is None
    assert await db.get(ConversationImage, sent.id) is not None
