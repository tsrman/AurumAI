from collections.abc import Sequence

from sqlalchemy import and_, exists, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from src.core.authorization_types import (
    AccessPermission,
    AuthorizationSubject,
    PrincipalType,
    ResourceRef,
    ResourceType,
)
from src.models.access_grant import AccessGrant
from src.models.user_group import UserGroup


class AccessGrantRepository:
    @staticmethod
    def _principal_clause(subject: AuthorizationSubject) -> ColumnElement[bool]:
        group_ids = select(UserGroup.group_id).where(
            UserGroup.user_id == subject.user_id
        )
        return or_(
            and_(
                AccessGrant.principal_type == PrincipalType.ORGANIZATION.value,
                AccessGrant.principal_id == subject.org_id,
            ),
            and_(
                AccessGrant.principal_type == PrincipalType.USER.value,
                AccessGrant.principal_id == subject.user_id,
            ),
            and_(
                AccessGrant.principal_type == PrincipalType.GROUP.value,
                AccessGrant.principal_id.in_(group_ids),
            ),
        )

    @staticmethod
    def _permission_values(required: AccessPermission) -> tuple[str, ...]:
        if required is AccessPermission.READ:
            return (AccessPermission.READ.value, AccessPermission.WRITE.value)
        return (AccessPermission.WRITE.value,)

    async def has_access(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        resource: ResourceRef,
        required: AccessPermission,
    ) -> bool:
        statement = select(AccessGrant.id).where(
            AccessGrant.org_id == subject.org_id,
            AccessGrant.resource_type == resource.resource_type.value,
            AccessGrant.resource_id == resource.resource_id,
            AccessGrant.permission.in_(self._permission_values(required)),
            self._principal_clause(subject),
        ).limit(1)
        return (await session.execute(statement)).scalar_one_or_none() is not None

    async def accessible_resource_ids(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        resource_type: ResourceType,
        required: AccessPermission,
        resource_ids: Sequence[str],
    ) -> set[str]:
        if not resource_ids:
            return set()
        statement = (
            select(AccessGrant.resource_id)
            .where(
                AccessGrant.org_id == subject.org_id,
                AccessGrant.resource_type == resource_type.value,
                AccessGrant.resource_id.in_(resource_ids),
                AccessGrant.permission.in_(self._permission_values(required)),
                self._principal_clause(subject),
            )
            .distinct()
        )
        result = await session.execute(statement)
        return set(result.scalars())

    def accessible_resource_predicate(
        self,
        subject: AuthorizationSubject,
        resource_type: ResourceType,
        required: AccessPermission,
        resource_id: ColumnElement[str],
        *,
        allow_admin_bypass: bool = True,
    ) -> ColumnElement[bool]:
        if subject.is_org_admin and allow_admin_bypass:
            return true()
        return exists(
            select(AccessGrant.id).where(
                AccessGrant.org_id == subject.org_id,
                AccessGrant.resource_type == resource_type.value,
                AccessGrant.resource_id == resource_id,
                AccessGrant.permission.in_(self._permission_values(required)),
                self._principal_clause(subject),
            )
        )


access_grant_repository = AccessGrantRepository()
