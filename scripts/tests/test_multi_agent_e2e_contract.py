from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
HARNESS_PATH = ROOT / "scripts/multi_agent_e2e.py"
SMOKE_PATH = ROOT / "smoke-test-multi-agent.sh"


def _source() -> str:
    return HARNESS_PATH.read_text()


def _harness_module():
    spec = importlib.util.spec_from_file_location("multi_agent_e2e", HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_harness_covers_the_required_authorization_and_runtime_scenarios() -> None:
    source = _source()
    tree = ast.parse(source)
    methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    required = {
        "verify_admin_boundaries",
        "create_agents",
        "grant_member_access",
        "run_sequential",
        "run_parallel",
        "run_live_revoke",
        "run_quota_exhaustion",
        "run_cancellation",
        "cleanup_owned_resources",
    }
    assert required <= methods
    for marker in (
        "default-deny",
        "audience separation",
        "exactly one final message",
        "safe SSE",
        "live revoke",
        "quota exhausted",
        "cancelled",
        "ownership marker",
    ):
        assert marker in source


def test_harness_never_prints_tokens_or_raw_payloads() -> None:
    source = _source()
    assert "Authorization: Bearer" not in source
    assert not re.search(r"print\([^\n]*(token|payload)", source, re.IGNORECASE)
    assert "response payload:" not in source.lower()
    assert "request payload:" not in source.lower()


def test_shell_wrapper_delegates_with_existing_runtime_environment() -> None:
    smoke = SMOKE_PATH.read_text()
    assert 'scripts/multi_agent_e2e.py' in smoke
    for name in ("BASE_URL", "ADMIN_BASE_URL", "KEYCLOAK_URL"):
        assert name in smoke
    assert "MULTI_AGENT_E2E_RUNTIME" in smoke
    assert "exec python3" in smoke


def test_compose_wrapper_raises_only_the_e2e_rate_limits() -> None:
    smoke = SMOKE_PATH.read_text()
    assert (
        'MULTI_AGENT_E2E_RATE_LIMIT_PER_MINUTE="${MULTI_AGENT_E2E_RATE_LIMIT_PER_MINUTE:-2000}"'
        in smoke
    )
    assert (
        'MULTI_AGENT_E2E_RATE_LIMIT_PER_MINUTE_STREAM="${MULTI_AGENT_E2E_RATE_LIMIT_PER_MINUTE_STREAM:-500}"'
        in smoke
    )
    assert 'APP_RATE_LIMIT_PER_MINUTE="$MULTI_AGENT_E2E_RATE_LIMIT_PER_MINUTE"' in smoke
    assert (
        'APP_RATE_LIMIT_PER_MINUTE_STREAM="$MULTI_AGENT_E2E_RATE_LIMIT_PER_MINUTE_STREAM"'
        in smoke
    )
    assert "export APP_RATE_LIMIT_PER_MINUTE APP_RATE_LIMIT_PER_MINUTE_STREAM" not in smoke


def test_compose_wrapper_disables_stale_warm_pool_for_e2e_backend() -> None:
    smoke = SMOKE_PATH.read_text()
    assert 'RUNNER_POOL_SIZE=0 docker compose -p "$COMPOSE_PROJECT_NAME"' in smoke


def test_cleanup_detaches_usage_correlation_before_deleting_runs() -> None:
    source = _source()
    detach = source.index("UPDATE llm_usage_events SET workflow_run_id=NULL")
    delete_runs = source.index("DELETE FROM workflow_runs WHERE workflow_definition_id")
    assert detach < delete_runs
    assert "workflow_phase=NULL, workflow_node_key=NULL" in source


def test_cleanup_hard_deletes_only_owned_conversations_after_runs() -> None:
    source = _source()
    delete_runs = source.index("DELETE FROM workflow_runs WHERE workflow_definition_id")
    delete_conversations = source.index("DELETE FROM conversations WHERE org_id=")
    assert delete_runs < delete_conversations
    assert "AND title='{escaped}'" in source[delete_conversations : delete_conversations + 250]
    assert "UPDATE llm_usage_events SET conversation_id" not in source


def test_quota_scenario_allows_failing_alpha_but_never_starts_next_node() -> None:
    harness = _harness_module()
    events = [
        {
            "type": "workflow-run-status",
            "data": {"status": "failed", "errorCode": "quota_exhausted"},
        },
        {
            "type": "workflow-node-status",
            "data": {"nodeKey": "alpha", "status": "running"},
        },
    ]

    harness.Acceptance.assert_quota_exhaustion(events)

    events.append(
        {
            "type": "workflow-node-status",
            "data": {"nodeKey": "beta", "status": "running"},
        }
    )
    with pytest.raises(harness.SmokeFailure, match="next agent"):
        harness.Acceptance.assert_quota_exhaustion(events)
