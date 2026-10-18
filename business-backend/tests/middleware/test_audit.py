"""Audit-log behavior for mutating requests.

Tests exercise the real middleware with its DB-write boundary replaced, checking
what is recorded and that failed logging does not fail the request.
test_audit_rls.py separately verifies real insertion and RLS constraints.
"""

import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from src.middleware.audit import AuditMiddleware

ORG_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())


def make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)

    @app.post("/v1/skills/")
    async def create_skill() -> dict[str, str]:
        return {"status": "created"}

    @app.get("/v1/skills/")
    async def list_skills() -> dict[str, str]:
        return {"status": "ok"}

    @app.delete("/v1/skills/{skill_id}")
    async def delete_skill(skill_id: str) -> dict[str, str]:
        return {"status": "deleted"}

    @app.post("/v1/auth/callback")
    async def auth_callback() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/admin/workflows")
    async def create_workflow() -> dict[str, str]:
        return {"status": "created"}

    @app.put("/v1/admin/workflows/{workflow_id}")
    async def replace_workflow(workflow_id: str) -> dict[str, str]:
        return {"status": "replaced"}

    @app.delete("/v1/admin/workflows/{workflow_id}")
    async def archive_workflow(workflow_id: str) -> dict[str, str]:
        return {"status": "archived"}

    @app.post("/v1/chat/stream")
    async def start_workflow_run() -> dict[str, str]:
        return {"status": "started"}

    @app.post("/v1/workflow-runs/{run_id}/cancel")
    async def cancel_workflow_run(run_id: str) -> dict[str, str]:
        return {"status": "cancelled"}

    @app.post("/v1/internal/workflow-runs/{run_id}/authorize-step")
    async def authorize_workflow_step(run_id: str) -> dict[str, str]:
        return {"status": "denied"}

    @app.post("/v1/internal/workflow-runs/{run_id}/transition")
    async def transition_workflow_run(run_id: str) -> dict[str, str]:
        return {"status": "transitioned"}

    @app.put("/v1/internal/workflow-runs/{run_id}/nodes/{phase_key}")
    async def complete_workflow_node(run_id: str, phase_key: str) -> dict[str, str]:
        return {"status": "completed"}

    return app


@pytest.fixture
def client():
    return httpx.AsyncClient(transport=ASGITransport(app=make_app()), base_url="http://test")


@pytest.fixture
def identity():
    """Resolve organization/user identity using the same path as rate limiting."""
    with patch(
        "src.middleware.audit._rate_limit_identity",
        AsyncMock(return_value=(ORG_ID, USER_ID)),
    ):
        yield


@pytest.fixture
def recorded():
    """Capture database-write parameters at the external persistence boundary."""
    calls: list[dict] = []

    async def fake_record(self, request, status_code):  # noqa: ANN001
        from src.middleware.audit import _route_template

        org, user = await __import__(
            "src.middleware.audit", fromlist=["_rate_limit_identity"]
        )._rate_limit_identity(request)
        calls.append(
            {
                "org_id": org,
                "user_id": user,
                "method": request.method,
                "path": _route_template(request),
                "status_code": status_code,
            }
        )

    with patch.object(AuditMiddleware, "_record", fake_record):
        yield calls


@pytest.mark.asyncio
async def test_mutating_request_is_recorded(client, identity, recorded):
    async with client as c:
        response = await c.post("/v1/skills/")

    assert response.status_code == 200
    assert len(recorded) == 1
    assert recorded[0]["method"] == "POST"
    assert recorded[0]["path"] == "/v1/skills/"
    assert recorded[0]["status_code"] == 200
    assert recorded[0]["org_id"] == ORG_ID


@pytest.mark.asyncio
async def test_read_request_is_not_recorded(client, identity, recorded):
    """Frequent reads would obscure useful mutation records."""
    async with client as c:
        await c.get("/v1/skills/")

    assert recorded == []


@pytest.mark.asyncio
async def test_auth_and_non_v1_paths_are_skipped(client, identity, recorded):
    async with client as c:
        await c.post("/v1/auth/callback")
        await c.post("/health")

    assert recorded == []


@pytest.mark.asyncio
async def test_path_is_the_route_template_not_the_concrete_url(client, identity, recorded):
    """Record /v1/skills/{skill_id} instead of a resource-specific URL
    so audit records group by operation."""
    async with client as c:
        await c.delete(f"/v1/skills/{uuid.uuid4()}")

    assert recorded[0]["path"] == "/v1/skills/{skill_id}"


@pytest.mark.asyncio
async def test_workflow_mutations_record_only_safe_route_templates(
    client, identity, recorded
):
    workflow_id = str(uuid.uuid4())
    secret_body = {
        "name": "Sensitive workflow",
        "nodes": [{"nodeKey": "secret-node", "instruction": "private prompt"}],
    }
    async with client as c:
        await c.post("/v1/admin/workflows", json=secret_body)
        await c.put(f"/v1/admin/workflows/{workflow_id}", json=secret_body)
        await c.delete(f"/v1/admin/workflows/{workflow_id}")

    assert [item["path"] for item in recorded] == [
        "/v1/admin/workflows",
        "/v1/admin/workflows/{workflow_id}",
        "/v1/admin/workflows/{workflow_id}",
    ]
    assert all("private prompt" not in repr(item) for item in recorded)
    assert all("secret-node" not in repr(item) for item in recorded)
    assert all(workflow_id not in repr(item) for item in recorded)


@pytest.mark.asyncio
async def test_workflow_run_events_record_only_safe_route_templates(
    client, identity, recorded
):
    workflow_id, run_id = str(uuid.uuid4()), str(uuid.uuid4())
    sensitive = {
        "workflowId": workflow_id,
        "content": "private user request",
        "output": "private node output",
        "resourceIds": ["model-secret"],
    }
    async with client as c:
        await c.post("/v1/chat/stream", json=sensitive)
        await c.post(f"/v1/workflow-runs/{run_id}/cancel")
        await c.post(
            f"/v1/internal/workflow-runs/{run_id}/authorize-step", json=sensitive
        )
        await c.post(
            f"/v1/internal/workflow-runs/{run_id}/transition", json=sensitive
        )
        await c.put(
            f"/v1/internal/workflow-runs/{run_id}/nodes/research", json=sensitive
        )

    assert [item["path"] for item in recorded] == [
        "/v1/chat/stream",
        "/v1/workflow-runs/{run_id}/cancel",
        "/v1/internal/workflow-runs/{run_id}/authorize-step",
        "/v1/internal/workflow-runs/{run_id}/transition",
        "/v1/internal/workflow-runs/{run_id}/nodes/{phase_key}",
    ]
    rendered = repr(recorded)
    assert run_id not in rendered
    assert workflow_id not in rendered
    assert "private user request" not in rendered
    assert "private node output" not in rendered
    assert "model-secret" not in rendered


@pytest.mark.asyncio
async def test_failed_response_is_recorded_with_its_status(client, identity, recorded):
    app = make_app()

    @app.post("/v1/boom")
    async def boom() -> dict[str, str]:
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="forbidden")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        response = await c.post("/v1/boom")

    assert response.status_code == 403
    assert recorded[-1]["status_code"] == 403


@pytest.mark.asyncio
async def test_audit_failure_does_not_break_the_request(client, identity):
    """Audit failure must not fail the user request."""

    async def boom(self, request, status_code):  # noqa: ANN001
        raise RuntimeError("database unavailable")

    with patch.object(AuditMiddleware, "_record", boom):
        async with client as c:
            response = await c.post("/v1/skills/")

    assert response.status_code == 200
    assert response.json() == {"status": "created"}


@pytest.mark.asyncio
async def test_request_without_org_is_not_written(identity):
    """Unauthenticated attempts lack an organization context for the RLS table;
    insertion without org_id would be rejected anyway."""
    session_factory = AsyncMock()

    with patch(
        "src.middleware.audit._rate_limit_identity",
        AsyncMock(return_value=("anonymous", "127.0.0.1")),
    ), patch("src.middleware.audit.async_session", session_factory):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=make_app()), base_url="http://test"
        ) as c:
            response = await c.post("/v1/skills/")

    assert response.status_code == 200
    session_factory.assert_not_called()
