"""The migrated database accepts the Hermes Agent capability on both scopes."""

from sqlalchemy import text

from src.core.capability_catalog import Capability
from src.models.organization_capability import OrganizationCapability


async def test_capability_constraints_accept_hermes(db, test_org) -> None:
    db.add(OrganizationCapability(org_id=test_org.id, capability=Capability.AGENTS_HERMES.value))
    await db.flush()
    definitions = (
        await db.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = ANY(:names)"
            ),
            {
                "names": [
                    "ck_organization_capabilities_capability",
                    "ck_group_capabilities_capability",
                ]
            },
        )
    ).scalars().all()
    assert len(definitions) == 2
    assert all("agents.hermes" in definition for definition in definitions)
