"""User-uploaded conversation images and their delivery to vision models.

Uploads are stored like generated images but carry no tool operation. A user
message claims them by id; the chat completions proxy later inlines them as
base64 ``image_url`` parts, so image bytes never travel through the runner or
its workflow history.
"""

import base64
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.conversation_image import ConversationImage
from src.services.image_content import inspect_image
from src.services.organization_model_settings import organization_model_settings_service

logger = logging.getLogger(__name__)

MAX_IMAGES_PER_MESSAGE = 4
# Images inlined into one completion request, newest first; older ones are
# replaced by OMITTED_IMAGE_NOTE so a long conversation stays within provider
# request limits.
MAX_VISION_IMAGES_PER_REQUEST = 4
OMITTED_IMAGE_NOTE = "[An image attached here is not available to this model.]"


class ImageUploadError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class UploadStorage(Protocol):
    async def put_bytes(self, key: str, data: bytes) -> None: ...


class ReadStorage(Protocol):
    async def get_bytes(self, key: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class VisionImage:
    storage_key: str
    content_type: str


def uploaded_image_key(org_id: UUID, conversation_id: UUID, image_id: UUID, extension: str) -> str:
    return f"conversation-images/{org_id}/{conversation_id}/uploads/{image_id}.{extension}"


async def store_uploaded_image(
    session: AsyncSession,
    storage: UploadStorage,
    org_id: UUID,
    conversation_id: UUID,
    data: bytes,
) -> ConversationImage:
    """Validate and store one upload; raises ImageContentError for bad bytes."""
    info = inspect_image(data, max_bytes=settings.IMAGES_UPLOAD_MAX_BYTES)
    image_id = uuid4()
    key = uploaded_image_key(org_id, conversation_id, image_id, info.extension)
    await storage.put_bytes(key, data)
    image = ConversationImage(
        id=image_id,
        org_id=org_id,
        conversation_id=conversation_id,
        origin="uploaded",
        storage_key=key,
        content_type=info.content_type,
        size_bytes=info.size_bytes,
        width=info.width,
        height=info.height,
    )
    session.add(image)
    await session.flush()
    return image


async def vision_enabled(session: AsyncSession, org_id: UUID, model: str) -> bool:
    if not settings.IMAGES_ENABLED:
        return False
    return model in await organization_model_settings_service.vision_models(session, org_id)


def _unique(image_ids: Sequence[UUID]) -> tuple[UUID, ...]:
    return tuple(dict.fromkeys(image_ids))


async def check_unclaimed_uploads(
    session: AsyncSession, org_id: UUID, conversation_id: UUID, image_ids: Sequence[UUID]
) -> None:
    """Every id must be an upload of this conversation no message has used yet."""
    wanted = _unique(image_ids)
    if not wanted:
        return
    found = set(
        await session.scalars(
            select(ConversationImage.id).where(
                ConversationImage.org_id == org_id,
                ConversationImage.conversation_id == conversation_id,
                ConversationImage.origin == "uploaded",
                ConversationImage.turn_id.is_(None),
                ConversationImage.id.in_(wanted),
            )
        )
    )
    if found != set(wanted):
        raise ImageUploadError("image_not_available")


async def claim_uploads(
    session: AsyncSession,
    org_id: UUID,
    conversation_id: UUID,
    turn_id: UUID,
    image_ids: Sequence[UUID],
) -> list[str]:
    """Bind the uploads to the turn whose user message carries them."""
    wanted = _unique(image_ids)
    if not wanted:
        return []
    claimed = set(
        await session.scalars(
            update(ConversationImage)
            .where(
                ConversationImage.org_id == org_id,
                ConversationImage.conversation_id == conversation_id,
                ConversationImage.origin == "uploaded",
                ConversationImage.turn_id.is_(None),
                ConversationImage.id.in_(wanted),
            )
            .values(turn_id=turn_id)
            .returning(ConversationImage.id)
        )
    )
    if claimed != set(wanted):
        raise ImageUploadError("image_not_available")
    return [str(image_id) for image_id in wanted]


async def delete_unclaimed_upload(
    session: AsyncSession, org_id: UUID, conversation_id: UUID, image_id: UUID
) -> str | None:
    """Remove an upload no message carries yet; returns its storage key."""
    return await session.scalar(
        delete(ConversationImage)
        .where(
            ConversationImage.org_id == org_id,
            ConversationImage.conversation_id == conversation_id,
            ConversationImage.id == image_id,
            ConversationImage.origin == "uploaded",
            ConversationImage.turn_id.is_(None),
        )
        .returning(ConversationImage.storage_key)
    )


async def select_vision_images(
    session: AsyncSession,
    org_id: UUID,
    conversation_id: UUID | None,
    model: str,
    image_ids_by_message: Sequence[Sequence[UUID]],
) -> dict[int, list[VisionImage]]:
    """The newest images, per message index, that this request may inline.

    Only images of the conversation bound to the caller's session count; a
    non-vision model, the operator switch being off or a missing row means the
    image is left out.
    """
    if conversation_id is None or not any(image_ids_by_message):
        return {}
    if not await vision_enabled(session, org_id, model):
        return {}
    candidates: list[tuple[int, UUID]] = []
    for index in range(len(image_ids_by_message) - 1, -1, -1):
        for image_id in reversed(_unique(image_ids_by_message[index])):
            candidates.append((index, image_id))
    rows = {
        image_id: VisionImage(storage_key, content_type)
        for image_id, storage_key, content_type in (
            await session.execute(
                select(
                    ConversationImage.id,
                    ConversationImage.storage_key,
                    ConversationImage.content_type,
                ).where(
                    ConversationImage.org_id == org_id,
                    ConversationImage.conversation_id == conversation_id,
                    ConversationImage.id.in_({image_id for _, image_id in candidates}),
                )
            )
        ).all()
    }
    selected: dict[int, list[VisionImage]] = {}
    remaining = MAX_VISION_IMAGES_PER_REQUEST
    for index, image_id in candidates:
        if remaining == 0:
            break
        image = rows.get(image_id)
        if image is None:
            continue
        selected.setdefault(index, []).insert(0, image)
        remaining -= 1
    return selected


async def vision_llm_messages(
    messages: Sequence[tuple[str, str, Sequence[UUID]]],
    selected: dict[int, list[VisionImage]],
    storage: ReadStorage,
) -> list[dict[str, Any]]:
    """Provider messages; a message with images becomes text plus image parts."""
    result: list[dict[str, Any]] = []
    for index, (role, content, image_ids) in enumerate(messages):
        if not image_ids:
            result.append({"role": role, "content": content})
            continue
        parts: list[dict[str, Any]] = []
        for image in selected.get(index, []):
            try:
                data = await storage.get_bytes(image.storage_key)
            except Exception:
                logger.warning("vision image unavailable", exc_info=True)
                continue
            encoded = base64.b64encode(data).decode("ascii")
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{image.content_type};base64,{encoded}"},
                }
            )
        text = content
        if len(parts) < len(_unique(image_ids)):
            text = f"{content}\n\n{OMITTED_IMAGE_NOTE}" if content else OMITTED_IMAGE_NOTE
        if not parts:
            result.append({"role": role, "content": text})
            continue
        # Some providers reject an empty text block, so an image-only message
        # sends just its images.
        text_part = [{"type": "text", "text": text}] if text else []
        result.append({"role": role, "content": [*text_part, *parts]})
    return result
