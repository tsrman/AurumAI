import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import text

from src.core.auth import UserInfo, get_current_user
from src.core.dependencies import get_org_db_session
from src.models.organization import Organization
from src.models.organization_settings import OrganizationSettings
from src.models.user import User
from src.routers import settings as settings_router


@pytest_asyncio.fixture
async def client(db, test_org: Organization, test_user: User):
    async def override_get_org_db_session():
        await db.execute(
            text("SELECT set_config('app.current_org_id', :org_id, false)"),
            {"org_id": str(test_org.id)},
        )
        yield db
        await db.execute(text("SELECT set_config('app.current_org_id', '', false)"))

    def override_get_current_user():
        return UserInfo(user_id=str(test_user.id), org_id=str(test_org.id), role="member")

    test_app = FastAPI()
    test_app.include_router(settings_router.router, prefix="/v1/settings")
    test_app.dependency_overrides[get_org_db_session] = override_get_org_db_session
    test_app.dependency_overrides[get_current_user] = override_get_current_user

    async with httpx.AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://test"
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_get_settings_creates_defaults_on_first_access(client):
    response = await client.get("/v1/settings/me")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["theme"]
    assert data["default_model"] is None
    assert data["organization_default_model"] is None

    # second call must return the same row, not create another one
    response2 = await client.get("/v1/settings/me")
    assert response2.json()["data"] == data


@pytest.mark.asyncio
async def test_patch_settings_partial_update(client):
    await client.get("/v1/settings/me")  # ensure row exists

    response = await client.patch(
        "/v1/settings/me", json={"default_model": "gpt-4o", "show_timestamps": True}
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["default_model"] == "gpt-4o"
    assert data["show_timestamps"] is True

    # unset fields must be untouched by the partial update
    response2 = await client.get("/v1/settings/me")
    assert response2.json()["data"]["default_model"] == "gpt-4o"


@pytest.mark.asyncio
async def test_patch_settings_creates_row_if_missing(client):
    response = await client.patch("/v1/settings/me", json={"default_model": "claude-3-5-sonnet"})
    assert response.status_code == 200
    assert response.json()["data"]["default_model"] == "claude-3-5-sonnet"


@pytest.mark.asyncio
async def test_settings_keep_personal_and_organization_defaults_separate(
    client,
    db,
    test_org: Organization,
):
    db.add(
        OrganizationSettings(
            org_id=test_org.id,
            default_model="provider/organization-default",
        )
    )
    await db.commit()

    personal = await client.patch(
        "/v1/settings/me",
        json={"default_model": "provider/personal-default"},
    )
    cleared = await client.patch(
        "/v1/settings/me",
        json={"default_model": None},
    )

    assert personal.status_code == 200
    assert personal.json()["data"]["default_model"] == "provider/personal-default"
    assert (
        personal.json()["data"]["organization_default_model"]
        == "provider/organization-default"
    )
    assert cleared.status_code == 200
    assert cleared.json()["data"]["default_model"] is None
    assert (
        cleared.json()["data"]["organization_default_model"]
        == "provider/organization-default"
    )


@pytest.mark.asyncio
async def test_settings_require_auth():
    test_app = FastAPI()
    test_app.include_router(settings_router.router, prefix="/v1/settings")

    async def override_get_org_db_session():
        yield None

    test_app.dependency_overrides[get_org_db_session] = override_get_org_db_session

    async with httpx.AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/settings/me")
    assert response.status_code == 401
