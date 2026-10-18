from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
K8S_SMOKE = (ROOT / "smoke-test-k8s.sh").read_text()
MCP_SMOKE = (ROOT / "smoke-test-mcp.sh").read_text()
CODE_SMOKE = (ROOT / "smoke-test-code-skill.sh").read_text()


def _function_body(source: str, name: str) -> str:
    match = re.search(rf"(?ms)^{re.escape(name)}\(\) \{{\n(.*?)^\}}$", source)
    assert match is not None, f"missing shell function {name}"
    return match.group(1)


def test_fetch_fixture_is_owned_and_passed_to_mcp_smoke() -> None:
    assert "MCP_FETCH_TARGET_URL=" in MCP_SMOKE
    assert "MCP_FETCH_EXPECTED_TITLE=" in MCP_SMOKE
    assert "start_fetch_fixture" in K8S_SMOKE
    assert 'MCP_FETCH_TARGET_URL="$MCP_FETCH_TARGET_URL"' in K8S_SMOKE
    assert 'MCP_FETCH_EXPECTED_TITLE="$MCP_FETCH_EXPECTED_TITLE"' in K8S_SMOKE
    cleanup = _function_body(K8S_SMOKE, "cleanup")
    assert "CREATED_FETCH_FIXTURE" in cleanup
    assert 'docker rm -f "$FETCH_FIXTURE_NAME"' in cleanup


def test_imported_images_force_all_permanent_workloads_to_restart() -> None:
    main = _function_body(K8S_SMOKE, "main")
    assert main.index("build_and_import_images") < main.index(
        "restart_permanent_workloads"
    )
    restart = _function_body(K8S_SMOKE, "restart_permanent_workloads")
    for component in (
        "backend",
        "backend-admin",
        "frontend",
        "admin-ui",
        "tool-worker",
        "skill-executor",
    ):
        assert component in restart
    assert "rollout restart" in restart
    assert "wait_for_rollouts" in restart


def test_failure_diagnostics_never_dump_temporal_payload_history() -> None:
    diagnostics = _function_body(K8S_SMOKE, "dump_failure_diagnostics")
    assert "workflow show" not in diagnostics
    assert "--detailed" not in diagnostics
    assert "workflow describe" in diagnostics
    assert "workflow show" not in K8S_SMOKE
    assert "--detailed" not in K8S_SMOKE
    assert not re.search(r"eyJ[A-Za-z0-9_-]{20,}", K8S_SMOKE)


def test_temporal_default_namespace_is_ready_before_isolated_namespace_mutation() -> None:
    main = _function_body(K8S_SMOKE, "main")
    assert main.index("wait_for_temporal") < main.index("ensure_temporal_namespace")
    readiness = _function_body(K8S_SMOKE, "wait_for_temporal")
    assert "operator namespace describe" in readiness
    assert "--namespace default" in readiness
    assert "sleep 1" in readiness


def test_existing_release_or_gateway_requires_explicit_keep() -> None:
    preflight = _function_body(K8S_SMOKE, "assert_reuse_policy")
    assert "helm status" in preflight
    assert "deployment service configmap" in preflight
    assert 'KEEP_RESOURCES" != "1' in preflight
    main = _function_body(K8S_SMOKE, "main")
    assert main.index("preflight_existing_cluster_reuse") < main.index(
        "start_compose_data_plane"
    )
    assert main.index("assert_reuse_policy") < main.index("ensure_owned_namespace")
    assert main.index("assert_reuse_policy") < main.index("ensure_temporal_namespace")


def test_created_resource_flags_follow_successful_create_before_label() -> None:
    namespace = _function_body(K8S_SMOKE, "ensure_owned_namespace")
    create_ns = namespace.index('kubectl create namespace "$RELEASE_NAMESPACE"')
    flag_ns = namespace.index("CREATED_RELEASE_NAMESPACE=1")
    label_ns = namespace.index('kubectl label namespace "$RELEASE_NAMESPACE"')
    assert create_ns < flag_ns < label_ns

    create_config = namespace.index(
        'kubectl -n "$RELEASE_NAMESPACE" create configmap "$OWNER_CONFIGMAP"'
    )
    flag_config = namespace.index("CREATED_OWNER_CONFIGMAP=1")
    label_config = namespace.index(
        'kubectl -n "$RELEASE_NAMESPACE" label configmap "$OWNER_CONFIGMAP"'
    )
    assert create_config < flag_config < label_config

    fixture = _function_body(K8S_SMOKE, "start_fetch_fixture")
    assert fixture.index("docker run") < fixture.index("CREATED_FETCH_FIXTURE=1")


def test_keycloak_credentials_default_to_admin_and_are_not_duplicated() -> None:
    assert 'TEST_USERNAME="${K8S_TEST_USER:-admin}"' in K8S_SMOKE
    assert 'TEST_PASSWORD="${K8S_TEST_PASSWORD:-admin}"' in K8S_SMOKE
    token = _function_body(K8S_SMOKE, "get_token")
    assert token.count("username=") == 1
    assert token.count("password=") == 1
    assert 'username=$TEST_USERNAME' in token
    assert 'password=$TEST_PASSWORD' in token


def test_fetch_smoke_validates_parsed_final_marker_not_raw_fixture_title() -> None:
    assert "validate-smoke-sse.py" in MCP_SMOKE
    assert "mcp-fetch" in MCP_SMOKE
    assert "FETCH FIXTURE VERIFIED:" in MCP_SMOKE


def test_code_smoke_validates_parsed_run_results_and_final_messages() -> None:
    assert "validate-smoke-sse.py" in CODE_SMOKE
    main = _function_body(CODE_SMOKE, "main")
    assert "code-greet" in main
    assert "code-multistep" in main
    assert '"Hello, World!"' not in main
    assert '"142"' not in main


def test_ordinary_smoke_uses_semantic_sse_validation_and_exact_runner_count() -> None:
    ordinary = _function_body(K8S_SMOKE, "ordinary_turn")
    assert "validate-smoke-sse.py" in ordinary
    assert "ordinary" in ordinary
    assert "rg -q" not in ordinary

    lookup = _function_body(K8S_SMOKE, "runner_uid")
    assert ".items[0]" not in lookup
    assert "runner_count" in lookup
    assert "runner Pods found" in lookup


def test_live_smoke_denies_non_tool_worker_access_to_skill_executor() -> None:
    isolation = _function_body(K8S_SMOKE, "assert_skill_executor_network_isolation")
    assert "NETWORK_PROBE_NAME" in isolation
    assert "aurum-ai-backend:latest" in isolation
    assert "test-skill-executor:8090" not in isolation
    assert 'service/${RELEASE_NAME}-skill-executor' not in isolation
    assert "socket.gethostbyname" in isolation
    assert "socket.create_connection" in isolation
    assert 'status.phase}' in isolation
    assert "Succeeded" in isolation

    main = _function_body(K8S_SMOKE, "main")
    readiness = 'wait_for_url "http://127.0.0.1:$SKILL_EXECUTOR_PORT/ready"'
    assert main.index(readiness) < main.index("assert_skill_executor_network_isolation")
    assert main.index("assert_skill_executor_network_isolation") < main.index(
        "assert_runner_reuse"
    )


def test_authorization_acceptance_is_unconditional_and_runs_after_runtime_scenarios() -> None:
    main = _function_body(K8S_SMOKE, "main")
    assert main.index("assert_rbac_denials") < main.index(
        "run_authorization_acceptance"
    )

    acceptance = _function_body(K8S_SMOKE, "run_authorization_acceptance")
    assert "helm upgrade" not in acceptance
    assert 'AUTHORIZATION_E2E_RUNTIME="kubernetes"' in acceptance
    assert 'AUTHORIZATION_E2E_KUBECONFIG="$KUBECONFIG"' in acceptance
    assert 'AUTHORIZATION_E2E_RUNNER_NAMESPACE="$RUNNER_NAMESPACE"' in acceptance
    assert 'AUTHORIZATION_E2E_SESSION_SECRET="$APP_SECRET_KEY"' in acceptance
    assert '"$REPO_ROOT/smoke-test-groups.sh"' in acceptance


def test_multi_agent_acceptance_runs_before_short_runner_ttl() -> None:
    main = _function_body(K8S_SMOKE, "main")
    assert main.index("run_skill_smokes") < main.index("run_multi_agent_acceptance")
    assert main.index("run_multi_agent_acceptance") < main.index(
        "install_short_runner_ttl"
    )

    acceptance = _function_body(K8S_SMOKE, "run_multi_agent_acceptance")
    assert 'MULTI_AGENT_E2E_RUNTIME="kubernetes"' in acceptance
    assert 'MULTI_AGENT_E2E_KUBECONFIG="$KUBECONFIG"' in acceptance
    assert 'MULTI_AGENT_E2E_RUNNER_NAMESPACE="$RUNNER_NAMESPACE"' in acceptance
    assert 'MULTI_AGENT_E2E_DB_CONTAINER="aurum-ai-postgres"' in acceptance
    assert 'MULTI_AGENT_E2E_MODEL="k8s-test"' in acceptance
    assert '"$REPO_ROOT/smoke-test-multi-agent.sh"' in acceptance


def test_backend_port_forward_is_recovered_after_short_ttl_rollout() -> None:
    main = _function_body(K8S_SMOKE, "main")
    assert main.index("install_short_runner_ttl") < main.index(
        "restart_backend_port_forward"
    )
    assert main.index("restart_backend_port_forward") < main.index(
        "run_authorization_acceptance"
    )

    restart = _function_body(K8S_SMOKE, "restart_backend_port_forward")
    assert "kill_port_forwards" in restart
    assert 'start_port_forward "${RELEASE_NAME}-backend"' in restart
    assert 'start_port_forward "${RELEASE_NAME}-backend-admin"' in restart
    assert 'start_port_forward "${RELEASE_NAME}-skill-executor"' in restart
    assert 'start_port_forward "$GATEWAY_NAME"' in restart
    assert 'wait_for_url "http://127.0.0.1:$BACKEND_PORT/ready"' in restart


def test_kubernetes_smoke_uses_dedicated_high_rate_limits() -> None:
    values = _function_body(K8S_SMOKE, "write_helm_values")
    assert values.count("rateLimitPerMinute: $K8S_RATE_LIMIT_PER_MINUTE") == 2
    assert values.count("rateLimitPerMinuteStream: $K8S_RATE_LIMIT_PER_MINUTE_STREAM") == 2
    backend_admin = values.index("backendAdmin:")
    assert values.index(
        "rateLimitPerMinute: $K8S_RATE_LIMIT_PER_MINUTE", backend_admin
    )
    assert values.index(
        "rateLimitPerMinuteStream: $K8S_RATE_LIMIT_PER_MINUTE_STREAM", backend_admin
    )
    assert 'K8S_RATE_LIMIT_PER_MINUTE="${K8S_TEST_RATE_LIMIT_PER_MINUTE:-2000}"' in K8S_SMOKE
    assert (
        'K8S_RATE_LIMIT_PER_MINUTE_STREAM="${K8S_TEST_RATE_LIMIT_PER_MINUTE_STREAM:-500}"'
        in K8S_SMOKE
    )
