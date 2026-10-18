import uuid

from sqlalchemy import select

from src.core.capability_catalog import Capability
from src.models.access_grant import AccessGrant
from src.models.organization import Organization
from src.models.organization_capability import OrganizationCapability
from src.models.skill import Skill
from src.services.organization_provisioning import (
    DEFAULT_ORGANIZATION_CAPABILITIES,
    OrganizationProvisioningService,
)


def _catalog(*model_ids: str):
    async def catalog() -> list[dict[str, object]]:
        return [{"id": model_id} for model_id in model_ids]

    return catalog


async def _org(db) -> Organization:
    org = Organization(name="Provisioned Org", slug=f"provisioned-{uuid.uuid4()}")
    db.add(org)
    await db.commit()
    return org


async def _grants(db, org_id: uuid.UUID, resource_type: str) -> set[str]:
    rows = await db.execute(
        select(AccessGrant.resource_id).where(
            AccessGrant.org_id == org_id,
            AccessGrant.resource_type == resource_type,
        )
    )
    return set(rows.scalars())


async def test_a_new_organization_can_send_chat_and_nothing_else(db) -> None:
    """Default-deny means the seeded set is the whole product surface a member
    gets on day one — so it is asserted exactly, not just checked for chat.send.
    Anything added here hands every member of every new organization a new
    ability without an admin ever deciding to."""
    org = await _org(db)
    service = OrganizationProvisioningService(model_catalog=_catalog())

    await service.provision(db, org.id)
    await db.commit()

    rows = await db.execute(
        select(OrganizationCapability.capability).where(
            OrganizationCapability.org_id == org.id
        )
    )
    assert set(rows.scalars()) == {Capability.CHAT_SEND.value}
    assert DEFAULT_ORGANIZATION_CAPABILITIES == (Capability.CHAT_SEND,)


async def test_every_chat_model_the_gateway_reports_becomes_organization_readable(
    db,
) -> None:
    org = await _org(db)
    service = OrganizationProvisioningService(
        model_catalog=_catalog("openai/gpt-4o-mini", "openai/text-embedding-3-small")
    )

    result = await service.provision(db, org.id)
    await db.commit()

    # The embedding model is dropped: it is listed by the gateway but rejects
    # /v1/chat/completions, so granting read on it only widens the ACL surface.
    assert result.model_ids == ("openai/gpt-4o-mini",)
    assert await _grants(db, org.id, "model") == {"openai/gpt-4o-mini"}


async def test_an_unreachable_gateway_grants_no_models_rather_than_guessing(db) -> None:
    """The catalog is fail-closed, so "we could not ask" and "there are none"
    look the same here. Seeding a guessed list would grant access to models that
    may not exist; leaving it empty is recoverable by re-running the CLI."""
    org = await _org(db)
    service = OrganizationProvisioningService(model_catalog=_catalog())

    result = await service.provision(db, org.id)
    await db.commit()

    assert result.model_ids == ()
    assert await _grants(db, org.id, "model") == set()


async def test_global_skills_are_granted_to_the_new_organization(db) -> None:
    global_skill = Skill(org_id=None, name=f"global-{uuid.uuid4()}", type="tool")
    db.add(global_skill)
    await db.commit()
    org = await _org(db)
    service = OrganizationProvisioningService(model_catalog=_catalog())

    await service.provision(db, org.id)
    await db.commit()

    assert str(global_skill.id) in await _grants(db, org.id, "skill")


async def test_provisioning_twice_changes_nothing(db) -> None:
    """The CLI re-runs this against organizations that may be fully or partly
    seeded already, so a second pass must not duplicate rows or raise on the
    unique constraint."""
    org = await _org(db)
    service = OrganizationProvisioningService(model_catalog=_catalog("gpt-4o-mini"))

    await service.provision(db, org.id)
    await db.commit()
    await service.provision(db, org.id)
    await db.commit()

    capabilities = await db.execute(
        select(OrganizationCapability.capability).where(
            OrganizationCapability.org_id == org.id
        )
    )
    grants = await db.execute(
        select(AccessGrant.id).where(
            AccessGrant.org_id == org.id, AccessGrant.resource_type == "model"
        )
    )
    assert len(list(capabilities.scalars())) == 1
    assert len(list(grants.scalars())) == 1


async def test_provisioning_never_revokes_what_an_admin_changed(db) -> None:
    """Re-running the CLI on a live organization must be safe: it is additive
    only, so a capability or model an admin deliberately removed stays removed
    for everything except the defaults it is seeding."""
    org = await _org(db)
    service = OrganizationProvisioningService(model_catalog=_catalog("gpt-4o-mini"))
    await service.provision(db, org.id)
    await db.commit()
    db.add(
        OrganizationCapability(
            org_id=org.id, capability=Capability.WORKSPACE_SKILLS_CREATE.value
        )
    )
    await db.commit()

    await service.provision(db, org.id)
    await db.commit()

    rows = await db.execute(
        select(OrganizationCapability.capability).where(
            OrganizationCapability.org_id == org.id
        )
    )
    assert set(rows.scalars()) == {
        Capability.CHAT_SEND.value,
        Capability.WORKSPACE_SKILLS_CREATE.value,
    }
