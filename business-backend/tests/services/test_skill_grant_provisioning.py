import uuid

from sqlalchemy import select

from src.core.auth import UserInfo
from src.models.access_grant import AccessGrant
from src.models.organization import Organization
from src.models.skill import Skill
from src.services.auth_service import auth_service
from src.services.skill_grant_provisioning import skill_grant_provisioning_service


async def _skill_grants(db, org_id: uuid.UUID) -> list[AccessGrant]:
    rows = await db.execute(
        select(AccessGrant).where(
            AccessGrant.org_id == org_id,
            AccessGrant.resource_type == "skill",
        )
    )
    return list(rows.scalars())


async def test_new_organization_gets_only_explicit_global_skill_grants(db) -> None:
    existing_org = Organization(
        name="Existing Global Skills Org",
        slug=f"existing-global-{uuid.uuid4()}",
    )
    db.add(existing_org)
    await db.flush()
    global_skill = Skill(org_id=None, name="published-global", type="tool")
    private_skill = Skill(org_id=existing_org.id, name="private-existing", type="tool")
    db.add_all([global_skill, private_skill])
    await db.commit()
    new_org_id = uuid.uuid4()
    user_id = uuid.uuid4()

    await auth_service.sync_keycloak_user(
        db,
        UserInfo(
            user_id=str(user_id),
            email=f"global-provision-{user_id}@example.com",
            org_id=str(new_org_id),
            org_name="New Global Skills Org",
            role="member",
            raw_claims={"groups": []},
        ),
    )

    grants = await _skill_grants(db, new_org_id)
    assert [
        (
            grant.resource_id,
            grant.principal_type,
            grant.principal_id,
            grant.permission,
            grant.created_via,
        )
        for grant in grants
    ] == [
        (
            str(global_skill.id),
            "organization",
            new_org_id,
            "read",
            "provisioning",
        )
    ]
    assert str(private_skill.id) not in {grant.resource_id for grant in grants}


async def test_publishing_a_global_skill_reaches_exactly_the_named_organizations(
    db,
) -> None:
    """The organization list is a parameter, not a query: under the runtime role
    RLS shows `organizations` only the current tenant. Asserting against the
    caller's own list — rather than every row in the database — is also what
    keeps this test honest on a dev database that already has organizations in
    it, where an assertion over "all organizations" fails for reasons that have
    nothing to do with the code under test."""
    organizations = [
        Organization(name=f"Publication Org {index}", slug=f"publication-{uuid.uuid4()}")
        for index in range(2)
    ]
    bystander = Organization(
        name="Not In The List", slug=f"publication-bystander-{uuid.uuid4()}"
    )
    db.add_all([*organizations, bystander])
    await db.commit()
    skill = Skill(org_id=None, name=f"new-global-publication-{uuid.uuid4()}", type="tool")
    db.add(skill)
    await db.commit()

    published = await skill_grant_provisioning_service.publish_global_skill(
        db, skill, organization_ids=[org.id for org in organizations]
    )
    await db.commit()

    assert set(published) == {org.id for org in organizations}
    rows = await db.execute(
        select(AccessGrant).where(
            AccessGrant.resource_type == "skill",
            AccessGrant.resource_id == str(skill.id),
        )
    )
    assert {
        (
            grant.org_id,
            grant.principal_type,
            grant.principal_id,
            grant.permission,
            grant.created_via,
        )
        for grant in rows.scalars()
    } == {(org.id, "organization", org.id, "read", "provisioning") for org in organizations}


async def test_publishing_the_same_skill_twice_is_a_no_op(db) -> None:
    org = Organization(name="Republish Org", slug=f"republish-{uuid.uuid4()}")
    db.add(org)
    await db.commit()
    skill = Skill(org_id=None, name=f"republished-{uuid.uuid4()}", type="tool")
    db.add(skill)
    await db.commit()

    for _ in range(2):
        await skill_grant_provisioning_service.publish_global_skill(
            db, skill, organization_ids=[org.id]
        )
        await db.commit()

    rows = await db.execute(
        select(AccessGrant).where(
            AccessGrant.resource_type == "skill",
            AccessGrant.resource_id == str(skill.id),
        )
    )
    assert len(list(rows.scalars())) == 1


async def test_an_organization_owned_skill_cannot_be_published(db) -> None:
    org = Organization(name="Owner Org", slug=f"owner-{uuid.uuid4()}")
    db.add(org)
    await db.commit()
    owned = Skill(org_id=org.id, name=f"owned-{uuid.uuid4()}", type="tool")
    db.add(owned)
    await db.commit()

    try:
        await skill_grant_provisioning_service.publish_global_skill(
            db, owned, organization_ids=[org.id]
        )
    except ValueError as exc:
        assert "global" in str(exc)
    else:  # pragma: no cover - the assertion below is the failure message
        raise AssertionError("publishing an org-owned skill to another org must fail")
