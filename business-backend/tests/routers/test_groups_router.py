import uuid
from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import text

from src.core.auth import UserInfo, get_current_user
from src.core.dependencies import get_org_db_session
from src.models.organization import Organization
from src.models.user import User
from src.routers import groups as groups_router


@pytest_asyncio.fixture
async def make_client(db, test_org: Organization, test_user: User):
    async def override_get_org_db_session():
        await db.execute(
            text("SELECT set_config('app.current_org_id', :org_id, false)"),
            {"org_id": str(test_org.id)},
        )
        yield db
        await db.execute(text("SELECT set_config('app.current_org_id', '', false)"))

    def make(role: str = "member"):
        def override_get_current_user():
            return UserInfo(
                user_id=str(test_user.id),
                org_id=str(test_org.id),
                role=role,
            )

        test_app = FastAPI()
        test_app.include_router(groups_router.router, prefix="/v1/admin/groups")
        test_app.dependency_overrides[get_org_db_session] = override_get_org_db_session
        test_app.dependency_overrides[get_current_user] = override_get_current_user
        return test_app

    return make


@pytest_asyncio.fixture
async def member_client(make_client):
    async with httpx.AsyncClient(
        transport=ASGITransport(app=make_client()), base_url="http://test"
    ) as c:
        yield c


@pytest_asyncio.fixture
async def admin_client(make_client):
    async with httpx.AsyncClient(
        transport=ASGITransport(app=make_client("org_admin")), base_url="http://test"
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_list_groups_requires_org_admin(member_client):
    response = await member_client.get("/v1/admin/groups/")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_list_groups_accessible_to_org_admin(admin_client):
    response = await admin_client.get("/v1/admin/groups/")
    assert response.status_code == 200
    assert response.json()["data"] == []


@pytest.mark.asyncio
async def test_get_groups_without_auth_returns_401():
    test_app = FastAPI()
    test_app.include_router(groups_router.router, prefix="/v1/admin/groups")
    async with httpx.AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/admin/groups/")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_create_group_requires_org_admin(member_client):
    response = await member_client.post("/v1/admin/groups/", json={"name": "analysts"})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_create_group_succeeds(admin_client):
    response = await admin_client.post("/v1/admin/groups/", json={"name": "analysts"})
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["name"] == "analysts"
    assert "permissions" not in data
    assert data["lastSyncedAt"] is None
    assert data["keycloakId"] is None


@pytest.mark.asyncio
async def test_create_group_duplicate_name_conflicts(admin_client):
    first = await admin_client.post("/v1/admin/groups/", json={"name": "analysts"})
    assert first.status_code == 201

    second = await admin_client.post("/v1/admin/groups/", json={"name": "analysts"})
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_group_payloads_reject_legacy_permissions(admin_client):
    created = await admin_client.post("/v1/admin/groups/", json={"name": "analysts"})
    group_id = created.json()["data"]["id"]

    create_response = await admin_client.post(
        "/v1/admin/groups/",
        json={"name": "legacy-create", "permissions": ["files:read"]},
    )
    patch_response = await admin_client.patch(
        f"/v1/admin/groups/{group_id}",
        json={"permissions": ["files:write"]},
    )

    assert create_response.status_code == 422
    assert patch_response.status_code == 422


@pytest.mark.asyncio
async def test_update_group_rename_before_sync_succeeds(admin_client):
    created = await admin_client.post("/v1/admin/groups/", json={"name": "old-name"})
    group_id = created.json()["data"]["id"]

    response = await admin_client.patch(f"/v1/admin/groups/{group_id}", json={"name": "new-name"})
    assert response.status_code == 200
    assert response.json()["data"]["name"] == "new-name"


@pytest.mark.asyncio
async def test_update_group_rename_after_sync_is_blocked(admin_client, db, test_org):
    created = await admin_client.post("/v1/admin/groups/", json={"name": "analysts"})
    group_id = created.json()["data"]["id"]

    await db.execute(
        text("UPDATE groups SET last_synced_at = :now WHERE id = :id"),
        {"now": datetime.now(timezone.utc), "id": group_id},
    )
    await db.commit()

    response = await admin_client.patch(f"/v1/admin/groups/{group_id}", json={"name": "renamed"})
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_delete_group_succeeds(admin_client):
    created = await admin_client.post("/v1/admin/groups/", json={"name": "temp"})
    group_id = created.json()["data"]["id"]

    response = await admin_client.delete(f"/v1/admin/groups/{group_id}")
    assert response.status_code == 204

    listing = await admin_client.get("/v1/admin/groups/")
    assert listing.json()["data"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_field", ["last_synced_at", "keycloak_id"])
async def test_delete_keycloak_managed_group_preserves_its_identity(admin_client, db, identity_field):
    from src.models.group import Group

    created = await admin_client.post("/v1/admin/groups/", json={"name": "identity-owned"})
    group_id = created.json()["data"]["id"]
    group = await db.get(Group, uuid.UUID(group_id))
    setattr(group, identity_field, datetime.now(timezone.utc) if identity_field == "last_synced_at" else uuid.uuid4())
    await db.commit()

    response = await admin_client.delete(f"/v1/admin/groups/{group_id}")

    assert response.status_code == 409
    assert response.json()["detail"] == "Manage synchronized groups in Keycloak"
    listing = await admin_client.get("/v1/admin/groups/")
    assert [g["id"] for g in listing.json()["data"]] == [group_id]


@pytest.mark.asyncio
async def test_group_from_other_org_is_not_visible(admin_client, db):
    other_org = Organization(name="Other Org", slug=f"other-{uuid.uuid4().hex[:8]}")
    db.add(other_org)
    await db.flush()
    from src.models.group import Group

    other_group = Group(org_id=other_org.id, name="foreign")
    db.add(other_group)
    await db.commit()
    await db.refresh(other_group)

    patch_response = await admin_client.patch(
        f"/v1/admin/groups/{other_group.id}", json={"name": "hijacked"}
    )
    assert patch_response.status_code == 404

    delete_response = await admin_client.delete(f"/v1/admin/groups/{other_group.id}")
    assert delete_response.status_code == 404

    listing = await admin_client.get("/v1/admin/groups/")
    names = [g["name"] for g in listing.json()["data"]]
    assert "foreign" not in names


def test_legacy_group_catalog_route_is_removed() -> None:
    routes = {
        (route.path, frozenset(route.methods or set()))
        for route in groups_router.router.routes
    }

    assert ("/permission-catalog", frozenset({"GET"})) not in routes
