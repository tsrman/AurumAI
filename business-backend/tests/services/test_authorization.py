import uuid
import pytest
from sqlalchemy import delete, event
from sqlalchemy.engine import Connection
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
from src.models.access_grant import AccessGrant
from src.models.group import Group
from src.models.group_capability import GroupCapability
from src.models.organization import Organization
from src.models.organization_capability import OrganizationCapability
from src.models.user import User
from src.models.user_group import UserGroup
from src.services.authorization import (
    AuthorizationService,
    CapabilityAccessDenied,
    ResourceAccessDenied,
)


async def _create_identity(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    role: str = "member",
) -> AuthorizationSubject:
    user_id = uuid.uuid4()
    db.add(
        User(
            id=user_id,
            keycloak_id=uuid.uuid4(),
            email=f"authorization-{user_id}@example.com",
            org_id=org_id,
            role=role,
        )
    )
    await db.flush()
    return AuthorizationSubject(user_id=user_id, org_id=org_id, role=role)


def _grant(
    *,
    org_id: uuid.UUID,
    resource_id: str,
    principal_type: PrincipalType,
    principal_id: uuid.UUID,
    permission: AccessPermission = AccessPermission.READ,
) -> AccessGrant:
    return AccessGrant(
        org_id=org_id,
        resource_type=ResourceType.SKILL.value,
        resource_id=resource_id,
        principal_type=principal_type.value,
        principal_id=principal_id,
        permission=permission.value,
        created_via=GrantSource.PROVISIONING.value,
    )


async def test_effective_capabilities_are_live_union_of_org_and_current_groups(
    db: AsyncSession,
) -> None:
    org_id = uuid.uuid4()
    db.add(Organization(id=org_id, name="Authorization Org", slug=f"auth-{org_id}"))
    await db.flush()
    subject = await _create_identity(db, org_id=org_id)
    group = Group(org_id=org_id, name="authorization-editors")
    db.add(group)
    await db.flush()
    db.add_all(
        [
            OrganizationCapability(org_id=org_id, capability=Capability.CHAT_SEND.value),
            GroupCapability(
                group_id=group.id,
                capability=Capability.WORKSPACE_SKILLS_CREATE.value,
            ),
            UserGroup(user_id=subject.user_id, group_id=group.id),
        ]
    )
    await db.commit()

    service = AuthorizationService()
    assert await service.effective_capabilities(db, subject) == {
        Capability.CHAT_SEND,
        Capability.WORKSPACE_SKILLS_CREATE,
    }

    await db.execute(
        delete(UserGroup).where(
            UserGroup.user_id == subject.user_id,
            UserGroup.group_id == group.id,
        )
    )
    await db.commit()

    assert await service.effective_capabilities(db, subject) == {Capability.CHAT_SEND}


async def test_org_admin_gets_all_capabilities_only_inside_its_subject_context(
    db: AsyncSession,
) -> None:
    org_id = uuid.uuid4()
    db.add(Organization(id=org_id, name="Admin Org", slug=f"admin-{org_id}"))
    await db.flush()
    admin = await _create_identity(db, org_id=org_id, role="org_admin")

    assert await AuthorizationService().effective_capabilities(db, admin) == set(Capability)


async def test_resource_decision_table_covers_org_user_group_write_and_admin(
    db: AsyncSession,
) -> None:
    org_id = uuid.uuid4()
    other_org_id = uuid.uuid4()
    db.add_all(
        [
            Organization(id=org_id, name="Decision Org", slug=f"decision-{org_id}"),
            Organization(id=other_org_id, name="Other Org", slug=f"other-{other_org_id}"),
        ]
    )
    await db.flush()
    direct_user = await _create_identity(db, org_id=org_id)
    group_user = await _create_identity(db, org_id=org_id)
    no_grant_user = await _create_identity(db, org_id=org_id)
    admin = await _create_identity(db, org_id=org_id, role="org_admin")
    group = Group(org_id=org_id, name="decision-group")
    db.add(group)
    await db.flush()
    db.add(UserGroup(user_id=group_user.user_id, group_id=group.id))
    db.add_all(
        [
            _grant(
                org_id=org_id,
                resource_id="org-skill",
                principal_type=PrincipalType.ORGANIZATION,
                principal_id=org_id,
            ),
            _grant(
                org_id=org_id,
                resource_id="user-skill",
                principal_type=PrincipalType.USER,
                principal_id=direct_user.user_id,
            ),
            _grant(
                org_id=org_id,
                resource_id="group-skill",
                principal_type=PrincipalType.GROUP,
                principal_id=group.id,
            ),
            _grant(
                org_id=org_id,
                resource_id="write-skill",
                principal_type=PrincipalType.GROUP,
                principal_id=group.id,
                permission=AccessPermission.WRITE,
            ),
        ]
    )
    await db.commit()

    service = AuthorizationService()
    cases = [
        (direct_user, "org-skill", AccessPermission.READ, org_id, True),
        (direct_user, "user-skill", AccessPermission.READ, org_id, True),
        (group_user, "group-skill", AccessPermission.READ, org_id, True),
        (group_user, "write-skill", AccessPermission.READ, org_id, True),
        (group_user, "write-skill", AccessPermission.WRITE, org_id, True),
        (group_user, "group-skill", AccessPermission.WRITE, org_id, False),
        (no_grant_user, "user-skill", AccessPermission.READ, org_id, False),
        (admin, "private-skill", AccessPermission.WRITE, org_id, True),
        (admin, "private-skill", AccessPermission.READ, other_org_id, False),
    ]

    for subject, resource_id, permission, resource_org_id, expected in cases:
        actual = await service.can_access(
            db,
            subject,
            ResourceRef(ResourceType.SKILL, resource_id),
            permission,
            resource_org_id=resource_org_id,
        )
        assert actual is expected, (subject, resource_id, permission)


async def test_require_methods_raise_domain_errors_not_http_exceptions(
    db: AsyncSession,
) -> None:
    org_id = uuid.uuid4()
    db.add(Organization(id=org_id, name="Denied Org", slug=f"denied-{org_id}"))
    await db.flush()
    subject = await _create_identity(db, org_id=org_id)
    await db.commit()
    service = AuthorizationService()

    with pytest.raises(CapabilityAccessDenied):
        await service.require_capability(db, subject, Capability.SHARING_MANAGE)
    with pytest.raises(ResourceAccessDenied):
        await service.require_access(
            db,
            subject,
            ResourceRef(ResourceType.SKILL, "private-skill"),
            AccessPermission.READ,
            resource_org_id=org_id,
        )


async def test_batch_accessible_ids_uses_one_statement_for_one_hundred_resources(
    db: AsyncSession,
) -> None:
    org_id = uuid.uuid4()
    db.add(Organization(id=org_id, name="Batch Org", slug=f"batch-{org_id}"))
    await db.flush()
    subject = await _create_identity(db, org_id=org_id)
    resource_ids = [f"model-{index}" for index in range(100)]
    db.add_all(
        [
            AccessGrant(
                org_id=org_id,
                resource_type=ResourceType.MODEL.value,
                resource_id=resource_id,
                principal_type=PrincipalType.USER.value,
                principal_id=subject.user_id,
                permission=AccessPermission.READ.value,
                created_via=GrantSource.PROVISIONING.value,
            )
            for resource_id in resource_ids[::2]
        ]
    )
    await db.commit()

    statements = 0

    def count_statement(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal statements
        if "access_grants" in statement.lower():
            statements += 1

    bind = db.get_bind()
    event.listen(bind, "before_cursor_execute", count_statement)
    try:
        allowed = await AuthorizationService().accessible_resource_ids(
            db,
            subject,
            ResourceType.MODEL,
            AccessPermission.READ,
            resource_ids,
        )
    finally:
        event.remove(bind, "before_cursor_execute", count_statement)

    assert allowed == set(resource_ids[::2])
    assert statements == 1
