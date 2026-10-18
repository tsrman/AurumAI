"""The migrated database, not only the Python enum, accepts web.access."""

from sqlalchemy import text

from src.core.capability_catalog import Capability
from src.models.group import Group
from src.models.group_capability import GroupCapability
from src.models.organization_capability import OrganizationCapability


async def test_both_canonical_capability_constraints_include_web_access(db, test_org) -> None:
    group = Group(org_id=test_org.id, name="Web operators")
    db.add(group)
    await db.flush()
    db.add_all(
        [
            OrganizationCapability(
                org_id=test_org.id,
                capability=Capability.WEB_ACCESS.value,
            ),
            GroupCapability(
                group_id=group.id,
                capability=Capability.WEB_ACCESS.value,
            ),
        ]
    )
    await db.flush()

    definitions = dict(
        (
            await db.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) "
                    "FROM pg_constraint WHERE conname = ANY(:names)"
                ),
                {
                    "names": [
                        "ck_organization_capabilities_capability",
                        "ck_group_capabilities_capability",
                    ]
                },
            )
        ).all()
    )
    assert set(definitions) == {
        "ck_organization_capabilities_capability",
        "ck_group_capabilities_capability",
    }
    assert all("web.access" in definition for definition in definitions.values())
