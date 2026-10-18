from typing import List, Optional, Tuple
from uuid import UUID
from sqlalchemy import select, desc, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from src.models.conversation import Conversation
from src.models.data_identity import ConversationIdentity
from src.repositories.base import BaseRepository


class ConversationRepository(BaseRepository[Conversation]):
    def __init__(self) -> None:
        super().__init__(Conversation)

    async def get(self, session: AsyncSession, obj_id: str) -> Optional[Conversation]:
        try:
            key = UUID(obj_id)
        except (ValueError, TypeError, AttributeError):
            return None
        timestamp = await session.scalar(select(ConversationIdentity.created_at).where(ConversationIdentity.id == key, ConversationIdentity.lifecycle == "active"))
        if timestamp is None:
            return None
        return await session.get(Conversation, {"id": key, "created_at": timestamp})

    async def list_by_user(
        self,
        session: AsyncSession,
        user_id: str,
        org_id: str,
        limit: int = 20,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Conversation], bool]:
        # Sort by last activity, falling back to creation time for conversations
        # that haven't had a message yet (last_message_at is null until then).
        sort_key = func.coalesce(Conversation.last_message_at, Conversation.created_at)
        stmt = select(Conversation).join(ConversationIdentity, ConversationIdentity.id == Conversation.id).where(
            ConversationIdentity.lifecycle == "active",
            Conversation.user_id == user_id,
            Conversation.org_id == org_id,
            Conversation.status != "deleted",
        )

        if cursor:
            try:
                cursor_conv = await self.get(session, cursor)
            except ValueError:
                cursor_conv = None
            if cursor_conv is not None:
                cursor_sort_key = cursor_conv.last_message_at or cursor_conv.created_at
                stmt = stmt.where(
                    or_(
                        sort_key < cursor_sort_key,
                        and_(sort_key == cursor_sort_key, Conversation.id < cursor_conv.id),
                    )
                )

        stmt = stmt.order_by(desc(sort_key), desc(Conversation.id)).limit(limit + 1)
        result = await session.execute(stmt)
        rows = list(result.scalars().all())
        has_more = len(rows) > limit
        return rows[:limit], has_more


conversation_repository = ConversationRepository()
