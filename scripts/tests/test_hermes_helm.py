"""Rendered Helm contracts for the Hermes Agent egress boundary on Kubernetes."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[2]
HELM_CHART = ROOT / "charts" / "aurum-ai"
RELEASE = "rel"
NAMESPACE = "platform"
HERMES_NAMESPACE = "hermes-agents"
ENABLE = (
    "hermes.enabled=true",
    f"hermes.namespace={HERMES_NAMESPACE}",
)
REQUIRED_EXCLUDED_IPV4 = [
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.88.99.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
]


def _helm(*values: str) -> subprocess.CompletedProcess[str]:
    command = ["helm", "template", RELEASE, str(HELM_CHART), "--namespace", NAMESPACE]
    for value in values:
        command.extend(("--set", value))
    return subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)


def _render(*values: str) -> dict[tuple[str, str, str], dict[str, Any]]:
    result = _helm(*values)
    assert result.returncode == 0, result.stderr
    documents = [document for document in yaml.safe_load_all(result.stdout) if document]
    return {
        (
            document["kind"],
            "" if document["kind"] == "Namespace" else document["metadata"].get("namespace", NAMESPACE),
            document["metadata"]["name"],
        ): document
        for document in documents
    }


def _enabled(*values: str) -> dict[tuple[str, str, str], dict[str, Any]]:
    return _render(*ENABLE, *values)


def _error(*values: str) -> str:
    result = _helm(*values)
    assert result.returncode != 0, "render unexpectedly succeeded"
    return result.stderr


def _pod(deployment: dict[str, Any]) -> dict[str, Any]:
    return deployment["spec"]["template"]["spec"]


def _env(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in container.get("env", [])}


def _hermes(resources: dict[tuple[str, str, str], dict[str, Any]]) -> list[tuple[str, str, str]]:
    return [key for key in resources if "hermes" in key[2] or key[1] == HERMES_NAMESPACE]


def _guard_labels() -> dict[str, str]:
    return {
        "app.kubernetes.io/name": "hermes-egress",
        "app.kubernetes.io/instance": RELEASE,
    }


def _sandbox_labels() -> dict[str, str]:
    return {
        "app.kubernetes.io/instance": RELEASE,
        "aurum-ai.component": "hermes-sandbox",
    }


def test_hermes_is_off_by_default_and_changes_no_backend_secret_wiring() -> None:
    resources = _render()
    assert _hermes(resources) == []
    backend = _pod(resources[("Deployment", NAMESPACE, f"{RELEASE}-backend")])["containers"][0]
    env = _env(backend)
    assert env["APP_HERMES_ENABLED"]["value"] == "false"
    assert "APP_HERMES_EGRESS_SERVICE_SECRET" not in env
    assert not any("hermes" in role[2] for role in resources if role[0] in {"Role", "RoleBinding"})


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (("hermes.enabled=true",), "hermes.namespace is required"),
        (
            ("hermes.enabled=true", f"hermes.namespace={NAMESPACE}"),
            "hermes.namespace must differ",
        ),
        (
            ("hermes.enabled=true", f"hermes.namespace={RELEASE}-runners"),
            "hermes.namespace must differ",
        ),
        (
            ("hermes.enabled=true", f"hermes.namespace={RELEASE}-sandboxes"),
            "hermes.namespace must differ",
        ),
        (
            ("hermes.enabled=true", "hermes.namespace=Not_A_Label"),
            "hermes.namespace must be a valid DNS label",
        ),
        (
            (*ENABLE, "networkPolicies.enabled=false"),
            "hermes.enabled requires networkPolicies.enabled=true",
        ),
        (
            (*ENABLE, "hermes.egress.maxMode=everything"),
            "hermes.egress.maxMode must be none, packages or public",
        ),
        (
            (*ENABLE, "hermes.egress.serviceSecret=short"),
            "hermes.egress.serviceSecret must contain at least 16 characters",
        ),
        (
            (*ENABLE, "hermes.egress.serviceSecret=", "hermes.egress.existingSecret="),
            "hermes.egress.serviceSecret or hermes.egress.existingSecret is required",
        ),
        (
            (*ENABLE, "hermes.egress.existingSecret=hermes-secrets"),
            "set only one of hermes.egress.serviceSecret or hermes.egress.existingSecret",
        ),
        (
            (
                *ENABLE,
                "hermes.egress.serviceSecret=",
                "hermes.egress.existingSecret=hermes-secrets",
            ),
            "hermes.egress.secretRotationToken is required",
        ),
    ],
)
def test_hermes_helm_rejects_unsafe_configuration(values: tuple[str, ...], message: str) -> None:
    assert message in _error(*values)


def test_hermes_namespace_may_not_collide_with_the_web_namespace() -> None:
    error = _error(
        *ENABLE,
        "web.enabled=true",
        f"web.namespace={HERMES_NAMESPACE}",
        "web.runtimeClassName=aurum-ai-web-restricted",
    )
    assert "must differ" in error


def test_hermes_namespace_is_restricted_and_optional() -> None:
    resources = _enabled()
    labels = resources[("Namespace", "", HERMES_NAMESPACE)]["metadata"]["labels"]
    assert labels["aurum-ai.runtime"] == "hermes"
    assert labels["pod-security.kubernetes.io/enforce"] == "restricted"
    assert ("Namespace", "", HERMES_NAMESPACE) not in _enabled("hermes.createNamespace=false")


def test_hermes_egress_guard_is_hardened_and_runs_only_the_proxy() -> None:
    resources = _enabled()
    deployment = resources[("Deployment", HERMES_NAMESPACE, f"{RELEASE}-hermes-egress")]
    pod = _pod(deployment)
    container = pod["containers"][0]
    assert deployment["spec"]["selector"]["matchLabels"] == _guard_labels()
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    # No nftables here: NetworkPolicy is the packet filter, so the guard image
    # runs the proxy directly and needs no NET_ADMIN.
    assert container["command"] == ["python3", "/usr/local/bin/hermes-egress-proxy"]
    assert "hostNetwork" not in pod or pod["hostNetwork"] is False
    assert {port["containerPort"] for port in container["ports"]} == {8080, 3128}
    assert container["readinessProbe"]["tcpSocket"]["port"] == "proxy"
    assert container["image"] == "aurum-ai-web-egress:latest"


def test_hermes_egress_guard_environment_matches_the_proxy_contract() -> None:
    resources = _enabled(
        "hermes.egress.maxMode=public",
        "hermes.egress.internalPackageHosts={registry.internal:8443}",
    )
    container = _pod(resources[("Deployment", HERMES_NAMESPACE, f"{RELEASE}-hermes-egress")])[
        "containers"
    ][0]
    env = _env(container)
    assert env["HERMES_EGRESS_MAX_MODE"]["value"] == "public"
    assert env["HERMES_EGRESS_BACKEND_HOST"]["value"] == f"{RELEASE}-backend.{NAMESPACE}.svc"
    assert env["HERMES_EGRESS_BACKEND_PORT"]["value"] == "8000"
    assert env["HERMES_EGRESS_PLATFORM_PORT"]["value"] == "8080"
    assert env["HERMES_EGRESS_PROXY_PORT"]["value"] == "3128"
    assert env["HERMES_EGRESS_PACKAGE_HOSTS"]["value"] == (
        "pypi.org,files.pythonhosted.org,registry.npmjs.org"
    )
    assert env["HERMES_EGRESS_INTERNAL_PACKAGE_HOSTS"]["value"] == "registry.internal:8443"
    # The proxy refuses private destinations by address; the chart hands it the
    # same exclusions the NetworkPolicy uses so the two cannot drift.
    forbidden = env["WEB_EGRESS_FORBIDDEN_CIDRS"]["value"].split(",")
    assert set(REQUIRED_EXCLUDED_IPV4) <= set(forbidden)
    secret = env["HERMES_EGRESS_SERVICE_SECRET"]["valueFrom"]["secretKeyRef"]
    assert secret == {
        "name": f"{RELEASE}-hermes-secrets",
        "key": "HERMES_EGRESS_SERVICE_SECRET",
    }


def test_hermes_service_publishes_the_two_proxy_ports_only() -> None:
    service = _enabled()[("Service", HERMES_NAMESPACE, "hermes-egress")]
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["selector"] == _guard_labels()
    assert {(port["name"], port["port"]) for port in service["spec"]["ports"]} == {
        ("platform", 8080),
        ("proxy", 3128),
    }


def test_hermes_secret_exists_in_both_namespaces_and_backend_reads_it() -> None:
    resources = _enabled()
    for namespace in (NAMESPACE, HERMES_NAMESPACE):
        secret = resources[("Secret", namespace, f"{RELEASE}-hermes-secrets")]
        assert secret["stringData"]["HERMES_EGRESS_SERVICE_SECRET"] == "change-me-hermes-egress-secret"
    external = _enabled(
        "hermes.egress.serviceSecret=",
        "hermes.egress.existingSecret=my-hermes",
        "hermes.egress.secretRotationToken=r1",
    )
    assert not [key for key in external if key[0] == "Secret" and "hermes" in key[2]]
    for resources_ in (resources, external):
        backend = _pod(resources_[("Deployment", NAMESPACE, f"{RELEASE}-backend")])["containers"][0]
        env = _env(backend)
        assert env["APP_HERMES_ENABLED"]["value"] == "true"
        assert env["APP_HERMES_EGRESS_MAX_MODE"]["value"] == "packages"
        assert "secretKeyRef" in env["APP_HERMES_EGRESS_SERVICE_SECRET"]["valueFrom"]
    admin = _pod(resources[("Deployment", NAMESPACE, f"{RELEASE}-backend-admin")])["containers"][0]
    admin_env = _env(admin)
    assert admin_env["APP_HERMES_ENABLED"]["value"] == "true"
    assert admin_env["APP_HERMES_EGRESS_MAX_MODE"]["value"] == "packages"
    assert "APP_HERMES_EGRESS_SERVICE_SECRET" not in admin_env


def test_hermes_secret_rotation_rolls_the_guard_and_the_backend() -> None:
    def checksums(*values: str) -> tuple[str, str]:
        resources = _enabled(*values)
        guard = resources[("Deployment", HERMES_NAMESPACE, f"{RELEASE}-hermes-egress")]
        backend = resources[("Deployment", NAMESPACE, f"{RELEASE}-backend")]
        return (
            guard["spec"]["template"]["metadata"]["annotations"]["checksum/hermes-secret"],
            backend["spec"]["template"]["metadata"]["annotations"]["checksum/hermes-secret"],
        )

    first = checksums()
    assert first == checksums()
    assert checksums("hermes.egress.serviceSecret=another-secret-value") != first


def test_skill_executor_may_manage_only_hermes_pods_and_read_the_guard_service() -> None:
    resources = _enabled()
    role = resources[("Role", HERMES_NAMESPACE, f"{RELEASE}-hermes-manager")]
    assert role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["pods"],
            "verbs": ["get", "list", "watch", "create", "delete", "patch"],
        },
        {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["create", "get"]},
        {"apiGroups": [""], "resources": ["services"], "verbs": ["get"]},
    ]
    binding = resources[("RoleBinding", HERMES_NAMESPACE, f"{RELEASE}-hermes-manager")]
    assert binding["roleRef"]["name"] == f"{RELEASE}-hermes-manager"
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": f"{RELEASE}-skill-executor", "namespace": NAMESPACE}
    ]


def _policy(resources: dict[tuple[str, str, str], dict[str, Any]], name: str) -> dict[str, Any]:
    return resources[("NetworkPolicy", HERMES_NAMESPACE, f"{RELEASE}-{name}")]["spec"]


def test_hermes_namespace_denies_everything_by_default() -> None:
    spec = _policy(_enabled(), "hermes-default-deny")
    assert spec == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": []}


def test_a_sandbox_can_reach_only_the_guard_and_has_no_dns() -> None:
    spec = _policy(_enabled(), "hermes-sandbox-egress")
    assert spec["podSelector"]["matchLabels"] == _sandbox_labels()
    assert spec["policyTypes"] == ["Egress"]
    assert spec["egress"] == [
        {
            "to": [{"podSelector": {"matchLabels": _guard_labels()}}],
            "ports": [
                {"protocol": "TCP", "port": 8080},
                {"protocol": "TCP", "port": 3128},
            ],
        }
    ]


def test_only_sandboxes_reach_the_guard() -> None:
    spec = _policy(_enabled(), "hermes-egress-ingress")
    assert spec["podSelector"]["matchLabels"] == _guard_labels()
    assert spec["policyTypes"] == ["Ingress"]
    assert spec["ingress"] == [
        {
            "from": [{"podSelector": {"matchLabels": _sandbox_labels()}}],
            "ports": [
                {"protocol": "TCP", "port": 8080},
                {"protocol": "TCP", "port": 3128},
            ],
        }
    ]


def _guard_rules(*values: str) -> list[dict[str, Any]]:
    spec = _policy(_enabled(*values), "hermes-egress-egress")
    assert spec["podSelector"]["matchLabels"] == _guard_labels()
    assert spec["policyTypes"] == ["Egress"]
    return spec["egress"]


def _backend_rule(rules: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "to": [
            {
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": NAMESPACE}},
                "podSelector": {
                    "matchLabels": {
                        "app.kubernetes.io/name": "backend",
                        "app.kubernetes.io/instance": RELEASE,
                    }
                },
            }
        ],
        "ports": [{"protocol": "TCP", "port": 8000}],
    }


def test_guard_egress_reaches_dns_and_the_backend_and_nothing_private() -> None:
    rules = _guard_rules()
    assert _backend_rule(rules) in rules
    dns = [rule for rule in rules if {"protocol": "UDP", "port": 53} in rule["ports"]]
    assert len(dns) == 1
    assert dns[0]["to"][0]["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "kube-system"
    }
    public = [rule for rule in rules if "ipBlock" in rule["to"][0]]
    assert len(public) == 2
    for rule in public:
        assert rule["ports"] == [{"protocol": "TCP", "port": 443}]
    ipv4 = next(rule for rule in public if rule["to"][0]["ipBlock"]["cidr"] == "0.0.0.0/0")
    assert set(REQUIRED_EXCLUDED_IPV4) <= set(ipv4["to"][0]["ipBlock"]["except"])
    ipv6 = next(rule for rule in public if rule["to"][0]["ipBlock"]["cidr"] == "2000::/3")
    assert "2001:db8::/32" in ipv6["to"][0]["ipBlock"]["except"]


def test_egress_mode_decides_which_internet_rules_exist() -> None:
    none_rules = _guard_rules("hermes.egress.maxMode=none")
    assert not [rule for rule in none_rules if "ipBlock" in rule["to"][0]]
    public_rules = _guard_rules("hermes.egress.maxMode=public")
    for rule in public_rules:
        if "ipBlock" in rule["to"][0]:
            assert rule["ports"] == [
                {"protocol": "TCP", "port": 80},
                {"protocol": "TCP", "port": 443},
            ]


def test_internal_package_registries_need_an_explicit_destination() -> None:
    rules = _guard_rules(
        "hermes.egress.maxMode=packages",
        "hermes.egress.internalDestinations[0].cidr=10.20.0.0/24",
        "hermes.egress.internalDestinations[0].ports[0].protocol=TCP",
        "hermes.egress.internalDestinations[0].ports[0].port=8443",
    )
    assert {
        "to": [{"ipBlock": {"cidr": "10.20.0.0/24"}}],
        "ports": [{"protocol": "TCP", "port": 8443}],
    } in rules
    assert "hermes.egress.internalDestinations" in _error(
        *ENABLE,
        "hermes.egress.internalDestinations[0].cidr=not-a-cidr",
        "hermes.egress.internalDestinations[0].ports[0].protocol=TCP",
        "hermes.egress.internalDestinations[0].ports[0].port=8443",
    )


@pytest.mark.parametrize("removed", REQUIRED_EXCLUDED_IPV4)
def test_hermes_helm_keeps_every_required_exclusion(removed: str, tmp_path: Path) -> None:
    kept = [cidr for cidr in REQUIRED_EXCLUDED_IPV4 if cidr != removed]
    values = tmp_path / "values.yaml"
    values.write_text(yaml.safe_dump({"web": {"networkPolicy": {"excludedIpv4Cidrs": kept}}}))
    result = subprocess.run(
        [
            "helm",
            "template",
            RELEASE,
            str(HELM_CHART),
            "--namespace",
            NAMESPACE,
            "-f",
            str(values),
            *[arg for value in ENABLE for arg in ("--set", value)],
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert f"must retain required exclusion {removed}" in result.stderr


def test_every_hermes_pod_is_in_the_hermes_namespace_and_unprivileged() -> None:
    resources = _enabled()
    for key, document in resources.items():
        if document["kind"] != "Deployment" or key[1] != HERMES_NAMESPACE:
            continue
        pod = _pod(document)
        assert not pod.get("hostNetwork") and not pod.get("hostPID") and not pod.get("hostIPC")
        for container in pod["containers"]:
            assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
            assert "add" not in container["securityContext"]["capabilities"]


def _executor_env(*values: str) -> dict[str, dict[str, Any]]:
    resources = _render(*values)
    deployment = resources[("Deployment", NAMESPACE, f"{RELEASE}-skill-executor")]
    return _env(_pod(deployment)["containers"][0])


def test_the_executor_runs_hermes_sandboxes_only_when_the_block_is_enabled() -> None:
    assert _executor_env()["SKILL_EXECUTOR_HERMES_ENABLED"]["value"] == "false"
    assert "SKILL_EXECUTOR_HERMES_NAMESPACE" not in _executor_env()
    env = _executor_env(*ENABLE)
    assert env["SKILL_EXECUTOR_HERMES_ENABLED"]["value"] == "true"
    assert env["SKILL_EXECUTOR_HERMES_NAMESPACE"]["value"] == HERMES_NAMESPACE
    assert env["SKILL_EXECUTOR_HERMES_SANDBOX_IMAGE"]["value"] == "aurum-ai-hermes-sandbox:latest"
    # The labels the NetworkPolicies select come from the release name.
    assert env["SKILL_EXECUTOR_KUBERNETES_RELEASE_NAME"]["value"] == RELEASE
    assert env["SKILL_EXECUTOR_HERMES_WORKSPACE_MODE"]["value"] == "volume"
    assert env["SKILL_EXECUTOR_HERMES_MAX_RUNNING_PER_ORG"]["value"] == "4"
    assert env["SKILL_EXECUTOR_HERMES_RUNTIME_CLASS"]["value"] == ""


def test_the_sandbox_image_and_limits_come_from_values() -> None:
    env = _executor_env(
        *ENABLE,
        "image.registry=registry.example.test/team",
        "hermes.sandbox.image.tag=v7",
        "hermes.sandbox.cpus=1.5",
        "hermes.sandbox.memoryBytes=1073741824",
        "hermes.sandbox.workspaceMode=tmpfs",
        "hermes.sandbox.runtimeClassName=gvisor",
        "hermes.sandbox.maxRunning=3",
    )
    assert env["SKILL_EXECUTOR_HERMES_SANDBOX_IMAGE"]["value"] == "registry.example.test/team/aurum-ai-hermes-sandbox:v7"
    assert env["SKILL_EXECUTOR_HERMES_CPUS"]["value"] == "1.5"
    assert env["SKILL_EXECUTOR_HERMES_MEMORY_BYTES"]["value"] == "1073741824"
    assert env["SKILL_EXECUTOR_HERMES_WORKSPACE_MODE"]["value"] == "tmpfs"
    assert env["SKILL_EXECUTOR_HERMES_RUNTIME_CLASS"]["value"] == "gvisor"
    assert env["SKILL_EXECUTOR_HERMES_MAX_RUNNING"]["value"] == "3"


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (("sandboxRuntime.type=docker",), "hermes.enabled requires sandboxRuntime.type=kubernetes"),
        (("hermes.sandbox.workspaceMode=disk",), "hermes.sandbox.workspaceMode must be volume or tmpfs"),
        (("skillExecutor.replicaCount=2",), "hermes.enabled requires skillExecutor.replicaCount=1"),
    ],
)
def test_the_executor_settings_are_validated(values: tuple[str, ...], message: str) -> None:
    assert message in _error(*ENABLE, *values)


# Previews ---------------------------------------------------------------------

PREVIEW = (
    *ENABLE,
    "hermes.preview.enabled=true",
    "hermes.preview.origin=https://{preview}.preview.example.org",
    "hermes.preview.host=*.preview.example.org",
)
PREVIEW_NAME = f"{RELEASE}-hermes-preview"


def test_previews_are_off_unless_asked_for() -> None:
    for resources in (_render(), _enabled()):
        assert not [key for key in resources if "hermes-preview" in key[2]]
        backend = _pod(resources[("Deployment", NAMESPACE, f"{RELEASE}-backend")])["containers"][0]
        assert _env(backend)["APP_HERMES_PREVIEW_ENABLED"]["value"] == "false"
        assert "APP_HERMES_PREVIEW_ORIGIN" not in _env(backend)
        executor = _pod(resources[("Deployment", NAMESPACE, f"{RELEASE}-skill-executor")])["containers"][0]
        assert "SKILL_EXECUTOR_HERMES_PREVIEW_SECRET" not in _env(executor)
    rule_hosts = [
        rule["host"]
        for rule in _enabled()[("Ingress", NAMESPACE, RELEASE)]["spec"]["rules"]
    ]
    assert not [host for host in rule_hosts if host.startswith("*")]


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (("hermes.preview.enabled=true",), "hermes.preview.enabled requires hermes.enabled=true"),
        (
            (*ENABLE, "hermes.preview.enabled=true"),
            "hermes.preview.origin must be an http(s) URL with a {preview} label",
        ),
        (
            (*ENABLE, "hermes.preview.enabled=true", "hermes.preview.origin=https://app.example.org"),
            "hermes.preview.origin must be an http(s) URL with a {preview} label",
        ),
        (
            (*ENABLE, "hermes.preview.enabled=true", "hermes.preview.origin=ftp://{preview}.example.org"),
            "hermes.preview.origin must be an http(s) URL with a {preview} label",
        ),
        (
            (*ENABLE, "hermes.preview.enabled=true", "hermes.preview.origin=https://{preview}.example.org"),
            "hermes.preview.host is required",
        ),
        (
            (*PREVIEW, "hermes.preview.serviceSecret=short"),
            "hermes.preview.serviceSecret must contain at least 16 characters",
        ),
        (
            (*PREVIEW, "hermes.preview.executorSecret="),
            "hermes.preview.executorSecret or hermes.preview.existingSecret is required",
        ),
        (
            (*PREVIEW, "hermes.preview.existingSecret=mine"),
            "set only one of hermes.preview.serviceSecret/executorSecret or hermes.preview.existingSecret",
        ),
        (
            (
                *PREVIEW,
                "hermes.preview.serviceSecret=",
                "hermes.preview.executorSecret=",
                "hermes.preview.existingSecret=mine",
            ),
            "hermes.preview.secretRotationToken is required",
        ),
        ((*PREVIEW, "hermes.preview.replicaCount=0"), "hermes.preview.replicaCount must be at least 1"),
    ],
)
def test_previews_are_validated(values: tuple[str, ...], message: str) -> None:
    assert message in _error(*values)


def test_a_host_is_not_needed_without_an_ingress() -> None:
    resources = _render(
        *ENABLE,
        "hermes.preview.enabled=true",
        "hermes.preview.origin=https://{preview}.example.org",
        "ingress.enabled=false",
    )
    assert ("Deployment", NAMESPACE, PREVIEW_NAME) in resources


def test_the_preview_service_is_hardened_and_configured_from_the_chart() -> None:
    resources = _render(*PREVIEW)
    deployment = resources[("Deployment", NAMESPACE, PREVIEW_NAME)]
    pod = _pod(deployment)
    container = pod["containers"][0]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True and pod["securityContext"]["runAsUser"] == 10001
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert container["image"] == "aurum-ai-hermes-preview:latest"
    assert container["ports"] == [{"name": "http", "containerPort": 8095, "protocol": "TCP"}]
    assert container["readinessProbe"]["httpGet"] == {"path": "/health", "port": "http", "scheme": "HTTP"}
    env = _env(container)
    assert env["HERMES_PREVIEW_ORIGIN"]["value"] == "https://{preview}.preview.example.org"
    assert env["HERMES_PREVIEW_BACKEND_URL"]["value"] == f"http://{RELEASE}-backend.{NAMESPACE}.svc:8000"
    assert env["HERMES_PREVIEW_EXECUTOR_URL"]["value"] == f"ws://{RELEASE}-skill-executor.{NAMESPACE}.svc:8090"
    assert env["HERMES_PREVIEW_MAX_CONNECTIONS"]["value"] == "32"
    for name in ("HERMES_PREVIEW_SERVICE_SECRET", "HERMES_PREVIEW_EXECUTOR_SECRET"):
        assert env[name]["valueFrom"]["secretKeyRef"] == {"name": f"{RELEASE}-hermes-preview-secrets", "key": name}
    service = resources[("Service", NAMESPACE, PREVIEW_NAME)]
    assert service["spec"]["selector"] == {
        "app.kubernetes.io/name": "hermes-preview",
        "app.kubernetes.io/instance": RELEASE,
    }
    assert [(p["port"], p["targetPort"]) for p in service["spec"]["ports"]] == [(8095, "http")]


def test_the_preview_secret_is_one_secret_with_both_credentials() -> None:
    secret = _render(*PREVIEW)[("Secret", NAMESPACE, f"{RELEASE}-hermes-preview-secrets")]
    assert set(secret["stringData"]) == {"HERMES_PREVIEW_SERVICE_SECRET", "HERMES_PREVIEW_EXECUTOR_SECRET"}
    assert secret["stringData"]["HERMES_PREVIEW_SERVICE_SECRET"] != secret["stringData"]["HERMES_PREVIEW_EXECUTOR_SECRET"]
    external = _render(
        *PREVIEW,
        "hermes.preview.serviceSecret=",
        "hermes.preview.executorSecret=",
        "hermes.preview.existingSecret=mine",
        "hermes.preview.secretRotationToken=r1",
    )
    assert not [key for key in external if key[0] == "Secret" and "preview" in key[2]]
    env = _env(_pod(external[("Deployment", NAMESPACE, PREVIEW_NAME)])["containers"][0])
    assert env["HERMES_PREVIEW_SERVICE_SECRET"]["valueFrom"]["secretKeyRef"]["name"] == "mine"


def test_backend_and_executor_are_told_about_previews() -> None:
    resources = _render(*PREVIEW)
    backend = _env(_pod(resources[("Deployment", NAMESPACE, f"{RELEASE}-backend")])["containers"][0])
    assert backend["APP_HERMES_PREVIEW_ENABLED"]["value"] == "true"
    assert backend["APP_HERMES_PREVIEW_ORIGIN"]["value"] == "https://{preview}.preview.example.org"
    assert backend["APP_HERMES_PREVIEW_SERVICE_SECRET"]["valueFrom"]["secretKeyRef"] == {
        "name": f"{RELEASE}-hermes-preview-secrets",
        "key": "HERMES_PREVIEW_SERVICE_SECRET",
    }
    executor = _env(_pod(resources[("Deployment", NAMESPACE, f"{RELEASE}-skill-executor")])["containers"][0])
    assert executor["SKILL_EXECUTOR_HERMES_PREVIEW_SECRET"]["valueFrom"]["secretKeyRef"] == {
        "name": f"{RELEASE}-hermes-preview-secrets",
        "key": "HERMES_PREVIEW_EXECUTOR_SECRET",
    }
    admin = _env(_pod(resources[("Deployment", NAMESPACE, f"{RELEASE}-backend-admin")])["containers"][0])
    assert "APP_HERMES_PREVIEW_SERVICE_SECRET" not in admin


def test_the_ingress_routes_the_wildcard_host_to_the_preview_service_only() -> None:
    rules = _render(*PREVIEW)[("Ingress", NAMESPACE, RELEASE)]["spec"]["rules"]
    (rule,) = [rule for rule in rules if rule["host"] == "*.preview.example.org"]
    assert rule["http"]["paths"] == [
        {
            "path": "/",
            "pathType": "Prefix",
            "backend": {"service": {"name": PREVIEW_NAME, "port": {"number": 8095}}},
        }
    ]


def test_only_the_preview_service_and_the_tool_worker_reach_the_executor() -> None:
    def sources(*values: str) -> list[str]:
        policy = _render(*values)[("NetworkPolicy", NAMESPACE, f"{RELEASE}-skill-executor-ingress")]["spec"]
        return [peer["podSelector"]["matchLabels"]["app.kubernetes.io/name"] for rule in policy["ingress"] for peer in rule["from"]]

    assert sources() == ["tool-worker"]
    assert sources(*PREVIEW) == ["tool-worker", "hermes-preview"]


def test_preview_secret_rotation_rolls_every_consumer() -> None:
    def checksums(*values: str) -> list[str]:
        resources = _render(*PREVIEW, *values)
        return [
            resources[("Deployment", NAMESPACE, name)]["spec"]["template"]["metadata"]["annotations"][
                "checksum/hermes-preview-secret"
            ]
            for name in (PREVIEW_NAME, f"{RELEASE}-backend", f"{RELEASE}-skill-executor")
        ]

    first = checksums()
    assert len(set(first)) == 1 and checksums() == first
    assert checksums("hermes.preview.serviceSecret=another-secret-value")[0] != first[0]


# The k3d acceptance --------------------------------------------------------------

SMOKE = ROOT / "smoke-test-hermes-k8s.sh"
DRIVER = ROOT / "scripts" / "hermes-k8s-acceptance.py"


def test_the_k8s_acceptance_is_disposable_and_applies_the_charts_own_templates() -> None:
    script = SMOKE.read_text()
    assert subprocess.run(["bash", "-n", str(SMOKE)], capture_output=True).returncode == 0
    assert "trap teardown EXIT" in script and "k3d cluster delete" in script
    assert 'if (( $# )); then' in script
    rendered = set(re.findall(r"-s templates/(hermes-[a-z-]+\.yaml)", script))
    assert rendered, "the script renders no Hermes templates"
    for name in rendered:
        assert (HELM_CHART / "templates" / name).exists(), name
    # Every resource the chart makes for Hermes is applied, except the pure validation.
    chart = {path.name for path in (HELM_CHART / "templates").glob("hermes-*.yaml")}
    assert chart - rendered == {"hermes-preview-validation.yaml"}
    assert DRIVER.exists() and "hermes-k8s-acceptance.py" in script


def test_the_k8s_acceptance_driver_exits_nonzero_on_a_failed_check() -> None:
    source = DRIVER.read_text()
    assert "sys.exit(1 if FAILED else 0)" in source
    compile(source, str(DRIVER), "exec")


EGRESS_SMOKE = ROOT / "smoke-test-hermes-k8s-egress.sh"
EGRESS_DRIVER = ROOT / "scripts" / "hermes-k8s-egress-modes.py"


def test_the_egress_acceptance_is_disposable_and_applies_the_charts_own_templates() -> None:
    launcher, driver = EGRESS_SMOKE.read_text(), EGRESS_DRIVER.read_text()
    assert subprocess.run(["bash", "-n", str(EGRESS_SMOKE)], capture_output=True).returncode == 0
    compile(driver, str(EGRESS_DRIVER), "exec")
    assert 'if (( $# )); then' in launcher and "scripts/hermes-k8s-egress-modes.py" in launcher
    # The cluster is removed at the end unless asked to be kept, and a clash is refused.
    assert '"k3d", "cluster", "delete"' in driver and "KEEP_HERMES_K8S_CLUSTER" in driver
    assert "already exists" in driver
    for name in set(re.findall(r'"(hermes-[a-z-]+)"', driver)):
        if name.startswith(("hermes-namespace", "hermes-secret", "hermes-egress-", "hermes-network", "hermes-rbac")):
            assert (HELM_CHART / "templates" / f"{name}.yaml").exists(), name
    assert "sys.exit(1 if FAILED else 0)" in driver
    # Machine-independent: no path of the machine it was written on.
    assert "/home/" not in driver and "/tmp/claude" not in driver


# Internal destinations by selector -------------------------------------------------

PORTS = [{"protocol": "TCP", "port": 8443}]
MIRRORS = {"matchLabels": {"kubernetes.io/metadata.name": "mirrors"}}
MIRROR_PODS = {"matchLabels": {"app": "mirror"}}


def _with_destinations(destinations: list[dict[str, Any]]) -> subprocess.CompletedProcess[str]:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml") as handle:
        yaml.safe_dump({"hermes": {"egress": {"internalDestinations": destinations}}}, handle)
        handle.flush()
        command = ["helm", "template", RELEASE, str(HELM_CHART), "--namespace", NAMESPACE, "-f", handle.name]
        for value in ENABLE:
            command.extend(("--set", value))
        return subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)


def _destination_rules(destinations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = _with_destinations(destinations)
    assert result.returncode == 0, result.stderr
    for document in yaml.safe_load_all(result.stdout):
        if document and document["kind"] == "NetworkPolicy" and document["metadata"]["name"] == f"{RELEASE}-hermes-egress-egress":
            # DNS, the backend and the two public blocks come first.
            return document["spec"]["egress"][4:]
    raise AssertionError("no guard egress policy")


def test_an_internal_mirror_can_be_selected_by_namespace_and_pod_labels() -> None:
    rules = _destination_rules([{"namespaceSelector": MIRRORS, "podSelector": MIRROR_PODS, "ports": PORTS}])
    # One peer holding both selectors: the Pods with those labels in those namespaces.
    assert rules == [{"to": [{"namespaceSelector": MIRRORS, "podSelector": MIRROR_PODS}], "ports": PORTS}]


def test_a_whole_namespace_can_be_selected_and_cidrs_still_work_beside_selectors() -> None:
    rules = _destination_rules(
        [
            {"namespaceSelector": MIRRORS, "ports": PORTS},
            {"cidr": "10.20.0.0/24", "ports": PORTS},
        ]
    )
    assert rules == [
        {"to": [{"namespaceSelector": MIRRORS}], "ports": PORTS},
        {"to": [{"ipBlock": {"cidr": "10.20.0.0/24"}}], "ports": PORTS},
    ]


@pytest.mark.parametrize(
    ("destination", "message"),
    [
        ({"ports": PORTS}, "must name a cidr, or a namespaceSelector with an optional podSelector"),
        (
            {"cidr": "10.0.0.0/24", "namespaceSelector": MIRRORS, "ports": PORTS},
            "cidr cannot be combined with a selector",
        ),
        (
            # Alone, a podSelector means the Pods of the guard's own namespace: the sandboxes.
            {"podSelector": MIRROR_PODS, "ports": PORTS},
            "podSelector needs a namespaceSelector",
        ),
        (
            {"namespaceSelector": {}, "ports": PORTS},
            "namespaceSelector must have matchLabels",
        ),
        (
            {"namespaceSelector": {"matchLabels": {}}, "ports": PORTS},
            "namespaceSelector must have matchLabels",
        ),
        (
            {"namespaceSelector": MIRRORS, "podSelector": {"matchLabels": {}}, "ports": PORTS},
            "podSelector must have matchLabels",
        ),
        (
            {"namespaceSelector": {"matchExpressions": [{"key": "a", "operator": "Exists"}]}, "ports": PORTS},
            "namespaceSelector must have matchLabels",
        ),
        (
            {"namespaceSelector": {"matchLabels": {"a": "b"}, "matchExpressions": []}, "ports": PORTS},
            "namespaceSelector takes only matchLabels",
        ),
        (
            {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": HERMES_NAMESPACE}}, "ports": PORTS},
            "must not select the Hermes or the release namespace",
        ),
        (
            {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": NAMESPACE}}, "ports": PORTS},
            "must not select the Hermes or the release namespace",
        ),
    ],
)
def test_internal_destinations_refuse_selectors_that_open_too_much(destination: dict[str, Any], message: str) -> None:
    result = _with_destinations([destination])
    assert result.returncode != 0
    assert message in result.stderr


NODES_SMOKE = ROOT / "smoke-test-hermes-k8s-nodes.sh"
NODES_DRIVER = ROOT / "scripts" / "hermes-k8s-nodes-limits.py"


def test_the_nodes_acceptance_is_disposable_and_machine_independent() -> None:
    launcher, driver = NODES_SMOKE.read_text(), NODES_DRIVER.read_text()
    assert subprocess.run(["bash", "-n", str(NODES_SMOKE)], capture_output=True).returncode == 0
    compile(driver, str(NODES_DRIVER), "exec")
    assert 'if (( $# )); then' in launcher and "scripts/hermes-k8s-nodes-limits.py" in launcher
    # The Kubernetes client reads KUBECONFIG when it is imported: the launcher sets it first.
    assert launcher.index("export KUBECONFIG") < launcher.index("uv run python")
    # The cluster and the registry are removed unless asked to be kept, and a clash is refused.
    assert '"k3d", "cluster", "delete"' in driver and '"k3d", "registry", "delete"' in driver
    assert "KEEP_HERMES_K8S_CLUSTER" in driver and "already exists" in driver
    assert "sys.exit(1 if FAILED else 0)" in driver
    assert "/home/" not in driver and "/tmp/claude" not in driver
    for name in re.findall(r'"(hermes-[a-z-]+)"', driver):
        if name.startswith(("hermes-namespace", "hermes-secret", "hermes-egress-", "hermes-network", "hermes-rbac")):
            assert (HELM_CHART / "templates" / f"{name}.yaml").exists(), name


def test_the_acceptance_scripts_can_run_on_calico() -> None:
    installer = ROOT / "scripts" / "install-calico-k3d.sh"
    assert subprocess.run(["bash", "-n", str(installer)], capture_output=True).returncode == 0
    assert installer.stat().st_mode & 0o111, "the installer is not executable"
    text = installer.read_text()
    # The pool must lie inside k3s's cluster CIDR, and be set before calico-node starts.
    assert "10.42.0.0/16" in text and "CALICO_IPV4POOL_CIDR" in text
    for script in (ROOT / "smoke-test-hermes-k8s.sh", ROOT / "scripts" / "hermes-k8s-egress-modes.py"):
        source = script.read_text()
        assert "HERMES_K8S_CNI" in source and "install-calico-k3d.sh" in source, script.name
        # Calico replaces both of k3s's networking pieces.
        assert "--flannel-backend=none@server:*" in source and "--disable-network-policy@server:*" in source, script.name
    assert "HERMES_K8S_CNI" in (ROOT / "smoke-test-hermes-k8s-egress.sh").read_text()


PLATFORM_SMOKE = ROOT / "smoke-test-k8s.sh"
PLATFORM_DRIVER = ROOT / "scripts" / "hermes-k8s-platform-acceptance.py"


def test_the_platform_acceptance_has_a_hermes_profile_with_its_own_driver() -> None:
    script, driver = PLATFORM_SMOKE.read_text(), PLATFORM_DRIVER.read_text()
    assert subprocess.run(["bash", "-n", str(PLATFORM_SMOKE)], capture_output=True).returncode == 0
    compile(driver, str(PLATFORM_DRIVER), "exec")
    assert 'K8S_TEST_ACCEPTANCE must be full, user-questions or hermes' in script
    assert "hermes-k8s-platform-acceptance.py" in script and "run_hermes_acceptance" in script
    # A data plane with generated secrets needs its own values; the chart's defaults are the fallback.
    for variable in ("K8S_TEST_DATABASE_ROLE_PASSWORD", "K8S_TEST_REDIS_PASSWORD", "K8S_TEST_S3_SECRET_KEY"):
        assert variable in script, variable
    # The real gateway key reaches the chart only through helm's flags, never a values file or a log line.
    assert "--set-string" in script and "K8S_TEST_REAL_GATEWAY_API_KEY" in script
    assert "K8S_TEST_REAL_GATEWAY_API_KEY" not in script.split("write_helm_values()")[1].split("render_and_install()")[0]
    assert "sys.exit(1 if FAILED else 0)" in driver
    assert "/home/" not in driver and "/tmp/claude" not in driver
    # The organization's Hermes settings and the user's memory are put back after the run.
    assert "restore" in driver and "DELETE" in driver


def test_the_platform_acceptance_serves_previews_over_tls_with_its_own_certificate() -> None:
    script, driver = PLATFORM_SMOKE.read_text(), PLATFORM_DRIVER.read_text()
    # A wildcard host of its own, a self-signed certificate offered by the Ingress, and the HTTPS port published.
    assert "create_preview_tls_secret" in script and "hermes-preview-tls" in script and "openssl req -x509" in script
    assert "PREVIEW_HTTPS_PORT" in script and "30443" in script
    assert "origin: \"https://{preview}.$PREVIEW_DOMAIN:$PREVIEW_HTTPS_PORT\"" in script
    assert 'recheckSeconds: 5' in script
    # The domain is not the platform's own, and no resolver is asked for it.
    assert 'PREVIEW_DOMAIN="preview.example.test"' in script and "nip.io" not in script.split('PREVIEW_DOMAIN="')[1].split("\n")[0]
    assert "--resolve" in driver and "def previews(" in driver and "def uploads(" in driver
    # What a previewed app must not be given, and what revoking must end.
    assert "aurum-preview" in driver and "set_capabilities" in driver and "restored" in driver
