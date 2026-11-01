"""Built-in capabilities an administrator switches on or off for an agent."""

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user, get_current_user_or_session
from src.core.capability_catalog import Capability
from src.core.dependencies import get_org_db_session, get_service_db_session
from src.models.access_grant import AccessGrant
from src.models.organization import Organization
from src.models.organization_capability import OrganizationCapability
from src.models.user import User
from src.routers import agent_definitions
from src.services.agent_authorization import AgentAuthorizationService
from src.services.resource_registry import ResourceRegistry


async def _catalog() -> list[dict[str, object]]:
    return [{"id": "provider/model-v1"}]


@pytest.fixture(autouse=True)
def _registry(monkeypatch: pytest.MonkeyPatch) -> None:
    service = AgentAuthorizationService(registry=ResourceRegistry(model_catalog=_catalog))
    monkeypatch.setattr(agent_definitions, "agent_authorization_service", service)


@pytest.fixture
async def client(db: AsyncSession, test_org: Organization, test_user: User):  # noqa: ANN201
    db.add(OrganizationCapability(org_id=test_org.id, capability=Capability.WORKSPACE_AGENTS_CREATE.value))
    db.add(
        AccessGrant(
            org_id=test_org.id,
            resource_type="model",
            resource_id="provider/model-v1",
            principal_type="user",
            principal_id=test_user.id,
            permission="read",
            created_via="provisioning",
        )
    )
    await db.commit()
    identity = UserInfo(user_id=str(test_user.id), org_id=str(test_org.id), role="member", raw_claims={})

    async def override():  # noqa: ANN202
        yield db

    app = FastAPI()
    app.include_router(agent_definitions.router, prefix="/v1")
    app.dependency_overrides[get_current_user] = lambda: identity
    app.dependency_overrides[get_current_user_or_session] = lambda: identity
    app.dependency_overrides[get_org_db_session] = override
    app.dependency_overrides[get_service_db_session] = override
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


PAYLOAD = {"name": "Researcher", "modelId": "provider/model-v1", "systemPrompt": "Research"}


async def test_a_new_agent_has_every_built_in_capability_on(client: httpx.AsyncClient) -> None:
    created = await client.post("/v1/agent-definitions", json=PAYLOAD)

    assert created.status_code == 201
    data = created.json()["data"]
    assert (data["webEnabled"], data["imagesEnabled"], data["askUserEnabled"]) == (True, True, True)


async def test_capabilities_are_chosen_at_creation_and_changed_one_by_one(client: httpx.AsyncClient) -> None:
    created = await client.post("/v1/agent-definitions", json={**PAYLOAD, "webEnabled": False, "imagesEnabled": False})
    agent_id = created.json()["data"]["id"]
    assert [created.json()["data"][key] for key in ("webEnabled", "imagesEnabled", "askUserEnabled")] == [False, False, True]

    changed = await client.patch(f"/v1/agent-definitions/{agent_id}", json={"webEnabled": True, "askUserEnabled": False})
    read = await client.get(f"/v1/agent-definitions/{agent_id}")

    assert changed.status_code == 200
    assert [read.json()["data"][key] for key in ("webEnabled", "imagesEnabled", "askUserEnabled")] == [True, False, False]


async def test_a_capability_cannot_be_set_to_null(client: httpx.AsyncClient) -> None:
    created = await client.post("/v1/agent-definitions", json=PAYLOAD)

    rejected = await client.patch(f"/v1/agent-definitions/{created.json()['data']['id']}", json={"webEnabled": None})

    assert rejected.status_code == 422
