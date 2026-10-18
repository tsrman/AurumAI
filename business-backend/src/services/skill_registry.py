import uuid
from typing import Any, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import AuthorizationSubject
from src.models.skill import Skill
from src.repositories.skill_repository import skill_repository


class SkillRegistry:
    async def list_skills(
        self,
        session: AsyncSession,
        org_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Skill]:
        return await skill_repository.list_by_org(session, org_id, limit, offset)

    async def get_skill(self, session: AsyncSession, skill_id: str) -> Optional[Skill]:
        return await skill_repository.get(session, skill_id)

    async def list_accessible_skills(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        limit: int = 100,
        offset: int = 0,
        *,
        conversation_id: uuid.UUID | None = None,
    ) -> List[Skill]:
        return await skill_repository.list_accessible_by_org(
            session,
            subject,
            limit,
            offset,
            conversation_id=conversation_id,
        )

    async def create_skill(self, session: AsyncSession, **kwargs: Any) -> Skill:
        """Plain create. A global skill (org_id=None) is *not* published to any
        organization here: fanning one out is an operator action that needs the
        cross-tenant organization list RLS hides from the runtime role — see
        `src/cli/provision_org.py` and
        `SkillGrantProvisioningService.publish_global_skill`."""
        return await skill_repository.create(session, **kwargs)

    async def create_owned_skill(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        **kwargs: Any,
    ) -> Skill:
        return await skill_repository.create_with_owner_write_grant(
            session,
            subject,
            **kwargs,
        )

    async def update_skill(self, session: AsyncSession, skill: Skill, **kwargs: Any) -> Skill:
        return await skill_repository.update(session, skill, **kwargs)

    async def delete_skill(self, session: AsyncSession, skill: Skill) -> None:
        await skill_repository.delete(session, skill)

    async def delete_skill_with_grants(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        skill: Skill,
    ) -> None:
        await skill_repository.delete_with_grants(session, skill, subject.org_id)


skill_registry = SkillRegistry()
