from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def compose_config(tmp_path: Path, extra: str = ""):
    env = tmp_path / ".env"
    env.write_text("APP_SECRET_KEY=test-only\nKEYCLOAK_ADMIN_PASSWORD=test-only\n" + extra)
    environment = {k: v for k, v in os.environ.items() if k not in {"EVALUATOR_MODEL", "EVALUATOR_ENABLED"}}
    process = subprocess.run(
        ["docker", "compose", "-f", str(ROOT / "docker-compose.yml"), "--env-file", str(env), "config", "--format", "json"],
        env=environment, capture_output=True, text=True, check=True,
    )
    return json.loads(process.stdout)


def test_clean_compose_does_not_select_a_personal_evaluator_account(tmp_path: Path):
    env = compose_config(tmp_path)["services"]["backend"]["environment"]
    assert env["EVALUATOR_MODEL"] == ""
    assert env["EVALUATOR_ENABLED"] == "false"


def test_explicit_operator_evaluator_model_and_flag_are_forwarded(tmp_path: Path):
    env = compose_config(tmp_path, "EVALUATOR_MODEL=provider/my-judge\nEVALUATOR_ENABLED=true\n")["services"]["backend"]["environment"]
    assert env["EVALUATOR_MODEL"] == "provider/my-judge"
    assert env["EVALUATOR_ENABLED"] == "true"


@pytest.mark.parametrize('script,override', [('smoke-test-mcp.sh', 'MCP_TEST_MODEL'), ('smoke-test-code-skill.sh', 'CODE_SKILL_TEST_MODEL')])
def test_tool_smoke_defaults_follow_deterministic_gateway_without_account_ids(script, override):
    import os
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    assignment = next(line for line in (root / script).read_text().splitlines() if line.startswith('MODEL='))
    command = ['bash', '-c', assignment + '\n printf %s "$MODEL"']
    basic = {'PATH': os.environ['PATH']}
    default = subprocess.run(command, env=basic, capture_output=True, text=True, check=True).stdout
    assert default == 'gpt-4o-mini'
    configured = subprocess.run(command, env={**basic, 'TEST_GATEWAY_MODEL_ID': 'owned-test-model'}, capture_output=True, text=True, check=True).stdout
    assert configured == 'owned-test-model'
    explicit = subprocess.run(command, env={**basic, 'TEST_GATEWAY_MODEL_ID': 'owned-test-model', override: 'operator-model'}, capture_output=True, text=True, check=True).stdout
    assert explicit == 'operator-model'


def test_deterministic_mcp_fetch_title_matches_rebranded_backend(tmp_path: Path):
    import ast
    module = ast.parse((ROOT / 'business-backend/src/core/config.py').read_text())
    settings = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == 'Settings')
    title = next(ast.literal_eval(node.value) for node in settings.body if isinstance(node, ast.AnnAssign) and getattr(node.target, 'id', None) == 'APP_NAME')
    command = ['docker', 'compose', '-f', str(ROOT / 'docker-compose.yml'), '-f', str(ROOT / 'docker-compose.e2e.yml'), 'config', '--format', 'json']
    result = subprocess.run(command, env={**os.environ, 'APP_SECRET_KEY': 'test-only', 'KEYCLOAK_ADMIN_PASSWORD': 'test-only'}, capture_output=True, text=True, check=True)
    config = json.loads(result.stdout)
    assert config['services']['test-gateway']['environment']['TEST_GATEWAY_FETCH_TITLE'] == title
    assignment = next(line for line in (ROOT / 'smoke-test-mcp.sh').read_text().splitlines() if line.startswith('MCP_FETCH_EXPECTED_TITLE='))
    value = subprocess.run(['bash', '-c', assignment + '\n printf %s "$MCP_FETCH_EXPECTED_TITLE"'], env={'PATH': os.environ['PATH']}, capture_output=True, text=True, check=True).stdout
    assert value == title
