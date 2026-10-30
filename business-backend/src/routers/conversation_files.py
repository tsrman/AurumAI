"""Owner-only download of the files an agent attached to a conversation."""

from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user
from src.core.dependencies import get_org_db_session
from src.models.conversation import Conversation
from src.models.conversation_file import ConversationFile
from src.services.object_storage import object_storage

router = APIRouter()


@router.get("/conversations/{conversation_id}/files/{file_id}/content")
async def get_file_content(
    conversation_id: UUID,
    file_id: UUID,
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
    attached = (
        await session.scalar(
            select(ConversationFile).where(
                ConversationFile.org_id == org_id,
                ConversationFile.conversation_id == conversation_id,
                ConversationFile.id == file_id,
            )
        )
        if owner is not None
        else None
    )
    if attached is None:
        # One answer for a foreign, deleted or unknown file: no existence oracle.
        raise HTTPException(status_code=404, detail="File not found")
    data = await object_storage.get_bytes(attached.storage_key)
    return Response(
        content=data,
        media_type=attached.content_type,
        headers={
            # Always a download: the bytes are an agent's, never a page.
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(attached.name, safe='')}",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, max-age=3600",
        },
    )
