from sqlalchemy.ext.asyncio import AsyncSession
from src.core.auth import UserInfo
from src.models.conversation import Conversation
from src.schemas.conversation import ConversationCreate
from src.repositories.conversation_repository import conversation_repository


class ChatService:
    async def create_conversation(
        self,
        session: AsyncSession,
        user: UserInfo,
        data: ConversationCreate,
    ) -> Conversation:
        return await conversation_repository.create(
            session,
            org_id=user.org_id,
            user_id=user.user_id,
            title=data.title,
            model=data.model,
            system_prompt=data.system_prompt,
            extra=data.metadata,
        )


chat_service = ChatService()
