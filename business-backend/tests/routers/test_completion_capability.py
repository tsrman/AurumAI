from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from src.core.auth import UserInfo, get_current_user_or_session
from src.core.capability_catalog import Capability
from src.core.dependencies import get_service_db_session
from src.models.organization_capability import OrganizationCapability
from src.models.access_grant import AccessGrant
from src.routers import chat
from src.services.quota import get_quota_service


@pytest.mark.asyncio
@pytest.mark.parametrize("session_token", [False, True])
async def test_completion_capability_is_live_for_jwt_and_session(db, test_org, test_user, session_token):
    user = UserInfo(
        user_id=str(test_user.id), org_id=str(test_org.id), role="member",
        raw_claims={"type": "session", "conversation_id": str(uuid4())} if session_token else {},
    )
    db.add(AccessGrant(
        org_id=test_org.id, resource_type="model", resource_id="allowed-model",
        principal_type="user", principal_id=test_user.id, permission="read",
        created_via="provisioning",
    ))
    capability = OrganizationCapability(org_id=test_org.id, capability=Capability.CHAT_SEND.value)
    db.add(capability)
    await db.commit()

    async def session_dep():
        yield db

    async def user_dep():
        return user

    async def stream(*args, **kwargs):
        yield '{"choices":[{"delta":{"content":"allowed answer"}}]}'

    quota = AsyncMock()
    app = FastAPI()
    app.include_router(chat.router, prefix="/v1")
    app.dependency_overrides[get_current_user_or_session] = user_dep
    app.dependency_overrides[get_service_db_session] = session_dep
    app.dependency_overrides[get_quota_service] = lambda: quota
    request = {"model": "allowed-model", "messages": [{"role": "user", "content": "hello"}]}
    with patch.object(chat.llm_client, "stream_chat", stream), patch.object(chat, "get_langfuse", return_value=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            allowed = await client.post("/v1/chat/completions", json=request)
            await db.delete(capability)
            await db.commit()
            revoked = await client.post("/v1/chat/completions", json=request)
    assert allowed.status_code == 200
    assert "allowed answer" in allowed.text
    assert revoked.status_code == 403
    assert revoked.json()["detail"] == "Missing capability: chat.send"
    assert quota.reserve.await_count == 1
