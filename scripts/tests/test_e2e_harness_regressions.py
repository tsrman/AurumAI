from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from unittest.mock import patch


ROOT = Path(__file__).parents[2]


def _source(name: str) -> str:
    return (ROOT / name).read_text()


def _load_authorization_harness() -> ModuleType:
    path = ROOT / "scripts" / "authorization_v2_e2e.py"
    spec = importlib.util.spec_from_file_location("authorization_v2_e2e_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _decode_segment(value: str) -> dict[str, object]:
    value += "=" * (-len(value) % 4)
    return json.loads(base64.urlsafe_b64decode(value))


def test_general_smokes_default_to_admin_identity() -> None:
    expected = {
        "smoke-test.sh": ('USERNAME="${TEST_USER:-admin}"', 'PASSWORD="${TEST_PASSWORD:-admin}"'),
        "smoke-test-langfuse.sh": (
            'USERNAME="${TEST_USER:-admin}"',
            'PASSWORD="${TEST_PASSWORD:-admin}"',
        ),
        "smoke-test-mcp.sh": ('USERNAME="${TEST_USER:-admin}"', 'PASSWORD="${TEST_PASSWORD:-admin}"'),
    }
    for filename, assignments in expected.items():
        source = _source(filename)
        for assignment in assignments:
            assert assignment in source, f"{filename} must contain {assignment}"


def test_authorization_config_accepts_compose_app_secret(monkeypatch: object) -> None:
    module = _load_authorization_harness()
    with patch.dict(
        os.environ,
        {"APP_SECRET_KEY": "compose-authorization-secret"},
        clear=True,
    ):
        config = module.Config()
    assert config.session_secret == "compose-authorization-secret"


def test_warm_pool_runner_uses_identity_token_without_static_session_env() -> None:
    module = _load_authorization_harness()
    secret = "warm-pool-authorization-secret"
    acceptance = module.Acceptance(module.Config(runtime="docker", session_secret=secret))
    acceptance.state.organization_id = "550e8400-e29b-41d4-a716-446655440001"
    acceptance.state.user_id = "550e8400-e29b-41d4-a716-446655440002"
    acceptance.state.conversation_id = "550e8400-e29b-41d4-a716-446655440003"
    inspected = subprocess.CompletedProcess(
        args=["docker", "inspect"],
        returncode=0,
        stdout=json.dumps([{"Id": "runner-container-id", "Config": {"Env": []}}]),
        stderr="",
    )

    with (
        patch.object(module.subprocess, "run", return_value=inspected),
        patch.object(module.time, "sleep", return_value=None),
        patch.object(module.time, "monotonic", side_effect=[0.0, 0.0, 91.0]),
    ):
        identity, token = acceptance.runner_identity_and_token()

    assert identity == "runner-container-id"
    header, payload, signature = token.split(".")
    decoded = _decode_segment(payload)
    assert decoded == {
        "sub": acceptance.state.user_id,
        "conversation_id": acceptance.state.conversation_id,
        "org_id": acceptance.state.organization_id,
        "exp": decoded["exp"],
        "type": "session",
    }
    expected_signature = hmac.new(
        secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256
    ).digest()
    assert hmac.compare_digest(
        base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)),
        expected_signature,
    )


def test_quota_smoke_issues_identity_only_session_token() -> None:
    source = _source("smoke-test-quota.sh")
    assert 'os.environ["QUOTA_E2E_ORG_ID"]))' in source
    assert 'os.environ["QUOTA_E2E_ORG_ID"], [])' not in source


def test_compose_supports_isolated_runner_network_and_admin_rag_cleanup() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "APP_SECRET_KEY": "compose-contract-secret",
            "KEYCLOAK_ADMIN_PASSWORD": "compose-contract-keycloak",
            "RUNNER_DOCKER_NETWORK": "isolated-e2e_runner-net",
        }
    )
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "config", "--format", "json"],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )
    config = json.loads(result.stdout)
    assert (
        config["services"]["backend"]["environment"]["APP_DOCKER_NETWORK"]
        == "isolated-e2e_runner-net"
    )
    assert config["networks"]["runner-net"]["name"] == "isolated-e2e_runner-net"
    assert "rag-control-net" in config["services"]["backend-admin"]["networks"]


def test_compose_e2e_overlay_routes_backend_to_deterministic_gateway() -> None:
    environment = os.environ.copy()
    environment["APP_SECRET_KEY"] = "compose-e2e-overlay-secret"
    environment["KEYCLOAK_ADMIN_PASSWORD"] = "compose-e2e-overlay-keycloak"
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
    gateway = config["services"]["test-gateway"]
    backend = config["services"]["backend"]
    assert gateway["environment"]["TEST_GATEWAY_FETCH_TITLE"] == (
        "AurumAI Backend"
    )
    assert gateway["environment"]["TEST_GATEWAY_MODEL_ID"] == "gpt-4o-mini"
    assert backend["environment"]["APP_LLM_GATEWAY_URL"] == (
        "http://test-gateway:8080"
    )
    assert backend["environment"]["APP_GATEWAY_API_KEY"] == (
        "deterministic-e2e-no-auth"
    )
    assert "test-gateway" in backend["depends_on"]


def test_compose_e2e_runbooks_export_secret_and_restore_normal_backend() -> None:
    for filename in ("README.md", "AGENTS.md", "docs/ARCHITECTURE.md"):
        source = _source(filename)
        assert "export APP_SECRET_KEY=<" in source, filename
        assert "rm -sf test-gateway" in source, filename
        assert "--force-recreate backend" in source, filename
