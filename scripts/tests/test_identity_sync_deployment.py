"""Deployment admission and worker isolation contracts."""

from pathlib import Path
import subprocess
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_compose_sync_has_independent_worker_and_equal_api_flags():
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    worker = services["identity-worker"]
    assert worker["entrypoint"] == ["python", "-m", "src.maintenance_worker", "identity"]
    assert worker["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-m",
        "src.maintenance_worker",
        "identity-health",
    ]
    assert worker.get("volumes", []) == []
    assert worker.get("ports", []) == []
    assert worker["user"] == "10001:10001"
    assert (
        worker["depends_on"]["temporal-create-namespace"]["condition"]
        == "service_completed_successfully"
    )
    assert worker["depends_on"]["backend"]["condition"] == "service_healthy"
    for api in ["backend", "backend-admin"]:
        assert (
            services[api]["environment"]["APP_IDENTITY_SYNC_ENABLED"]
            == "${APP_IDENTITY_SYNC_ENABLED:-false}"
        )
        assert (
            services[api]["environment"]["APP_IDENTITY_SYNC_WAIT_SECONDS"]
            == "${APP_IDENTITY_SYNC_WAIT_SECONDS:-10}"
        )


def render(enabled, admission=True):
    result = subprocess.run(
        [
            "helm",
            "template",
            "identity-test",
            str(ROOT / "charts/aurum-ai"),
            "--set",
            f"identitySync.enabled={str(enabled).lower()}",
            "--set",
            f"identitySync.admissionEnabled={str(enabled and admission).lower()}",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    return list(yaml.safe_load_all(result.stdout))


def test_helm_sync_enabled_starts_worker_and_both_api_gates():
    docs = render(True)
    deployments = {d["metadata"]["name"]: d for d in docs if d and d["kind"] == "Deployment"}
    worker = deployments["identity-test-identity-worker"]["spec"]["template"]["spec"]
    assert worker["automountServiceAccountToken"] is False
    container = worker["containers"][0]
    assert container["command"] == ["python", "-m", "src.maintenance_worker", "identity"]
    assert container.get("volumeMounts", []) == []
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["runAsNonRoot"] is True
    assert container["readinessProbe"]["exec"]["command"] == [
        "python",
        "-m",
        "src.maintenance_worker",
        "identity-health",
    ]
    assert (
        container["livenessProbe"]["exec"]["command"]
        == container["readinessProbe"]["exec"]["command"]
    )
    for api in ["backend", "backend-admin"]:
        env = deployments["identity-test-" + api]["spec"]["template"]["spec"]["containers"][0][
            "env"
        ]
        assert next(x["value"] for x in env if x["name"] == "APP_IDENTITY_SYNC_ENABLED") == "true"


def test_helm_sync_disabled_does_not_add_worker():
    docs = render(False)
    assert not any(
        d and d.get("metadata", {}).get("name") == "identity-test-identity-worker" for d in docs
    )


def test_helm_worker_can_be_ready_before_admission_and_during_rollback():
    docs = render(True, admission=False)
    deployments = {d["metadata"]["name"]: d for d in docs if d and d["kind"] == "Deployment"}
    assert "identity-test-identity-worker" in deployments
    for api in ["backend", "backend-admin"]:
        env = deployments["identity-test-" + api]["spec"]["template"]["spec"]["containers"][0][
            "env"
        ]
        assert next(x["value"] for x in env if x["name"] == "APP_IDENTITY_SYNC_ENABLED") == "false"


def test_worker_secret_references_exist_in_rendered_release():
    docs = render(True)
    secrets = {
        d["metadata"]["name"]: d.get("stringData", {}) for d in docs if d and d["kind"] == "Secret"
    }
    worker = next(
        d
        for d in docs
        if d
        and d["kind"] == "Deployment"
        and d["metadata"]["name"] == "identity-test-identity-worker"
    )
    env = worker["spec"]["template"]["spec"]["containers"][0]["env"]
    for var in env:
        ref = var.get("valueFrom", {}).get("secretKeyRef")
        if ref:
            assert ref["key"] in secrets[ref["name"]], ref["key"]


def test_worker_network_policy_denies_ingress_and_bounds_egress():
    policy = next(
        d
        for d in render(True)
        if d
        and d["kind"] == "NetworkPolicy"
        and d["metadata"]["name"] == "identity-test-identity-worker"
    )
    assert policy["spec"]["ingress"] == []
    assert set(policy["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    rules = policy["spec"]["egress"]
    assert all(rule.get("to") and rule.get("ports") for rule in rules)
    assert not any(
        peer.get("ipBlock", {}).get("cidr") == "0.0.0.0/0" for rule in rules for peer in rule["to"]
    )
