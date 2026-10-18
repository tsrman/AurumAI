import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import (
    AccessPermission,
    GrantSource,
    PrincipalType,
    ResourceType,
)
from src.models.access_grant import AccessGrant
from src.models.organization import Organization
from src.models.organization_settings import OrganizationSettings
from src.models.user import User
from src.services.model_catalog import GatewayCatalogSnapshot, GatewayCatalogStatus
from src.services.organization_model_settings import (
    GatewayUnavailable,
    ModelNotFound,
    ModelNotOrganizationReadable,
    OrganizationModelSettingsService,
)


READABLE_MODEL = "provider/chat-model"
PRIVATE_MODEL = "provider/private-model"


def _snapshot(
    status: GatewayCatalogStatus = GatewayCatalogStatus.AVAILABLE,
) -> GatewayCatalogSnapshot:
    return GatewayCatalogSnapshot(
        status,
        (
            {"id": READABLE_MODEL, "object": "model"},
            {"id": PRIVATE_MODEL, "object": "model"},
            {"id": "provider/text-embedding-3-large", "object": "model"},
        ),
    )


def _grant(
    org_id: uuid.UUID,
    resource_id: str,
    principal_type: PrincipalType,
    principal_id: uuid.UUID,
    permission: AccessPermission = AccessPermission.READ,
) -> AccessGrant:
    return AccessGrant(
        org_id=org_id,
        resource_type=ResourceType.MODEL.value,
        resource_id=resource_id,
        principal_type=principal_type.value,
        principal_id=principal_id,
        permission=permission.value,
        created_via=GrantSource.PROVISIONING.value,
    )


@pytest.mark.asyncio
async def test_catalog_marks_only_explicit_organization_grants_readable(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    db.add_all(
        [
            _grant(
                test_org.id,
                READABLE_MODEL,
                PrincipalType.ORGANIZATION,
                test_org.id,
                AccessPermission.WRITE,
            ),
            _grant(
                test_org.id,
                PRIVATE_MODEL,
                PrincipalType.USER,
                test_user.id,
            ),
        ]
    )
    await db.commit()
    service = OrganizationModelSettingsService()

    view = await service.inspect(db, test_org.id, _snapshot())

    assert view.gateway_status is GatewayCatalogStatus.AVAILABLE
    assert view.default_model is None
    assert view.default_available is False
    assert [
        (entry.id, entry.organization_readable, entry.is_default)
        for entry in view.models
    ] == [
        (READABLE_MODEL, True, False),
        (PRIVATE_MODEL, False, False),
    ]


@pytest.mark.asyncio
async def test_default_model_upsert_and_clear_are_atomic(
    db: AsyncSession,
    test_org: Organization,
) -> None:
    db.add(
        _grant(
            test_org.id,
            READABLE_MODEL,
            PrincipalType.ORGANIZATION,
            test_org.id,
        )
    )
    await db.commit()
    service = OrganizationModelSettingsService()

    await service.set_default(db, test_org.id, READABLE_MODEL, _snapshot())
    await service.set_default(db, test_org.id, READABLE_MODEL, _snapshot())
    await db.commit()
    stored = await db.get(OrganizationSettings, test_org.id)
    assert stored is not None
    assert stored.default_model == READABLE_MODEL

    await service.set_default(db, test_org.id, None, None)
    await db.commit()
    await db.refresh(stored)
    assert stored.default_model is None


@pytest.mark.asyncio
async def test_default_rejects_user_or_group_only_access(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    db.add_all(
        [
            _grant(test_org.id, READABLE_MODEL, PrincipalType.USER, test_user.id),
            _grant(test_org.id, PRIVATE_MODEL, PrincipalType.GROUP, uuid.uuid4()),
        ]
    )
    await db.commit()
    service = OrganizationModelSettingsService()

    with pytest.raises(ModelNotOrganizationReadable):
        await service.set_default(db, test_org.id, READABLE_MODEL, _snapshot())
    with pytest.raises(ModelNotOrganizationReadable):
        await service.set_default(db, test_org.id, PRIVATE_MODEL, _snapshot())


@pytest.mark.asyncio
async def test_default_rejects_missing_non_chat_and_unavailable_catalogs(
    db: AsyncSession,
    test_org: Organization,
) -> None:
    service = OrganizationModelSettingsService()

    with pytest.raises(ModelNotFound):
        await service.set_default(db, test_org.id, "provider/missing", _snapshot())
    with pytest.raises(ModelNotFound):
        await service.set_default(
            db,
            test_org.id,
            "provider/text-embedding-3-large",
            _snapshot(),
        )
    with pytest.raises(GatewayUnavailable):
        await service.set_default(
            db,
            test_org.id,
            READABLE_MODEL,
            GatewayCatalogSnapshot(GatewayCatalogStatus.UNAVAILABLE, ()),
        )


@pytest.mark.asyncio
async def test_existing_unavailable_default_is_reported_without_becoming_readable(
    db: AsyncSession,
    test_org: Organization,
) -> None:
    db.add(OrganizationSettings(org_id=test_org.id, default_model=READABLE_MODEL))
    await db.commit()

    view = await OrganizationModelSettingsService().inspect(
        db,
        test_org.id,
        GatewayCatalogSnapshot(GatewayCatalogStatus.UNAVAILABLE, ()),
    )

    assert view.default_model == READABLE_MODEL
    assert view.default_available is False
    assert view.models == ()
