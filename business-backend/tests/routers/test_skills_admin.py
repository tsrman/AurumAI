from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import delete, select, text

from src.core.auth import UserInfo, get_current_user, get_current_user_or_session
from src.core.authorization_types import AccessPermission, GrantSource, PrincipalType
from src.core.capability_catalog import Capability
from src.core.dependencies import get_org_db_session, get_service_db_session
from src.models.access_grant import AccessGrant
from src.models.organization import Organization
from src.models.organization_capability import OrganizationCapability
from src.models.skill import Skill
from src.models.user import User
from src.routers import skills as skills_router, skills_admin as skills_admin_router
from src.services.mcp_discovery import McpDiscoveryError


@pytest_asyncio.fixture
async def make_client(db, test_org: Organization, test_user: User):
    async def override_get_org_db_session():
        await db.execute(
            text("SELECT set_config('app.current_org_id', :org_id, false)"),
            {"org_id": str(test_org.id)},
        )
        yield db
        await db.execute(text("SELECT set_config('app.current_org_id', '', false)"))

    def make(raw_claims: dict | None = None, *, role: str = "member"):
        def override_get_current_user():
            return UserInfo(
                user_id=str(test_user.id),
                org_id=str(test_org.id),
                role=role,
                raw_claims=raw_claims or {},
            )

        test_app = FastAPI()
        # Same pairing as production's main_admin.py: skills_admin's write
        # endpoints alongside the read-only skills router (create needs a
        # GET to assert round-trips against).
        test_app.include_router(skills_router.router, prefix="/v1/skills")
        test_app.include_router(skills_admin_router.router, prefix="/v1/skills")
        test_app.dependency_overrides[get_org_db_session] = override_get_org_db_session
        test_app.dependency_overrides[get_service_db_session] = override_get_org_db_session
        test_app.dependency_overrides[get_current_user] = override_get_current_user
        test_app.dependency_overrides[get_current_user_or_session] = override_get_current_user
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
        transport=ASGITransport(app=make_client(role="org_admin")), base_url="http://test"
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_create_skill_requires_create_capability(member_client):
    response = await member_client.post(
        "/v1/skills/", json={"name": "web_search", "type": "tool"}
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_create_skill_as_org_admin_succeeds(admin_client):
    response = await admin_client.post(
        "/v1/skills/", json={"name": "web_search", "type": "tool"}
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["name"] == "web_search"
    assert data["type"] == "tool"
    assert "permissions" not in data


@pytest.mark.asyncio
async def test_skill_payloads_reject_legacy_permissions(admin_client):
    created = await admin_client.post(
        "/v1/skills/",
        json={"name": "legacy-create", "type": "tool", "permissions": ["chat:write"]},
    )
    valid = await admin_client.post(
        "/v1/skills/",
        json={"name": "valid-skill", "type": "tool"},
    )
    patched = await admin_client.patch(
        f"/v1/skills/{valid.json()['data']['id']}",
        json={"permissions": ["chat:write"]},
    )

    assert created.status_code == 422
    assert patched.status_code == 422


@pytest.mark.asyncio
async def test_v2_create_requires_capability_and_seeds_private_creator_write_grant(
    make_client,
    db,
    test_org,
    test_user,
):
    app = make_client()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.post(
            "/v1/skills/", json={"name": "v2-capability-denied", "type": "tool"}
        )

        db.add(
            OrganizationCapability(
                org_id=test_org.id,
                capability=Capability.WORKSPACE_SKILLS_CREATE.value,
            )
        )
        await db.commit()
        created = await client.post(
            "/v1/skills/", json={"name": "v2-capability-created", "type": "tool"}
        )

    assert denied.status_code == 403
    assert created.status_code == 201
    skill_id = created.json()["data"]["id"]
    grants = (
        await db.execute(
            select(AccessGrant).where(
                AccessGrant.org_id == test_org.id,
                AccessGrant.resource_type == "skill",
                AccessGrant.resource_id == skill_id,
            )
        )
    ).scalars().all()
    assert [
        (
            grant.principal_type,
            grant.principal_id,
            grant.permission,
            grant.created_via,
        )
        for grant in grants
    ] == [
        (
            PrincipalType.USER.value,
            test_user.id,
            AccessPermission.WRITE.value,
            GrantSource.PROVISIONING.value,
        )
    ]


@pytest.mark.asyncio
async def test_v2_update_requires_write_not_read_grant(
    admin_client,
    make_client,
    db,
    test_org,
    test_user,
):
    created = await admin_client.post(
        "/v1/skills/", json={"name": "v2-read-only", "type": "tool"}
    )
    skill_id = created.json()["data"]["id"]
    await db.execute(
        delete(AccessGrant).where(
            AccessGrant.org_id == test_org.id,
            AccessGrant.resource_type == "skill",
            AccessGrant.resource_id == skill_id,
            AccessGrant.principal_type == PrincipalType.USER.value,
            AccessGrant.principal_id == test_user.id,
        )
    )
    read_grant = AccessGrant(
        org_id=test_org.id,
        resource_type="skill",
        resource_id=skill_id,
        principal_type=PrincipalType.USER.value,
        principal_id=test_user.id,
        permission=AccessPermission.READ.value,
        created_via=GrantSource.PROVISIONING.value,
    )
    db.add(read_grant)
    await db.commit()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=make_client()), base_url="http://test"
    ) as client:
        read_only = await client.patch(
            f"/v1/skills/{skill_id}", json={"name": "must-not-change"}
        )
        await db.execute(delete(AccessGrant).where(AccessGrant.id == read_grant.id))
        db.add(
            AccessGrant(
                org_id=test_org.id,
                resource_type="skill",
                resource_id=skill_id,
                principal_type=PrincipalType.USER.value,
                principal_id=test_user.id,
                permission=AccessPermission.WRITE.value,
                created_via=GrantSource.PROVISIONING.value,
            )
        )
        await db.commit()
        writable = await client.patch(
            f"/v1/skills/{skill_id}", json={"name": "v2-written"}
        )

    assert read_only.status_code == 403
    assert writable.status_code == 200
    assert writable.json()["data"]["name"] == "v2-written"


@pytest.mark.asyncio
async def test_v2_delete_removes_all_skill_grants(
    admin_client,
    make_client,
    db,
    test_org,
    test_user,
):
    created = await admin_client.post(
        "/v1/skills/", json={"name": "v2-delete-grants", "type": "tool"}
    )
    skill_id = created.json()["data"]["id"]
    db.add(
        AccessGrant(
            org_id=test_org.id,
            resource_type="skill",
            resource_id=skill_id,
            principal_type=PrincipalType.ORGANIZATION.value,
            principal_id=test_org.id,
            permission=AccessPermission.READ.value,
            created_via=GrantSource.PROVISIONING.value,
        )
    )
    await db.commit()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=make_client()), base_url="http://test"
    ) as client:
        response = await client.delete(f"/v1/skills/{skill_id}")

    assert response.status_code == 204
    assert (
        await db.execute(
            select(AccessGrant).where(
                AccessGrant.org_id == test_org.id,
                AccessGrant.resource_type == "skill",
                AccessGrant.resource_id == skill_id,
            )
        )
    ).scalars().all() == []


@pytest.mark.asyncio
async def test_v2_global_skill_is_read_only_even_with_tenant_write_grant(
    make_client,
    db,
    test_org,
):
    global_skill = Skill(org_id=None, name="global-operator-owned", type="tool")
    db.add(global_skill)
    await db.flush()
    db.add(
        AccessGrant(
            org_id=test_org.id,
            resource_type="skill",
            resource_id=str(global_skill.id),
            principal_type=PrincipalType.ORGANIZATION.value,
            principal_id=test_org.id,
            permission=AccessPermission.WRITE.value,
            created_via=GrantSource.PROVISIONING.value,
        )
    )
    await db.commit()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=make_client(role="org_admin")),
        base_url="http://test",
    ) as client:
        patched = await client.patch(
            f"/v1/skills/{global_skill.id}", json={"name": "tenant-overwrite"}
        )
        deleted = await client.delete(f"/v1/skills/{global_skill.id}")

    assert patched.status_code == 403
    assert deleted.status_code == 403
    assert await db.get(Skill, global_skill.id) is not None


@pytest.mark.asyncio
async def test_create_skill_with_description_round_trips(admin_client):
    response = await admin_client.post(
        "/v1/skills/",
        json={
            "name": "fetch_url",
            "description": "Fetches a URL and returns its content",
            "type": "mcp",
            "mcp_transport": "sse",
            "mcp_endpoint": "http://mcp-fetch:8080/sse",
            "activity_name": "fetch",
        },
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["description"] == "Fetches a URL and returns its content"
    assert data["mcpTransport"] == "sse"
    assert data["mcpEndpoint"] == "http://mcp-fetch:8080/sse"

    get_response = await admin_client.get(f"/v1/skills/{data['id']}")
    assert get_response.json()["data"]["description"] == "Fetches a URL and returns its content"


@pytest.mark.asyncio
async def test_create_skill_stores_mcp_auth_token_but_never_echoes_it(admin_client, db):
    """mcp_auth_token is write-only for browser clients: it must be persisted
    (tool-worker reads it back with an internal session token — see
    test_skills.py::test_mcp_auth_token_returned_to_internal_session_tokens),
    but never come back in responses to JWT users, admin or not."""
    response = await admin_client.post(
        "/v1/skills/",
        json={
            "name": "fetch_url",
            "type": "mcp",
            "mcp_transport": "sse",
            "mcp_endpoint": "http://mcp-fetch:8080/sse",
            "mcp_auth_token": "secret-token",
        },
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["mcpAuthToken"] is None

    get_response = await admin_client.get(f"/v1/skills/{data['id']}")
    assert get_response.json()["data"]["mcpAuthToken"] is None

    from src.models.skill import Skill
    import uuid

    stored = await db.get(Skill, uuid.UUID(data["id"]))
    assert stored.mcp_auth_token == "secret-token"


@pytest.mark.asyncio
async def test_has_mcp_auth_token_reflects_whether_a_token_is_stored(admin_client):
    """mcpAuthToken is always null to browsers, so
    hasMcpAuthToken is the only way the UI can tell "a token is saved" apart
    from "no token was ever set"."""
    with_token = await admin_client.post(
        "/v1/skills/",
        json={"name": "with-token", "type": "mcp", "mcp_transport": "sse", "mcp_auth_token": "secret"},
    )
    assert with_token.json()["data"]["hasMcpAuthToken"] is True

    without_token = await admin_client.post(
        "/v1/skills/", json={"name": "without-token", "type": "mcp", "mcp_transport": "sse"}
    )
    assert without_token.json()["data"]["hasMcpAuthToken"] is False


@pytest.mark.asyncio
async def test_create_skill_without_description_defaults_to_none(admin_client):
    response = await admin_client.post(
        "/v1/skills/", json={"name": "no_desc", "type": "tool"}
    )
    assert response.status_code == 201
    assert response.json()["data"]["description"] is None


@pytest.mark.asyncio
async def test_create_skill_rejects_invalid_type(admin_client):
    response = await admin_client.post(
        "/v1/skills/", json={"name": "bad", "type": "not-a-real-type"}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_skill_creator_keeps_write_access_without_admin_role(
    admin_client,
    member_client,
):
    created = await admin_client.post("/v1/skills/", json={"name": "x", "type": "tool"})
    skill_id = created.json()["data"]["id"]

    response = await member_client.patch(f"/v1/skills/{skill_id}", json={"name": "y"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_update_skill_as_admin_succeeds(admin_client):
    created = await admin_client.post("/v1/skills/", json={"name": "x", "type": "tool"})
    skill_id = created.json()["data"]["id"]

    response = await admin_client.patch(f"/v1/skills/{skill_id}", json={"name": "renamed"})
    assert response.status_code == 200
    assert response.json()["data"]["name"] == "renamed"


@pytest.mark.asyncio
async def test_update_skill_rejects_unknown_field(admin_client):
    created = await admin_client.post("/v1/skills/", json={"name": "x", "type": "tool"})
    skill_id = created.json()["data"]["id"]

    response = await admin_client.patch(f"/v1/skills/{skill_id}", json={"type": "not-a-real-type"})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_update_code_skill_ignores_bundle_fields(admin_client, db, test_org):
    from src.models.skill import Skill

    skill = Skill(
        org_id=test_org.id,
        name="greet",
        type="code",
        bundle_storage_key="skills/x/abc.zip",
        bundle_checksum="abc",
    )
    db.add(skill)
    await db.commit()
    await db.refresh(skill)

    response = await admin_client.patch(
        f"/v1/skills/{skill.id}",
        json={"name": "renamed", "mcp_endpoint": "http://should-be-ignored"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["name"] == "renamed"
    assert data["mcpEndpoint"] is None
    assert data["bundleChecksum"] == "abc"


@pytest.mark.asyncio
async def test_skill_creator_can_delete_without_admin_role(admin_client, member_client):
    created = await admin_client.post("/v1/skills/", json={"name": "to-delete", "type": "tool"})
    skill_id = created.json()["data"]["id"]

    response = await member_client.delete(f"/v1/skills/{skill_id}")
    assert response.status_code == 204


@pytest.mark.asyncio
async def test_delete_skill_as_admin_removes_it(admin_client):
    created = await admin_client.post("/v1/skills/", json={"name": "to-delete", "type": "tool"})
    skill_id = created.json()["data"]["id"]

    response = await admin_client.delete(f"/v1/skills/{skill_id}")
    assert response.status_code == 204

    get_response = await admin_client.get(f"/v1/skills/{skill_id}")
    assert get_response.status_code == 404


@pytest.mark.asyncio
async def test_discover_mcp_tools_requires_admin_write(member_client):
    response = await member_client.post(
        "/v1/skills/discover-mcp-tools",
        json={"mcpEndpoint": "http://mcp-everything:3001/sse", "mcpTransport": "sse"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_discover_mcp_tools_rejects_non_sse_transport(admin_client):
    response = await admin_client.post(
        "/v1/skills/discover-mcp-tools",
        json={"mcpEndpoint": "some-command", "mcpTransport": "stdio"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_discover_mcp_tools_returns_discovered_tools(admin_client):
    discovered = [
        {"name": "echo", "description": "Echoes back the input string", "inputSchema": {"type": "object"}},
    ]
    with patch(
        "src.routers.skills_admin.discover_mcp_tools",
        AsyncMock(return_value=discovered),
    ):
        response = await admin_client.post(
            "/v1/skills/discover-mcp-tools",
            json={"mcpEndpoint": "http://mcp-everything:3001/sse", "mcpTransport": "sse"},
        )
    assert response.status_code == 200
    assert response.json()["data"] == discovered


@pytest.mark.asyncio
async def test_discover_mcp_tools_passes_auth_token_to_service(admin_client):
    mock_discover = AsyncMock(return_value=[])
    with patch("src.routers.skills_admin.discover_mcp_tools", mock_discover):
        response = await admin_client.post(
            "/v1/skills/discover-mcp-tools",
            json={
                "mcpEndpoint": "http://mcp-everything:3001/sse",
                "mcpTransport": "sse",
                "mcpAuthToken": "secret-token",
            },
        )
    assert response.status_code == 200
    mock_discover.assert_awaited_once_with(
        "http://mcp-everything:3001/sse", auth_token="secret-token"
    )


@pytest.mark.asyncio
async def test_discover_mcp_tools_falls_back_to_stored_token_for_existing_skill(admin_client):
    """re-discovering tools for an existing MCP skill
    whose token the admin isn't retyping (mcpAuthToken always comes back
    null) must use the skill's own stored token, not discover with none."""
    created = await admin_client.post(
        "/v1/skills/",
        json={
            "name": "fetch_url",
            "type": "mcp",
            "mcp_transport": "sse",
            "mcp_endpoint": "http://mcp-fetch:8080/sse",
            "mcp_auth_token": "stored-secret",
        },
    )
    skill_id = created.json()["data"]["id"]

    mock_discover = AsyncMock(return_value=[])
    with patch("src.routers.skills_admin.discover_mcp_tools", mock_discover):
        response = await admin_client.post(
            "/v1/skills/discover-mcp-tools",
            json={
                "mcpEndpoint": "http://mcp-fetch:8080/sse",
                "mcpTransport": "sse",
                "skillId": skill_id,
            },
        )
    assert response.status_code == 200
    mock_discover.assert_awaited_once_with(
        "http://mcp-fetch:8080/sse", auth_token="stored-secret"
    )


@pytest.mark.asyncio
async def test_discover_mcp_tools_prefers_typed_token_over_stored_one(admin_client):
    created = await admin_client.post(
        "/v1/skills/",
        json={
            "name": "fetch_url",
            "type": "mcp",
            "mcp_transport": "sse",
            "mcp_auth_token": "stored-secret",
        },
    )
    skill_id = created.json()["data"]["id"]

    mock_discover = AsyncMock(return_value=[])
    with patch("src.routers.skills_admin.discover_mcp_tools", mock_discover):
        response = await admin_client.post(
            "/v1/skills/discover-mcp-tools",
            json={
                "mcpEndpoint": "http://mcp-fetch:8080/sse",
                "mcpTransport": "sse",
                "mcpAuthToken": "freshly-typed",
                "skillId": skill_id,
            },
        )
    assert response.status_code == 200
    mock_discover.assert_awaited_once_with(
        "http://mcp-fetch:8080/sse", auth_token="freshly-typed"
    )


@pytest.mark.asyncio
async def test_discover_mcp_tools_returns_502_when_server_unreachable(admin_client):
    with patch(
        "src.routers.skills_admin.discover_mcp_tools",
        AsyncMock(side_effect=McpDiscoveryError("Could not reach MCP server at http://x/sse: refused")),
    ):
        response = await admin_client.post(
            "/v1/skills/discover-mcp-tools",
            json={"mcpEndpoint": "http://x/sse", "mcpTransport": "sse"},
        )
    assert response.status_code == 502


@pytest.mark.asyncio
async def test_network_policy_is_settable_and_validated(admin_client):
    created = await admin_client.post(
        "/v1/skills/", json={"name": "t", "type": "tool", "network_policy": "egress"}
    )
    assert created.status_code == 201
    assert created.json()["data"]["networkPolicy"] == "egress"
    skill_id = created.json()["data"]["id"]

    patched = await admin_client.patch(
        f"/v1/skills/{skill_id}", json={"network_policy": "none"}
    )
    assert patched.status_code == 200
    assert patched.json()["data"]["networkPolicy"] == "none"

    rejected = await admin_client.patch(
        f"/v1/skills/{skill_id}", json={"network_policy": "host"}
    )
    assert rejected.status_code == 422
