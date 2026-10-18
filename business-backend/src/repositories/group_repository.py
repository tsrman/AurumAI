from uuid import UUID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.models.group import Group
from src.repositories.base import BaseRepository


class GroupRepository(BaseRepository[Group]):
    def __init__(self) -> None:
        super().__init__(Group)

    async def get_by_name_and_org(
        self,
        session: AsyncSession,
        name: str,
        org_id: UUID,
    ) -> Group | None:
        result = await session.execute(
            select(Group).where(Group.name == name, Group.org_id == org_id)
        )
        return result.scalar_one_or_none()

group_repository = GroupRepository()
