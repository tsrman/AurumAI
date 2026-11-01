"""Archiving agents: what an agent with history can be, instead of deleted."""

import uuid

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user, get_current_user_or_session
from src.core.authorization_types import AccessPermission, AuthorizationSubject, GrantSource, PrincipalType
from src.core.capability_catalog import Capability
from src.core.dependencies import get_org_db_session, get_service_db_session
from src.models.access_grant import AccessGrant
from src.models.agent_definition import AgentDefinition
from src.models.conversation import Conversation
from src.models.message import Message
from src.models.organization import Organization
from src.models.organization_capability import OrganizationCapability
from src.models.user import User
from src.models.workflow_definition import WorkflowDefinition, WorkflowNode

from src.models.workflow_run import WorkflowRun
from src.routers import agent_definitions
from src.services.agent_authorization import AgentAuthorizationService
from src.services.resource_registry import ResourceRegistry
from src.schemas.workflow import WorkflowDefinitionCreate, WorkflowNodeDraft
from src.services.workflow_authorization import WorkflowAuthorizationService
from src.services.workflow_definition import WorkflowDefinitionService, WorkflowDependencyNotFound
from src.services.workflow_run import WorkflowRunService


async def _model_catalog() -> list[dict[str, object]]:
    return [{"id": "provider/model-v1"}]


def _grant(
    org_id: uuid.UUID, resource_id: str, user_id: uuid.UUID, resource_type: str = "agent_definition"
) -> AccessGrant:
    return AccessGrant(
        org_id=org_id,
        resource_type=resource_type,
        resource_id=resource_id,
        principal_type=PrincipalType.USER.value,
        principal_id=user_id,
        permission=AccessPermission.WRITE.value,
        created_via=GrantSource.PROVISIONING.value,
    )


def _app(db: AsyncSession, org: Organization, user: User) -> FastAPI:
    identity = UserInfo(user_id=str(user.id), org_id=str(org.id), role="member", raw_claims={})

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
    service = AgentAuthorizationService(registry=ResourceRegistry(model_catalog=_model_catalog))
    monkeypatch.setattr(agent_definitions, "agent_authorization_service", service)


@pytest.fixture(autouse=True)
async def _model_access(db: AsyncSession, test_org: Organization, test_user: User) -> None:
    db.add(_grant(test_org.id, "provider/model-v1", test_user.id, "model"))
    await db.commit()


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _agent(db: AsyncSession, org: Organization, user: User, name: str) -> AgentDefinition:
    agent = AgentDefinition(org_id=org.id, name=name, model_id="provider/model-v1", system_prompt="Answer")
    db.add(agent)
    await db.flush()
    db.add(_grant(org.id, str(agent.id), user.id))
    await db.commit()
    return agent


async def _workflow(
    db: AsyncSession, org: Organization, agent: AgentDefinition, name: str, *, as_supervisor: bool = False
) -> WorkflowDefinition:
    supervisor = agent
    if not as_supervisor:
        supervisor = AgentDefinition(
            org_id=org.id, name=f"supervisor-{uuid.uuid4()}", model_id="provider/model-v1", system_prompt="Plan"
        )
        db.add(supervisor)
        await db.flush()
    workflow = WorkflowDefinition(org_id=org.id, name=name, supervisor_agent_definition_id=supervisor.id)
    db.add(workflow)
    await db.flush()
    if not as_supervisor:
        db.add(
            WorkflowNode(
                org_id=org.id,
                workflow_definition_id=workflow.id,
                node_key="work",
                display_name="Work",
                agent_definition_id=agent.id,
                instruction="Do it",
            )
        )
    await db.commit()
    return workflow


async def _run_for(
    db: AsyncSession, org: Organization, user: User, workflow: WorkflowDefinition, agent: AgentDefinition
) -> WorkflowRun:
    """A run of the workflow, made the way the platform makes one (its node runs hold the agents)."""
    registry = ResourceRegistry(model_catalog=_model_catalog)
    service = WorkflowRunService(
        authorization=WorkflowAuthorizationService(registry=registry),
        agent_authorization=AgentAuthorizationService(registry=registry),
        interactions=None,
    )
    conversation = Conversation(org_id=org.id, user_id=user.id, title="Run", model="provider/model-v1")
    db.add(conversation)
    await db.flush()
    turn_id = uuid.uuid4()
    message = Message(
        org_id=org.id, conversation_id=conversation.id, turn_id=turn_id, user_id=user.id, role="user", content="Go"
    )
    db.add(message)
    await db.commit()
    return await service.create_authorized_run(
        db,
        AuthorizationSubject(user.id, org.id, "org_admin"),
        workflow.id,
        conversation.id,
        turn_id,
        message.id,
    )


async def test_an_archived_agent_leaves_the_list_but_can_be_read_and_restored(
    db: AsyncSession, test_org: Organization, test_user: User
) -> None:
    agent = await _agent(db, test_org, test_user, "Old helper")
    async with _client(_app(db, test_org, test_user)) as client:
        archived = await client.post(f"/v1/agent-definitions/{agent.id}/archive")
        again = await client.post(f"/v1/agent-definitions/{agent.id}/archive")
        listed = await client.get("/v1/agent-definitions")
        with_archived = await client.get("/v1/agent-definitions", params={"includeArchived": "true"})
        read = await client.get(f"/v1/agent-definitions/{agent.id}")
        restored = await client.post(f"/v1/agent-definitions/{agent.id}/unarchive")
        restored_again = await client.post(f"/v1/agent-definitions/{agent.id}/unarchive")
        after = await client.get("/v1/agent-definitions")

    assert archived.status_code == 200 and archived.json()["data"]["archivedAt"] is not None
    assert again.status_code == 200 and again.json()["data"]["archivedAt"] == archived.json()["data"]["archivedAt"]
    assert [item["id"] for item in listed.json()["data"]] == []
    assert [item["id"] for item in with_archived.json()["data"]] == [str(agent.id)]
    assert read.json()["data"]["archivedAt"] is not None
    assert restored.status_code == 200 and restored.json()["data"]["archivedAt"] is None
    assert restored_again.status_code == 200
    assert [item["id"] for item in after.json()["data"]] == [str(agent.id)]


async def test_an_archived_agent_cannot_be_executed_or_put_in_a_new_workflow(
    db: AsyncSession, test_org: Organization, test_user: User
) -> None:
    agent = await _agent(db, test_org, test_user, "Retired")
    async with _client(_app(db, test_org, test_user)) as client:
        before = await client.get(f"/v1/agent-definitions/{agent.id}/execution")
        await client.post(f"/v1/agent-definitions/{agent.id}/archive")
        after = await client.get(f"/v1/agent-definitions/{agent.id}/execution")

    assert before.status_code == 200
    assert after.status_code == 404


async def test_an_agent_in_an_active_workflow_is_neither_archived_nor_deleted(
    db: AsyncSession, test_org: Organization, test_user: User
) -> None:
    node_agent = await _agent(db, test_org, test_user, "Node agent")
    supervisor = await _agent(db, test_org, test_user, "Supervisor")
    node_workflow = await _workflow(db, test_org, node_agent, "Uses it as a node")
    supervisor_workflow = await _workflow(db, test_org, supervisor, "Uses it as supervisor", as_supervisor=True)
    async with _client(_app(db, test_org, test_user)) as client:
        archive_node = await client.post(f"/v1/agent-definitions/{node_agent.id}/archive")
        delete_node = await client.delete(f"/v1/agent-definitions/{node_agent.id}")
        archive_supervisor = await client.post(f"/v1/agent-definitions/{supervisor.id}/archive")

        node_workflow.status = "archived"
        await db.commit()
        archived_ok = await client.post(f"/v1/agent-definitions/{node_agent.id}/archive")

    for response, workflow in ((archive_node, node_workflow), (delete_node, node_workflow), (archive_supervisor, supervisor_workflow)):
        assert response.status_code == 409
        assert response.json()["detail"] == {
            "code": "agent_in_use",
            "workflows": [{"id": str(workflow.id), "name": workflow.name}],
        }
    assert archived_ok.status_code == 200


async def test_an_agent_without_references_is_deleted(
    db: AsyncSession, test_org: Organization, test_user: User
) -> None:
    agent = await _agent(db, test_org, test_user, "Unused")
    async with _client(_app(db, test_org, test_user)) as client:
        deleted = await client.delete(f"/v1/agent-definitions/{agent.id}")
        missing = await client.get(f"/v1/agent-definitions/{agent.id}")

    assert deleted.status_code == 204
    assert missing.status_code == 404


async def test_an_agent_only_an_archived_workflow_holds_is_archived_not_deleted(
    db: AsyncSession, test_org: Organization, test_user: User
) -> None:
    agent = await _agent(db, test_org, test_user, "Held by history")
    workflow = await _workflow(db, test_org, agent, "Old workflow")
    workflow.status = "archived"
    await db.commit()
    async with _client(_app(db, test_org, test_user)) as client:
        deleted = await client.delete(f"/v1/agent-definitions/{agent.id}")
        archived = await client.post(f"/v1/agent-definitions/{agent.id}/archive")

    assert deleted.status_code == 409
    assert deleted.json()["detail"] == {"code": "agent_has_history", "workflows": [{"id": str(workflow.id), "name": "Old workflow"}]}
    assert archived.status_code == 200


async def test_run_history_keeps_an_agent_from_deletion_and_a_live_run_from_archiving(
    db: AsyncSession, test_org: Organization, test_user: User
) -> None:
    agent = await _agent(db, test_org, test_user, "Ran once")
    workflow = await _workflow(db, test_org, agent, "Ran")
    run = await _run_for(db, test_org, test_user, workflow, agent)
    workflow.status = "archived"
    # The graph is gone too: only the run's own record (its node runs) refers to the agent.
    for node in (await db.execute(select(WorkflowNode).where(WorkflowNode.workflow_definition_id == workflow.id))).scalars():
        await db.delete(node)
    await db.commit()
    async with _client(_app(db, test_org, test_user)) as client:
        live = await client.post(f"/v1/agent-definitions/{agent.id}/archive")
        run.status = "completed"
        await db.commit()
        deleted = await client.delete(f"/v1/agent-definitions/{agent.id}")
        archived = await client.post(f"/v1/agent-definitions/{agent.id}/archive")

    assert live.status_code == 409 and live.json()["detail"]["code"] == "agent_in_active_run"
    # Not a 500 from the foreign key: the history is named.
    assert deleted.status_code == 409 and deleted.json()["detail"]["code"] == "agent_has_history"
    assert archived.status_code == 200


async def test_an_archived_agent_still_holds_its_name_and_the_error_says_so(
    db: AsyncSession, test_org: Organization, test_user: User
) -> None:
    agent_id = (await _agent(db, test_org, test_user, "Reporter")).id
    db.add(OrganizationCapability(org_id=test_org.id, capability=Capability.WORKSPACE_AGENTS_CREATE.value))
    await db.commit()
    payload = {"name": "Reporter", "modelId": "provider/model-v1", "systemPrompt": "Be brief"}
    async with _client(_app(db, test_org, test_user)) as client:
        taken = await client.post("/v1/agent-definitions", json=payload)
        await client.post(f"/v1/agent-definitions/{agent_id}/archive")
        taken_by_archived = await client.post("/v1/agent-definitions", json=payload)

    assert taken.status_code == 409
    assert taken.json()["detail"] == "Agent definition name already exists"
    assert taken_by_archived.status_code == 409
    assert taken_by_archived.json()["detail"] == {"code": "agent_name_archived"}


async def test_an_archived_agent_cannot_join_a_new_workflow_or_start_a_run(
    db: AsyncSession, test_org: Organization, test_user: User
) -> None:
    agent = await _agent(db, test_org, test_user, "Archived worker")
    supervisor = await _agent(db, test_org, test_user, "Active supervisor")
    # A run made while the agent is still active, in a workflow that is then archived and so lets the agent go.
    workflow = await _workflow(db, test_org, agent, "Old")
    workflow.status = "archived"
    await db.commit()
    agent_id, supervisor_id = agent.id, supervisor.id
    async with _client(_app(db, test_org, test_user)) as client:
        assert (await client.post(f"/v1/agent-definitions/{agent_id}/archive")).status_code == 200
    draft = WorkflowDefinitionCreate(
        name="New",
        supervisor_agent_definition_id=supervisor_id,
        max_parallelism=1,
        nodes=[WorkflowNodeDraft(node_key="work", display_name="Work", agent_definition_id=agent_id, instruction="Do")],
        edges=[],
    )

    with pytest.raises(WorkflowDependencyNotFound):
        await WorkflowDefinitionService().validate_draft(db, AuthorizationSubject(test_user.id, test_org.id, "org_admin"), draft)
