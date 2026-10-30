"""Files attached to a conversation's answers (see services/conversation_files)."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.conversation_file import ConversationFile
from src.models.interaction import InteractionTurn


async def turn_files(
    session: AsyncSession, org_id: UUID, conversation_id: UUID, owner_id: UUID, turn_id: UUID
) -> list[dict[str, Any]]:
    """The files this owner's turn attached, oldest first, as an answer shows them."""
    rows = await session.scalars(
        select(ConversationFile)
        .join(
            InteractionTurn,
            (InteractionTurn.org_id == ConversationFile.org_id)
            & (InteractionTurn.id == ConversationFile.turn_id),
        )
        .where(
            ConversationFile.org_id == org_id,
            ConversationFile.conversation_id == conversation_id,
            ConversationFile.turn_id == turn_id,
            InteractionTurn.conversation_id == conversation_id,
            InteractionTurn.owner_id == owner_id,
        )
        .order_by(ConversationFile.created_at, ConversationFile.id)
    )
    return [
        {
            "fileId": str(row.id),
            "name": row.name,
            "contentType": row.content_type,
            "sizeBytes": row.size_bytes,
        }
        for row in rows
    ]
