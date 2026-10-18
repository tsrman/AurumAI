from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from src.core.auth import UserInfo, get_current_user_or_session
from src.core.dependencies import get_service_db_session
from src.models.workflow_run import WorkflowRunStatus
from src.routers import workflow_runs_internal


class _Session:
    committed = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        pass


class _Repository:
    def __init__(self, run) -> None:
        self.run = run

    async def get_by_id(self, session, run_id, **kwargs):  # noqa: ANN001
        return self.run if run_id == self.run.id else None


class _Authorization:
    async def require_snapshot_step_executable(
        self, session, subject, run, *, phase, node_key  # noqa: ANN001
    ):
        return SimpleNamespace(run_id=run.id, phase=phase, node_key=node_key)


class _Runs:
    def __init__(self) -> None:
        self.transitions: list[tuple] = []
        self.results: list[tuple] = []

    async def transition(self, session, run_id, status, error_code):  # noqa: ANN001
        self.transitions.append((run_id, status, error_code))
        return SimpleNamespace(id=run_id, status=status.value)

    async def record_node_result(
        self, session, run_id, phase_key, status, output, error_code  # noqa: ANN001
    ):
        self.results.append((run_id, phase_key, status, output, error_code))
        return SimpleNamespace(id=uuid4())


def _run(org_id, user_id, conversation_id):  # noqa: ANN001
    run_id = uuid4()
    return SimpleNamespace(
        id=run_id,
        org_id=org_id,
        initiating_user_id=user_id,
        conversation_id=conversation_id,
        execution_snapshot={
            "runId": str(run_id),
            "orgId": str(org_id),
            "workflowDefinitionId": str(uuid4()),
            "workflowDefinitionVersion": 1,
            "conversationId": str(conversation_id),
            "turnId": str(uuid4()),
            "sourceMessageId": str(uuid4()),
            "temporalWorkflowId": "conversation:test:workflow:turn",
            "userRequest": "Compare",
            "maxParallelism": 1,
            "supervisor": {
                "agentDefinitionId": str(uuid4()),
                "name": "Supervisor",
                "model": "provider/model",
                "systemPrompt": "Plan",
                "temperature": 0.2,
                "allowedSkillIds": [],
                "knowledgeBases": [],
            },
            "nodes": [
                {
                    "nodeKey": "research",
                    "displayName": "Research",
                    "instruction": "Find evidence",
                    "agent": {
                        "agentDefinitionId": str(uuid4()),
                        "name": "Research",
                        "model": "provider/model",
                        "systemPrompt": "Find",
                        "temperature": 0.2,
                        "allowedSkillIds": [],
                        "knowledgeBases": [],
                    },
                }
            ],
            "edges": [],
            "status": "pending",
            "createdAt": datetime.now(UTC).isoformat(),
        },
    )


def _app(identity: UserInfo, session: _Session) -> FastAPI:
    async def db_override():
        yield session

    app = FastAPI()
    app.include_router(workflow_runs_internal.router, prefix="/v1/internal")
    app.dependency_overrides[get_current_user_or_session] = lambda: identity
    app.dependency_overrides[get_service_db_session] = db_override
    return app


async def test_internal_routes_reject_keycloak_identity(monkeypatch) -> None:
    org_id, user_id, conversation_id = uuid4(), uuid4(), uuid4()
    run = _run(org_id, user_id, conversation_id)
    monkeypatch.setattr(workflow_runs_internal, "workflow_run_repository", _Repository(run))
    identity = UserInfo(user_id=str(user_id), org_id=str(org_id), raw_claims={})

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(identity, _Session())), base_url="http://test"
    ) as client:
        response = await client.get(f"/v1/internal/workflow-runs/{run.id}/snapshot")

    assert response.status_code == 403


async def test_internal_run_identity_mismatch_is_indistinguishable_404(monkeypatch) -> None:
    org_id, user_id, conversation_id = uuid4(), uuid4(), uuid4()
    run = _run(org_id, user_id, conversation_id)
    monkeypatch.setattr(workflow_runs_internal, "workflow_run_repository", _Repository(run))
    identity = UserInfo(
        user_id=str(user_id),
        org_id=str(org_id),
        raw_claims={"type": "session", "conversation_id": str(uuid4())},
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(identity, _Session())), base_url="http://test"
    ) as client:
        mismatch = await client.get(f"/v1/internal/workflow-runs/{run.id}/snapshot")
        missing = await client.get(f"/v1/internal/workflow-runs/{uuid4()}/snapshot")

    assert mismatch.status_code == missing.status_code == 404
    assert mismatch.json() == missing.json()


async def test_session_can_read_authorize_transition_and_record_result(monkeypatch) -> None:
    org_id, user_id, conversation_id = uuid4(), uuid4(), uuid4()
    run = _run(org_id, user_id, conversation_id)
    service = _Runs()
    monkeypatch.setattr(workflow_runs_internal, "workflow_run_repository", _Repository(run))
    monkeypatch.setattr(workflow_runs_internal, "workflow_authorization_service", _Authorization())
    monkeypatch.setattr(workflow_runs_internal, "workflow_run_service", service)
    session = _Session()
    identity = UserInfo(
        user_id=str(user_id),
        org_id=str(org_id),
        raw_claims={"type": "session", "conversation_id": str(conversation_id)},
    )
    app = _app(identity, session)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        snapshot = await client.get(f"/v1/internal/workflow-runs/{run.id}/snapshot")
        authorized = await client.post(
            f"/v1/internal/workflow-runs/{run.id}/authorize-step",
            json={"phase": "node", "nodeKey": "research"},
        )
        transitioned = await client.post(
            f"/v1/internal/workflow-runs/{run.id}/transition",
            json={"status": WorkflowRunStatus.PLANNING.value},
        )
        result = await client.put(
            f"/v1/internal/workflow-runs/{run.id}/nodes/research",
            json={"status": "completed", "output": "done"},
        )

    assert snapshot.status_code == authorized.status_code == 200
    assert transitioned.status_code == result.status_code == 200
    assert snapshot.json()["data"]["runId"] == str(run.id)
    assert authorized.json()["data"] == {
        "runId": str(run.id),
        "phase": "node",
        "nodeKey": "research",
    }
    assert service.transitions == [(run.id, WorkflowRunStatus.PLANNING, None)]
    assert service.results == [(run.id, "research", "completed", "done", None)]
    assert session.committed is True


class _SessionManager:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def get_or_create(self, conversation_id, user_id, org_id):  # noqa: ANN001
        self.calls.append((conversation_id, user_id, org_id))
        return SimpleNamespace(resource_id="runner-1")


async def test_touch_activity_refreshes_this_conversations_runner(monkeypatch) -> None:
    org_id, user_id, conversation_id = uuid4(), uuid4(), uuid4()
    run = _run(org_id, user_id, conversation_id)
    monkeypatch.setattr(workflow_runs_internal, "workflow_run_repository", _Repository(run))
    session_manager = _SessionManager()
    monkeypatch.setattr(workflow_runs_internal, "session_manager", session_manager)
    identity = UserInfo(
        user_id=str(user_id),
        org_id=str(org_id),
        raw_claims={"type": "session", "conversation_id": str(conversation_id)},
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(identity, _Session())), base_url="http://test"
    ) as client:
        response = await client.post(f"/v1/internal/workflow-runs/{run.id}/touch-activity")

    assert response.status_code == 204
    assert session_manager.calls == [(str(conversation_id), str(user_id), str(org_id))]


async def test_touch_activity_rejects_a_run_from_another_conversation(monkeypatch) -> None:
    org_id, user_id, conversation_id = uuid4(), uuid4(), uuid4()
    run = _run(org_id, user_id, conversation_id)
    monkeypatch.setattr(workflow_runs_internal, "workflow_run_repository", _Repository(run))
    monkeypatch.setattr(workflow_runs_internal, "session_manager", _SessionManager())
    identity = UserInfo(
        user_id=str(user_id),
        org_id=str(org_id),
        raw_claims={"type": "session", "conversation_id": str(uuid4())},
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(identity, _Session())), base_url="http://test"
    ) as client:
        response = await client.post(f"/v1/internal/workflow-runs/{run.id}/touch-activity")

    assert response.status_code == 404
