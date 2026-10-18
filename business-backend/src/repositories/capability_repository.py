from sqlalchemy import select, union
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import AuthorizationSubject
from src.core.capability_catalog import Capability
from src.models.group_capability import GroupCapability
from src.models.organization_capability import OrganizationCapability
from src.models.user_group import UserGroup


class CapabilityRepository:
    async def effective_for_subject(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
    ) -> set[Capability]:
        organization_capabilities = select(
            OrganizationCapability.capability.label("capability")
        ).where(OrganizationCapability.org_id == subject.org_id)
        group_capabilities = (
            select(GroupCapability.capability.label("capability"))
            .join(UserGroup, UserGroup.group_id == GroupCapability.group_id)
            .where(UserGroup.user_id == subject.user_id)
        )
        result = await session.execute(
            union(organization_capabilities, group_capabilities)
        )
        return {Capability(value) for value in result.scalars()}


capability_repository = CapabilityRepository()
