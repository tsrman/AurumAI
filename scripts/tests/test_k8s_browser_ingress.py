"""Execute wrapper functions: browser traffic must not depend on port-forward."""

import os
from pathlib import Path
import re
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[2]


def function(name):
    source = (ROOT / "smoke-test-k8s.sh").read_text()
    match = re.search(rf"(?ms)^{name}\(\) \{{\n.*?^\}}$", source)
    assert match
    return match.group()


def run_shell(script, tmp_path):
    result = subprocess.run(
        ["bash", "-euc", script], cwd=ROOT,
        env={**os.environ, "WORK_DIR": str(tmp_path), "APP_SECRET_KEY": "test-only",
             "ACCEPTANCE_PROFILE": "user-questions", "BACKEND_PORT": "18000",
             "ADMIN_PORT": "18001", "FRONTEND_PORT": "18081",
             "INGRESS_NODE_PORT": "30080",
             "KEYCLOAK_PUBLIC_URL": "http://localhost:8080"},
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return result


def test_fresh_question_cluster_publishes_ingress_only_on_loopback(tmp_path):
    result = run_shell(function("ensure_cluster") + r'''
cluster_exists() { return 1; }
configure_cluster_access() { :; }
log() { :; }
k3d() { printf '%s\n' "$@"; }
CLUSTER_NAME=owned-question-cluster
DOCKER_NETWORK=owned-network
ensure_cluster
''', tmp_path)
    args = result.stdout.splitlines()
    assert "127.0.0.1:18081:30080@loadbalancer" in args
    assert "--disable=servicelb@server:*" in args


def test_question_values_route_browser_api_via_matching_ingress(tmp_path):
    script = function("write_helm_values") + r'''
K8S_RATE_LIMIT_PER_MINUTE=2000
K8S_RATE_LIMIT_PER_MINUTE_STREAM=500
RELEASE_NAME=owned
RUNNER_NAMESPACE=owned-runners
SANDBOX_NAMESPACE=owned-sandboxes
RUNNER_ACTIVE_IDLE_SECONDS=300
RUNNER_REAP_SECONDS=2
POSTGRES_IP=172.20.0.2
REDIS_IP=172.20.0.3
KEYCLOAK_IP=172.20.0.4
TEMPORAL_IP=172.20.0.5
MINIO_IP=172.20.0.6
TEMPORAL_NAMESPACE=owned
GATEWAY_NAME=owned-gateway
OWNER_LABEL=aurum-ai.k8s-smoke/owned
write_helm_values 172.20.0.0/16
'''
    run_shell(script, tmp_path)
    values = yaml.safe_load((tmp_path / "k8s-smoke-values.yaml").read_text())
    assert values["ingress"]["enabled"] is True
    assert values["ingress"]["externalPort"] == 18081
    assert values["frontend"]["runtimeConfig"]["apiUrl"] == (
        "http://backend.127.0.0.1.nip.io:18081/v1"
    )


def test_question_browser_and_recovery_cli_receive_ingress_urls(tmp_path):
    executable = tmp_path / "business-backend/.venv/bin/python"
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/bin/bash\n[[ "$2" != prepare ]] || touch "$E2E_USER_QUESTIONS_STATE"\n')
    executable.chmod(0o700)
    (tmp_path / "business-ui").mkdir()
    result = run_shell(function("run_user_questions_acceptance") + r'''
REPO_ROOT="$WORK_DIR"
TMPDIR="$WORK_DIR"
RUNNER_NAMESPACE=owned-runners
RELEASE_NAME=owned
TEMPORAL_NAMESPACE=owned
USER_QUESTIONS_MARKER=user-questions-e2e-0123456789abcdef
uv() { :; }
npm() {
  printf '%s\n' "$E2E_BUSINESS_UI_URL" "$E2E_BUSINESS_BACKEND_URL" "$E2E_ADMIN_BACKEND_URL"
}
run_user_questions_acceptance
''', tmp_path)
    assert result.stdout.splitlines() == [
        "http://chat.127.0.0.1.nip.io:18081",
        "http://backend.127.0.0.1.nip.io:18081",
        "http://backend-admin.127.0.0.1.nip.io:18081",
    ]
