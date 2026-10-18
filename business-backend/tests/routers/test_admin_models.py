from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user
from src.core.authorization_types import (
    AccessPermission,
    GrantSource,
    PrincipalType,
    ResourceType,
)
from src.core.dependencies import get_org_db_session
from src.models.access_grant import AccessGrant
from src.models.organization import Organization
from src.models.organization_settings import OrganizationSettings
from src.models.user import User
from src.routers import admin_models
from src.services.model_catalog import GatewayCatalogSnapshot, GatewayCatalogStatus


MODEL_ID = "provider/chat-model"
PRIVATE_MODEL_ID = "provider/private-model"


def _snapshot(
    status: GatewayCatalogStatus = GatewayCatalogStatus.AVAILABLE,
) -> GatewayCatalogSnapshot:
    models: tuple[dict[str, object], ...] = (
        ({"id": MODEL_ID}, {"id": PRIVATE_MODEL_ID})
        if status is not GatewayCatalogStatus.UNAVAILABLE
        else ()
    )
    return GatewayCatalogSnapshot(status, models)


def _app(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
    *,
    role: str = "org_admin",
    authenticated: bool = True,
    snapshot: GatewayCatalogSnapshot | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(admin_models.router, prefix="/v1/admin")

    async def session_override() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_org_db_session] = session_override
    if authenticated:
        app.dependency_overrides[get_current_user] = lambda: UserInfo(
            user_id=str(test_user.id),
            org_id=str(test_org.id),
            role=role,
        )

    resolved = snapshot or _snapshot()

    async def catalog_override() -> GatewayCatalogSnapshot:
        return resolved

    app.dependency_overrides[admin_models.get_gateway_catalog_loader] = lambda: (
        catalog_override
    )
    return app


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _organization_grant(test_org: Organization) -> AccessGrant:
    return AccessGrant(
        org_id=test_org.id,
        resource_type=ResourceType.MODEL.value,
        resource_id=MODEL_ID,
        principal_type=PrincipalType.ORGANIZATION.value,
        principal_id=test_org.id,
        permission=AccessPermission.READ.value,
        created_via=GrantSource.PROVISIONING.value,
    )


@pytest.mark.asyncio
async def test_admin_model_catalog_returns_default_and_organization_access(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    db.add_all(
        [
            _organization_grant(test_org),
            OrganizationSettings(org_id=test_org.id, default_model=MODEL_ID),
        ]
    )
    await db.commit()

    async with await _client(_app(db, test_org, test_user)) as client:
        response = await client.get("/v1/admin/models")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "gatewayStatus": "available",
        "defaultModel": MODEL_ID,
        "defaultAvailable": True,
        "defaultTemperature": None,
        "models": [
            {
                "id": MODEL_ID,
                "organizationReadable": True,
                "isDefault": True,
            },
            {
                "id": PRIVATE_MODEL_ID,
                "organizationReadable": False,
                "isDefault": False,
            },
        ],
    }
    assert response.json()["meta"]["requestId"].startswith("req_")


@pytest.mark.asyncio
async def test_admin_model_routes_reject_member_and_anonymous_callers(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    async with await _client(_app(db, test_org, test_user, role="member")) as member:
        member_get = await member.get("/v1/admin/models")
        member_put = await member.put(
            "/v1/admin/settings/models", json={"defaultModel": None}
        )
    async with await _client(
        _app(db, test_org, test_user, authenticated=False)
    ) as anonymous:
        anonymous_get = await anonymous.get("/v1/admin/models")

    assert member_get.status_code == 403
    assert member_put.status_code == 403
    assert anonymous_get.status_code == 401


@pytest.mark.asyncio
async def test_invalid_org_context_fails_before_opening_org_session(
    db: AsyncSession,
) -> None:
    app = FastAPI()
    app.include_router(admin_models.router, prefix="/v1/admin")
    session_opened = False

    async def session_override() -> AsyncIterator[AsyncSession]:
        nonlocal session_opened
        session_opened = True
        yield db

    app.dependency_overrides[get_current_user] = lambda: UserInfo(
        user_id="11111111-1111-1111-1111-111111111111",
        org_id="malformed-org-id",
        role="org_admin",
    )
    app.dependency_overrides[get_org_db_session] = session_override

    async with await _client(app) as client:
        response = await client.get("/v1/admin/models")

    assert response.status_code == 400
    assert session_opened is False


@pytest.mark.asyncio
async def test_admin_can_set_and_clear_an_organization_readable_default(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    db.add(_organization_grant(test_org))
    await db.commit()

    async with await _client(_app(db, test_org, test_user)) as client:
        set_response = await client.put(
            "/v1/admin/settings/models",
            json={"defaultModel": MODEL_ID, "defaultTemperature": 0.9},
        )
        clear_response = await client.put(
            "/v1/admin/settings/models",
            json={"defaultModel": None, "defaultTemperature": None},
        )

    assert set_response.status_code == 200
    assert set_response.json()["data"] == {
        "defaultModel": MODEL_ID,
        "defaultTemperature": 0.9,
    }
    assert clear_response.status_code == 200
    assert clear_response.json()["data"] == {
        "defaultModel": None,
        "defaultTemperature": None,
    }
    stored = await db.get(OrganizationSettings, test_org.id)
    assert stored is not None
    assert stored.default_model is None
    assert stored.default_temperature is None


@pytest.mark.asyncio
async def test_admin_default_rejects_unknown_and_ungranted_models(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    async with await _client(_app(db, test_org, test_user)) as client:
        unknown = await client.put(
            "/v1/admin/settings/models",
            json={"defaultModel": "provider/missing", "defaultTemperature": None},
        )
        ungranted = await client.put(
            "/v1/admin/settings/models",
            json={"defaultModel": PRIVATE_MODEL_ID, "defaultTemperature": None},
        )

    assert unknown.status_code == 422
    assert ungranted.status_code == 409


@pytest.mark.asyncio
async def test_gateway_outage_blocks_non_null_default_but_allows_clear(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    unavailable = _snapshot(GatewayCatalogStatus.UNAVAILABLE)
    async with await _client(
        _app(db, test_org, test_user, snapshot=unavailable)
    ) as client:
        get_response = await client.get("/v1/admin/models")
        set_response = await client.put(
            "/v1/admin/settings/models",
            json={"defaultModel": MODEL_ID, "defaultTemperature": None},
        )
        clear_response = await client.put(
            "/v1/admin/settings/models",
            json={"defaultModel": None, "defaultTemperature": None},
        )

    assert get_response.status_code == 200
    assert get_response.json()["data"]["gatewayStatus"] == "unavailable"
    assert get_response.json()["data"]["models"] == []
    assert set_response.status_code == 503
    assert clear_response.status_code == 200


@pytest.mark.asyncio
async def test_admin_model_payload_rejects_noncanonical_and_extra_fields(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    async with await _client(_app(db, test_org, test_user)) as client:
        noncanonical = await client.put(
            "/v1/admin/settings/models", json={"defaultModel": " leading-space"}
        )
        extra = await client.put(
            "/v1/admin/settings/models",
            json={"defaultModel": None, "allowedModels": [MODEL_ID]},
        )

    assert noncanonical.status_code == 422
    assert extra.status_code == 422
