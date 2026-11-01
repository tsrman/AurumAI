import uuid

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user, get_current_user_or_session
from src.core.authorization_types import (
    AccessPermission,
    GrantSource,
    PrincipalType,
)
from src.core.capability_catalog import Capability
from src.core.dependencies import get_org_db_session, get_service_db_session
from src.models.access_grant import AccessGrant
from src.models.agent_definition import AgentDefinition
from src.models.knowledge_base import KnowledgeBase
from src.models.organization import Organization
from src.models.organization_capability import OrganizationCapability
from src.models.skill import Skill
from src.models.user import User
from src.models.workflow_definition import WorkflowDefinition
from src.routers import agent_definitions
from src.services.agent_authorization import AgentAuthorizationService
from src.services.resource_registry import ResourceRegistry


async def _model_catalog() -> list[dict[str, object]]:
    return [{"id": "provider/model-v1"}]


def _grant(
    org_id: uuid.UUID,
    resource_type: str,
    resource_id: str,
    user_id: uuid.UUID,
    permission: AccessPermission = AccessPermission.READ,
) -> AccessGrant:
    return AccessGrant(
        org_id=org_id,
        resource_type=resource_type,
        resource_id=resource_id,
        principal_type=PrincipalType.USER.value,
        principal_id=user_id,
        permission=permission.value,
        created_via=GrantSource.PROVISIONING.value,
    )


def _app(
    db: AsyncSession,
    org: Organization,
    user: User,
    *,
    session_token: bool = False,
) -> FastAPI:
    identity = UserInfo(
        user_id=str(user.id),
        org_id=str(org.id),
        role="member",
        raw_claims={"type": "session"} if session_token else {},
    )

    async def db_override():
        yield db

    app = FastAPI()
    app.include_router(agent_definitions.router, prefix="/v1")
    app.dependency_overrides[get_current_user] = lambda: identity
    app.dependency_overrides[get_current_user_or_session] = lambda: identity
    app.dependency_overrides[get_org_db_session] = db_override
    app.dependency_overrides[get_service_db_session] = db_override
    return app


@pytest.fixture(autouse=True)
def _agent_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    service = AgentAuthorizationService(
        registry=ResourceRegistry(model_catalog=_model_catalog)
    )
    monkeypatch.setattr(agent_definitions, "agent_authorization_service", service)


async def test_v2_create_requires_capability_and_dependency_access(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    skill = Skill(org_id=test_org.id, name="Search", type="tool")
    base = KnowledgeBase(org_id=test_org.id, name="Runbooks")
    db.add_all([skill, base])
    await db.flush()
    db.add_all(
        [
            _grant(test_org.id, "model", "provider/model-v1", test_user.id),
            _grant(test_org.id, "skill", str(skill.id), test_user.id),
            _grant(test_org.id, "knowledge_base", str(base.id), test_user.id),
        ]
    )
    await db.commit()
    app = _app(db, test_org, test_user)
    payload = {
        "name": "Research",
        "description": "Evidence",
        "modelId": "provider/model-v1",
        "systemPrompt": "Be precise",
        "temperature": 0.2,
        "skillIds": [str(skill.id)],
        "knowledgeBaseIds": [str(base.id)],
    }

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.post("/v1/agent-definitions", json=payload)
        db.add(
            OrganizationCapability(
                org_id=test_org.id,
                capability=Capability.WORKSPACE_AGENTS_CREATE.value,
            )
        )
        await db.commit()
        created = await client.post("/v1/agent-definitions", json=payload)

    assert denied.status_code == 403
    assert created.status_code == 201
    assert created.json()["data"]["skillIds"] == [str(skill.id)]
    assert created.json()["data"]["knowledgeBaseIds"] == [str(base.id)]
    agent_id = created.json()["data"]["id"]
    owner = (
        await db.execute(
            select(AccessGrant).where(
                AccessGrant.resource_type == "agent_definition",
                AccessGrant.resource_id == agent_id,
                AccessGrant.principal_id == test_user.id,
                AccessGrant.permission == "write",
            )
        )
    ).scalar_one_or_none()
    assert owner is not None


async def test_v2_list_hides_unreadable_and_patch_requires_write(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    visible = AgentDefinition(
        org_id=test_org.id,
        name="Visible",
        model_id="provider/model-v1",
        system_prompt="Answer",
    )
    hidden = AgentDefinition(
        org_id=test_org.id,
        name="Hidden",
        model_id="provider/model-v1",
        system_prompt="Answer",
    )
    other_org = Organization(name="Agent Other", slug=f"agent-other-{uuid.uuid4()}")
    db.add(other_org)
    await db.flush()
    foreign = AgentDefinition(
        org_id=other_org.id,
        name="Foreign",
        model_id="provider/model-v1",
        system_prompt="Answer",
    )
    db.add_all([visible, hidden, foreign])
    await db.flush()
    grant = _grant(
        test_org.id,
        "agent_definition",
        str(visible.id),
        test_user.id,
    )
    db.add_all(
        [
            grant,
            _grant(
                test_org.id,
                "model",
                visible.model_id,
                test_user.id,
            ),
        ]
    )
    await db.commit()
    app = _app(db, test_org, test_user)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        listing = await client.get("/v1/agent-definitions")
        hidden_get = await client.get(f"/v1/agent-definitions/{hidden.id}")
        foreign_get = await client.get(f"/v1/agent-definitions/{foreign.id}")
        read_only_patch = await client.patch(
            f"/v1/agent-definitions/{visible.id}", json={"description": "Changed"}
        )
        grant.permission = AccessPermission.WRITE.value
        await db.commit()
        writable_patch = await client.patch(
            f"/v1/agent-definitions/{visible.id}", json={"description": "Changed"}
        )

    assert [item["name"] for item in listing.json()["data"]] == ["Visible"]
    assert hidden_get.status_code == 404
    assert foreign_get.status_code == 404
    assert hidden_get.json()["detail"] == foreign_get.json()["detail"]
    assert read_only_patch.status_code == 403
    assert writable_patch.status_code == 200
    assert writable_patch.json()["data"]["description"] == "Changed"


async def test_execution_boundary_rechecks_dependencies_after_revoke(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    agent = AgentDefinition(
        org_id=test_org.id,
        name="Executable",
        model_id="provider/model-v1",
        system_prompt="Answer",
    )
    db.add(agent)
    await db.flush()
    grants = [
        _grant(test_org.id, "agent_definition", str(agent.id), test_user.id),
        _grant(test_org.id, "model", agent.model_id, test_user.id),
    ]
    db.add_all(grants)
    await db.commit()
    app = _app(db, test_org, test_user, session_token=True)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        allowed = await client.get(f"/v1/agent-definitions/{agent.id}/execution")
        await db.execute(delete(AccessGrant).where(AccessGrant.id == grants[1].id))
        await db.commit()
        revoked = await client.get(f"/v1/agent-definitions/{agent.id}/execution")

    assert allowed.status_code == 200
    assert revoked.status_code == 403
    assert revoked.json()["detail"] == "Agent dependencies are not accessible"


async def test_delete_requires_write_and_removes_agent_grants(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    agent = AgentDefinition(
        org_id=test_org.id,
        name="Disposable agent",
        model_id="provider/model-v1",
        system_prompt="Answer",
    )
    db.add(agent)
    await db.flush()
    grant = _grant(
        test_org.id,
        "agent_definition",
        str(agent.id),
        test_user.id,
    )
    db.add(grant)
    await db.commit()
    app = _app(db, test_org, test_user)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.delete(f"/v1/agent-definitions/{agent.id}")
        grant.permission = AccessPermission.WRITE.value
        await db.commit()
        deleted = await client.delete(f"/v1/agent-definitions/{agent.id}")

    assert denied.status_code == 403
    assert deleted.status_code == 204
    assert await db.get(AgentDefinition, agent.id) is None
    remaining = await db.execute(
        select(AccessGrant).where(AccessGrant.id == grant.id)
    )
    assert remaining.scalar_one_or_none() is None


async def test_delete_rejects_agent_referenced_by_active_or_archived_workflow(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    agent = AgentDefinition(
        org_id=test_org.id,
        name="Referenced agent",
        model_id="provider/model-v1",
        system_prompt="Answer",
    )
    db.add(agent)
    await db.flush()
    workflow = WorkflowDefinition(
        org_id=test_org.id,
        name="Saved workflow",
        supervisor_agent_definition_id=agent.id,
    )
    db.add_all(
        [
            workflow,
            _grant(
                test_org.id,
                "agent_definition",
                str(agent.id),
                test_user.id,
                AccessPermission.WRITE,
            ),
        ]
    )
    await db.commit()
    app = _app(db, test_org, test_user)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        active = await client.delete(f"/v1/agent-definitions/{agent.id}")
        workflow.status = "archived"
        await db.commit()
        archived = await client.delete(f"/v1/agent-definitions/{agent.id}")

    assert active.status_code == 409
    assert archived.status_code == 409
    workflows = [{"id": str(workflow.id), "name": "Saved workflow"}]
    assert active.json()["detail"] == {"code": "agent_in_use", "workflows": workflows}
    # An archived workflow keeps its nodes: the agent is held by history, not in use.
    assert archived.json()["detail"] == {"code": "agent_has_history", "workflows": workflows}
