from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).parents[2]
HARNESS_PATH = ROOT / "scripts" / "authorization_ui_e2e.py"
MARKER = "authorization-ui-e2e-0123456789"
ORG_ID = "11111111-1111-4111-8111-111111111111"
GROUP_ID = "22222222-2222-4222-8222-222222222222"
ADMIN_ID = "33333333-3333-4333-8333-333333333333"
USER_ID = "44444444-4444-4444-8444-444444444444"
USER2_ID = "55555555-5555-4555-8555-555555555555"
SKILL_ID = "66666666-6666-4666-8666-666666666666"
KNOWLEDGE_BASE_ID = "77777777-7777-4777-8777-777777777777"
AGENT_ID = "88888888-8888-4888-8888-888888888888"
WORKFLOW_ID = "99999999-9999-4999-8999-999999999999"
CONVERSATION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WORKFLOW_RUN_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _load_harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "authorization_ui_e2e_contract", HARNESS_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_harness = _load_harness()
Config = _harness.Config
Database = _harness.Database
DockerRunnerCleaner = _harness.DockerRunnerCleaner
FixtureError = _harness.FixtureError
FixtureState = _harness.FixtureState
FixtureController = _harness.FixtureController
OwnedResource = _harness.OwnedResource
load_state = _harness.load_state
primary_exit_code = _harness.primary_exit_code
save_state = _harness.save_state
sync_identities = _harness.sync_identities
validate_owned_state = _harness.validate_owned_state


def owned_state() -> FixtureState:
    return FixtureState(
        marker=MARKER,
        model_id=f"{MARKER}-model",
        organization_id=ORG_ID,
        organization_label="Example LLC",
        analysts_group_id=GROUP_ID,
        admin_id=ADMIN_ID,
        user_id=USER_ID,
        user2_id=USER2_ID,
        original_organization_capabilities=["chat.send"],
        original_analysts_capabilities=[],
        original_model_grants=[],
        skill=OwnedResource(SKILL_ID, f"{MARKER}-skill", []),
        knowledge_base=OwnedResource(
            KNOWLEDGE_BASE_ID, f"{MARKER}-knowledge", []
        ),
        agent=OwnedResource(AGENT_ID, f"{MARKER}-agent", []),
        workflow=OwnedResource(WORKFLOW_ID, f"{MARKER}-workflow", []),
        conversation_ids=[CONVERSATION_ID],
        workflow_run_ids=[WORKFLOW_RUN_ID],
        prepared=True,
    )


def test_private_state_round_trip_is_strict_and_secret_free(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = owned_state()

    save_state(path, state)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_state(path) == state
    raw = path.read_text(encoding="utf-8").lower()
    for forbidden in (
        "access_token",
        "refresh_token",
        "password",
        "app_secret_key",
        "cookie",
    ):
        assert forbidden not in raw

    document = json.loads(raw)
    document["unexpected"] = "value"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(FixtureError, match="unknown state field"):
        load_state(path)


def test_state_rejects_unknown_owned_resource_fields(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(path, owned_state())
    document = json.loads(path.read_text(encoding="utf-8"))
    document["skill"]["unexpected"] = "value"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(FixtureError, match="unknown skill field"):
        load_state(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("marker", "authorization-ui-e2e-short"),
        ("organization_id", "not-a-uuid"),
        ("skill_name", "foreign-skill"),
        ("model_id", "gpt-4o-mini"),
    ],
)
def test_owned_state_rejects_unscoped_values(field: str, value: str) -> None:
    state = owned_state()
    if field == "skill_name":
        state.skill.name = value
    else:
        setattr(state, field, value)

    with pytest.raises(FixtureError):
        validate_owned_state(state, require_resources=True)


def test_database_uses_argument_array_and_fails_without_leaking_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, str(kwargs["input"])))
        return subprocess.CompletedProcess(command, 1, "", "secret database error")

    monkeypatch.setattr(subprocess, "run", run)
    database = Database(Config())

    with pytest.raises(FixtureError, match="database operation failed") as failure:
        database.query_values("SELECT 1")

    assert "secret" not in str(failure.value)
    assert calls == [
        (
            [
                "docker",
                "exec",
                "-i",
                "aurum-ai-postgres",
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-At",
                "-U",
                "postgres",
                "-d",
                "aurum_ai",
            ],
            "SELECT 1",
        )
    ]


def test_runner_cleanup_requires_owned_labels_and_removes_inspected_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:2] == ["docker", "inspect"]:
            payload = [
                {
                    "Id": "sha256:owned-runner",
                    "Config": {
                        "Labels": {
                            "aurum-ai.component": "conversation-runner"
                        },
                        "Env": [f"CONVERSATION_ID={CONVERSATION_ID}"],
                    },
                }
            ]
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)

    DockerRunnerCleaner().remove_owned(CONVERSATION_ID)

    assert calls[-1] == ["docker", "rm", "-f", "sha256:owned-runner"]


@pytest.mark.parametrize(
    "config",
    [
        {
            "Labels": {"aurum-ai.component": "foreign"},
            "Env": [f"CONVERSATION_ID={CONVERSATION_ID}"],
        },
        {
            "Labels": {"aurum-ai.component": "conversation-runner"},
            "Env": ["CONVERSATION_ID=cccccccc-cccc-4ccc-8ccc-cccccccccccc"],
        },
    ],
)
def test_runner_cleanup_refuses_foreign_container(
    config: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps([{"Id": "sha256:foreign", "Config": config}]),
            "",
        )

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(FixtureError, match="runner ownership mismatch"):
        DockerRunnerCleaner().remove_owned(CONVERSATION_ID)


@pytest.mark.parametrize(
    "stderr",
    [
        "Error: No such object: runner-owned",
        "error: no such object: runner-owned",
    ],
)
def test_missing_runner_is_already_clean(
    stderr: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 1, "", stderr
        ),
    )

    DockerRunnerCleaner().remove_owned(CONVERSATION_ID)
    DockerRunnerCleaner().assert_absent(CONVERSATION_ID)


@pytest.mark.parametrize("operation", ["remove_owned", "assert_absent"])
def test_runner_cleanup_fails_closed_when_docker_inspect_fails(
    operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 1, "", "Cannot connect to the Docker daemon"
        ),
    )

    with pytest.raises(FixtureError, match="runner inspection failed"):
        getattr(DockerRunnerCleaner(), operation)(CONVERSATION_ID)


def test_primary_failure_wins_over_cleanup_failure() -> None:
    assert primary_exit_code(7, 9) == 7
    assert primary_exit_code(0, 9) == 9
    assert primary_exit_code(0, 0) == 0


class RecordingApi:
    def __init__(
        self,
        *,
        organization_capabilities: list[str] | None = None,
        analysts_capabilities: list[str] | None = None,
        model_grants: list[dict[str, str | None]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, str, object]] = []
        self.organization_capabilities = organization_capabilities or ["chat.send"]
        self.analysts_capabilities = analysts_capabilities or []
        self.model_grants = model_grants or []

    def token(self, username: str, password: str, client_id: str) -> str:
        assert password == username
        return f"{client_id}:{username}"

    def request(
        self,
        plane: str,
        method: str,
        path: str,
        _token: str,
        *,
        body: dict[str, object] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> object:
        del expected
        self.calls.append((plane, method, path, body))
        if path == "/v1/auth/me":
            return {"data": {}}
        if path == "/v1/admin/authorization/subjects":
            return {
                "data": [
                    {
                        "principalType": "organization",
                        "principalId": ORG_ID,
                        "label": "Example LLC",
                    },
                    {
                        "principalType": "group",
                        "principalId": GROUP_ID,
                        "label": "analysts",
                    },
                    {
                        "principalType": "user",
                        "principalId": ADMIN_ID,
                        "label": "Admin User",
                    },
                    {
                        "principalType": "user",
                        "principalId": USER_ID,
                        "label": "Regular User",
                    },
                    {
                        "principalType": "user",
                        "principalId": USER2_ID,
                        "label": "Second User",
                    },
                ]
            }
        if path == "/v1/admin/authorization/capabilities/organization":
            return {"data": {"capabilities": self.organization_capabilities}}
        if path == f"/v1/admin/authorization/capabilities/groups/{GROUP_ID}":
            return {"data": {"capabilities": self.analysts_capabilities}}
        if path.startswith(
            "/v1/admin/authorization/grants?resourceType=model"
        ):
            return {"data": self.model_grants}
        if path.startswith("/v1/admin/authorization/grants?"):
            return {"data": []}
        created = {
            ("POST", "/v1/skills/"): {"id": SKILL_ID},
            ("POST", "/v1/knowledge-bases"): {"id": KNOWLEDGE_BASE_ID},
            ("POST", "/v1/agent-definitions"): {"id": AGENT_ID},
            ("POST", "/v1/admin/workflows"): {
                "id": WORKFLOW_ID,
                "status": "active",
            },
        }
        if (method, path) in created:
            return {"data": created[(method, path)]}
        raise AssertionError((plane, method, path, body))

    def body_for(self, method: str, path: str) -> dict[str, object]:
        bodies = [
            body
            for _plane, called_method, called_path, body in self.calls
            if called_method == method and called_path == path
        ]
        assert len(bodies) == 1 and isinstance(bodies[0], dict)
        return bodies[0]


class RecordingDatabase:
    def __init__(
        self, *, membership: list[str] | None = None, collision: bool = False
    ) -> None:
        self.membership = [USER_ID] if membership is None else membership
        self.collision = collision

    def query_values(self, sql: str) -> list[str]:
        if "FROM user_groups" in sql:
            return self.membership
        return ["collision"] if self.collision else []

    def execute(self, _sql: str) -> None:
        raise AssertionError("prepare must not execute SQL")


def fixture_controller(
    tmp_path: Path,
    *,
    api: object | None = None,
    database: object | None = None,
    runner_cleaner: object | None = None,
) -> FixtureController:
    return FixtureController(
        Config(model_id=f"{MARKER}-model"),
        state_path=tmp_path / "state.json",
        marker=MARKER,
        api=api or RecordingApi(),
        database=database or RecordingDatabase(),
        runner_cleaner=runner_cleaner or object(),
    )


def test_sync_and_prepare_create_the_exact_dependency_chain(tmp_path: Path) -> None:
    fake_api = RecordingApi()
    subject = fixture_controller(tmp_path, api=fake_api)

    sync_identities(Config(model_id=f"{MARKER}-model"), fake_api)

    assert [
        (plane, method, path)
        for plane, method, path, _body in fake_api.calls
    ] == [
        ("business", "GET", "/v1/auth/me"),
        ("business", "GET", "/v1/auth/me"),
        ("business", "GET", "/v1/auth/me"),
    ]
    fake_api.calls.clear()

    subject.prepare()

    state = load_state(subject.state_path)
    created = [
        (method, path)
        for _plane, method, path, _body in fake_api.calls
        if method == "POST"
    ]
    assert created == [
        ("POST", "/v1/skills/"),
        ("POST", "/v1/knowledge-bases"),
        ("POST", "/v1/agent-definitions"),
        ("POST", "/v1/admin/workflows"),
    ]
    agent_body = fake_api.body_for("POST", "/v1/agent-definitions")
    assert agent_body["modelId"] == f"{MARKER}-model"
    assert agent_body["skillIds"] == [SKILL_ID]
    assert agent_body["knowledgeBaseIds"] == [KNOWLEDGE_BASE_ID]
    workflow_body = fake_api.body_for("POST", "/v1/admin/workflows")
    assert workflow_body["supervisorAgentDefinitionId"] == AGENT_ID
    assert workflow_body["nodes"] == [
        {
            "nodeKey": "authorized-node",
            "displayName": "Authorization node",
            "agentDefinitionId": AGENT_ID,
            "instruction": (
                "MULTI_AGENT_NODE:authorized-node "
                "MULTI_AGENT_BARRIER:authorization-ui"
            ),
            "positionX": 0,
            "positionY": 0,
        }
    ]
    assert workflow_body["edges"] == []
    assert state.prepared is True


def test_prepare_refuses_an_existing_journal(tmp_path: Path) -> None:
    subject = fixture_controller(tmp_path)
    save_state(subject.state_path, owned_state())

    with pytest.raises(FixtureError, match="already exists"):
        subject.prepare()


@pytest.mark.parametrize(
    ("api", "database", "message"),
    [
        (RecordingApi(), RecordingDatabase(collision=True), "collision"),
        (RecordingApi(), RecordingDatabase(membership=[]), "membership"),
        (
            RecordingApi(),
            RecordingDatabase(membership=[USER_ID, USER2_ID]),
            "membership",
        ),
        (
            RecordingApi(
                organization_capabilities=[
                    "chat.send",
                    "workspace.knowledge.create",
                ]
            ),
            RecordingDatabase(),
            "workspace.knowledge.create",
        ),
        (
            RecordingApi(
                analysts_capabilities=["workspace.knowledge.create"]
            ),
            RecordingDatabase(),
            "workspace.knowledge.create",
        ),
        (
            RecordingApi(
                model_grants=[
                    {
                        "principalType": "organization",
                        "principalId": ORG_ID,
                        "permission": "read",
                    }
                ]
            ),
            RecordingDatabase(),
            "model grants",
        ),
    ],
)
def test_prepare_refuses_unsafe_shared_state_before_creating_resources(
    tmp_path: Path, api: RecordingApi, database: RecordingDatabase, message: str
) -> None:
    subject = fixture_controller(tmp_path, api=api, database=database)

    with pytest.raises(FixtureError, match=message):
        subject.prepare()

    assert not [call for call in api.calls if call[1] == "POST"]


class CleanupApi:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def token(self, username: str, password: str, client_id: str) -> str:
        assert username == password == "admin"
        assert client_id == "admin-ui"
        return "admin-token"

    def request(
        self,
        _plane: str,
        method: str,
        path: str,
        _token: str,
        *,
        body: dict[str, object] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> object:
        del expected
        if method == "PUT" and path.endswith("/grants/reconcile"):
            assert body is not None and body["resourceType"] == "model"
            self.events.append("restore-model")
        elif method == "PUT" and "/capabilities/groups/" in path:
            self.events.append("restore-analysts-capabilities")
        elif method == "PUT" and path.endswith("/capabilities/organization"):
            self.events.append("restore-organization-capabilities")
        elif method == "GET" and path == f"/v1/agent-definitions/{AGENT_ID}":
            return {"data": {"id": AGENT_ID, "name": f"{MARKER}-agent"}}
        elif method == "GET" and path == f"/v1/knowledge-bases/{KNOWLEDGE_BASE_ID}":
            return {
                "data": {
                    "id": KNOWLEDGE_BASE_ID,
                    "name": f"{MARKER}-knowledge",
                }
            }
        elif method == "GET" and path == f"/v1/skills/{SKILL_ID}":
            return {"data": {"id": SKILL_ID, "name": f"{MARKER}-skill"}}
        elif method == "DELETE" and path == f"/v1/agent-definitions/{AGENT_ID}":
            self.events.append(f"delete-agent:{AGENT_ID}")
        elif method == "DELETE" and path == f"/v1/knowledge-bases/{KNOWLEDGE_BASE_ID}":
            self.events.append(f"delete-knowledge:{KNOWLEDGE_BASE_ID}")
        elif method == "DELETE" and path == f"/v1/skills/{SKILL_ID}":
            self.events.append(f"delete-skill:{SKILL_ID}")
        else:
            raise AssertionError((method, path, body))
        return None


class MissingModelCleanupApi(CleanupApi):
    def request(
        self,
        plane: str,
        method: str,
        path: str,
        token: str,
        *,
        body: dict[str, object] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> object:
        if method == "PUT" and path.endswith("/grants/reconcile"):
            assert body is not None and body["resourceType"] == "model"
            self.events.append("missing-model")
            if 404 not in expected:
                raise FixtureError("PUT grants/reconcile: expected (200,), got 404")
            return {"_httpStatus": 404}
        return super().request(
            plane,
            method,
            path,
            token,
            body=body,
            expected=expected,
        )


class CleanupDatabase:
    def __init__(self, events: list[str], *, fail_delete: bool = False) -> None:
        self.events = events
        self.fail_delete = fail_delete
        self.sql = ""

    def query_values(self, _sql: str) -> list[str]:
        self.events.append("discover-runtime")
        return [f"{CONVERSATION_ID}|{WORKFLOW_RUN_ID}"]

    def execute(self, sql: str) -> None:
        self.events.append("delete-runtime-and-workflow")
        self.sql = sql
        if self.fail_delete:
            raise FixtureError("owned delete failed")


class CleanupRunner:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def remove_owned(self, conversation_id: str) -> None:
        self.events.append(f"runner:{conversation_id}")


def test_cleanup_restores_then_deletes_in_reverse_dependency_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    api = CleanupApi(events)
    database = CleanupDatabase(events)
    subject = fixture_controller(
        tmp_path,
        api=api,
        database=database,
        runner_cleaner=CleanupRunner(events),
    )
    save_state(subject.state_path, owned_state())
    monkeypatch.setattr(subject, "verify_clean", lambda: events.append("verify-clean"))

    subject.cleanup()

    assert events == [
        "restore-model",
        "restore-analysts-capabilities",
        "restore-organization-capabilities",
        "discover-runtime",
        f"runner:{CONVERSATION_ID}",
        "delete-runtime-and-workflow",
        f"delete-agent:{AGENT_ID}",
        f"delete-knowledge:{KNOWLEDGE_BASE_ID}",
        f"delete-skill:{SKILL_ID}",
        "verify-clean",
    ]


def test_cleanup_sql_is_scoped_to_saved_ids_and_exact_workflow_name(
    tmp_path: Path,
) -> None:
    subject = fixture_controller(tmp_path)

    sql = subject._cleanup_sql(owned_state())
    upper = sql.upper()

    for value in (
        ORG_ID,
        USER_ID,
        CONVERSATION_ID,
        WORKFLOW_RUN_ID,
        WORKFLOW_ID,
        f"{MARKER}-workflow",
    ):
        assert value in sql
    assert "LIKE" not in upper
    assert f"{MARKER}%" not in sql


def test_runtime_discovery_includes_denied_conversation_without_a_run(
    tmp_path: Path,
) -> None:
    state = owned_state()
    state.conversation_ids = []
    state.workflow_run_ids = []

    class DeniedConversationDatabase:
        sql = ""

        def query_values(self, sql: str) -> list[str]:
            self.sql = sql
            return [f"{CONVERSATION_ID}|"]

    database = DeniedConversationDatabase()
    subject = fixture_controller(tmp_path, database=database)
    save_state(subject.state_path, state)

    subject._discover_runtime(state)

    assert "FROM conversations c" in database.sql
    assert "c.title IN" in database.sql
    assert f"{MARKER}-agent-revoked" in database.sql
    assert f"{MARKER}-knowledge-revoked" in database.sql
    assert "LIKE" not in database.sql.upper()
    assert state.conversation_ids == [CONVERSATION_ID]
    assert state.workflow_run_ids == []
    assert load_state(subject.state_path).conversation_ids == [CONVERSATION_ID]


def test_collision_check_includes_exact_marker_conversation_titles(
    tmp_path: Path,
) -> None:
    subject = fixture_controller(tmp_path)

    sql = subject._collision_query(owned_state())

    assert "FROM conversations" in sql
    assert "title IN" in sql
    assert f"{MARKER}-agent-revoked" in sql
    assert f"{MARKER}-knowledge-revoked" in sql
    assert "LIKE" not in sql.upper()


def test_cleanup_continues_restoration_and_verification_after_delete_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    subject = fixture_controller(
        tmp_path,
        api=CleanupApi(events),
        database=CleanupDatabase(events, fail_delete=True),
        runner_cleaner=CleanupRunner(events),
    )
    save_state(subject.state_path, owned_state())
    monkeypatch.setattr(subject, "verify_clean", lambda: events.append("verify-clean"))

    with pytest.raises(FixtureError, match="owned delete failed"):
        subject.cleanup()

    assert events[:3] == [
        "restore-model",
        "restore-analysts-capabilities",
        "restore-organization-capabilities",
    ]
    assert f"delete-agent:{AGENT_ID}" in events
    assert "verify-clean" in events


def test_cleanup_is_idempotent_after_empty_virtual_model_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    subject = fixture_controller(
        tmp_path,
        api=MissingModelCleanupApi(events),
        database=CleanupDatabase(events),
        runner_cleaner=CleanupRunner(events),
    )
    save_state(subject.state_path, owned_state())
    monkeypatch.setattr(subject, "verify_clean", lambda: events.append("verify-clean"))

    subject.cleanup()

    assert events[0] == "missing-model"
    assert "verify-clean" in events


def test_final_verification_checks_virtual_model_grants_in_database(
    tmp_path: Path,
) -> None:
    subject = fixture_controller(tmp_path)

    sql, expected = subject._verification_query(owned_state())

    assert "FROM access_grants" in sql
    assert "resource_type='model'" in sql
    assert f"resource_id='{MARKER}-model'" in sql
    assert "model-grant|0" in expected


def test_final_verification_checks_saved_capabilities_in_database(
    tmp_path: Path,
) -> None:
    subject = fixture_controller(tmp_path)

    sql, expected = subject._verification_query(owned_state())

    assert "FROM organization_capabilities" in sql
    assert f"org_id='{ORG_ID}'::uuid" in sql
    assert "FROM group_capabilities" in sql
    assert f"group_id='{GROUP_ID}'::uuid" in sql
    assert "organization-capability|chat.send" in expected
    assert not any(item.startswith("group-capability|") for item in expected)


def test_final_verification_accepts_missing_virtual_model_after_empty_db_check(
    tmp_path: Path,
) -> None:
    state = owned_state()

    class ApiMustNotBeUsed:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"verification unexpectedly used API: {name}")

    class EmptyDatabase:
        def query_values(self, _sql: str) -> list[str]:
            return [
                "skill|0",
                "knowledge|0",
                "agent|0",
                "workflow|0",
                "conversation|0",
                "workflow-run|0",
                "model-grant|0",
                "organization-capability|chat.send",
            ]

    class AbsentRunner:
        def assert_absent(self, _conversation_id: str) -> None:
            return None

    subject = fixture_controller(
        tmp_path,
        api=ApiMustNotBeUsed(),
        database=EmptyDatabase(),
        runner_cleaner=AbsentRunner(),
    )
    save_state(subject.state_path, state)

    subject.verify_clean()


def test_compose_allows_a_unique_marker_model() -> None:
    compose = (ROOT / "docker-compose.e2e.yml").read_text(encoding="utf-8")
    assert (
        'TEST_GATEWAY_MODEL_ID: "${TEST_GATEWAY_MODEL_ID:-gpt-4o-mini}"'
        in compose
    )
    assert (
        compose.count('APP_LLM_GATEWAY_URL: "http://test-gateway:8080"') == 2
    )
    assert "backend-admin:" in compose


@pytest.mark.parametrize("command", ["cleanup", "verify-clean"])
@pytest.mark.parametrize("model_env", [None, "foreign-model"])
def test_recovery_cli_without_wrapper_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str,
    model_env: str | None,
) -> None:
    path = tmp_path / "state.json"
    save_state(path, owned_state())
    monkeypatch.delenv("TEST_GATEWAY_MODEL_ID", raising=False)
    if model_env is not None:
        monkeypatch.setenv("TEST_GATEWAY_MODEL_ID", model_env)
    monkeypatch.setattr(sys, "argv", [
        str(HARNESS_PATH), command, "--state", str(path), "--marker", MARKER,
    ])
    recovered = []

    def recover(controller: FixtureController) -> None:
        recovered.append(controller._state().model_id)

    monkeypatch.setattr(FixtureController, command.replace("-", "_"), recover)
    assert _harness.main() == (0 if model_env is None else 1)
    assert recovered == ([f"{MARKER}-model"] if model_env is None else [])


def test_wrapper_is_exclusive_owned_and_trace_free() -> None:
    path = ROOT / "smoke-test-authorization-ui.sh"
    text = path.read_text(encoding="utf-8")

    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    assert "authorization-ui.real.spec.ts" in text
    assert "--project=chromium" in text
    assert "--workers=1" in text
    assert "--trace=off" in text
    assert "--profile build build runner" in text
    assert 'TEST_GATEWAY_MODEL_ID="$MARKER-model"' in text
    assert "com.docker.compose.project" in text
    assert "com.docker.compose.service" in text
    assert 'authorization_ui_e2e.py" cleanup' in text
    assert 'authorization_ui_e2e.py" verify-clean' in text
    assert "sync-identities" in text
    assert text.index("sync-identities") < text.index(
        'TEST_GATEWAY_MODEL_ID="$MARKER-model"'
    )
    assert "rm -sf test-gateway" in text
    # Build separately: `up --build` would also rebuild and recreate postgres.
    assert "--profile rag build rag-api" in text
    assert "--profile rag up -d --no-build rag-api" in text
    assert text.index("--profile rag build rag-api") < text.index(
        "--profile rag up -d --no-build rag-api"
    ) < text.index('authorization_ui_e2e.py" prepare')


def test_browser_source_covers_ui_only_acl_and_all_resource_boundaries() -> None:
    source = (
        ROOT / "business-ui/e2e/authorization-ui.real.spec.ts"
    ).read_text(encoding="utf-8")
    for resource_type in (
        "model",
        "skill",
        "knowledge_base",
        "agent_definition",
        "workflow",
    ):
        assert f'"{resource_type}"' in source
    for principal in (
        "Organization:",
        "Group: analysts",
        "User: Regular User",
    ):
        assert principal in source
    assert 'test.use({ trace: "off" })' in source
    assert 'new URL(page.url()).pathname !== "/admin/access"' in source
    new_chat_helper = source.split("async function goToNewChat", 1)[1].split(
        "async function", 1
    )[0]
    assert 'getByLabel("Sidebar")' in new_chat_helper
    assert 'getByRole("button", { name: "New chat" })' in new_chat_helper
    assert 'name: "Knowledge bases", exact: true' in source
    assert 'goto(`${ADMIN_UI_URL}/admin/' not in source
    assert 'goto(`${BUSINESS_UI_URL}/' not in source
    assert (
        'getByRole("combobox", { name: "Resource type", exact: true })'
        in source
    )
    assert (
        'getByRole("textbox", { name: "Search resources", exact: true })'
        in source
    )
    assert 'getByRole("combobox", { name: "Resource", exact: true })' in source
    assert "getByText(id" not in source
    assert "function pickerOption" in source
    assert source.count('getByRole("option"') == 1
    assert 'getByRole("button", { name: "Save grants" })' in source
    assert 'getByRole("button", { name: "Apply" })' in source
    assert "function analystsCapabilitiesEditor" in source
    assert source.count("analystsCapabilitiesEditor(adminPage)") == 2
    assert "MULTI_AGENT_FINAL:authorized-node" in source
    assert "Access to the workflow was revoked" in source
    assert ".request.put(" not in source
    assert ".request.post(" not in source
    assert "APIRequestContext" not in source


def test_documentation_names_the_supported_authorization_ui_acceptance() -> None:
    for relative in ("README.md", "AGENTS.md", "docs/ARCHITECTURE.md", "ROADMAP.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "./smoke-test-authorization-ui.sh" in text, relative
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "Keycloak" in agents
    assert "trace" in agents.lower()
    assert "exclusive" in agents.lower()
