import uuid
from typing import List, Optional

from sqlalchemy import String, and_, cast, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import (
    AccessPermission,
    AuthorizationSubject,
    GrantSource,
    PrincipalType,
    ResourceType,
)
from src.models.access_grant import AccessGrant
from src.models.skill import Skill
from src.repositories.base import BaseRepository
from src.services.authorization import authorization_service


class SkillRepository(BaseRepository[Skill]):
    def __init__(self) -> None:
        super().__init__(Skill)

    async def list_by_org(
        self,
        session: AsyncSession,
        org_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Skill]:
        stmt = select(Skill)
        if org_id:
            stmt = stmt.where(Skill.org_id == org_id)
        else:
            stmt = stmt.where(Skill.org_id.is_(None))
        stmt = stmt.order_by(Skill.created_at.desc()).limit(limit).offset(offset)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def list_accessible_by_org(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        limit: int = 100,
        offset: int = 0,
        *,
        conversation_id: uuid.UUID | None = None,
    ) -> List[Skill]:
        access = authorization_service.accessible_resource_predicate(
            subject,
            ResourceType.SKILL,
            AccessPermission.READ,
            cast(Skill.id, String),
            allow_admin_bypass=False,
        )
        scope = (
            or_(
                Skill.org_id == subject.org_id,
                and_(Skill.org_id.is_(None), access),
            )
            if subject.is_org_admin
            else and_(
                or_(Skill.org_id == subject.org_id, Skill.org_id.is_(None)),
                access,
            )
        )
        # A hidden per-conversation skill (chat file attachments) is only
        # ever visible to the one conversation it was created for — passed
        # in by conversation-runner's session token, which can't be spoofed
        # to another conversation_id (get_current_user_or_session rejects a
        # mismatch outright). Every other caller (browser JWTs, no
        # conversation_id) only ever sees the ordinary, unowned skills.
        visibility = or_(
            Skill.owning_conversation_id.is_(None),
            Skill.owning_conversation_id == conversation_id,
        )
        statement = (
            select(Skill)
            .where(scope, visibility)
            .order_by(Skill.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await session.execute(statement)
        return list(result.scalars().all())

    async def create_with_owner_write_grant(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        **kwargs: object,
    ) -> Skill:
        skill = Skill(**kwargs)
        session.add(skill)
        await session.flush()
        session.add(
            AccessGrant(
                org_id=subject.org_id,
                resource_type=ResourceType.SKILL.value,
                resource_id=str(skill.id),
                principal_type=PrincipalType.USER.value,
                principal_id=subject.user_id,
                permission=AccessPermission.WRITE.value,
                created_via=GrantSource.PROVISIONING.value,
            )
        )
        await session.commit()
        await session.refresh(skill)
        return skill

    async def delete_with_grants(
        self,
        session: AsyncSession,
        skill: Skill,
        org_id: object,
    ) -> None:
        await session.execute(
            delete(AccessGrant).where(
                AccessGrant.org_id == org_id,
                AccessGrant.resource_type == ResourceType.SKILL.value,
                AccessGrant.resource_id == str(skill.id),
            )
        )
        await session.delete(skill)
        await session.commit()


skill_repository = SkillRepository()
