from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from src.core.authorization_types import (
    AccessPermission,
    AuthorizationSubject,
    ResourceRef,
    ResourceType,
)
from src.core.capability_catalog import Capability
from src.repositories.access_grant_repository import (
    AccessGrantRepository,
    access_grant_repository,
)
from src.repositories.capability_repository import (
    CapabilityRepository,
    capability_repository,
)


@dataclass(frozen=True, slots=True)
class CapabilityAccessDenied(Exception):
    capability: Capability


@dataclass(frozen=True, slots=True)
class ResourceAccessDenied(Exception):
    resource: ResourceRef
    permission: AccessPermission


class AuthorizationService:
    def __init__(
        self,
        *,
        capabilities: CapabilityRepository = capability_repository,
        grants: AccessGrantRepository = access_grant_repository,
    ) -> None:
        self._capabilities = capabilities
        self._grants = grants

    async def effective_capabilities(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
    ) -> set[Capability]:
        if subject.is_org_admin:
            return set(Capability)
        return await self._capabilities.effective_for_subject(session, subject)

    async def has_capability(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        capability: Capability,
    ) -> bool:
        return capability in await self.effective_capabilities(session, subject)

    async def require_capability(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        capability: Capability,
    ) -> None:
        if not await self.has_capability(session, subject, capability):
            raise CapabilityAccessDenied(capability)

    async def can_access(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        resource: ResourceRef,
        permission: AccessPermission,
        *,
        resource_org_id: UUID | None,
    ) -> bool:
        if resource_org_id is not None and resource_org_id != subject.org_id:
            return False
        if subject.is_org_admin and resource_org_id == subject.org_id:
            return True
        return await self._grants.has_access(session, subject, resource, permission)

    async def require_access(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        resource: ResourceRef,
        permission: AccessPermission,
        *,
        resource_org_id: UUID | None,
    ) -> None:
        if not await self.can_access(
            session,
            subject,
            resource,
            permission,
            resource_org_id=resource_org_id,
        ):
            raise ResourceAccessDenied(resource, permission)

    async def accessible_resource_ids(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        resource_type: ResourceType,
        permission: AccessPermission,
        resource_ids: Sequence[str],
    ) -> set[str]:
        if subject.is_org_admin:
            return set(resource_ids)
        return await self._grants.accessible_resource_ids(
            session,
            subject,
            resource_type,
            permission,
            resource_ids,
        )

    def accessible_resource_predicate(
        self,
        subject: AuthorizationSubject,
        resource_type: ResourceType,
        permission: AccessPermission,
        resource_id: ColumnElement[str],
        *,
        allow_admin_bypass: bool = True,
    ) -> ColumnElement[bool]:
        return self._grants.accessible_resource_predicate(
            subject,
            resource_type,
            permission,
            resource_id,
            allow_admin_bypass=allow_admin_bypass,
        )


authorization_service = AuthorizationService()
