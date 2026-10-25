"""The migrated database supports image tools: capability, operation kinds,
conversation images under forced RLS, and per-organization image models."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.core.capability_catalog import Capability
from src.models.organization_capability import OrganizationCapability


async def test_capability_constraints_accept_image_generation(db, test_org) -> None:
    db.add(OrganizationCapability(org_id=test_org.id, capability=Capability.IMAGES_GENERATE.value))
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
    assert all("images.generate" in definition for definition in definitions)


async def test_tool_operation_kinds_include_image_tools(db) -> None:
    definition = await db.scalar(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_tool_operation_kind'"
        )
    )
    assert definition is not None
    for kind in ("ask_user", "web_search", "web_read", "image_generate", "image_edit"):
        assert kind in definition


async def test_conversation_images_table_is_org_isolated(db) -> None:
    row = (
        await db.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = 'conversation_images'"
            )
        )
    ).one()
    assert row == (True, True)
    policies = (
        await db.execute(
            text("SELECT policyname FROM pg_policies WHERE tablename = 'conversation_images'")
        )
    ).scalars().all()
    assert policies == ["conversation_images_org_isolation"]


@pytest.mark.parametrize(
    ("origin", "content_type"),
    [
        ("pasted", "image/png"),
        ("generated", "image/svg+xml"),
        # A generated image must point at the tool operation that made it.
        ("generated", "image/png"),
        ("uploaded", "image/gif"),
    ],
)
async def test_conversation_images_reject_invalid_rows(
    db, test_org, test_user, origin, content_type
) -> None:
    org_id = test_org.id
    conversation_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO conversation_identities (id, org_id, user_id) "
            "VALUES (:id, :org, :user)"
        ),
        {"id": conversation_id, "org": org_id, "user": test_user.id},
    )
    await db.flush()
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(
                text(
                    "INSERT INTO conversation_images (id, org_id, conversation_id, origin, "
                    "storage_key, content_type, size_bytes) "
                    "VALUES (:id, :org, :conv, :origin, :key, :type, 1)"
                ),
                {
                    "id": uuid.uuid4(),
                    "org": org_id,
                    "conv": conversation_id,
                    "origin": origin,
                    "key": f"images/{uuid.uuid4()}",
                    "type": content_type,
                },
            )


async def test_uploaded_images_need_no_tool_operation(db, test_org, test_user) -> None:
    org_id = test_org.id
    conversation_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO conversation_identities (id, org_id, user_id) "
            "VALUES (:id, :org, :user)"
        ),
        {"id": conversation_id, "org": org_id, "user": test_user.id},
    )
    await db.execute(
        text(
            "INSERT INTO conversation_images (id, org_id, conversation_id, origin, "
            "storage_key, content_type, size_bytes) "
            "VALUES (:id, :org, :conv, 'uploaded', :key, 'image/webp', 10)"
        ),
        {"id": uuid.uuid4(), "org": org_id, "conv": conversation_id, "key": f"images/{uuid.uuid4()}"},
    )
    await db.flush()


async def test_organization_settings_store_separate_image_models(db) -> None:
    columns = (
        await db.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'organization_settings' "
                "AND column_name IN ('image_generation_model', 'image_edit_model')"
            )
        )
    ).scalars().all()
    assert sorted(columns) == ["image_edit_model", "image_generation_model"]


async def test_vision_models_default_empty_and_are_bounded(db, test_org) -> None:
    from src.models.organization_settings import OrganizationSettings

    await db.execute(
        text("INSERT INTO organization_settings (org_id) VALUES (:org)"), {"org": test_org.id}
    )
    stored = await db.get(OrganizationSettings, test_org.id)
    assert stored is not None and stored.vision_models == []
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(
                text("UPDATE organization_settings SET vision_models = :models WHERE org_id = :org"),
                {"models": [f"m{i}" for i in range(101)], "org": test_org.id},
            )
