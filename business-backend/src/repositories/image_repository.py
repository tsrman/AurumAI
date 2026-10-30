"""Durable image operations and the conversation images they produce."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.conversation_image import ConversationImage
from src.models.interaction import InteractionTurn
from src.repositories.tool_operation_leases import ToolOperationLeaseError, ToolOperationLeases

# One provider call can take minutes for large edits; the lease and the
# operation deadline cover a full call plus storage.
IMAGE_OPERATION_DEADLINE_SECONDS = 300.0


class ImageRepositoryError(ToolOperationLeaseError):
    pass


@dataclass(frozen=True, slots=True)
class StoredImage:
    storage_key: str
    content_type: str
    size_bytes: int
    width: int | None
    height: int | None
    prompt: str
    model: str
    source_image_id: UUID | None


class ImageRepository(ToolOperationLeases):
    deadline_seconds = IMAGE_OPERATION_DEADLINE_SECONDS
    timeout_code = "image_timeout"
    unavailable_code = "image_unavailable"
    error = ImageRepositoryError

    async def complete_operation(
        self,
        session: AsyncSession,
        operation_id: UUID,
        lease_id: UUID,
        conversation_id: UUID,
        image: StoredImage,
    ) -> dict[str, Any]:
        turn, operation = await self._locked_operation(session, operation_id)
        if turn.status != "active":
            raise ImageRepositoryError("interaction_conflict")
        if operation.status == "completed" and operation.result is not None:
            return operation.result
        if operation.status != "running" or operation.lease_id != lease_id:
            raise ImageRepositoryError("operation_in_progress")
        if operation.kind not in {"image_generate", "image_edit"}:
            raise ImageRepositoryError("interaction_conflict")
        now = await self._clock(session)
        if self._remaining_seconds(operation, now) <= 0:
            self._mark_failed(operation, self.timeout_code)
            await session.flush()
            raise ImageRepositoryError(self.timeout_code)
        image_id = uuid4()
        session.add(
            ConversationImage(
                id=image_id,
                org_id=self.org_id,
                conversation_id=conversation_id,
                turn_id=operation.turn_id,
                execution_id=operation.execution_id,
                operation_id=operation.id,
                origin="generated",
                source_image_id=image.source_image_id,
                storage_key=image.storage_key,
                content_type=image.content_type,
                size_bytes=image.size_bytes,
                width=image.width,
                height=image.height,
                prompt=image.prompt,
                model=image.model,
            )
        )
        result = {
            "imageId": str(image_id),
            "contentType": image.content_type,
            "width": image.width,
            "height": image.height,
        }
        operation.status = "completed"
        operation.result = result
        operation.error_code = None
        operation.lease_id = None
        operation.lease_until = None
        await session.flush()
        return result

    async def conversation_image(
        self, session: AsyncSession, conversation_id: UUID, image_id: UUID
    ) -> ConversationImage:
        image = await session.scalar(
            select(ConversationImage).where(
                ConversationImage.org_id == self.org_id,
                ConversationImage.conversation_id == conversation_id,
                ConversationImage.id == image_id,
            )
        )
        if image is None:
            raise ImageRepositoryError("image_not_found")
        return image

    async def owned_image_ids(
        self,
        session: AsyncSession,
        conversation_id: UUID,
        owner_id: UUID,
        turn_id: UUID,
        image_ids: tuple[str, ...],
    ) -> set[str]:
        """Image ids from ``image_ids`` that this owner's turn produced."""
        owned_turn = await session.scalar(
            select(InteractionTurn.id).where(
                InteractionTurn.org_id == self.org_id,
                InteractionTurn.id == turn_id,
                InteractionTurn.conversation_id == conversation_id,
                InteractionTurn.owner_id == owner_id,
            )
        )
        if owned_turn is None:
            raise ImageRepositoryError("interaction_not_found")
        if not image_ids:
            return set()
        rows = await session.scalars(
            select(ConversationImage.id).where(
                ConversationImage.org_id == self.org_id,
                ConversationImage.conversation_id == conversation_id,
                ConversationImage.turn_id == turn_id,
                ConversationImage.id.in_(tuple(UUID(value) for value in image_ids)),
            )
        )
        return {str(image_id) for image_id in rows}

    async def turn_image_ids(
        self, session: AsyncSession, conversation_id: UUID, owner_id: UUID, turn_id: UUID
    ) -> list[str]:
        """Every image this owner's turn produced, oldest first: what an agent
        whose tool results the runner never sees (Hermes) made."""
        rows = await session.scalars(
            select(ConversationImage.id)
            .join(
                InteractionTurn,
                (InteractionTurn.org_id == ConversationImage.org_id)
                & (InteractionTurn.id == ConversationImage.turn_id),
            )
            .where(
                ConversationImage.org_id == self.org_id,
                ConversationImage.conversation_id == conversation_id,
                ConversationImage.turn_id == turn_id,
                # The user's own uploads to the turn belong to their message.
                ConversationImage.origin == "generated",
                InteractionTurn.conversation_id == conversation_id,
                InteractionTurn.owner_id == owner_id,
            )
            .order_by(ConversationImage.created_at, ConversationImage.id)
        )
        return [str(image_id) for image_id in rows]
