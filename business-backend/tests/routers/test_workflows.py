from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from src.core.auth import UserInfo, get_current_user
from src.core.dependencies import get_org_db_session
from src.models.workflow_definition import WorkflowDefinitionStatus
from src.models.workflow_run import WorkflowRunStatus
from src.routers import workflows
from src.schemas.workflow import WorkflowRunSafeResponse
from src.services.workflow_authorization import (
    WorkflowDefinitionNotReadable,
    WorkflowStepAccessDenied,
)
from src.services.workflow_run import WorkflowRunNotFound


class _Session:
    committed = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        pass


class _Definitions:
    def __init__(self, definition) -> None:
        self.definition = definition

    async def list_accessible(self, session, subject):  # noqa: ANN001
        return [self.definition]


class _Authorization:
    def __init__(self, definition, *, denied: bool = False) -> None:
        self.definition = definition
        self.denied = denied

    async def require_definition_readable(self, session, subject, workflow_id):  # noqa: ANN001
        if self.denied:
            raise WorkflowDefinitionNotReadable(workflow_id)
        return self.definition


class _Runs:
    def __init__(self, response: WorkflowRunSafeResponse) -> None:
        self.response = response
        self.cancelled = False

    async def get_safe_run(self, session, subject, run_id):  # noqa: ANN001
        if run_id != self.response.id:
            raise WorkflowRunNotFound()
        return self.response

    async def request_cancel(self, session, subject, run_id):  # noqa: ANN001
        if run_id != self.response.id:
            raise WorkflowRunNotFound()
        self.cancelled = True
        return SimpleNamespace(id=run_id, conversation_id=self.response.conversation_id)

    async def request_decision(self, session, subject, run_id, node_key):  # noqa: ANN001
        if run_id != self.response.id:
            raise WorkflowRunNotFound()
        return SimpleNamespace(id=run_id, conversation_id=self.response.conversation_id)


class _StepAuthorization:
    def __init__(self, *, denied: bool = False) -> None:
        self.denied = denied
        self.calls: list[tuple] = []

    async def require_snapshot_step_executable(
        self, session, subject, run, *, phase, node_key  # noqa: ANN001
    ):
        self.calls.append((run.id, phase, node_key))
        if self.denied:
            raise WorkflowStepAccessDenied(run.id)
        return SimpleNamespace(run_id=run.id, phase=phase, node_key=node_key)


def _app(identity: UserInfo, session: _Session) -> FastAPI:
    async def db_override():
        yield session

    app = FastAPI()
    app.include_router(workflows.router, prefix="/v1")
    app.dependency_overrides[get_current_user] = lambda: identity
    app.dependency_overrides[get_org_db_session] = db_override
    return app


async def test_member_lists_only_repository_authorized_active_workflows(monkeypatch) -> None:
    org_id, user_id, workflow_id = uuid4(), uuid4(), uuid4()
    definition = SimpleNamespace(
        id=workflow_id,
        name="Research",
        description="Evidence",
        version=3,
        max_parallelism=2,
        status=WorkflowDefinitionStatus.ACTIVE.value,
        auto_routable=False,
    )
    monkeypatch.setattr(workflows, "workflow_definition_repository", _Definitions(definition))
    identity = UserInfo(user_id=str(user_id), org_id=str(org_id), role="member")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(identity, _Session())), base_url="http://test"
    ) as client:
        response = await client.get("/v1/workflows")

    assert response.status_code == 200
    assert response.json()["data"] == [
        {
            "id": str(workflow_id),
            "name": "Research",
            "description": "Evidence",
            "version": 3,
            "maxParallelism": 2,
            "status": "active",
            "autoRoutable": False,
        }
    ]


async def test_member_definition_denial_and_unknown_run_are_both_404(monkeypatch) -> None:
    org_id, user_id, workflow_id = uuid4(), uuid4(), uuid4()
    definition = SimpleNamespace(id=workflow_id)
    denied = _Authorization(definition, denied=True)
    now = datetime.now(UTC)
    safe = WorkflowRunSafeResponse(
        id=uuid4(),
        workflow_definition_id=workflow_id,
        workflow_definition_version=1,
        conversation_id=uuid4(),
        turn_id=uuid4(),
        status=WorkflowRunStatus.PENDING,
        error_code=None,
        created_at=now,
        started_at=None,
        completed_at=None,
        nodes=(),
    )
    monkeypatch.setattr(workflows, "workflow_authorization_service", denied)
    monkeypatch.setattr(workflows, "workflow_run_service", _Runs(safe))
    identity = UserInfo(user_id=str(user_id), org_id=str(org_id), role="member")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(identity, _Session())), base_url="http://test"
    ) as client:
        hidden = await client.get(f"/v1/workflows/{workflow_id}")
        missing_run = await client.get(f"/v1/workflow-runs/{uuid4()}")

    assert hidden.status_code == missing_run.status_code == 404


async def test_member_can_read_and_cancel_own_run(monkeypatch) -> None:
    org_id, user_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    safe = WorkflowRunSafeResponse(
        id=uuid4(),
        workflow_definition_id=uuid4(),
        workflow_definition_version=1,
        conversation_id=uuid4(),
        turn_id=uuid4(),
        status=WorkflowRunStatus.PENDING,
        error_code=None,
        created_at=now,
        started_at=None,
        completed_at=None,
        nodes=(),
    )
    service = _Runs(safe)
    monkeypatch.setattr(workflows, "workflow_run_service", service)
    temporal = MagicMock()
    temporal.get_workflow_handle.return_value = AsyncMock()
    monkeypatch.setattr(workflows, "get_temporal_client", lambda: temporal)
    session = _Session()
    identity = UserInfo(user_id=str(user_id), org_id=str(org_id), role="member")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(identity, session)), base_url="http://test"
    ) as client:
        read = await client.get(f"/v1/workflow-runs/{safe.id}")
        cancelled = await client.post(f"/v1/workflow-runs/{safe.id}/cancel")

    assert read.status_code == 200
    assert read.json()["data"]["id"] == str(safe.id)
    assert cancelled.status_code == 202
    assert service.cancelled is True
    assert session.committed is True


async def test_member_can_approve_own_run_node_decision(monkeypatch) -> None:
    org_id, user_id, run_id = uuid4(), uuid4(), uuid4()
    service = _Runs(
        WorkflowRunSafeResponse(
            id=run_id,
            workflow_definition_id=uuid4(),
            workflow_definition_version=1,
            conversation_id=uuid4(),
            turn_id=uuid4(),
            status=WorkflowRunStatus.RUNNING,
            error_code=None,
            created_at=datetime.now(UTC),
            started_at=None,
            completed_at=None,
            nodes=(),
        )
    )
    monkeypatch.setattr(workflows, "workflow_run_service", service)
    step_auth = _StepAuthorization()
    monkeypatch.setattr(workflows, "workflow_authorization_service", step_auth)
    temporal = MagicMock()
    handle = AsyncMock()
    temporal.get_workflow_handle.return_value = handle
    monkeypatch.setattr(workflows, "get_temporal_client", lambda: temporal)
    identity = UserInfo(user_id=str(user_id), org_id=str(org_id), role="member")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(identity, _Session())), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/workflow-runs/{run_id}/nodes/draft/decision",
            json={"decision": "approve"},
        )

    assert response.status_code == 202
    assert response.json()["data"] == {
        "runId": str(run_id),
        "nodeKey": "draft",
        "accepted": True,
    }
    temporal.get_workflow_handle.assert_called_once_with(f"{run_id}:orchestrator")
    handle.signal.assert_awaited_once_with(
        "submit_human_decision", args=["draft", "approve"]
    )
    assert step_auth.calls == [(run_id, "node", "draft")]


async def test_decision_on_unknown_run_is_404(monkeypatch) -> None:
    org_id, user_id, run_id = uuid4(), uuid4(), uuid4()
    service = _Runs(
        WorkflowRunSafeResponse(
            id=uuid4(),
            workflow_definition_id=uuid4(),
            workflow_definition_version=1,
            conversation_id=uuid4(),
            turn_id=uuid4(),
            status=WorkflowRunStatus.RUNNING,
            error_code=None,
            created_at=datetime.now(UTC),
            started_at=None,
            completed_at=None,
            nodes=(),
        )
    )
    monkeypatch.setattr(workflows, "workflow_run_service", service)
    identity = UserInfo(user_id=str(user_id), org_id=str(org_id), role="member")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(identity, _Session())), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/workflow-runs/{run_id}/nodes/draft/decision",
            json={"decision": "reject"},
        )

    assert response.status_code == 404


async def test_decision_denied_when_live_authorization_fails(monkeypatch) -> None:
    org_id, user_id, run_id = uuid4(), uuid4(), uuid4()
    service = _Runs(
        WorkflowRunSafeResponse(
            id=run_id,
            workflow_definition_id=uuid4(),
            workflow_definition_version=1,
            conversation_id=uuid4(),
            turn_id=uuid4(),
            status=WorkflowRunStatus.RUNNING,
            error_code=None,
            created_at=datetime.now(UTC),
            started_at=None,
            completed_at=None,
            nodes=(),
        )
    )
    monkeypatch.setattr(workflows, "workflow_run_service", service)
    monkeypatch.setattr(
        workflows, "workflow_authorization_service", _StepAuthorization(denied=True)
    )
    temporal = MagicMock()
    monkeypatch.setattr(workflows, "get_temporal_client", lambda: temporal)
    identity = UserInfo(user_id=str(user_id), org_id=str(org_id), role="member")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(identity, _Session())), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/workflow-runs/{run_id}/nodes/draft/decision",
            json={"decision": "approve"},
        )

    assert response.status_code == 403
    temporal.get_workflow_handle.assert_not_called()


def _safe_run(status: WorkflowRunStatus = WorkflowRunStatus.RUNNING) -> WorkflowRunSafeResponse:
    return WorkflowRunSafeResponse(
        id=uuid4(),
        workflow_definition_id=uuid4(),
        workflow_definition_version=1,
        conversation_id=uuid4(),
        turn_id=uuid4(),
        status=status,
        error_code=None,
        created_at=datetime.now(UTC),
        started_at=None,
        completed_at=None,
        nodes=(),
    )


def _failing_temporal(monkeypatch) -> None:  # noqa: ANN001
    handle = AsyncMock()
    handle.signal.side_effect = RuntimeError("temporal signal failed")
    temporal = MagicMock()
    temporal.get_workflow_handle.return_value = handle
    monkeypatch.setattr(workflows, "get_temporal_client", lambda: temporal)


async def test_cancel_unavailable_is_logged_with_the_cause(monkeypatch, caplog) -> None:
    safe = _safe_run()
    monkeypatch.setattr(workflows, "workflow_run_service", _Runs(safe))
    _failing_temporal(monkeypatch)
    identity = UserInfo(user_id=str(uuid4()), org_id=str(uuid4()), role="member")

    with caplog.at_level("WARNING", logger=workflows.__name__):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=_app(identity, _Session())), base_url="http://test"
        ) as client:
            response = await client.post(f"/v1/workflow-runs/{safe.id}/cancel")

    assert response.status_code == 503
    assert response.json()["detail"] == "Workflow cancellation unavailable"
    [record] = [r for r in caplog.records if r.name == workflows.__name__]
    assert record.getMessage() == "workflow_cancel_unavailable"
    assert record.run_id == str(safe.id)
    assert isinstance(record.exc_info[1], RuntimeError)


async def test_decision_unavailable_is_logged_with_the_cause(monkeypatch, caplog) -> None:
    safe = _safe_run()
    monkeypatch.setattr(workflows, "workflow_run_service", _Runs(safe))
    monkeypatch.setattr(workflows, "workflow_authorization_service", _StepAuthorization())
    _failing_temporal(monkeypatch)
    identity = UserInfo(user_id=str(uuid4()), org_id=str(uuid4()), role="member")

    with caplog.at_level("WARNING", logger=workflows.__name__):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=_app(identity, _Session())), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/v1/workflow-runs/{safe.id}/nodes/draft/decision",
                json={"decision": "approve"},
            )

    assert response.status_code == 503
    [record] = [r for r in caplog.records if r.name == workflows.__name__]
    assert record.getMessage() == "workflow_decision_unavailable"
    assert record.run_id == str(safe.id)
    assert record.node_key == "draft"
    assert isinstance(record.exc_info[1], RuntimeError)
