from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user
from src.core.dependencies import get_org_db_session
from src.models.agent_definition import AgentDefinition
from src.models.organization import Organization
from src.models.workflow_definition import WorkflowDefinition, WorkflowNode
from src.routers import workflows_admin
from src.models.workflow_run import WorkflowRunStatus
from src.schemas.workflow import WorkflowRunDiagnosticResponse
from src.services.agent_authorization import AgentAuthorizationService
from src.services.resource_registry import ResourceRegistry
from src.services.workflow_definition import WorkflowDefinitionService


async def model_catalog() -> list[dict[str, object]]:
    return [{"id": "provider/model-v1"}]


def payload(supervisor_id: UUID, task_ids: tuple[UUID, UUID]) -> dict[str, object]:
    return {
        "name": "Contract review",
        "description": "Review contract",
        "supervisorAgentDefinitionId": str(supervisor_id),
        "maxParallelism": 2,
        "nodes": [
            {
                "nodeKey": "research",
                "displayName": "Research",
                "agentDefinitionId": str(task_ids[0]),
                "instruction": "Find evidence",
            },
            {
                "nodeKey": "review",
                "displayName": "Review",
                "agentDefinitionId": str(task_ids[1]),
                "instruction": "Review evidence",
            },
        ],
        "edges": [{"sourceNodeKey": "research", "targetNodeKey": "review"}],
    }


def app_for(
    db: AsyncSession,
    org_id: UUID,
    user_id: UUID,
    *,
    role: str = "org_admin",
) -> FastAPI:
    app = FastAPI()
    app.include_router(workflows_admin.router, prefix="/v1/admin")
    app.include_router(workflows_admin.diagnostics_router, prefix="/v1/admin")

    async def session_override() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_current_user] = lambda: UserInfo(
        user_id=str(user_id), org_id=str(org_id), role=role
    )
    app.dependency_overrides[get_org_db_session] = session_override
    service = WorkflowDefinitionService(
        agent_authorization=AgentAuthorizationService(
            registry=ResourceRegistry(model_catalog=model_catalog)
        )
    )
    app.dependency_overrides[
        workflows_admin.get_workflow_definition_service
    ] = lambda: service
    return app


async def make_agents(
    db: AsyncSession,
    org_id: UUID,
    *,
    model: str = "provider/model-v1",
) -> tuple[AgentDefinition, AgentDefinition, AgentDefinition]:
    agents = tuple(
        AgentDefinition(
            org_id=org_id,
            name=f"{name}-{uuid4()}",
            model_id=model,
            system_prompt="Answer",
        )
        for name in ("Supervisor", "Researcher", "Reviewer")
    )
    db.add_all(agents)
    await db.commit()
    return agents  # type: ignore[return-value]


async def test_member_cannot_create_workflow(db, test_org, test_user) -> None:
    agents = await make_agents(db, test_org.id)
    app = app_for(db, test_org.id, test_user.id, role="member")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/admin/workflows",
            json=payload(agents[0].id, (agents[1].id, agents[2].id)),
        )

    assert response.status_code == 403


async def test_hitl_survives_admin_workflow_read(db, test_org, test_user) -> None:
    agents = await make_agents(db, test_org.id)
    body = payload(agents[0].id, (agents[1].id, agents[2].id))
    body["nodes"][0]["requiresConfirmation"] = True  # type: ignore[index]
    app = app_for(db, test_org.id, test_user.id)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post("/v1/admin/workflows", json=body)
        assert created.status_code == 201
        item = created.json()["data"]
        fetched = await client.get(f"/v1/admin/workflows/{item['id']}")

    nodes = {node["nodeKey"]: node for node in fetched.json()["data"]["nodes"]}
    assert nodes["research"]["requiresConfirmation"] is True


async def test_admin_create_list_get_validate_and_archive_are_consistent(
    db, test_org, test_user
) -> None:
    agents = await make_agents(db, test_org.id)
    app = app_for(db, test_org.id, test_user.id)
    body = payload(agents[0].id, (agents[1].id, agents[2].id))

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        validated = await client.post("/v1/admin/workflows/validate", json=body)
        created = await client.post("/v1/admin/workflows", json=body)
        workflow_id = created.json()["data"]["id"]
        listing = await client.get("/v1/admin/workflows")
        fetched = await client.get(f"/v1/admin/workflows/{workflow_id}")
        archived = await client.delete(f"/v1/admin/workflows/{workflow_id}")
        archived_again = await client.delete(f"/v1/admin/workflows/{workflow_id}")

    assert validated.status_code == 200
    assert validated.json()["data"]["topologicalOrder"] == ["research", "review"]
    assert created.status_code == 201
    assert created.json()["data"]["version"] == 1
    assert [item["name"] for item in listing.json()["data"]] == ["Contract review"]
    assert [item["nodeKey"] for item in fetched.json()["data"]["nodes"]] == [
        "research",
        "review",
    ]
    assert archived.status_code == 204
    assert archived_again.status_code == 204


async def test_replace_round_trip_preserves_hitl_graph_and_canvas_fields(
    db, test_org, test_user
) -> None:
    agents = await make_agents(db, test_org.id)
    app = app_for(db, test_org.id, test_user.id)
    body = payload(agents[0].id, (agents[1].id, agents[2].id))
    replacement = {
        **body,
        "version": 1,
        "nodes": [
            {
                "nodeKey": "research",
                "displayName": "Research",
                "agentDefinitionId": str(agents[1].id),
                "instruction": "Use the primary sources",
                "positionX": 120.5,
                "positionY": -40.25,
                "requiresConfirmation": True,
            },
            {
                "nodeKey": "review",
                "displayName": "Review",
                "agentDefinitionId": str(agents[2].id),
                "instruction": "Confirm the evidence",
                "positionX": 480.0,
                "positionY": 75.0,
                "requiresConfirmation": False,
            },
        ],
        "edges": [
            {
                "sourceNodeKey": "research",
                "targetNodeKey": "review",
                "condition": "on_failure",
            }
        ],
    }

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post("/v1/admin/workflows", json=body)
        workflow_id = created.json()["data"]["id"]
        replaced = await client.put(
            f"/v1/admin/workflows/{workflow_id}", json=replacement
        )

    assert replaced.status_code == 200
    data = replaced.json()["data"]
    assert data["version"] == 2
    assert data["nodes"] == [
        {
            "nodeKey": "research",
            "displayName": "Research",
            "agentDefinitionId": str(agents[1].id),
            "instruction": "Use the primary sources",
            "positionX": 120.5,
            "positionY": -40.25,
            "requiresConfirmation": True,
        },
        {
            "nodeKey": "review",
            "displayName": "Review",
            "agentDefinitionId": str(agents[2].id),
            "instruction": "Confirm the evidence",
            "positionX": 480.0,
            "positionY": 75.0,
            "requiresConfirmation": False,
        },
    ]
    assert data["edges"] == [
        {
            "sourceNodeKey": "research",
            "targetNodeKey": "review",
            "condition": "on_failure",
            "isLoopBack": False,
            "maxIterations": None,
        }
    ]


async def test_replace_rejects_stale_version_without_partial_rows(
    db, test_org, test_user
) -> None:
    agents = await make_agents(db, test_org.id)
    app = app_for(db, test_org.id, test_user.id)
    body = payload(agents[0].id, (agents[1].id, agents[2].id))

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post("/v1/admin/workflows", json=body)
        workflow_id = created.json()["data"]["id"]
        replaced = await client.put(
            f"/v1/admin/workflows/{workflow_id}",
            json={**body, "version": 1, "description": "Version two"},
        )
        stale = await client.put(
            f"/v1/admin/workflows/{workflow_id}",
            json={
                **body,
                "version": 1,
                "nodes": [body["nodes"][0]],  # type: ignore[index]
                "edges": [],
            },
        )

    assert replaced.status_code == 200
    assert replaced.json()["data"]["version"] == 2
    assert stale.status_code == 409
    persisted = (
        await db.execute(
            select(WorkflowNode).where(
                WorkflowNode.workflow_definition_id == UUID(workflow_id)
            )
        )
    ).scalars().all()
    assert {item.node_key for item in persisted} == {"research", "review"}


async def test_admin_rejects_foreign_agent_missing_model_and_duplicate_name(
    db, test_org, test_user
) -> None:
    agents = await make_agents(db, test_org.id)
    other = Organization(name="Workflow Other", slug=f"workflow-other-{uuid4()}")
    db.add(other)
    await db.flush()
    foreign = await make_agents(db, other.id)
    missing_model = await make_agents(db, test_org.id, model="provider/missing")
    app = app_for(db, test_org.id, test_user.id)
    valid_body = payload(agents[0].id, (agents[1].id, agents[2].id))
    foreign_body = payload(foreign[0].id, (agents[1].id, agents[2].id))
    missing_model_body = payload(
        missing_model[0].id,
        (missing_model[1].id, missing_model[2].id),
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post("/v1/admin/workflows", json=valid_body)
        foreign_response = await client.post(
            "/v1/admin/workflows",
            json=foreign_body,
        )
        missing_model_response = await client.post(
            "/v1/admin/workflows",
            json=missing_model_body,
        )
        duplicate = await client.post("/v1/admin/workflows", json=valid_body)

    assert created.status_code == 201
    assert duplicate.status_code == 409
    assert foreign_response.status_code == 404
    assert missing_model_response.status_code == 404


async def test_archived_definition_remains_persisted(db, test_org, test_user) -> None:
    agents = await make_agents(db, test_org.id)
    app = app_for(db, test_org.id, test_user.id)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/admin/workflows",
            json=payload(agents[0].id, (agents[1].id, agents[2].id)),
        )
        workflow_id = UUID(created.json()["data"]["id"])
        await client.delete(f"/v1/admin/workflows/{workflow_id}")

    stored = await db.get(WorkflowDefinition, workflow_id)
    assert stored is not None
    assert stored.status == "archived"


async def test_only_admin_can_read_same_org_run_diagnostics(
    db, test_org, test_user, monkeypatch
) -> None:
    run_id = uuid4()
    now = datetime.now(UTC)
    response = WorkflowRunDiagnosticResponse(
        id=run_id,
        workflow_definition_id=uuid4(),
        workflow_definition_version=1,
        conversation_id=uuid4(),
        turn_id=uuid4(),
        status=WorkflowRunStatus.FAILED,
        error_code="execution_failed",
        internal_error="provider timeout",
        created_at=now,
        started_at=now,
        completed_at=now,
        nodes=(),
    )

    class Runs:
        async def get_admin_diagnostic(self, session, org_id, requested_run_id):  # noqa: ANN001
            assert org_id == test_org.id
            assert requested_run_id == run_id
            return response

    monkeypatch.setattr(workflows_admin, "workflow_run_service", Runs())

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app_for(db, test_org.id, test_user.id)),
        base_url="http://test",
    ) as admin:
        allowed = await admin.get(f"/v1/admin/workflow-runs/{run_id}")
    async with httpx.AsyncClient(
        transport=ASGITransport(
            app=app_for(db, test_org.id, test_user.id, role="member")
        ),
        base_url="http://test",
    ) as member:
        denied = await member.get(f"/v1/admin/workflow-runs/{run_id}")

    assert allowed.status_code == 200
    assert allowed.json()["data"]["internalError"] == "provider timeout"
    assert denied.status_code == 403
