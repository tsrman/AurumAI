from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy import and_, delete, or_, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import (
    AccessPermission,
    AuthorizationSubject,
    GrantSource,
    PrincipalType,
    ResourceRef,
    ResourceType,
)
from src.core.capability_catalog import Capability
from src.core.logging import logger
from src.models.access_grant import AccessGrant
from src.models.authorization_audit_event import AuthorizationAuditEvent
from src.models.group import Group
from src.models.group_capability import GroupCapability
from src.models.organization import Organization
from src.models.organization_capability import OrganizationCapability
from src.models.user import User
from src.services.authorization import authorization_service
from src.services.resource_registry import resource_registry


class GrantWriteDenied(Exception):
    pass


class GrantTargetNotFound(Exception):
    pass


@dataclass(frozen=True, slots=True, order=True)
class GrantSpec:
    principal_type: PrincipalType
    principal_id: UUID
    permission: AccessPermission


@dataclass(frozen=True, slots=True)
class GrantAuditEvent:
    actor_id: UUID
    org_id: UUID
    resource: ResourceRef
    action: str
    before: tuple[GrantSpec, ...]
    after: tuple[GrantSpec, ...]
    correlation_id: str


@dataclass(frozen=True, slots=True)
class PreviewResource:
    resource: ResourceRef
    can_read: bool
    can_write: bool
    source: str


@dataclass(frozen=True, slots=True)
class AccessPreview:
    subject_type: PrincipalType
    subject_id: UUID
    admin_bypass: bool
    capabilities: tuple[Capability, ...]
    resources: tuple[PreviewResource, ...]


class ResourceLookup(Protocol):
    async def exists(
        self,
        session: AsyncSession,
        org_id: UUID,
        resource: ResourceRef,
    ) -> bool: ...


class GrantAuditSink(Protocol):
    async def record(
        self, session: AsyncSession, event: GrantAuditEvent
    ) -> None: ...


class StructuredGrantAuditSink:
    async def record(self, session: AsyncSession, event: GrantAuditEvent) -> None:
        session.add(
            AuthorizationAuditEvent(
                org_id=event.org_id,
                actor_id=event.actor_id,
                action=event.action,
                resource_type=event.resource.resource_type.value,
                resource_id=event.resource.resource_id,
                before_state=[_spec_payload(spec) for spec in event.before],
                after_state=[_spec_payload(spec) for spec in event.after],
                correlation_id=event.correlation_id,
            )
        )
        logger.info(
            "authorization_grants_changed",
            action=event.action,
            actor_id=str(event.actor_id),
            org_id=str(event.org_id),
            resource_type=event.resource.resource_type.value,
            resource_id=event.resource.resource_id,
            before=[_spec_payload(spec) for spec in event.before],
            after=[_spec_payload(spec) for spec in event.after],
            correlation_id=event.correlation_id,
        )


def _spec_payload(spec: GrantSpec) -> dict[str, str]:
    return {
        "principal_type": spec.principal_type.value,
        "principal_id": str(spec.principal_id),
        "permission": spec.permission.value,
    }


def _row_spec(row: AccessGrant) -> GrantSpec:
    return GrantSpec(
        PrincipalType(row.principal_type),
        row.principal_id,
        AccessPermission(row.permission),
    )


class GrantAdministrationService:
    def __init__(
        self,
        *,
        resources: ResourceLookup = resource_registry,
        audit: GrantAuditSink | None = None,
    ) -> None:
        self._resources = resources
        self._audit = audit or StructuredGrantAuditSink()

    @staticmethod
    def _require_admin(actor: AuthorizationSubject) -> None:
        if not actor.is_org_admin:
            raise GrantWriteDenied

    async def _validate_resource(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        resource: ResourceRef,
    ) -> None:
        if not await self._resources.exists(session, actor.org_id, resource):
            raise GrantTargetNotFound

    @staticmethod
    async def _validate_principal(
        session: AsyncSession,
        actor: AuthorizationSubject,
        spec: GrantSpec,
    ) -> None:
        if spec.principal_type is PrincipalType.ORGANIZATION:
            statement = select(Organization.id).where(
                Organization.id == spec.principal_id,
                Organization.id == actor.org_id,
            )
        elif spec.principal_type is PrincipalType.GROUP:
            statement = select(Group.id).where(
                Group.id == spec.principal_id,
                Group.org_id == actor.org_id,
            )
        else:
            statement = select(User.id).where(
                User.id == spec.principal_id,
                User.org_id == actor.org_id,
            )
        if (await session.execute(statement)).scalar_one_or_none() is None:
            raise GrantTargetNotFound

    @staticmethod
    async def _rows(
        session: AsyncSession,
        actor: AuthorizationSubject,
        resource: ResourceRef,
    ) -> list[AccessGrant]:
        result = await session.execute(
            select(AccessGrant)
            .where(
                AccessGrant.org_id == actor.org_id,
                AccessGrant.resource_type == resource.resource_type.value,
                AccessGrant.resource_id == resource.resource_id,
            )
            .order_by(
                AccessGrant.principal_type,
                AccessGrant.principal_id,
                AccessGrant.permission,
            )
        )
        return list(result.scalars())

    async def list_for_resource(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        resource: ResourceRef,
    ) -> list[AccessGrant]:
        self._require_admin(actor)
        await self._validate_resource(session, actor, resource)
        return await self._rows(session, actor, resource)

    async def organization_capabilities(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
    ) -> tuple[Capability, ...]:
        self._require_admin(actor)
        result = await session.execute(
            select(OrganizationCapability.capability)
            .where(OrganizationCapability.org_id == actor.org_id)
            .order_by(OrganizationCapability.capability)
        )
        return tuple(Capability(value) for value in result.scalars())

    async def reconcile_organization_capabilities(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        desired: Sequence[Capability],
    ) -> tuple[Capability, ...]:
        self._require_admin(actor)
        current = set(await self.organization_capabilities(session, actor))
        desired_set = set(desired)
        removed = current - desired_set
        if removed:
            await session.execute(
                delete(OrganizationCapability).where(
                    OrganizationCapability.org_id == actor.org_id,
                    OrganizationCapability.capability.in_(
                        capability.value for capability in removed
                    ),
                )
            )
        session.add_all(
            OrganizationCapability(org_id=actor.org_id, capability=capability.value)
            for capability in desired_set - current
        )
        await session.flush()
        return tuple(sorted(desired_set, key=lambda capability: capability.value))

    async def group_capabilities(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        group_id: UUID,
    ) -> tuple[Capability, ...]:
        self._require_admin(actor)
        await self._require_group(session, actor, group_id)
        result = await session.execute(
            select(GroupCapability.capability)
            .where(GroupCapability.group_id == group_id)
            .order_by(GroupCapability.capability)
        )
        return tuple(Capability(value) for value in result.scalars())

    async def reconcile_group_capabilities(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        group_id: UUID,
        desired: Sequence[Capability],
    ) -> tuple[Capability, ...]:
        self._require_admin(actor)
        current = set(await self.group_capabilities(session, actor, group_id))
        desired_set = set(desired)
        removed = current - desired_set
        if removed:
            await session.execute(
                delete(GroupCapability).where(
                    GroupCapability.group_id == group_id,
                    GroupCapability.capability.in_(
                        capability.value for capability in removed
                    ),
                )
            )
        session.add_all(
            GroupCapability(group_id=group_id, capability=capability.value)
            for capability in desired_set - current
        )
        await session.flush()
        return tuple(sorted(desired_set, key=lambda capability: capability.value))

    async def preview_user(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        user_id: UUID,
        resources: Sequence[ResourceRef],
    ) -> AccessPreview:
        self._require_admin(actor)
        result = await session.execute(
            select(User).where(User.id == user_id, User.org_id == actor.org_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise GrantTargetNotFound
        subject = AuthorizationSubject(
            user_id=user.id,
            org_id=user.org_id,
            role=user.role or "member",
        )
        capabilities = await authorization_service.effective_capabilities(session, subject)
        return AccessPreview(
            subject_type=PrincipalType.USER,
            subject_id=user.id,
            admin_bypass=subject.is_org_admin,
            capabilities=tuple(sorted(capabilities, key=lambda item: item.value)),
            resources=await self._preview_user_resources(session, subject, resources),
        )

    async def preview_group(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        group_id: UUID,
        resources: Sequence[ResourceRef],
    ) -> AccessPreview:
        self._require_admin(actor)
        await self._require_group(session, actor, group_id)
        organization_query = select(
            OrganizationCapability.capability.label("capability")
        ).where(OrganizationCapability.org_id == actor.org_id)
        group_query = select(GroupCapability.capability.label("capability")).where(
            GroupCapability.group_id == group_id
        )
        capabilities = {
            Capability(value)
            for value in (
                await session.execute(union(organization_query, group_query))
            ).scalars()
        }
        return AccessPreview(
            subject_type=PrincipalType.GROUP,
            subject_id=group_id,
            admin_bypass=False,
            capabilities=tuple(sorted(capabilities, key=lambda item: item.value)),
            resources=await self._preview_group_resources(
                session, actor.org_id, group_id, resources
            ),
        )

    @staticmethod
    async def _require_group(
        session: AsyncSession,
        actor: AuthorizationSubject,
        group_id: UUID,
    ) -> None:
        result = await session.execute(
            select(Group.id).where(Group.id == group_id, Group.org_id == actor.org_id)
        )
        if result.scalar_one_or_none() is None:
            raise GrantTargetNotFound

    @staticmethod
    async def _preview_user_resources(
        session: AsyncSession,
        subject: AuthorizationSubject,
        resources: Sequence[ResourceRef],
    ) -> tuple[PreviewResource, ...]:
        if subject.is_org_admin:
            return tuple(
                PreviewResource(resource, True, True, "admin_bypass")
                for resource in resources
            )
        output: list[PreviewResource] = []
        for resource_type in ResourceType:
            ids = [
                resource.resource_id
                for resource in resources
                if resource.resource_type is resource_type
            ]
            if not ids:
                continue
            readable = await authorization_service.accessible_resource_ids(
                session, subject, resource_type, AccessPermission.READ, ids
            )
            writable = await authorization_service.accessible_resource_ids(
                session, subject, resource_type, AccessPermission.WRITE, ids
            )
            output.extend(
                PreviewResource(
                    ResourceRef(resource_type, resource_id),
                    resource_id in readable,
                    resource_id in writable,
                    "grant" if resource_id in readable else "none",
                )
                for resource_id in ids
            )
        return tuple(output)

    @staticmethod
    async def _preview_group_resources(
        session: AsyncSession,
        org_id: UUID,
        group_id: UUID,
        resources: Sequence[ResourceRef],
    ) -> tuple[PreviewResource, ...]:
        output: list[PreviewResource] = []
        for resource_type in ResourceType:
            ids = [
                resource.resource_id
                for resource in resources
                if resource.resource_type is resource_type
            ]
            if not ids:
                continue
            result = await session.execute(
                select(AccessGrant.resource_id, AccessGrant.permission).where(
                    AccessGrant.org_id == org_id,
                    AccessGrant.resource_type == resource_type.value,
                    AccessGrant.resource_id.in_(ids),
                    or_(
                        and_(
                            AccessGrant.principal_type
                            == PrincipalType.ORGANIZATION.value,
                            AccessGrant.principal_id == org_id,
                        ),
                        and_(
                            AccessGrant.principal_type == PrincipalType.GROUP.value,
                            AccessGrant.principal_id == group_id,
                        ),
                    ),
                )
            )
            permissions: dict[str, set[AccessPermission]] = {}
            for resource_id, permission in result:
                permissions.setdefault(resource_id, set()).add(
                    AccessPermission(permission)
                )
            output.extend(
                PreviewResource(
                    ResourceRef(resource_type, resource_id),
                    bool(permissions.get(resource_id)),
                    AccessPermission.WRITE in permissions.get(resource_id, set()),
                    "grant" if permissions.get(resource_id) else "none",
                )
                for resource_id in ids
            )
        return tuple(output)

    async def create(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        resource: ResourceRef,
        spec: GrantSpec,
        *,
        correlation_id: str,
    ) -> AccessGrant:
        self._require_admin(actor)
        await self._validate_resource(session, actor, resource)
        await self._validate_principal(session, actor, spec)
        before_rows = await self._rows(session, actor, resource)
        for row in before_rows:
            if _row_spec(row) == spec:
                return row
        row = AccessGrant(
            org_id=actor.org_id,
            resource_type=resource.resource_type.value,
            resource_id=resource.resource_id,
            principal_type=spec.principal_type.value,
            principal_id=spec.principal_id,
            permission=spec.permission.value,
            created_by=actor.user_id,
            created_via=GrantSource.ADMIN.value,
        )
        session.add(row)
        await session.flush()
        await self._record(
            session,
            actor,
            resource,
            before_rows,
            [*before_rows, row],
            correlation_id,
            action="create",
        )
        return row

    async def reconcile(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        resource: ResourceRef,
        desired: Sequence[GrantSpec],
        *,
        correlation_id: str,
    ) -> list[AccessGrant]:
        self._require_admin(actor)
        await self._validate_resource(session, actor, resource)
        desired_set = set(desired)
        for spec in desired_set:
            await self._validate_principal(session, actor, spec)
        before_rows = await self._rows(session, actor, resource)
        current = {_row_spec(row): row for row in before_rows}
        if current.keys() == desired_set:
            return before_rows
        for spec, row in current.items():
            if spec not in desired_set:
                await session.delete(row)
        for spec in desired_set - current.keys():
            session.add(
                AccessGrant(
                    org_id=actor.org_id,
                    resource_type=resource.resource_type.value,
                    resource_id=resource.resource_id,
                    principal_type=spec.principal_type.value,
                    principal_id=spec.principal_id,
                    permission=spec.permission.value,
                    created_by=actor.user_id,
                    created_via=GrantSource.ADMIN.value,
                )
            )
        await session.flush()
        after_rows = await self._rows(session, actor, resource)
        await self._record(
            session,
            actor,
            resource,
            before_rows,
            after_rows,
            correlation_id,
            action="reconcile",
        )
        return after_rows

    async def delete(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        grant_id: UUID,
        *,
        correlation_id: str,
    ) -> None:
        self._require_admin(actor)
        result = await session.execute(
            select(AccessGrant).where(
                AccessGrant.id == grant_id,
                AccessGrant.org_id == actor.org_id,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise GrantTargetNotFound
        resource = ResourceRef(ResourceType(row.resource_type), row.resource_id)
        before_rows = await self._rows(session, actor, resource)
        await session.delete(row)
        await session.flush()
        after_rows = [item for item in before_rows if item.id != row.id]
        await self._record(
            session,
            actor,
            resource,
            before_rows,
            after_rows,
            correlation_id,
            action="delete",
        )

    async def _record(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        resource: ResourceRef,
        before: Sequence[AccessGrant],
        after: Sequence[AccessGrant],
        correlation_id: str,
        *,
        action: str,
    ) -> None:
        await self._audit.record(
            session,
            GrantAuditEvent(
                actor_id=actor.user_id,
                org_id=actor.org_id,
                resource=resource,
                action=action,
                before=tuple(sorted(_row_spec(row) for row in before)),
                after=tuple(sorted(_row_spec(row) for row in after)),
                correlation_id=correlation_id,
            )
        )


grant_administration_service = GrantAdministrationService(resources=resource_registry)
