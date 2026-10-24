"""Owner-only access to the images of a conversation."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user
from src.core.dependencies import get_org_db_session
from src.models.conversation import Conversation
from src.models.conversation_image import ConversationImage
from src.services.object_storage import object_storage


router = APIRouter()


@router.get("/conversations/{conversation_id}/images/{image_id}/content")
async def get_image_content(
    conversation_id: UUID,
    image_id: UUID,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> Response:
    org_id = UUID(user.org_id or "")
    owner = await session.scalar(
        select(Conversation.id).where(
            Conversation.org_id == org_id,
            Conversation.id == conversation_id,
            Conversation.user_id == UUID(user.user_id),
            Conversation.status != "deleted",
        )
    )
    image = (
        await session.scalar(
            select(ConversationImage).where(
                ConversationImage.org_id == org_id,
                ConversationImage.conversation_id == conversation_id,
                ConversationImage.id == image_id,
            )
        )
        if owner is not None
        else None
    )
    if image is None:
        # One answer for a foreign, deleted or unknown image: no existence oracle.
        raise HTTPException(status_code=404, detail="Image not found")
    data = await object_storage.get_bytes(image.storage_key)
    return Response(
        content=data,
        media_type=image.content_type,
        headers={
            # Bytes were validated on write; still never let a browser guess.
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, max-age=3600",
            "Content-Disposition": f'inline; filename="image-{image.id}"',
        },
    )
