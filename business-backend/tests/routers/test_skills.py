import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import text

from src.core.auth import UserInfo, get_current_user, get_current_user_or_session
from src.core.authorization_types import AccessPermission, GrantSource, PrincipalType
from src.core.dependencies import get_org_db_session, get_service_db_session
from src.models.access_grant import AccessGrant
from src.models.conversation import Conversation
from src.models.organization import Organization
from src.models.skill import Skill
from src.models.user import User
from src.routers import skills as skills_router, skills_admin as skills_admin_router


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
        # skills_admin mounted alongside the read-only skills router — same
        # as production's main_admin.py — so read tests can still create
        # fixture skills via POST as setup, even though the create/update/
        # delete/discover endpoints themselves are tested in
        # test_skills_admin.py.
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
async def test_list_skills_accessible_to_any_authenticated_user(member_client):
    response = await member_client.get("/v1/skills/")
    assert response.status_code == 200
    assert response.json()["data"] == []


@pytest.mark.asyncio
async def test_conversation_id_surfaces_that_conversations_hidden_skill(
    member_client, db, test_org, test_user
):
    conv = Conversation(
        org_id=test_org.id, user_id=test_user.id, title="Attach test", model="gpt-4o-mini"
    )
    db.add(conv)
    await db.flush()
    hidden = Skill(
        org_id=test_org.id,
        name="hidden",
        type="tool",
        activity_name="execute_hybrid_search",
        owning_conversation_id=conv.id,
    )
    db.add(hidden)
    await db.flush()
    db.add(
        AccessGrant(
            org_id=test_org.id,
            resource_type="skill",
            resource_id=str(hidden.id),
            principal_type=PrincipalType.USER.value,
            principal_id=test_user.id,
            permission=AccessPermission.READ.value,
            created_by=None,
            created_via=GrantSource.PROVISIONING.value,
        )
    )
    await db.commit()

    without = await member_client.get("/v1/skills/")
    assert all(s["id"] != str(hidden.id) for s in without.json()["data"])

    with_it = await member_client.get(
        "/v1/skills/", params={"conversation_id": str(conv.id)}
    )
    assert any(s["id"] == str(hidden.id) for s in with_it.json()["data"])


@pytest.mark.asyncio
async def test_get_skill_after_create_round_trips(admin_client):
    created = await admin_client.post(
        "/v1/skills/", json={"name": "hybrid_search", "type": "agent"}
    )
    skill_id = created.json()["data"]["id"]

    response = await admin_client.get(f"/v1/skills/{skill_id}")
    assert response.status_code == 200
    assert response.json()["data"]["name"] == "hybrid_search"


@pytest.mark.asyncio
async def test_get_skill_404_for_nonexistent_id(admin_client):
    response = await admin_client.get("/v1/skills/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


_MCP_SKILL_PAYLOAD = {
    "name": "mcp_tool",
    "type": "mcp",
    "mcp_transport": "sse",
    "mcp_endpoint": "http://mcp:3000",
    "mcp_auth_token": "super-secret",
}


@pytest.mark.asyncio
async def test_mcp_auth_token_is_write_only_for_browser_users(admin_client):
    created = await admin_client.post("/v1/skills/", json=_MCP_SKILL_PAYLOAD)
    assert created.status_code == 201
    skill_id = created.json()["data"]["id"]
    assert created.json()["data"]["mcpAuthToken"] is None

    listed = await admin_client.get("/v1/skills/")
    assert all(s["mcpAuthToken"] is None for s in listed.json()["data"])

    got = await admin_client.get(f"/v1/skills/{skill_id}")
    assert got.json()["data"]["mcpAuthToken"] is None


@pytest.mark.asyncio
async def test_mcp_auth_token_returned_to_internal_session_tokens(
    admin_client, make_client
):
    created = await admin_client.post("/v1/skills/", json=_MCP_SKILL_PAYLOAD)
    assert created.status_code == 201
    skill_id = created.json()["data"]["id"]

    # tool-worker fetches skills with an internal session token and needs the
    # real token to call the MCP server.
    session_app = make_client(raw_claims={"type": "session"})
    async with httpx.AsyncClient(
        transport=ASGITransport(app=session_app), base_url="http://test"
    ) as session_client:
        got = await session_client.get(f"/v1/skills/{skill_id}")
    assert got.status_code == 200
    assert got.json()["data"]["mcpAuthToken"] == "super-secret"


@pytest.mark.asyncio
async def test_get_skill_with_invalid_uuid_returns_404_not_500(member_client):
    response = await member_client.get("/v1/skills/not-a-uuid")
    assert response.status_code == 404
