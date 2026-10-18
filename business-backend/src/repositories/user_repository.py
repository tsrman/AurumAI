from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.models.user import User
from src.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    def __init__(self) -> None:
        super().__init__(User)

    async def get_by_keycloak_id(self, session: AsyncSession, keycloak_id: str) -> User | None:
        result = await session.execute(select(User).where(User.keycloak_id == keycloak_id))
        return result.scalar_one_or_none()


user_repository = UserRepository()
