from typing import Any, Generic, TypeVar, Type, Optional
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession

ModelT = TypeVar("ModelT")


class BaseRepository(Generic[ModelT]):
    def __init__(self, model: Type[ModelT]):
        self.model = model

    async def get(self, session: AsyncSession, obj_id: str) -> Optional[ModelT]:
        try:
            key = UUID(obj_id)
        except (ValueError, TypeError, AttributeError):
            # Garbage/None ids resolve to "not found" (404 at the router),
            # never a 500 — routers that want a 422 instead parse the UUID
            # themselves before calling this (see chat._get_owned_conversation).
            return None
        return await session.get(self.model, key)

    async def create(self, session: AsyncSession, **kwargs: Any) -> ModelT:
        obj = self.model(**kwargs)
        session.add(obj)
        await session.commit()
        await session.refresh(obj)
        return obj

    async def update(self, session: AsyncSession, obj: ModelT, **kwargs: Any) -> ModelT:
        for key, value in kwargs.items():
            setattr(obj, key, value)
        await session.commit()
        await session.refresh(obj)
        return obj

    async def delete(self, session: AsyncSession, obj: ModelT) -> None:
        await session.delete(obj)
        await session.commit()
