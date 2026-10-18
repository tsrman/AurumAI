import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import (
    AccessPermission,
    AuthorizationSubject,
    PrincipalType,
    ResourceRef,
    ResourceType,
)
from src.models.access_grant import AccessGrant
from src.models.authorization_audit_event import AuthorizationAuditEvent
from src.models.group import Group
from src.models.organization import Organization
from src.models.user import User
from src.services.grant_administration import (
    GrantAdministrationService,
    GrantAuditEvent,
    GrantSpec,
    GrantTargetNotFound,
    GrantWriteDenied,
)


class ExistingResources:
    def __init__(self, *resources: ResourceRef) -> None:
        self.resources = set(resources)

    async def exists(
        self,
        _session: AsyncSession,
        _org_id: uuid.UUID,
        resource: ResourceRef,
    ) -> bool:
        return resource in self.resources


class AuditRecorder:
    def __init__(self) -> None:
        self.events: list[GrantAuditEvent] = []

    async def record(
        self, _session: AsyncSession, event: GrantAuditEvent
    ) -> None:
        self.events.append(event)


async def _identity(
    db: AsyncSession,
) -> tuple[Organization, AuthorizationSubject, User, Group]:
    org = Organization(name="Grant Org", slug=f"grant-{uuid.uuid4()}")
    db.add(org)
    await db.flush()
    actor = User(
        keycloak_id=uuid.uuid4(),
        email=f"actor-{uuid.uuid4()}@example.com",
        org_id=org.id,
        role="org_admin",
    )
    target = User(
        keycloak_id=uuid.uuid4(),
        email=f"target-{uuid.uuid4()}@example.com",
        org_id=org.id,
        role="member",
    )
    group = Group(org_id=org.id, name="grant-targets")
    db.add_all([actor, target, group])
    await db.flush()
    return (
        org,
        AuthorizationSubject(actor.id, org.id, "org_admin"),
        target,
        group,
    )


async def test_only_org_admin_can_mutate_grants(db: AsyncSession) -> None:
    org, actor, target, _group = await _identity(db)
    resource = ResourceRef(ResourceType.SKILL, str(uuid.uuid4()))
    service = GrantAdministrationService(resources=ExistingResources(resource))

    with pytest.raises(GrantWriteDenied):
        await service.create(
            db,
            AuthorizationSubject(actor.user_id, org.id, "member"),
            resource,
            GrantSpec(PrincipalType.USER, target.id, AccessPermission.READ),
            correlation_id="req_member",
        )


async def test_create_validates_principal_and_records_structured_audit(
    db: AsyncSession,
) -> None:
    org, actor, target, group = await _identity(db)
    resource = ResourceRef(ResourceType.SKILL, str(uuid.uuid4()))
    audit = AuditRecorder()
    service = GrantAdministrationService(
        resources=ExistingResources(resource), audit=audit
    )

    grants = []
    for principal_type, principal_id in (
        (PrincipalType.ORGANIZATION, org.id),
        (PrincipalType.GROUP, group.id),
        (PrincipalType.USER, target.id),
    ):
        grants.append(
            await service.create(
                db,
                actor,
                resource,
                GrantSpec(principal_type, principal_id, AccessPermission.READ),
                correlation_id="req_create",
            )
        )
    await db.commit()

    assert {grant.principal_type for grant in grants} == {
        "organization",
        "group",
        "user",
    }
    assert all(grant.created_by == actor.user_id for grant in grants)
    assert all(grant.created_via == "admin" for grant in grants)
    assert len(audit.events) == 3
    assert audit.events[-1].correlation_id == "req_create"
    assert audit.events[-1].action == "create"
    assert audit.events[0].before == ()
    assert len(audit.events[0].after) == 1
    assert len(audit.events[-1].after) == 3


async def test_cross_org_nonexistent_principal_and_resource_are_hidden(
    db: AsyncSession,
) -> None:
    org, actor, _target, _group = await _identity(db)
    other = Organization(name="Other", slug=f"other-{uuid.uuid4()}")
    db.add(other)
    await db.flush()
    foreign_user = User(
        keycloak_id=uuid.uuid4(),
        email=f"foreign-{uuid.uuid4()}@example.com",
        org_id=other.id,
    )
    db.add(foreign_user)
    await db.flush()
    resource = ResourceRef(ResourceType.MODEL, "gateway/model:v1")
    service = GrantAdministrationService(resources=ExistingResources(resource))

    with pytest.raises(GrantTargetNotFound):
        await service.create(
            db,
            actor,
            resource,
            GrantSpec(PrincipalType.USER, foreign_user.id, AccessPermission.READ),
            correlation_id="req_foreign",
        )
    with pytest.raises(GrantTargetNotFound):
        await service.create(
            db,
            actor,
            ResourceRef(ResourceType.MODEL, "missing"),
            GrantSpec(PrincipalType.ORGANIZATION, org.id, AccessPermission.READ),
            correlation_id="req_missing",
        )


async def test_reconcile_is_idempotent_and_deletes_only_target_resource(
    db: AsyncSession,
) -> None:
    org, actor, target, group = await _identity(db)
    resource = ResourceRef(ResourceType.SKILL, str(uuid.uuid4()))
    other_resource = ResourceRef(ResourceType.SKILL, str(uuid.uuid4()))
    audit = AuditRecorder()
    service = GrantAdministrationService(
        resources=ExistingResources(resource, other_resource), audit=audit
    )
    desired = (
        GrantSpec(PrincipalType.USER, target.id, AccessPermission.WRITE),
        GrantSpec(PrincipalType.GROUP, group.id, AccessPermission.READ),
    )
    await service.create(
        db,
        actor,
        resource,
        GrantSpec(PrincipalType.ORGANIZATION, org.id, AccessPermission.READ),
        correlation_id="req_seed",
    )
    untouched = await service.create(
        db,
        actor,
        other_resource,
        GrantSpec(PrincipalType.USER, target.id, AccessPermission.READ),
        correlation_id="req_seed",
    )
    await db.commit()

    first = await service.reconcile(
        db, actor, resource, desired, correlation_id="req_reconcile"
    )
    await db.commit()
    audit_count = len(audit.events)
    second = await service.reconcile(
        db, actor, resource, desired, correlation_id="req_reconcile_again"
    )
    await db.commit()

    assert {(row.principal_type, row.permission) for row in first} == {
        ("user", "write"),
        ("group", "read"),
    }
    assert {(row.id, row.principal_type, row.permission) for row in second} == {
        (row.id, row.principal_type, row.permission) for row in first
    }
    assert await db.get(AccessGrant, untouched.id) is not None
    stored = (
        await db.execute(
            select(AccessGrant).where(
                AccessGrant.org_id == org.id,
                AccessGrant.resource_id == resource.resource_id,
            )
        )
    ).scalars().all()
    assert len(stored) == 2
    assert len(audit.events) == audit_count
    assert audit.events[-1].before != audit.events[-1].after


async def test_default_audit_sink_is_transactional_and_structured(db: AsyncSession) -> None:
    org, actor, target, _group = await _identity(db)
    resource = ResourceRef(ResourceType.SKILL, str(uuid.uuid4()))
    service = GrantAdministrationService(resources=ExistingResources(resource))

    await service.create(
        db,
        actor,
        resource,
        GrantSpec(PrincipalType.USER, target.id, AccessPermission.READ),
        correlation_id="req_transactional",
    )
    events_before_commit = (
        await db.execute(
            select(AuthorizationAuditEvent).where(
                AuthorizationAuditEvent.org_id == org.id
            )
        )
    ).scalars().all()
    assert len(events_before_commit) == 1
    event = events_before_commit[0]
    assert event.action == "create"
    assert event.correlation_id == "req_transactional"
    assert event.before_state == []
    assert event.after_state == [
        {
            "principal_type": "user",
            "principal_id": str(target.id),
            "permission": "read",
        }
    ]
