"""Rendered Helm contracts for the Hermes Agent egress boundary on Kubernetes."""

from __future__ import annotations

import subprocess
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
