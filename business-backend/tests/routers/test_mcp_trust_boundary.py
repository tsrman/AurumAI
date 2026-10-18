from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from src.core.auth import UserInfo
from src.models.skill import Skill
from src.routers import skills_admin
from src.schemas.skill import McpDiscoverRequest, SkillCreate


def stored_skill(**fields):
    return Skill(
        id=uuid4(), org_id=uuid4(), name="test", created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc), input_schema={}, **fields,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stdio", None, ""])
async def test_tenant_create_rejects_explicit_or_implicit_stdio(transport):
    user = UserInfo(user_id=str(uuid4()), org_id=str(uuid4()), role="org_admin")
    data = SkillCreate(name="test", type="mcp", mcp_transport=transport, mcp_endpoint="python run.py")
    skill = stored_skill(type="mcp", mcp_transport=transport)
    with (
        patch.object(skills_admin, "_require_skill_create", AsyncMock()),
        patch.object(skills_admin.skill_registry, "create_owned_skill", AsyncMock(return_value=skill)) as create,
    ):
        with pytest.raises(HTTPException) as raised:
            await skills_admin.create_skill(data, user, MagicMock())
    assert raised.value.status_code == 422
    create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_type,stored_transport,update", [
    ("mcp", "sse", {"mcpTransport": "stdio"}),
    ("mcp", "sse", {"mcpTransport": None}),
    ("tool", None, {"type": "mcp"}),
    ("mcp", "stdio", {"description": "still unsafe"}),
])
async def test_tenant_patch_validates_effective_transport(stored_type, stored_transport, update):
    skill = stored_skill(type=stored_type, mcp_transport=stored_transport)
    user = UserInfo(user_id=str(uuid4()), org_id=str(skill.org_id), role="org_admin")
    with (
        patch.object(skills_admin.skill_registry, "get_skill", AsyncMock(return_value=skill)),
        patch.object(skills_admin, "_require_skill_write", AsyncMock()),
        patch.object(skills_admin.skill_registry, "update_skill", AsyncMock(return_value=skill)) as save,
    ):
        with pytest.raises(HTTPException) as raised:
            await skills_admin.update_skill(str(skill.id), update, user, MagicMock())
    assert raised.value.status_code == 422
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_discovery_does_not_forward_saved_secret_to_another_endpoint():
    skill = stored_skill(type="mcp", mcp_transport="sse", mcp_endpoint="https://trusted.invalid/sse", mcp_auth_token="saved-secret")
    user = UserInfo(user_id=str(uuid4()), org_id=str(skill.org_id), role="member")
    with (
        patch.object(skills_admin.skill_registry, "get_skill", AsyncMock(return_value=skill)),
        patch.object(skills_admin, "_require_skill_write", AsyncMock()),
        patch.object(skills_admin, "discover_mcp_tools", AsyncMock(return_value=[])) as discover,
    ):
        with pytest.raises(HTTPException) as raised:
            await skills_admin.discover_mcp_tools_endpoint(
                McpDiscoverRequest(skillId=str(skill.id), mcpEndpoint="https://other.invalid/sse"),
                user, MagicMock(),
            )
    assert raised.value.status_code == 422
    discover.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("update,expected_secret", [
    ({"mcpEndpoint": "https://other.invalid/sse"}, None),
    ({"mcpEndpoint": "https://trusted.invalid/sse"}, "saved-secret"),
    ({"description": "edit"}, "saved-secret"),
    ({"mcpEndpoint": "https://other.invalid/sse", "mcpAuthToken": "new-secret"}, "new-secret"),
    ({"mcpEndpoint": "https://other.invalid/sse", "mcpAuthToken": None}, None),
])
async def test_endpoint_changes_cannot_reuse_stored_credentials(update, expected_secret):
    skill = stored_skill(type="mcp", mcp_transport="sse", mcp_endpoint="https://trusted.invalid/sse", mcp_auth_token="saved-secret")
    user = UserInfo(user_id=str(uuid4()), org_id=str(skill.org_id), role="member")

    async def save(session, current, **fields):
        for name, value in fields.items():
            setattr(current, name, value)
        return current

    with (
        patch.object(skills_admin.skill_registry, "get_skill", AsyncMock(return_value=skill)),
        patch.object(skills_admin, "_require_skill_write", AsyncMock()),
        patch.object(skills_admin.skill_registry, "update_skill", save),
    ):
        await skills_admin.update_skill(str(skill.id), update, user, MagicMock())
    assert skill.mcp_auth_token == expected_secret
