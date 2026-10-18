from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
MARKER = "workflow-templates-ui-e2e-0123456789abcdef"
ORG, ADMIN, MEMBER, A1, A2, SOURCE, CONVERSATION = (
    f"{i:08x}-1111-4111-8111-111111111111" for i in range(1, 8)
)


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("workflow_loops_e2e")


@pytest.fixture
def prepared(harness, tmp_path, monkeypatch):
    c5 = importlib.import_module("workflow_templates_ui_e2e")
    state = c5.FixtureState(
        marker=MARKER, organization_id=ORG, admin_id=ADMIN, member_id=MEMBER,
        source_agent_ids={"v1": A1, "v2": A2}, source_workflow_id=SOURCE,
        original_capabilities=["chat.send"], original_model_grants=[], prepared=True,
    )
    path = tmp_path / "private/state.json"
    c5.save_state(path, state)
    monkeypatch.setenv("E2E_WORKFLOW_TEMPLATES_UI_STATE", str(path))
    monkeypatch.setenv("E2E_WORKFLOW_TEMPLATES_UI_MARKER", MARKER)
    monkeypatch.setenv("WORKFLOW_ACCEPTANCE_KIND", "loops")
    return harness.Acceptance(harness.Config()), path


def test_standalone_refuses_before_network(harness, monkeypatch):
    monkeypatch.delenv("E2E_WORKFLOW_TEMPLATES_UI_STATE", raising=False)
    with pytest.raises(harness.SmokeFailure):
        harness.Acceptance(harness.Config())


@pytest.mark.parametrize("change", ["marker", "unprepared", "missing_agent"])
def test_invalid_journal_refuses_before_network(harness, prepared, monkeypatch, change):
    _, path = prepared
    if change == "marker":
        monkeypatch.setenv("E2E_WORKFLOW_TEMPLATES_UI_MARKER", MARKER[:-1] + "0")
    else:
        raw = json.loads(path.read_text())
        if change == "unprepared":
            raw["prepared"] = False
        else:
            raw["source_agent_ids"].pop("v2")
        path.write_text(json.dumps(raw))
    with pytest.raises(harness.SmokeFailure):
        harness.Acceptance(harness.Config())


def test_loop_updates_only_journal_source_and_exact_name(prepared, monkeypatch):
    app, _ = prepared
    requests = []
    def request(plane, method, path, token, **kwargs):
        requests.append((plane, method, path, kwargs.get("body")))
        if method == "GET":
            return 200, {"data": {"id": SOURCE, "version": 4}}
        if method == "PUT":
            assert kwargs["body"]["version"] == 4
        if path.endswith("/validate"):
            assert "version" not in kwargs["body"]
        return 200, {"data": {"id": SOURCE}}
    monkeypatch.setattr(app, "request", request)
    app.create_workflow("loop", app.loop_body("loop"))
    app.create_workflow("cancel-loop", app.cancel_loop_body("cancel-loop"))
    updates = [r for r in requests if r[1] == "PUT"]
    assert len(updates) == 2
    assert all(r[2] == f"/v1/admin/workflows/{SOURCE}" for r in updates)
    assert all(r[3]["name"] == f"{MARKER}-source" for r in updates)
    assert all(r[3]["supervisorAgentDefinitionId"] == A1 for r in updates)
    assert updates[0][3]["nodes"][0]["agentDefinitionId"] == A1
    assert updates[0][3]["nodes"][1]["agentDefinitionId"] == A2
    assert updates[0][3]["edges"][1]["maxIterations"] == 3
    assert updates[1][3]["edges"][1]["maxIterations"] == 5
    assert "MULTI_AGENT_BARRIER:loops-cancel" in updates[1][3]["nodes"][0]["instruction"]
    assert not any(r[1] == "POST" and r[2] == "/v1/admin/workflows" for r in requests)


def test_previous_iteration_context_workflow_shape(prepared):
    app, _ = prepared

    body = app.previous_iteration_context_body("previous-context")

    assert [node["nodeKey"] for node in body["nodes"]] == ["draft", "review"]
    assert "MULTI_AGENT_LOOP_CONTEXT:draft" in body["nodes"][0]["instruction"]
    assert "MULTI_AGENT_LOOP_CONTEXT:review" in body["nodes"][1]["instruction"]
    assert body["edges"] == [
        {"sourceNodeKey": "draft", "targetNodeKey": "review", "condition": "on_success"},
        {
            "sourceNodeKey": "review", "targetNodeKey": "draft",
            "condition": "on_success", "isLoopBack": True, "maxIterations": 2,
        },
    ]


def test_run_orders_previous_context_before_cancellation(prepared, monkeypatch):
    app, _ = prepared
    calls = []
    monkeypatch.setattr(app, "acquire_tokens", lambda: calls.append("tokens"))
    monkeypatch.setattr(
        app, "run_iteration_limit_and_fallthrough", lambda: calls.append("limit")
    )
    monkeypatch.setattr(
        app, "run_previous_iteration_context", lambda: calls.append("context")
    )
    monkeypatch.setattr(
        app, "run_cancellation_mid_loop", lambda: calls.append("cancel")
    )

    app.run()

    assert calls == ["tokens", "limit", "context", "cancel"]


def test_run_reports_safe_phase_for_unexpected_exception(
    prepared, harness, monkeypatch
):
    app, _ = prepared
    monkeypatch.setattr(app, "acquire_tokens", lambda: None)
    monkeypatch.setattr(app, "run_iteration_limit_and_fallthrough", lambda: None)
    monkeypatch.setattr(app, "run_previous_iteration_context", lambda: None)
    monkeypatch.setattr(
        app, "run_cancellation_mid_loop",
        lambda: (_ for _ in ()).throw(KeyError("private-value")),
    )

    with pytest.raises(harness.SmokeFailure, match=r"cancellation failed \(KeyError\)"):
        app.run()


def test_member_identity_and_grants_are_checked_before_scenarios(prepared, monkeypatch):
    app, _ = prepared
    calls = []
    def token(username, password, audience):
        return f"{username}-{audience}"
    def request(plane, method, path, bearer, **kwargs):
        calls.append((plane, method, path, bearer, kwargs.get("body")))
        if path == "/v1/auth/me":
            return 200, {"data": {"user_id": MEMBER if bearer.startswith("user-") else ADMIN, "org_id": ORG}}
        return 201, {}
    monkeypatch.setattr(app, "token", token)
    monkeypatch.setattr(app, "request", request)
    app.acquire_tokens()
    grants = [r[4] for r in calls if r[2] == "/v1/admin/authorization/grants"]
    assert {(g["resourceType"], g["resourceId"]) for g in grants} == {
        ("model", f"{MARKER}-model"), ("agent_definition", A1),
        ("agent_definition", A2), ("workflow", SOURCE),
    }
    assert all(g["principalId"] == MEMBER and g["permission"] == "read" for g in grants)
    assert any(r[3] == "user-business-ui" for r in calls)


def test_wrong_live_identity_refuses_grants(prepared, harness, monkeypatch):
    app, _ = prepared
    writes = []
    monkeypatch.setattr(app, "token", lambda *args: "identity-token")
    def request(plane, method, path, bearer, **kwargs):
        if method != "GET":
            writes.append(path)
        return 200, {"data": {"user_id": ADMIN, "org_id": ORG}}
    monkeypatch.setattr(app, "request", request)
    with pytest.raises(harness.SmokeFailure):
        app.acquire_tokens()
    assert writes == []


@pytest.mark.parametrize("record_failure", [False, True])
def test_conversation_recorded_before_any_turn(prepared, monkeypatch, record_failure):
    app, path = prepared
    c5 = importlib.import_module("workflow_templates_ui_e2e")
    api_calls = []
    app.state.member_business_token = "member-business"
    def request(plane, method, route, token, **kwargs):
        api_calls.append((method, route))
        assert token == "member-business"
        assert kwargs["body"]["title"] == f"{MARKER}-run-v1"
        return 201, {"data": {"id": CONVERSATION}}
    monkeypatch.setattr(app, "request", request)
    def inventory(org, model):
        assert org == ORG and model == f"{MARKER}-model"
        if record_failure:
            raise c5.FixtureError("inventory unavailable")
        return {
            "agent": [dict(id=id, org_id=ORG, created_by=ADMIN, name=f"{MARKER}-source-{version}") for version, id in (("v1", A1), ("v2", A2))],
            "workflow": [dict(id=SOURCE, org_id=ORG, created_by=ADMIN, name=f"{MARKER}-source", source_template_id=None, agent_ids=[A1, A2])],
            "conversation": [dict(id=CONVERSATION, org_id=ORG, user_id=MEMBER, title=f"{MARKER}-run-v1", has_attachments=False)],
            "template": [], "run": [],
        }
    monkeypatch.setattr(app.recovery.database, "inventory", inventory)
    if record_failure:
        with pytest.raises(c5.FixtureError):
            app.create_conversation()
        assert app.state.conversation_ids == []
        assert path.exists()
    else:
        assert app.create_conversation() == CONVERSATION
        assert c5.load_state(path).conversation_ids == [CONVERSATION]
    assert api_calls == [("POST", "/v1/conversations")]


def test_shared_endpoint_and_credentials_overrides(harness, monkeypatch):
    monkeypatch.setenv("E2E_BUSINESS_BACKEND_URL", "https://business.example/")
    monkeypatch.setenv("E2E_ADMIN_BACKEND_URL", "https://admin.example/")
    monkeypatch.setenv("E2E_KEYCLOAK_URL", "https://identity.example/")
    monkeypatch.setenv("E2E_POSTGRES_CONTAINER", "owned-postgres")
    monkeypatch.setenv("E2E_MEMBER_USERNAME", "fixture-member")
    config = harness.Config()
    assert (config.business_url, config.admin_url, config.keycloak_url) == (
        "https://business.example", "https://admin.example", "https://identity.example",
    )
    assert config.postgres_container == "owned-postgres"
    assert config.member_username == "fixture-member"


@pytest.mark.parametrize("override", [
    "BASE_URL", "ADMIN_BASE_URL", "KEYCLOAK_URL", "KEYCLOAK_REALM",
    "KEYCLOAK_CLIENT_ID", "ADMIN_KEYCLOAK_CLIENT_ID", "WORKFLOW_LOOPS_E2E_MODEL",
    "WORKFLOW_LOOPS_E2E_DB_CONTAINER", "WORKFLOW_LOOPS_E2E_COMPOSE_PROJECT",
    "WORKFLOW_LOOPS_E2E_RATE_LIMIT_PER_MINUTE",
    "WORKFLOW_LOOPS_E2E_RATE_LIMIT_PER_MINUTE_STREAM",
])
def test_launcher_rejects_legacy_overrides_before_stack_access(override):
    result = subprocess.run(
        ["bash", str(ROOT / "smoke-test-workflow-loops.sh")],
        env={"PATH": os.environ["PATH"], override: "legacy-value"},
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert override in result.stderr
    assert "E2E_" in result.stderr


@pytest.mark.parametrize("args,environment", [(["unexpected"], {}), ([], {"WORKFLOW_LOOPS_E2E_MANAGE_COMPOSE": "0"})])
def test_launcher_rejects_unsupported_execution_before_stack_access(args, environment):
    result = subprocess.run(
        ["bash", str(ROOT / "smoke-test-workflow-loops.sh"), *args],
        env={"PATH": os.environ["PATH"], **environment}, capture_output=True, text=True,
    )
    assert result.returncode == 2
