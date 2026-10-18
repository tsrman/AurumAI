from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import select

from src.core.auth import UserInfo, get_current_user
from src.core.config import settings
from src.core.dependencies import get_org_db_session
from src.core.keycloak import keycloak_client
from src.middleware.error_handler import setup_exception_handlers
from src.models.agent_definition import AgentDefinition
from src.models.workflow_definition import WorkflowDefinition, WorkflowNode
from src.models.workflow_template import WorkflowTemplate
from src.services.model_catalog import GatewayCatalogSnapshot, GatewayCatalogStatus
from src.services.workflow_template import WorkflowTemplateService


async def catalog():
    return GatewayCatalogSnapshot(GatewayCatalogStatus.AVAILABLE, ({"id": "provider/model-v1"},))


@pytest.fixture
def app_for():
    def build(db, org_id, user_id, *, role="org_admin", authenticate=True, service=None):
        from src.routers import workflow_templates_admin

        app = FastAPI()
        setup_exception_handlers(app)
        app.include_router(workflow_templates_admin.router, prefix="/v1/admin")

        async def session_override():
            yield db

        if authenticate:
            app.dependency_overrides[get_current_user] = lambda: UserInfo(
                user_id=str(user_id), org_id=str(org_id), role=role
            )
        app.dependency_overrides[get_org_db_session] = session_override
        app.dependency_overrides[workflow_templates_admin.get_workflow_template_service] = (
            lambda: service or WorkflowTemplateService(catalog=catalog)
        )
        return app

    return build


def client_for(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


def capture_body(source_id, **extra):
    return {
        "sourceWorkflowId": str(source_id),
        "sourceWorkflowVersion": 1,
        "name": "Saved analysis",
        **extra,
    }


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    for secret in ("systemPrompt", "synthetic-secret", "INSERT INTO", "constraint"):
        assert secret not in response.text


@pytest.fixture
async def saved_two_node_template(committed_template):
    fixture = committed_template
    async with fixture.sessions() as db:
        template = await db.get(WorkflowTemplate, fixture.template_id)
        snapshot = deepcopy(template.snapshot)
        snapshot["nodes"].append({**snapshot["nodes"][0], "nodeKey": "review"})
        template.snapshot = snapshot
        db.add(WorkflowNode(
            org_id=fixture.actor.org_id,
            workflow_definition_id=fixture.source_id,
            agent_definition_id=fixture.agent_id,
            node_key="review",
            display_name="Research",
            instruction="Find evidence",
            requires_confirmation=True,
        ))
        await db.commit()
    return fixture


@pytest.mark.parametrize("limit", ["WORKFLOW_MAX_NODES", "WORKFLOW_MAX_PARALLELISM"])
@pytest.mark.parametrize("operation", ["detail", "archive"])
async def test_reduced_limits_keep_saved_templates_readable_and_archivable(
    app_for, saved_two_node_template, monkeypatch, limit, operation
):
    fixture = saved_two_node_template
    monkeypatch.setattr(settings, limit, 1)
    path = f"/v1/admin/workflow-templates/{fixture.template_id}"
    async with fixture.sessions() as db:
        async with client_for(app_for(db, fixture.actor.org_id, fixture.actor.user_id)) as client:
            response = (
                await client.get(path) if operation == "detail"
                else await client.post(path + "/archive", json={"version": 1})
            )
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            assert len(data["snapshot"]["nodes"]) == 2
            assert data["snapshot"]["maxParallelism"] == 2
            assert (await client.get(path)).status_code == 200
    async with fixture.sessions() as observer:
        saved = await observer.get(WorkflowTemplate, fixture.template_id)
        assert saved.status == ("archived" if operation == "archive" else "active")
        assert saved.version == (2 if operation == "archive" else 1)
        assert saved.snapshot == data["snapshot"]


@pytest.mark.parametrize("limit", ["WORKFLOW_MAX_NODES", "WORKFLOW_MAX_PARALLELISM"])
@pytest.mark.parametrize("operation", ["capture", "replace", "instantiate"])
async def test_reduced_limits_reject_template_writes_without_partial_mutations(
    app_for, saved_two_node_template, monkeypatch, limit, operation
):
    fixture = saved_two_node_template
    async with fixture.sessions() as observer:
        saved = await observer.get(WorkflowTemplate, fixture.template_id)
        original_snapshot = deepcopy(saved.snapshot)
        original_updated_at = saved.updated_at
    monkeypatch.setattr(settings, limit, 1)
    path = f"/v1/admin/workflow-templates/{fixture.template_id}"
    async with fixture.sessions() as db:
        async with client_for(app_for(db, fixture.actor.org_id, fixture.actor.user_id)) as client:
            if operation == "capture":
                response = await client.post(
                    "/v1/admin/workflow-templates", json=capture_body(fixture.source_id)
                )
            elif operation == "replace":
                response = await client.put(
                    path, json=capture_body(fixture.source_id, version=1, name="Replacement")
                )
            else:
                response = await client.post(
                    path + "/instantiate", json={"name": "Rejected copy", "version": 1}
                )
    assert_error(response, 422, "snapshot_invalid")
    async with fixture.sessions() as observer:
        for model, expected_id in (
            (WorkflowTemplate, fixture.template_id),
            (WorkflowDefinition, fixture.source_id),
            (AgentDefinition, fixture.agent_id),
        ):
            assert (await observer.scalars(
                select(model.id).where(model.org_id == fixture.actor.org_id)
            )).all() == [expected_id]
        saved = await observer.get(WorkflowTemplate, fixture.template_id)
        assert (saved.name, saved.status, saved.version) == ("Template", "active", 1)
        assert saved.snapshot == original_snapshot
        assert saved.updated_at == original_updated_at
        source = await observer.get(WorkflowDefinition, fixture.source_id)
        assert (source.name, source.version, source.max_parallelism) == ("Source", 1, 2)
        assert len((await observer.scalars(select(WorkflowNode).where(
            WorkflowNode.workflow_definition_id == fixture.source_id
        ))).all()) == 2


@pytest.mark.parametrize(
    "method,suffix",
    [
        ("GET", ""),
        ("POST", ""),
        ("GET", "/{id}"),
        ("PUT", "/{id}"),
        ("POST", "/{id}/instantiate"),
        ("POST", "/{id}/archive"),
    ],
)
async def test_every_method_denies_members(app_for, db, test_org, test_user, method, suffix):
    async with client_for(app_for(db, test_org.id, test_user.id, role="member")) as client:
        response = await client.request(
            method, "/v1/admin/workflow-templates" + suffix.format(id=uuid4()), json={}
        )
    assert response.status_code == 403


async def test_capture_get_list_instantiate_provenance_and_archive(
    app_for, db, test_org, test_user, template_source
):
    async with client_for(app_for(db, test_org.id, test_user.id)) as client:
        created = await client.post(
            "/v1/admin/workflow-templates", json=capture_body(template_source.id)
        )
        assert created.status_code == 201, created.text
        item = created.json()["data"]
        path = f"/v1/admin/workflow-templates/{item['id']}"
        assert item["version"] == 1
        assert item["orgId"] == str(test_org.id)
        assert item["createdBy"] == str(test_user.id)
        assert item["snapshot"]["nodes"][0]["requiresConfirmation"] is True
        fetched = await client.get(path)
        assert fetched.json()["data"] == item
        listing = await client.get("/v1/admin/workflow-templates")
        assert set(listing.json()["data"][0]) == {
            "id",
            "name",
            "description",
            "status",
            "version",
            "createdAt",
            "updatedAt",
        }
        copied = await client.post(path + "/instantiate", json={"name": "Copy", "version": 1})
        assert copied.status_code == 201, copied.text
        data = copied.json()["data"]
        assert data["workflow"]["sourceTemplateId"] == item["id"]
        assert data["workflow"]["sourceTemplateVersion"] == 1
        assert data["workflow"]["autoRoutable"] is False
        assert data["workflow"]["nodes"][0]["requiresConfirmation"] is True
        assert data["agentIds"] == [data["workflow"]["supervisorAgentDefinitionId"]]
        archived = await client.post(path + "/archive", json={"version": 1})
        assert archived.status_code == 200, archived.text
        assert archived.json()["data"]["status"] == "archived"
        assert archived.json()["data"]["version"] == 2
        assert (await client.get("/v1/admin/workflow-templates")).json()["data"] == []
        assert (
            len(
                (await client.get("/v1/admin/workflow-templates?includeArchived=true")).json()[
                    "data"
                ]
            )
            == 1
        )
        assert_error(
            await client.post(path + "/instantiate", json={"name": "Late", "version": 2}),
            409,
            "template_archived",
        )
        assert (await client.post(path + "/archive", json={"version": 2})).status_code == 200


async def test_replace_then_stale_put_has_public_version_conflict(
    app_for, db, test_org, test_user, template_source
):
    body = capture_body(template_source.id)
    async with client_for(app_for(db, test_org.id, test_user.id)) as client:
        created = await client.post("/v1/admin/workflow-templates", json=body)
        path = f"/v1/admin/workflow-templates/{created.json()['data']['id']}"
        updated = await client.put(path, json={**body, "version": 1, "description": "New"})
        assert updated.status_code == 200, updated.text
        assert updated.json()["data"]["version"] == 2
        assert_error(await client.put(path, json={**body, "version": 1}), 409, "version_conflict")


@pytest.mark.parametrize(
    "suffix,method", [("", "GET"), ("", "PUT"), ("/instantiate", "POST"), ("/archive", "POST")]
)
async def test_foreign_template_is_not_found(
    app_for, db, test_org, test_user, template_source, suffix, method
):
    body = capture_body(template_source.id)
    async with client_for(app_for(db, test_org.id, test_user.id)) as client:
        created = await client.post("/v1/admin/workflow-templates", json=body)
    foreign_app = app_for(db, uuid4(), test_user.id)
    payload = {**body, "version": 1} if method == "PUT" else {"version": 1}
    if suffix == "/instantiate":
        payload["name"] = "Copy"
    async with client_for(foreign_app) as client:
        response = await client.request(
            method,
            f"/v1/admin/workflow-templates/{created.json()['data']['id']}{suffix}",
            json=payload,
        )
    assert_error(response, 404, "template_not_found")


@pytest.mark.parametrize(
    "extra,code",
    [
        ({"sourceWorkflowVersion": 2}, "source_version_conflict"),
        ({"sourceWorkflowId": str(uuid4())}, "source_not_found"),
    ],
)
async def test_source_failures_are_sanitized(
    app_for, db, test_org, test_user, template_source, extra, code
):
    async with client_for(app_for(db, test_org.id, test_user.id)) as client:
        response = await client.post(
            "/v1/admin/workflow-templates", json=capture_body(template_source.id, **extra)
        )
    assert_error(response, 404 if code == "source_not_found" else 409, code)


@pytest.mark.parametrize(
    "body",
    [
        '{"synthetic-secret":',
        '{"sourceWorkflowId":"synthetic-secret","sourceWorkflowVersion":1,"name":"N"}',
        '{"systemPrompt":"synthetic-secret"}',
    ],
)
async def test_invalid_requests_do_not_echo_input(app_for, db, test_org, test_user, body):
    async with client_for(app_for(db, test_org.id, test_user.id)) as client:
        response = await client.post(
            "/v1/admin/workflow-templates",
            content=body,
            headers={"Content-Type": "application/json"},
        )
    assert_error(response, 422, "snapshot_invalid")


async def test_template_validation_does_not_change_unrelated_routes(
    app_for, db, test_org, test_user
):
    app = app_for(db, test_org.id, test_user.id)

    class OtherRequest(BaseModel):
        count: int

    @app.post("/other")
    async def other(data: OtherRequest):
        return data

    async with client_for(app) as client:
        response = await client.post("/other", json={"count": "invalid"})
    assert response.status_code == 400


@pytest.mark.parametrize(
    "audience,expired,session_token,expected",
    [
        ("admin-ui", False, False, 200),
        ("business-ui", False, False, 401),
        ("admin-ui", True, False, 401),
        ("admin-ui", False, True, 401),
    ],
)
async def test_real_signed_auth_boundary(
    app_for, db, test_org, test_user, monkeypatch, audience, expired, session_token, expected
):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(settings, "KEYCLOAK_CLIENT_ID", "admin-ui")

    async def signing_key(kid):
        assert kid == "c5-test"
        return private.public_key()

    monkeypatch.setattr(keycloak_client, "get_signing_key", signing_key)
    claims = {
        "sub": str(test_user.id),
        "org_id": str(test_org.id),
        "role": "org_admin",
        "iss": settings.KEYCLOAK_ISSUER
        or f"{settings.KEYCLOAK_URL}/realms/{settings.KEYCLOAK_REALM}",
        "exp": datetime.now(UTC) + timedelta(seconds=-60 if expired else 300),
        "aud": audience,
    }
    if session_token:
        claims.update(type="session", conversation_id=str(uuid4()))
        token = jwt.encode(claims, settings.SECRET_KEY, algorithm="HS256")
    else:
        token = jwt.encode(claims, private, algorithm="RS256", headers={"kid": "c5-test"})
    async with client_for(app_for(db, test_org.id, test_user.id, authenticate=False)) as client:
        response = await client.get(
            "/v1/admin/workflow-templates", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == expected, response.text


@pytest.mark.parametrize("fault", ["after_agents", "duplicate_name", "presentation"])
async def test_failed_http_instantiate_rolls_back_flushed_agents(
    app_for, committed_template, monkeypatch, fault
):
    fixture = committed_template
    service = WorkflowTemplateService(catalog=catalog)
    if fault == "after_agents":
        original = service.create_agents

        async def fail_after_flush(*args):
            await original(*args)
            raise RuntimeError("synthetic-secret")

        monkeypatch.setattr(service, "create_agents", fail_after_flush)
    if fault == "presentation":
        from src.routers import workflow_templates_admin

        def broken_presentation(*args):
            raise RuntimeError("synthetic-secret")

        monkeypatch.setattr(
            workflow_templates_admin, "workflow_definition_response", broken_presentation
        )
    async with fixture.sessions() as db:
        app = app_for(db, fixture.actor.org_id, fixture.actor.user_id, service=service)
        async with client_for(app) as client:
            response = await client.post(
                f"/v1/admin/workflow-templates/{fixture.template_id}/instantiate",
                json={
                    "version": 1,
                    "name": "Source" if fault == "duplicate_name" else "Failed copy",
                },
            )
    assert_error(
        response,
        409 if fault == "duplicate_name" else 500,
        "name_conflict" if fault == "duplicate_name" else "INTERNAL_ERROR",
    )
    async with fixture.sessions() as observer:
        agents = (
            await observer.scalars(
                select(AgentDefinition.id).where(AgentDefinition.org_id == fixture.actor.org_id)
            )
        ).all()
        workflows = (
            await observer.scalars(
                select(WorkflowDefinition.id).where(
                    WorkflowDefinition.org_id == fixture.actor.org_id
                )
            )
        ).all()
    assert agents == [fixture.agent_id]
    assert workflows == [fixture.source_id]


async def test_unknown_integrity_constraint_is_sanitized_internal_error(
    app_for, committed_template, monkeypatch
):
    fixture = committed_template
    service = WorkflowTemplateService(catalog=catalog)
    original = service.capture

    async def violate_check(*args):
        item = await original(*args)
        item.version = 0
        await args[0].flush()
        return item

    monkeypatch.setattr(service, "capture", violate_check)
    async with fixture.sessions() as db:
        async with client_for(
            app_for(db, fixture.actor.org_id, fixture.actor.user_id, service=service)
        ) as client:
            response = await client.post(
                "/v1/admin/workflow-templates", json=capture_body(fixture.source_id)
            )
    assert_error(response, 500, "INTERNAL_ERROR")
    async with fixture.sessions() as observer:
        assert (
            await observer.scalars(
                select(WorkflowTemplate.id).where(WorkflowTemplate.org_id == fixture.actor.org_id)
            )
        ).all() == [fixture.template_id]


@pytest.mark.parametrize("operation", ["detail", "instantiate"])
async def test_invalid_stored_snapshot_is_sanitized(app_for, committed_template, operation):
    fixture = committed_template
    async with fixture.sessions() as db:
        item = await db.get(WorkflowTemplate, fixture.template_id)
        item.snapshot = {"systemPrompt": "synthetic-secret"}
        await db.commit()
        async with client_for(app_for(db, fixture.actor.org_id, fixture.actor.user_id)) as client:
            path = f"/v1/admin/workflow-templates/{fixture.template_id}"
            response = (
                await client.get(path)
                if operation == "detail"
                else await client.post(path + "/instantiate", json={"name": "Copy", "version": 1})
            )
    assert_error(response, 422, "snapshot_invalid")


async def test_duplicate_template_name_is_safe_conflict(app_for, committed_template):
    fixture = committed_template
    async with fixture.sessions() as db:
        async with client_for(app_for(db, fixture.actor.org_id, fixture.actor.user_id)) as client:
            response = await client.post(
                "/v1/admin/workflow-templates",
                json=capture_body(fixture.source_id, name="Template"),
            )
    assert_error(response, 409, "name_conflict")


@pytest.mark.parametrize(
    "available,code,status",
    [(False, "catalog_unavailable", 503), (True, "dependency_unavailable", 404)],
)
async def test_catalog_failures_are_sanitized(app_for, committed_template, available, code, status):
    fixture = committed_template

    async def failed_catalog():
        return GatewayCatalogSnapshot(
            GatewayCatalogStatus.AVAILABLE if available else GatewayCatalogStatus.UNAVAILABLE, ()
        )

    async with fixture.sessions() as db:
        async with client_for(
            app_for(
                db,
                fixture.actor.org_id,
                fixture.actor.user_id,
                service=WorkflowTemplateService(catalog=failed_catalog),
            )
        ) as client:
            response = await client.post(
                f"/v1/admin/workflow-templates/{fixture.template_id}/instantiate",
                json={"name": "Copy", "version": 1},
            )
    assert_error(response, status, code)


@pytest.mark.parametrize("operation", ["instantiate", "archive"])
async def test_stale_mutation_versions_are_safe_conflicts(app_for, committed_template, operation):
    fixture = committed_template
    body = {"version": 2}
    if operation == "instantiate":
        body["name"] = "Copy"
    async with fixture.sessions() as db:
        async with client_for(app_for(db, fixture.actor.org_id, fixture.actor.user_id)) as client:
            response = await client.post(
                f"/v1/admin/workflow-templates/{fixture.template_id}/{operation}", json=body
            )
    assert_error(response, 409, "version_conflict")


async def test_archived_source_is_rejected(app_for, committed_template):
    fixture = committed_template
    async with fixture.sessions() as db:
        source = await db.get(WorkflowDefinition, fixture.source_id)
        source.status = "archived"
        await db.commit()
        async with client_for(app_for(db, fixture.actor.org_id, fixture.actor.user_id)) as client:
            response = await client.post(
                "/v1/admin/workflow-templates", json=capture_body(fixture.source_id)
            )
    assert_error(response, 409, "source_archived")


async def test_foreign_source_and_extra_snapshot_fields_are_rejected(app_for, committed_template):
    fixture = committed_template
    async with fixture.sessions() as db:
        async with client_for(app_for(db, uuid4(), fixture.actor.user_id)) as client:
            foreign = await client.post(
                "/v1/admin/workflow-templates", json=capture_body(fixture.source_id)
            )
        async with client_for(app_for(db, fixture.actor.org_id, fixture.actor.user_id)) as client:
            extra = await client.post(
                "/v1/admin/workflow-templates",
                json=capture_body(fixture.source_id, snapshot={"systemPrompt": "synthetic-secret"}),
            )
    assert_error(foreign, 404, "source_not_found")
    assert_error(extra, 422, "snapshot_invalid")
