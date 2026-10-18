from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import urllib.error
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest


ROOT = Path(__file__).parents[2]
HARNESS_PATH = ROOT / "scripts" / "workflow_ui_e2e.py"
ORG_ID = "11111111-1111-4111-8111-111111111111"
MEMBER_ID = "22222222-2222-4222-8222-222222222222"
AGENT_ID = "33333333-3333-4333-8333-333333333333"
ALPHA_ID = "44444444-4444-4444-8444-444444444444"
BETA_ID = "55555555-5555-4555-8555-555555555555"
CONVERSATION_ID = "66666666-6666-4666-8666-666666666666"
MODEL_GRANT_ID = "77777777-7777-4777-8777-777777777777"
MARKER = "workflow-ui-e2e-0123456789"


class FakeApi:
    def __init__(self, *, model_visible: bool) -> None:
        self.model_visible = model_visible
        self.calls: list[tuple[str, str, str, object]] = []
        self.agent_ids = iter((AGENT_ID, ALPHA_ID, BETA_ID))

    def token(self, username: str, password: str, client_id: str) -> str:
        assert password == username
        return f"{client_id}:{username}"

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
        del token, expected
        self.calls.append((plane, method, path, body))
        if path == "/v1/auth/me":
            return {"data": {"id": MEMBER_ID}}
        if path == "/v1/admin/authorization/subjects":
            return {
                "data": [
                    {
                        "principalType": "organization",
                        "principalId": ORG_ID,
                        "label": "Test Organization",
                    },
                    {
                        "principalType": "user",
                        "principalId": MEMBER_ID,
                        "label": "Regular User",
                    },
                ]
            }
        if path == "/v1/admin/authorization/capabilities/organization":
            if method == "GET":
                return {"data": {"capabilities": ["chat.send"]}}
            return {"data": body}
        if path.startswith("/v1/admin/authorization/grants?resourceType=model"):
            return {"data": [_model_grant()]}
        if path == "/v1/models":
            data = [{"id": "gpt-4o-mini"}] if self.model_visible else []
            return {"data": data}
        if path == "/v1/agent-definitions" and method == "POST":
            return {"data": {"id": next(self.agent_ids)}}
        if path == "/v1/admin/authorization/grants/reconcile":
            return {"data": body}
        raise AssertionError(f"unexpected API call: {plane} {method} {path}")


class FakeDatabase:
    def __init__(self, conversation_ids: list[str] | None = None) -> None:
        self.conversation_ids = conversation_ids or []
        self.queries: list[str] = []
        self.statements: list[str] = []

    def query_values(self, sql: str) -> list[str]:
        self.queries.append(sql)
        return list(self.conversation_ids)

    def execute(self, sql: str) -> None:
        self.statements.append(sql)


class FakeRunnerCleaner:
    def __init__(self) -> None:
        self.removed: list[str] = []

    def remove_owned(self, conversation_id: str) -> None:
        self.removed.append(conversation_id)


def _model_grant() -> dict[str, str | None]:
    return {
        "id": MODEL_GRANT_ID,
        "resourceType": "model",
        "resourceId": "gpt-4o-mini",
        "principalType": "organization",
        "principalId": ORG_ID,
        "permission": "read",
        "createdBy": None,
        "createdVia": "provisioning",
    }


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("workflow_ui_e2e", HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_cli_exposes_prepare_and_cleanup_commands() -> None:
    result = subprocess.run(
        [sys.executable, str(HARNESS_PATH), "--help"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "prepare" in result.stdout
    assert "cleanup" in result.stdout


@pytest.mark.parametrize(
    "marker",
    ["", "multi-agent-e2e-0123456789", "workflow-ui-e2e-' OR true --"],
)
def test_state_validation_rejects_unowned_markers(marker: str) -> None:
    module = _module()
    state = module.FixtureState(marker=marker)

    with pytest.raises(module.FixtureError, match="ownership marker"):
        module.validate_owned_state(state, require_resources=False)


def test_destructive_validation_requires_uuid_org_and_owned_ids() -> None:
    module = _module()
    invalid_org = module.FixtureState(
        marker=MARKER,
        organization_id="not-a-uuid",
        agent_ids={"supervisor": AGENT_ID},
    )
    no_resources = module.FixtureState(marker=MARKER, organization_id=ORG_ID)

    with pytest.raises(module.FixtureError, match="organization"):
        module.validate_owned_state(invalid_org, require_resources=True)
    with pytest.raises(module.FixtureError, match="owned resource"):
        module.validate_owned_state(no_resources, require_resources=True)


def test_valid_owned_state_is_accepted() -> None:
    module = _module()
    state = module.FixtureState(
        marker=MARKER,
        organization_id=ORG_ID,
        member_id=MEMBER_ID,
        agent_ids={"supervisor": AGENT_ID},
    )

    module.validate_owned_state(state, require_resources=True)


def test_state_validation_rejects_unexpected_workflow_names() -> None:
    module = _module()
    state = module.FixtureState(
        marker=MARKER,
        organization_id=ORG_ID,
        workflow_names=[f"{MARKER}-original", f"{MARKER}-other"],
    )

    with pytest.raises(module.FixtureError, match="workflow name"):
        module.validate_owned_state(state, require_resources=True)


def test_state_round_trip_is_atomic_private_and_secret_free(tmp_path: Path) -> None:
    module = _module()
    state_path = tmp_path / "state.json"
    state = module.FixtureState(
        marker=MARKER,
        organization_id=ORG_ID,
        member_id=MEMBER_ID,
        original_capabilities=["chat.send"],
        original_model_grants=[
            {
                "id": "44444444-4444-4444-8444-444444444444",
                "principalType": "organization",
                "principalId": ORG_ID,
                "permission": "read",
                "createdBy": None,
                "createdVia": "provisioning",
            }
        ],
        agent_ids={"supervisor": AGENT_ID},
        workflow_names=[f"{MARKER}-renamed"],
        conversation_ids=[],
    )

    module.save_state(state_path, state)

    raw = state_path.read_text()
    payload = json.loads(raw)
    assert payload["marker"] == MARKER
    assert "token" not in raw.lower()
    assert "password" not in raw.lower()
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert module.load_state(state_path) == state


def test_failed_atomic_replace_preserves_previous_state(tmp_path: Path) -> None:
    module = _module()
    state_path = tmp_path / "state.json"
    original = module.FixtureState(marker=MARKER)
    changed = module.FixtureState(marker=MARKER, organization_id=ORG_ID)
    module.save_state(state_path, original)

    with patch.object(Path, "replace", side_effect=OSError("replace failed")):
        with pytest.raises(OSError, match="replace failed"):
            module.save_state(state_path, changed)

    assert module.load_state(state_path) == original


def test_load_state_rejects_unknown_fields(tmp_path: Path) -> None:
    module = _module()
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"marker": MARKER, "access_token": "secret"}))

    with pytest.raises(module.FixtureError, match="unknown state field"):
        module.load_state(state_path)


def test_prepare_cli_never_overwrites_an_existing_recovery_journal(
    tmp_path: Path,
) -> None:
    module = _module()
    state_path = tmp_path / "state.json"
    original = _complete_state(module)
    module.save_state(state_path, original)

    with (
        patch.object(sys, "argv", [str(HARNESS_PATH), "prepare", "--state", str(state_path), "--marker", MARKER]),
        patch.object(module.Acceptance, "prepare") as prepare,
    ):
        assert module.main() == 1

    prepare.assert_not_called()
    assert module.load_state(state_path) == original


def test_http_error_does_not_expose_token_or_payload() -> None:
    module = _module()
    token = "secret-bearer-token"
    error = urllib.error.HTTPError(
        "http://localhost:8001/v1/example",
        503,
        "unavailable",
        {},
        None,
    )
    client = module.ApiClient(module.Config())

    with patch.object(module.urllib.request, "urlopen", side_effect=error):
        with pytest.raises(module.FixtureError) as caught:
            client.request(
                "admin",
                "POST",
                "/v1/example",
                token,
                body={"password": "payload-secret"},
            )

    message = str(caught.value)
    assert "POST /v1/example" in message
    assert "503" in message
    assert token not in message
    assert "payload-secret" not in message


def test_config_uses_explicit_e2e_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    monkeypatch.setenv("E2E_BUSINESS_BACKEND_URL", "http://business.example/")
    monkeypatch.setenv("E2E_ADMIN_BACKEND_URL", "http://admin.example/")
    monkeypatch.setenv("E2E_KEYCLOAK_URL", "http://keycloak.example/")
    monkeypatch.setenv("E2E_WORKFLOW_UI_MODEL", "provider/model")
    monkeypatch.setenv("E2E_POSTGRES_CONTAINER", "postgres-e2e")

    config = module.Config()

    assert config.business_url == "http://business.example"
    assert config.admin_url == "http://admin.example"
    assert config.keycloak_url == "http://keycloak.example"
    assert config.model == "provider/model"
    assert config.postgres_container == "postgres-e2e"


def test_prepare_preserves_visible_model_grants_and_creates_owned_agents(
    tmp_path: Path,
) -> None:
    module = _module()
    state_path = tmp_path / "state.json"
    module.save_state(state_path, module.FixtureState(marker=MARKER))
    api = FakeApi(model_visible=True)

    module.Acceptance(
        module.Config(),
        state_path,
        MARKER,
        api=api,
        database=FakeDatabase(),
        runner_cleaner=FakeRunnerCleaner(),
    ).prepare()

    state = module.load_state(state_path)
    assert state.organization_id == ORG_ID
    assert state.member_id == MEMBER_ID
    assert state.original_capabilities == ["chat.send"]
    assert state.original_model_grants == [_model_grant()]
    assert state.agent_ids == {
        "supervisor": AGENT_ID,
        "alpha": ALPHA_ID,
        "beta": BETA_ID,
    }
    assert [call[2] for call in api.calls[:2]] == ["/v1/auth/me", "/v1/auth/me"]
    capability_update = next(
        call
        for call in api.calls
        if call[1:3]
        == ("PUT", "/v1/admin/authorization/capabilities/organization")
    )
    assert capability_update[3] == {
        "capabilities": ["chat.send", "workspace.agents.create"]
    }
    model_reconciles = [
        call
        for call in api.calls
        if call[2] == "/v1/admin/authorization/grants/reconcile"
        and isinstance(call[3], dict)
        and call[3].get("resourceType") == "model"
    ]
    assert model_reconciles == []
    creates = [call for call in api.calls if call[1:3] == ("POST", "/v1/agent-definitions")]
    assert [call[3] for call in creates] == [
        {
            "name": f"{MARKER}-{key}",
            "description": "owned workflow browser acceptance fixture",
            "modelId": "gpt-4o-mini",
            "systemPrompt": f"Deterministic {key} workflow browser fixture.",
            "temperature": 0,
            "skillIds": [],
            "knowledgeBaseIds": [],
        }
        for key in ("supervisor", "alpha", "beta")
    ]
    agent_reconciles = [
        call[3]
        for call in api.calls
        if call[2] == "/v1/admin/authorization/grants/reconcile"
        and isinstance(call[3], dict)
        and call[3].get("resourceType") == "agent_definition"
    ]
    assert agent_reconciles == [
        {
            "resourceType": "agent_definition",
            "resourceId": agent_id,
            "grants": [
                {
                    "principalType": "user",
                    "principalId": MEMBER_ID,
                    "permission": "read",
                }
            ],
        }
        for agent_id in (AGENT_ID, ALPHA_ID, BETA_ID)
    ]


def test_prepare_temporarily_grants_a_hidden_model(tmp_path: Path) -> None:
    module = _module()
    state_path = tmp_path / "state.json"
    module.save_state(state_path, module.FixtureState(marker=MARKER))
    api = FakeApi(model_visible=False)

    module.Acceptance(
        module.Config(),
        state_path,
        MARKER,
        api=api,
        database=FakeDatabase(),
        runner_cleaner=FakeRunnerCleaner(),
    ).prepare()

    model_reconciles = [
        call[3]
        for call in api.calls
        if call[2] == "/v1/admin/authorization/grants/reconcile"
        and isinstance(call[3], dict)
        and call[3].get("resourceType") == "model"
    ]
    assert model_reconciles == [
        {
            "resourceType": "model",
            "resourceId": "gpt-4o-mini",
            "grants": [
                {
                    "principalType": "organization",
                    "principalId": ORG_ID,
                    "permission": "read",
                },
                {
                    "principalType": "user",
                    "principalId": MEMBER_ID,
                    "permission": "read",
                },
            ],
        }
    ]
    assert module.load_state(state_path).original_model_grants == [_model_grant()]


def test_prepare_refuses_an_existing_exact_resource_before_mutating_configuration(
    tmp_path: Path,
) -> None:
    module = _module()
    state_path = tmp_path / "state.json"
    module.save_state(state_path, module.FixtureState(marker=MARKER))
    api = FakeApi(model_visible=True)
    database = FakeDatabase([f"conversation:{CONVERSATION_ID}"])

    with pytest.raises(module.FixtureError, match="already in use"):
        module.Acceptance(
            module.Config(),
            state_path,
            MARKER,
            api=api,
            database=database,
            runner_cleaner=FakeRunnerCleaner(),
        ).prepare()

    assert not any(call[1] in {"POST", "PUT"} for call in api.calls)
    state = module.load_state(state_path)
    assert state.organization_id == ""
    assert state.workflow_names == []
    assert state.agent_ids == {}
    assert "FROM conversations" in database.queries[0]
    assert f"title='{MARKER}'" in database.queries[0]


def _complete_state(module: ModuleType) -> object:
    return module.FixtureState(
        marker=MARKER,
        organization_id=ORG_ID,
        member_id=MEMBER_ID,
        original_capabilities=["chat.send"],
        original_model_grants=[_model_grant()],
        agent_ids={"supervisor": AGENT_ID, "alpha": ALPHA_ID, "beta": BETA_ID},
        workflow_names=[f"{MARKER}-original", f"{MARKER}-renamed"],
    )


def test_cleanup_is_owned_ordered_and_idempotent(tmp_path: Path) -> None:
    module = _module()
    state_path = tmp_path / "state.json"
    module.save_state(state_path, _complete_state(module))
    database = FakeDatabase([CONVERSATION_ID])
    runner_cleaner = FakeRunnerCleaner()
    api = FakeApi(model_visible=True)
    acceptance = module.Acceptance(
        module.Config(),
        state_path,
        MARKER,
        api=api,
        database=database,
        runner_cleaner=runner_cleaner,
    )

    acceptance.cleanup()
    acceptance.cleanup()

    assert runner_cleaner.removed == [CONVERSATION_ID, CONVERSATION_ID]
    assert module.load_state(state_path).conversation_ids == [CONVERSATION_ID]
    assert len(database.statements) == 2
    for sql in database.statements:
        assert " LIKE " not in sql
        assert f"{MARKER}-original" in sql
        assert f"{MARKER}-renamed" in sql
        assert AGENT_ID in sql
        assert ALPHA_ID in sql
        assert BETA_ID in sql
        detach = sql.index("UPDATE llm_usage_events SET workflow_run_id=NULL")
        delete_node_runs = sql.index("DELETE FROM workflow_node_runs")
        delete_runs = sql.index("DELETE FROM workflow_runs")
        delete_conversations = sql.index("DELETE FROM conversations")
        delete_workflows = sql.index("DELETE FROM workflow_definitions")
        delete_agents = sql.index("DELETE FROM agent_definitions")
        assert detach < delete_node_runs < delete_runs < delete_conversations
        assert delete_conversations < delete_workflows < delete_agents
        for statement in filter(None, (item.strip() for item in sql.split(";"))):
            assert ORG_ID in statement
            assert MARKER in statement or any(
                owned_id in statement
                for owned_id in (AGENT_ID, ALPHA_ID, BETA_ID, CONVERSATION_ID)
            )
    restores = [
        call
        for call in api.calls
        if call[1] == "PUT"
        and call[2]
        in {
            "/v1/admin/authorization/grants/reconcile",
            "/v1/admin/authorization/capabilities/organization",
        }
    ]
    assert any(call[3] == {"capabilities": ["chat.send"]} for call in restores)
    assert any(
        call[3]
        == {
            "resourceType": "model",
            "resourceId": "gpt-4o-mini",
            "grants": [
                {
                    "principalType": "organization",
                    "principalId": ORG_ID,
                    "permission": "read",
                }
            ],
        }
        for call in restores
    )


def test_cleanup_uses_exact_sql_fallback_when_admin_restore_fails(tmp_path: Path) -> None:
    module = _module()
    state_path = tmp_path / "state.json"
    module.save_state(state_path, _complete_state(module))
    database = FakeDatabase()

    class RestoreFailingApi(FakeApi):
        def request(self, *args: object, **kwargs: object) -> object:
            if len(args) >= 3 and args[1:3] in {
                ("PUT", "/v1/admin/authorization/grants/reconcile"),
                ("PUT", "/v1/admin/authorization/capabilities/organization"),
            }:
                raise module.FixtureError("PUT restore: transport failed")
            return super().request(*args, **kwargs)

    module.Acceptance(
        module.Config(),
        state_path,
        MARKER,
        api=RestoreFailingApi(model_visible=True),
        database=database,
        runner_cleaner=FakeRunnerCleaner(),
    ).cleanup()

    fallback = database.statements[-1]
    assert fallback.startswith("BEGIN;\n")
    assert fallback.endswith("\nCOMMIT;")
    assert "DELETE FROM organization_capabilities" in fallback
    assert "INSERT INTO organization_capabilities" in fallback
    assert "DELETE FROM access_grants" in fallback
    assert "INSERT INTO access_grants" in fallback
    assert ORG_ID in fallback
    assert MODEL_GRANT_ID in fallback
    assert "gpt-4o-mini" in fallback
    assert "transport failed" not in fallback


def test_docker_runner_cleanup_decision_table() -> None:
    module = _module()

    def missing(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "Error: No such object: runner")

    missing_calls: list[list[str]] = []

    def record_missing(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        missing_calls.append(command)
        return missing(command, **kwargs)

    module.DockerRunnerCleaner(run=record_missing).remove_owned(CONVERSATION_ID)
    assert missing_calls == [["docker", "inspect", f"runner-{CONVERSATION_ID}"]]

    removed: list[list[str]] = []

    def owned(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["docker", "inspect"]:
            payload = [
                {
                    "Id": "owned-container-id",
                    "Config": {
                        "Labels": {"aurum-ai.component": "conversation-runner"},
                        "Env": [f"CONVERSATION_ID={CONVERSATION_ID}"],
                    },
                }
            ]
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        removed.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    module.DockerRunnerCleaner(run=owned).remove_owned(CONVERSATION_ID)
    assert removed == [["docker", "rm", "-f", "owned-container-id"]]


def test_docker_runner_cleanup_surfaces_inspection_failures() -> None:
    module = _module()

    def unavailable(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 1, "", "permission denied while connecting to Docker daemon"
        )

    with pytest.raises(module.FixtureError, match="inspection failed"):
        module.DockerRunnerCleaner(run=unavailable).remove_owned(CONVERSATION_ID)


@pytest.mark.parametrize(
    ("labels", "environment"),
    [
        ({"aurum-ai.component": "other"}, [f"CONVERSATION_ID={CONVERSATION_ID}"]),
        ({"aurum-ai.component": "conversation-runner"}, []),
        (
            {"aurum-ai.component": "conversation-runner"},
            ["CONVERSATION_ID=88888888-8888-4888-8888-888888888888"],
        ),
    ],
)
def test_docker_runner_cleanup_refuses_unowned_container(
    labels: dict[str, str], environment: list[str]
) -> None:
    module = _module()
    calls: list[list[str]] = []

    def inspect(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        payload = [{"Id": "not-owned", "Config": {"Labels": labels, "Env": environment}}]
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    with pytest.raises(module.FixtureError, match="ownership"):
        module.DockerRunnerCleaner(run=inspect).remove_owned(CONVERSATION_ID)

    assert calls == [["docker", "inspect", f"runner-{CONVERSATION_ID}"]]


def test_compose_overlay_exposes_workflow_barrier_duration() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "APP_SECRET_KEY": "workflow-ui-compose-contract-secret",
            "KEYCLOAK_ADMIN_PASSWORD": "workflow-ui-compose-contract-keycloak",
            "TEST_GATEWAY_BARRIER_SECONDS": "8",
        }
    )
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.e2e.yml",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )

    config = json.loads(result.stdout)
    assert config["services"]["test-gateway"]["environment"][
        "TEST_GATEWAY_BARRIER_SECONDS"
    ] == "8"


def _fake_executable(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/usr/bin/env bash\nset -eu\n" + body)
    path.chmod(0o755)


def _run_wrapper(
    tmp_path: Path,
    npm_status: int,
    *,
    cleanup_status: int = 0,
    restore_status: int = 0,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    command_log = tmp_path / "commands.log"
    common_log = 'printf "%s %s\\n" "$(basename "$0")" "$*" >> "$COMMAND_LOG"\n'
    _fake_executable(
        fake_bin,
        "docker",
        common_log
        + 'if [[ "${1:-}" == "inspect" ]]; then printf "aurum-ai\\n"; fi\n'
        + 'if [[ "$*" == *"--force-recreate backend backend-admin"* '
        + '&& "$*" != *"docker-compose.e2e.yml"* ]]; then exit "$FAKE_RESTORE_STATUS"; fi\n',
    )
    _fake_executable(fake_bin, "curl", common_log)
    _fake_executable(
        fake_bin,
        "python3",
        common_log
        + 'if [[ " $* " == *" cleanup "* ]]; then exit "$FAKE_CLEANUP_STATUS"; fi\n',
    )
    _fake_executable(
        fake_bin,
        "npm",
        common_log
        + 'printf "npm-pwd %s\\n" "$PWD" >> "$COMMAND_LOG"\n'
        + 'exit "$FAKE_NPM_STATUS"\n',
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "COMMAND_LOG": str(command_log),
            "FAKE_NPM_STATUS": str(npm_status),
            "FAKE_CLEANUP_STATUS": str(cleanup_status),
            "FAKE_RESTORE_STATUS": str(restore_status),
            "APP_SECRET_KEY": "workflow-ui-wrapper-contract-secret",
            "E2E_WORKFLOW_UI_MARKER": MARKER,
            "TMPDIR": str(tmp_path),
        }
    )
    result = subprocess.run(
        ["bash", str(ROOT / "smoke-test-workflow-ui.sh")],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    commands = command_log.read_text().splitlines() if command_log.exists() else []
    return result, commands


def test_wrapper_retains_recovery_journal_when_cleanup_fails(tmp_path: Path) -> None:
    result, commands = _run_wrapper(tmp_path, 0, cleanup_status=9)

    assert result.returncode == 9
    cleanup = next(
        command for command in commands if command.startswith("python3 ") and " cleanup " in f" {command} "
    )
    state_path = Path(cleanup.split("--state ", 1)[1].split(" --marker", 1)[0])
    assert state_path.parent.exists()
    assert "recovery state retained" in result.stderr
    assert str(state_path) in result.stderr


@pytest.mark.parametrize(
    ("npm_status", "cleanup_status", "restore_status", "expected_status"),
    [(0, 0, 11, 11), (7, 9, 11, 7)],
)
def test_wrapper_retains_state_and_preserves_primary_failure_precedence(
    tmp_path: Path,
    npm_status: int,
    cleanup_status: int,
    restore_status: int,
    expected_status: int,
) -> None:
    result, commands = _run_wrapper(
        tmp_path,
        npm_status,
        cleanup_status=cleanup_status,
        restore_status=restore_status,
    )

    assert result.returncode == expected_status
    cleanup = next(
        command
        for command in commands
        if command.startswith("python3 ") and " cleanup " in f" {command} "
    )
    state_path = Path(cleanup.split("--state ", 1)[1].split(" --marker", 1)[0])
    assert state_path.parent.exists()
    assert "recovery state retained" in result.stderr


@pytest.mark.parametrize("npm_status", [0, 7])
def test_wrapper_runs_cleanup_and_restores_both_backends(
    tmp_path: Path, npm_status: int
) -> None:
    result, commands = _run_wrapper(tmp_path, npm_status)

    assert result.returncode == npm_status
    overlay_up_index = next(
        index
        for index, command in enumerate(commands)
        if command.startswith("docker compose")
        and "docker-compose.e2e.yml" in command
        and "up -d" in command
        and "test-gateway" in command
    )
    ui_up = next(
        command
        for command in commands
        if command.startswith("docker compose")
        and "up -d" in command
        and "frontend admin-ui" in command
    )
    assert "--no-deps" in ui_up
    playwright_index = next(
        index for index, command in enumerate(commands) if command.startswith("npm ")
    )
    cleanup_index = next(
        index
        for index, command in enumerate(commands)
        if command.startswith("python3 ") and " cleanup " in f" {command} "
    )
    gateway_remove_index = next(
        index
        for index, command in enumerate(commands)
        if command.startswith("docker compose")
        and "rm -sf test-gateway" in command
    )
    restore_index = next(
        index
        for index, command in enumerate(commands)
        if command.startswith("docker compose")
        and "--force-recreate backend backend-admin" in command
        and "docker-compose.e2e.yml" not in command
    )
    assert overlay_up_index < playwright_index < cleanup_index
    assert cleanup_index < gateway_remove_index < restore_index
    assert "workflow.real.spec.ts" in commands[playwright_index]
    assert "--workers=1" in commands[playwright_index]
    assert "--trace=off" in commands[playwright_index]
    assert commands[playwright_index + 1] == f"npm-pwd {ROOT / 'business-ui'}"


def test_cleanup_deletes_owned_interaction_turns_before_restricted_workflow_runs() -> None:
    # interaction_turns.workflow_run_id is ON DELETE RESTRICT and turns survive
    # conversation deletion through conversation_identities, so owned turns must
    # be removed before their workflow runs.
    module = _module()
    state = _complete_state(module)
    state.conversation_ids = [CONVERSATION_ID]
    sql = module.Acceptance.__new__(module.Acceptance)._cleanup_sql(state)
    statements = [item.strip() for item in sql.split(";") if item.strip()]
    turns = [item for item in statements if item.startswith("DELETE FROM interaction_turns")]
    assert len(turns) == 1
    delete_turns = turns[0]
    assert sql.index(delete_turns) < sql.index("DELETE FROM workflow_runs")
    assert f"org_id='{ORG_ID}'::uuid" in delete_turns
    assert f"conversation_id IN ('{CONVERSATION_ID}'::uuid)" in delete_turns
    assert "workflow_run_id IS NULL OR workflow_run_id IN" in delete_turns
    assert f"{MARKER}-original" in delete_turns


def test_cleanup_never_deletes_turns_without_owned_conversations() -> None:
    module = _module()
    state = _complete_state(module)
    state.conversation_ids = []
    sql = module.Acceptance.__new__(module.Acceptance)._cleanup_sql(state)
    assert "DELETE FROM interaction_turns" not in sql
