import uuid

import httpx
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import text

from src.core.auth import UserInfo, get_current_user
from src.core.dependencies import get_org_db_session
from src.models.group import Group
from src.models.organization import Organization
from src.models.skill import Skill
from src.models.user import User
from src.models.user_group import UserGroup
from src.routers import authorization_admin


@pytest_asyncio.fixture
async def authorization_fixture(db, test_org: Organization, test_user: User):
    test_user.role = "org_admin"
    group = Group(org_id=test_org.id, name="authorization-router-group")
    skill = Skill(org_id=test_org.id, name="authorization-router-skill", type="tool")
    db.add_all([group, skill])
    await db.commit()
    await db.refresh(group)
    await db.refresh(skill)

    async def override_session():
        await db.execute(
            text("SELECT set_config('app.current_org_id', :org_id, false)"),
            {"org_id": str(test_org.id)},
        )
        yield db
        await db.execute(text("SELECT set_config('app.current_org_id', '', false)"))

    def make(role: str) -> httpx.AsyncClient:
        def override_user() -> UserInfo:
            return UserInfo(
                user_id=str(test_user.id),
                org_id=str(test_org.id),
                role=role,
            )

        app = FastAPI()
        app.include_router(
            authorization_admin.router,
            prefix="/v1/admin/authorization",
        )
        app.dependency_overrides[get_current_user] = override_user
        app.dependency_overrides[get_org_db_session] = override_session
        return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    return make, test_org, test_user, group, skill


async def test_capability_catalog_is_typed_and_admin_only(authorization_fixture) -> None:
    make, _org, _user, _group, _skill = authorization_fixture
    async with make("member") as member:
        assert (await member.get("/v1/admin/authorization/capabilities")).status_code == 403
    async with make("org_admin") as admin:
        response = await admin.get("/v1/admin/authorization/capabilities")
    assert response.status_code == 200
    assert response.json()["data"][0] == {
        "key": "admin.users.read",
        "label": "View users",
        "description": "View users and their effective access inside the organization.",
    }


async def test_rollout_status_route_is_removed(authorization_fixture) -> None:
    make, _org, _user, _group, _skill = authorization_fixture
    async with make("org_admin") as admin:
        response = await admin.get("/v1/admin/authorization/status")

    assert response.status_code == 404


async def test_subject_lookup_is_same_org_and_includes_all_principal_types(
    authorization_fixture,
    db,
) -> None:
    make, org, actor, group, _skill = authorization_fixture
    member = User(
        keycloak_id=uuid.uuid4(),
        email=f"lookup-{uuid.uuid4()}@example.com",
        full_name="Lookup User",
        org_id=org.id,
        role="member",
    )
    foreign_org = Organization(
        name="Foreign Subject Org", slug=f"foreign-subject-{uuid.uuid4()}"
    )
    db.add_all([member, foreign_org])
    await db.flush()
    foreign_user = User(
        keycloak_id=uuid.uuid4(),
        email=f"foreign-subject-{uuid.uuid4()}@example.com",
        full_name="Foreign User",
        org_id=foreign_org.id,
        role="member",
    )
    foreign_group = Group(org_id=foreign_org.id, name="foreign-subject-group")
    db.add_all([foreign_user, foreign_group])
    await db.commit()

    async with make("org_admin") as admin:
        response = await admin.get("/v1/admin/authorization/subjects")

    assert response.status_code == 200
    subjects = response.json()["data"]
    assert {
        (item["principalType"], item["principalId"], item["label"])
        for item in subjects
    } >= {
        ("organization", str(org.id), org.name),
        ("group", str(group.id), group.name),
        ("user", str(actor.id), actor.email),
        ("user", str(member.id), "Lookup User"),
    }
    assert str(foreign_user.id) not in {item["principalId"] for item in subjects}
    assert str(foreign_group.id) not in {item["principalId"] for item in subjects}


async def test_admin_create_list_reconcile_and_delete_are_idempotent(
    authorization_fixture,
) -> None:
    make, org, user, group, skill = authorization_fixture
    base = "/v1/admin/authorization/grants"
    resource_query = f"resourceType=skill&resourceId={skill.id}"
    user_grant = {
        "resourceType": "skill",
        "resourceId": str(skill.id),
        "principalType": "user",
        "principalId": str(user.id),
        "permission": "read",
    }
    desired = {
        "resourceType": "skill",
        "resourceId": str(skill.id),
        "grants": [
            {
                "principalType": "organization",
                "principalId": str(org.id),
                "permission": "read",
            },
            {
                "principalType": "group",
                "principalId": str(group.id),
                "permission": "write",
            },
        ],
    }
    async with make("org_admin") as admin:
        created = await admin.post(base, json=user_grant, headers={"X-Request-ID": "req-create"})
        assert created.status_code == 201
        assert created.json()["data"]["createdVia"] == "admin"
        assert created.json()["data"]["createdBy"] == str(user.id)

        listing = await admin.get(f"{base}?{resource_query}")
        assert listing.status_code == 200
        assert len(listing.json()["data"]) == 1

        first = await admin.put(
            f"{base}/reconcile", json=desired, headers={"X-Request-ID": "req-reconcile"}
        )
        second = await admin.put(f"{base}/reconcile", json=desired)
        assert first.status_code == second.status_code == 200
        assert first.json()["data"] == second.json()["data"]

        grant_id = first.json()["data"][0]["id"]
        deleted = await admin.delete(f"{base}/{grant_id}")
        assert deleted.status_code == 204


async def test_member_mutation_and_malicious_payloads_fail_closed(
    authorization_fixture,
) -> None:
    make, _org, user, _group, skill = authorization_fixture
    base = "/v1/admin/authorization/grants"
    valid = {
        "resourceType": "skill",
        "resourceId": str(skill.id),
        "principalType": "user",
        "principalId": str(user.id),
        "permission": "read",
    }
    async with make("member") as member:
        assert (await member.post(base, json=valid)).status_code == 403
    async with make("org_admin") as admin:
        wildcard = {**valid, "principalId": "*"}
        anonymous = {**valid, "principalType": "anonymous"}
        arbitrary = {**valid, "resourceType": "database"}
        missing_resource = {**valid, "resourceId": str(uuid.uuid4())}
        assert (await admin.post(base, json=wildcard)).status_code == 422
        assert (await admin.post(base, json=anonymous)).status_code == 422
        assert (await admin.post(base, json=arbitrary)).status_code == 422
        assert (await admin.post(base, json=missing_resource)).status_code == 404


async def test_cross_org_principal_returns_same_404_as_missing(
    authorization_fixture,
    db,
) -> None:
    make, _org, _user, _group, skill = authorization_fixture
    other = Organization(name="Foreign Router Org", slug=f"foreign-{uuid.uuid4()}")
    db.add(other)
    await db.flush()
    foreign = User(
        keycloak_id=uuid.uuid4(),
        email=f"foreign-router-{uuid.uuid4()}@example.com",
        org_id=other.id,
    )
    db.add(foreign)
    await db.commit()
    payload = {
        "resourceType": "skill",
        "resourceId": str(skill.id),
        "principalType": "user",
        "principalId": str(foreign.id),
        "permission": "read",
    }
    async with make("org_admin") as admin:
        foreign_response = await admin.post("/v1/admin/authorization/grants", json=payload)
        missing_response = await admin.post(
            "/v1/admin/authorization/grants",
            json={**payload, "principalId": str(uuid.uuid4())},
        )
    assert foreign_response.status_code == missing_response.status_code == 404


async def test_capability_settings_and_user_group_preview(authorization_fixture, db) -> None:
    make, org, actor, group, skill = authorization_fixture
    member = User(
        keycloak_id=uuid.uuid4(),
        email=f"preview-{uuid.uuid4()}@example.com",
        org_id=org.id,
        role="member",
    )
    db.add(member)
    await db.flush()
    db.add(UserGroup(user_id=member.id, group_id=group.id))
    await db.commit()
    async with make("org_admin") as admin:
        organization_settings = await admin.put(
            "/v1/admin/authorization/capabilities/organization",
            json={"capabilities": ["chat.send"]},
        )
        group_settings = await admin.put(
            f"/v1/admin/authorization/capabilities/groups/{group.id}",
            json={"capabilities": ["workspace.skills.create"]},
        )
        assert organization_settings.json()["data"]["capabilities"] == ["chat.send"]
        assert group_settings.json()["data"]["capabilities"] == [
            "workspace.skills.create"
        ]
        invalid = await admin.put(
            "/v1/admin/authorization/capabilities/organization",
            json={"capabilities": ["wildcard:*"]},
        )
        assert invalid.status_code == 422

        grant = await admin.post(
            "/v1/admin/authorization/grants",
            json={
                "resourceType": "skill",
                "resourceId": str(skill.id),
                "principalType": "group",
                "principalId": str(group.id),
                "permission": "write",
            },
        )
        assert grant.status_code == 201
        body = {"resources": [{"resourceType": "skill", "resourceId": str(skill.id)}]}
        user_preview = await admin.post(
            f"/v1/admin/authorization/preview/users/{member.id}", json=body
        )
        group_preview = await admin.post(
            f"/v1/admin/authorization/preview/groups/{group.id}", json=body
        )
        admin_preview = await admin.post(
            f"/v1/admin/authorization/preview/users/{actor.id}", json=body
        )

    assert user_preview.status_code == group_preview.status_code == 200
    assert user_preview.json()["data"]["capabilities"] == [
        "chat.send",
        "workspace.skills.create",
    ]
    assert user_preview.json()["data"]["resources"][0] == {
        "resourceType": "skill",
        "resourceId": str(skill.id),
        "canRead": True,
        "canWrite": True,
        "source": "grant",
    }
    assert group_preview.json()["data"]["resources"][0]["canWrite"] is True
    assert admin_preview.json()["data"]["adminBypass"] is True
    assert admin_preview.json()["data"]["resources"][0]["source"] == "admin_bypass"
