"""Compose contracts and opt-in kernel acceptance for isolated web egress."""

from __future__ import annotations

import json
import ipaddress
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any
import uuid

import pytest
import yaml


ROOT = Path(__file__).parents[2]
BASE_COMPOSE = ROOT / "docker-compose.yml"
WEB_COMPOSE = ROOT / "docker-compose.web.yml"
WEB_PROBES_COMPOSE = ROOT / "docker-compose.e2e-web-probes.yml"
WEB_CONTROLLED_COMPOSE = ROOT / "docker-compose.e2e-web.yml"
READINESS_PROGRAM = ROOT / "infra" / "web-egress" / "readiness.py"
NFT_RULES = ROOT / "infra" / "web-egress" / "rules.nft"
APP_POLICY = ROOT / "web-tools" / "src" / "policy.py"
WEB_SERVICES = {
    "web-tools-egress",
    "web-tools",
    "searxng-egress",
    "searxng",
}
GUARDS = {"web-tools-egress", "searxng-egress"}
HELM_CHART = ROOT / "charts" / "aurum-ai"
REQUIRED_WEB_EXCLUDED_IPV4 = [
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
REQUIRED_WEB_EXCLUDED_IPV6 = [
    "2001::/23",
    "2001:db8::/32",
    "2002::/16",
    "3fff::/20",
]
PINNED_SEARXNG_IMAGE = (
    "docker.io/searxng/searxng@"
    "sha256:55e1fa15a63ff04e79e213e6aa2837549877b0c6d60757cdb633ae9111cb5fea"
)


def _nft_set_members(name: str) -> set[str]:
    """Extract one nftables set's literal elements from rules.nft."""
    body = re.search(
        rf"set {name} \{{.*?elements = \{{(.*?)\}}", NFT_RULES.read_text(), re.S
    )
    assert body is not None, f"nftables set {name} has no literal elements"
    return {item.strip() for item in body.group(1).split(",") if item.strip()}


def _app_policy_networks() -> set[str]:
    """Extract _FORBIDDEN_NETWORKS literals from the web-tools policy module."""
    body = re.search(
        r"_FORBIDDEN_NETWORKS = tuple\(.*?for value in \((.*?)\n    \)\n\)",
        APP_POLICY.read_text(),
        re.S,
    )
    assert body is not None, "_FORBIDDEN_NETWORKS literals not found"
    return set(re.findall(r'"([^"]+)"', body.group(1)))


@pytest.mark.parametrize(
    ("required", "nft_set"),
    (
        (REQUIRED_WEB_EXCLUDED_IPV4, "forbidden_v4"),
        (REQUIRED_WEB_EXCLUDED_IPV6, "forbidden_v6"),
    ),
)
def test_required_exclusions_are_denied_by_compose_and_application(
    required: list[str], nft_set: str
) -> None:
    """The Helm exclusions are a floor, not a ceiling.

    Kubernetes denies everything outside its allow block, so its required
    exclusion list only names reserved ranges *inside* that block. The nftables
    and application deny-lists start from "allow anything not listed" and must
    therefore be supersets of it, or Compose enforces less than Kubernetes.
    """
    nft_members = _nft_set_members(nft_set)
    policy_members = _app_policy_networks()
    missing_nft = [cidr for cidr in required if cidr not in nft_members]
    missing_policy = [cidr for cidr in required if cidr not in policy_members]
    assert not missing_nft, f"rules.nft {nft_set} is missing {missing_nft}"
    assert not missing_policy, f"policy.py _FORBIDDEN_NETWORKS is missing {missing_policy}"



def _helm_template(
    *values: str,
    namespace: str = "platform",
) -> subprocess.CompletedProcess[str]:
    command = [
        "helm",
        "template",
        "web-test",
        str(HELM_CHART),
        "--namespace",
        namespace,
    ]
    for value in values:
        command.extend(("--set", value))
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _helm_resources(*values: str) -> list[dict[str, Any]]:
    result = _helm_template(*values)
    assert result.returncode == 0, result.stderr
    documents = [document for document in yaml.safe_load_all(result.stdout) if document]
    assert all(isinstance(document, dict) for document in documents)
    return documents


def _helm_template_values_file(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "helm",
            "template",
            "web-test",
            str(HELM_CHART),
            "--namespace",
            "platform",
            "--values",
            str(path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _resource(
    resources: list[dict[str, Any]],
    kind: str,
    name: str,
    *,
    namespace: str | None = None,
) -> dict[str, Any]:
    matches = [
        resource
        for resource in resources
        if resource.get("kind") == kind
        and resource.get("metadata", {}).get("name") == name
        and (namespace is None or resource.get("metadata", {}).get("namespace") == namespace)
    ]
    assert (
        len(matches) == 1
    ), f"expected one {kind}/{name} in {namespace or 'any namespace'}, got {matches!r}"
    return matches[0]


def _deployment_container(deployment: dict[str, Any]) -> dict[str, Any]:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    return containers[0]


def _env(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["name"]: entry for entry in container.get("env", [])}


def _pod_template(resources: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return _resource(resources, "Deployment", name)["spec"]["template"]


def test_web_helm_is_default_off_and_rejects_namespace_collisions() -> None:
    disabled = _helm_resources()
    assert not any(
        resource.get("metadata", {}).get("namespace") == "web-test-tools"
        or resource.get("metadata", {}).get("name", "").startswith("web-test-web-")
        for resource in disabled
    )

    for collision in ("platform", "web-test-runners", "web-test-sandboxes"):
        result = _helm_template(
            "web.enabled=true",
            f"web.namespace={collision}",
        )
        assert result.returncode != 0
        assert "web.namespace" in result.stderr


def test_web_workloads_and_backend_gate_have_independent_rollout_switches() -> None:
    disabled = _helm_resources()
    staged = _helm_resources("web.enabled=true", "web.namespace=web-test-tools")
    enabled = _helm_resources(
        "web.enabled=true",
        "web.namespace=web-test-tools",
        "backend.webEnabled=true",
    )
    invalid = _helm_template("backend.webEnabled=true")

    for resources, expected in ((disabled, "false"), (staged, "false"), (enabled, "true")):
        backend = _resource(resources, "Deployment", "web-test-backend")
        assert _env(_deployment_container(backend))["APP_WEB_ENABLED"] == {
            "name": "APP_WEB_ENABLED",
            "value": expected,
        }
    assert invalid.returncode != 0
    assert "backend.webEnabled" in invalid.stderr

    staged_web = [
        resource
        for resource in staged
        if resource.get("metadata", {}).get("namespace") == "web-test-tools"
    ]
    enabled_web = [
        resource
        for resource in enabled
        if resource.get("metadata", {}).get("namespace") == "web-test-tools"
    ]
    assert staged_web == enabled_web


def test_web_helm_requires_a_namespace_local_credential_source() -> None:
    missing = _helm_template(
        "web.enabled=true",
        "web.namespace=web-test-tools",
        "web.serviceSecret=",
        "web.existingSecret=",
    )
    assert missing.returncode != 0
    assert "web.serviceSecret or web.existingSecret" in missing.stderr

    external_without_rotation_token = _helm_template(
        "web.enabled=true",
        "web.namespace=web-test-tools",
        "web.serviceSecret=",
        "web.existingSecret=operator-web-credentials",
    )
    assert external_without_rotation_token.returncode != 0
    assert "web.secretRotationToken" in external_without_rotation_token.stderr

    external = _helm_resources(
        "web.enabled=true",
        "web.namespace=web-test-tools",
        "web.serviceSecret=",
        "web.existingSecret=operator-web-credentials",
        "web.secretRotationToken=version-1",
        "web.createNamespace=false",
    )
    assert not any(
        resource.get("kind") == "Namespace"
        and resource.get("metadata", {}).get("name") == "web-test-tools"
        for resource in external
    )
    assert not any(
        resource.get("kind") == "Secret"
        and resource.get("metadata", {}).get("name") == "operator-web-credentials"
        for resource in external
    )
    backend = _resource(external, "Deployment", "web-test-backend")
    web_tools = _resource(
        external,
        "Deployment",
        "web-test-web-tools",
        namespace="web-test-tools",
    )
    assert _env(_deployment_container(backend))["APP_WEB_TOOLS_SERVICE_SECRET"] == {
        "name": "APP_WEB_TOOLS_SERVICE_SECRET",
        "valueFrom": {
            "secretKeyRef": {
                "name": "operator-web-credentials",
                "key": "WEB_TOOLS_SERVICE_SECRET",
            }
        },
    }
    assert _env(_deployment_container(web_tools))["WEB_TOOLS_SERVICE_SECRET"] == {
        "name": "WEB_TOOLS_SERVICE_SECRET",
        "valueFrom": {
            "secretKeyRef": {
                "name": "operator-web-credentials",
                "key": "WEB_TOOLS_SERVICE_SECRET",
            }
        },
    }


def test_web_helm_renders_hardened_local_workloads_and_secrets() -> None:
    resources = _helm_resources(
        "web.enabled=true",
        "web.namespace=web-test-tools",
    )
    namespace = _resource(resources, "Namespace", "web-test-tools")
    assert namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"

    release_secret = _resource(
        resources,
        "Secret",
        "web-test-web-secrets",
        namespace="platform",
    )
    web_secret = _resource(
        resources,
        "Secret",
        "web-test-web-secrets",
        namespace="web-test-tools",
    )
    assert set(release_secret["stringData"]) == {"WEB_TOOLS_SERVICE_SECRET"}
    assert set(web_secret["stringData"]) == {
        "WEB_TOOLS_SERVICE_SECRET",
        "SEARXNG_SECRET",
    }

    forbidden_env = {
        "APP_DATABASE_URL",
        "APP_REDIS_URL",
        "REDIS_URL",
        "APP_S3_SECRET_KEY",
        "APP_GATEWAY_API_KEY",
        "APP_SECRET_KEY",
        "LANGFUSE_SECRET_KEY",
        "SESSION_TOKEN",
    }
    for name in ("web-tools", "searxng"):
        deployment = _resource(
            resources,
            "Deployment",
            f"web-test-{name}",
            namespace="web-test-tools",
        )
        pod = deployment["spec"]["template"]["spec"]
        container = _deployment_container(deployment)
        security = container["securityContext"]
        assert pod["automountServiceAccountToken"] is False
        assert pod["hostNetwork"] is False
        assert pod["hostPID"] is False
        assert pod["hostIPC"] is False
        assert pod["runtimeClassName"] == "aurum-ai-web-restricted"
        assert pod["securityContext"]["runAsNonRoot"] is True
        if name == "searxng":
            assert pod["securityContext"]["runAsUser"] == 977
            assert pod["securityContext"]["runAsGroup"] == 977
            assert pod["securityContext"]["fsGroup"] == 977
        assert security["allowPrivilegeEscalation"] is False
        assert security["readOnlyRootFilesystem"] is True
        assert security["capabilities"] == {"drop": ["ALL"]}
        assert container["resources"]["requests"]["cpu"]
        assert container["resources"]["requests"]["memory"]
        assert container["resources"]["limits"]["cpu"]
        assert container["resources"]["limits"]["memory"]
        assert not (forbidden_env & set(_env(container)))
        assert all("hostPort" not in port for port in container.get("ports", []))
        if name == "web-tools":
            assert container["readinessProbe"]["httpGet"] == {
                "path": "/ready",
                "port": "http",
                "scheme": "HTTP",
            }
        else:
            assert container["readinessProbe"]["tcpSocket"] == {"port": "http"}

    backend = _resource(resources, "Deployment", "web-test-backend")
    backend_env = _env(_deployment_container(backend))
    assert backend_env["APP_WEB_ENABLED"] == {"name": "APP_WEB_ENABLED", "value": "false"}
    assert backend_env["APP_WEB_TOOLS_URL"] == {
        "name": "APP_WEB_TOOLS_URL",
        "value": "http://web-test-web-tools.web-test-tools.svc:8120",
    }

    for service_name in ("web-tools", "searxng"):
        service = _resource(
            resources,
            "Service",
            f"web-test-{service_name}",
            namespace="web-test-tools",
        )
        assert service["spec"]["type"] == "ClusterIP"
        assert "externalIPs" not in service["spec"]


def test_web_helm_network_policies_are_exact_and_keep_runners_unchanged() -> None:
    disabled = _helm_resources()
    enabled = _helm_resources("web.enabled=true", "web.namespace=web-test-tools")

    def runner_policies(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            resource
            for resource in resources
            if resource.get("kind") == "NetworkPolicy"
            and resource.get("metadata", {}).get("namespace") == "web-test-runners"
        ]

    assert runner_policies(enabled) == runner_policies(disabled)

    web_policies = [
        resource
        for resource in enabled
        if resource.get("kind") == "NetworkPolicy"
        and resource.get("metadata", {}).get("namespace") == "web-test-tools"
    ]
    assert {resource["metadata"]["name"] for resource in web_policies} == {
        "web-test-web-default-deny",
        "web-test-web-tools-ingress",
        "web-test-web-tools-egress",
        "web-test-searxng-ingress",
        "web-test-searxng-egress",
    }
    default_deny = _resource(
        enabled,
        "NetworkPolicy",
        "web-test-web-default-deny",
        namespace="web-test-tools",
    )
    assert default_deny["spec"] == {
        "podSelector": {},
        "policyTypes": ["Ingress", "Egress"],
        "ingress": [],
        "egress": [],
    }

    backend_peer = {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "platform"}},
        "podSelector": {
            "matchLabels": {
                "app.kubernetes.io/instance": "web-test",
                "app.kubernetes.io/name": "backend",
            }
        },
    }
    web_ingress = _resource(
        enabled,
        "NetworkPolicy",
        "web-test-web-tools-ingress",
        namespace="web-test-tools",
    )
    assert web_ingress["spec"]["ingress"] == [
        {
            "from": [backend_peer],
            "ports": [{"protocol": "TCP", "port": 8120}],
        }
    ]
    searxng_ingress = _resource(
        enabled,
        "NetworkPolicy",
        "web-test-searxng-ingress",
        namespace="web-test-tools",
    )
    assert searxng_ingress["spec"]["ingress"] == [
        {
            "from": [
                {
                    "podSelector": {
                        "matchLabels": {
                            "app.kubernetes.io/instance": "web-test",
                            "app.kubernetes.io/name": "web-tools",
                        }
                    }
                }
            ],
            "ports": [{"protocol": "TCP", "port": 8080}],
        }
    ]

    web_tools_egress = _resource(
        enabled,
        "NetworkPolicy",
        "web-test-web-tools-egress",
        namespace="web-test-tools",
    )
    searxng_egress = _resource(
        enabled,
        "NetworkPolicy",
        "web-test-searxng-egress",
        namespace="web-test-tools",
    )
    dns_rule = {
        "to": [
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                },
                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
            }
        ],
        "ports": [
            {"protocol": "UDP", "port": 53},
            {"protocol": "TCP", "port": 53},
        ],
    }
    public_rules = [
        {
            "to": [
                {
                    "ipBlock": {
                        "cidr": "0.0.0.0/0",
                        "except": REQUIRED_WEB_EXCLUDED_IPV4,
                    }
                }
            ],
            "ports": [
                {"protocol": "TCP", "port": 80},
                {"protocol": "TCP", "port": 443},
            ],
        },
        {
            "to": [
                {
                    "ipBlock": {
                        "cidr": "2000::/3",
                        "except": REQUIRED_WEB_EXCLUDED_IPV6,
                    }
                }
            ],
            "ports": [
                {"protocol": "TCP", "port": 80},
                {"protocol": "TCP", "port": 443},
            ],
        },
    ]
    web_tools_selector = {
        "matchLabels": {
            "app.kubernetes.io/name": "web-tools",
            "app.kubernetes.io/instance": "web-test",
        }
    }
    searxng_selector = {
        "matchLabels": {
            "app.kubernetes.io/name": "searxng",
            "app.kubernetes.io/instance": "web-test",
        }
    }
    expected_web_tools_spec = {
        "podSelector": web_tools_selector,
        "policyTypes": ["Egress"],
        "egress": [
            dns_rule,
            {
                "to": [{"podSelector": searxng_selector}],
                "ports": [{"protocol": "TCP", "port": 8080}],
            },
            *public_rules,
        ],
    }
    expected_searxng_spec = {
        "podSelector": searxng_selector,
        "policyTypes": ["Egress"],
        "egress": [dns_rule, *public_rules],
    }
    assert web_tools_egress["spec"] == expected_web_tools_spec
    assert searxng_egress["spec"] == expected_searxng_spec

    mutated = json.loads(json.dumps(web_tools_egress["spec"]))
    mutated["egress"].append({})
    with pytest.raises(AssertionError):
        assert mutated == expected_web_tools_spec


def test_web_helm_rejects_unsafe_network_and_image_values() -> None:
    cases = (
        "web.networkPolicy.publicIpv4Cidr=10.0.0.0/8",
        "web.networkPolicy.publicIpv6Cidr=fc00::/7",
        "web.networkPolicy.excludedIpv4Cidrs={10.0.0.0/8}",
        "web.networkPolicy.excludedIpv6Cidrs={fc00::/7}",
        "web.webTools.image.digest=latest",
        "web.runtimeClassName=",
        "networkPolicies.enabled=false",
        "networkPolicies.dns.enabled=false",
        "networkPolicies.dns.namespace=",
        "networkPolicies.dns.podLabels.k8s-app=",
        "web.webTools.replicaCount=0",
        "web.searxng.replicaCount=0",
        "web.webTools.resources.limits.cpu=",
        "web.searxng.resources.limits.memory=",
    )
    for unsafe in cases:
        result = _helm_template(
            "web.enabled=true",
            "web.namespace=web-test-tools",
            unsafe,
        )
        assert result.returncode != 0, unsafe

    digest = "sha256:" + "a" * 64
    resources = _helm_resources(
        "web.enabled=true",
        "web.namespace=web-test-tools",
        f"web.webTools.image.digest={digest}",
    )
    web_tools = _resource(
        resources,
        "Deployment",
        "web-test-web-tools",
        namespace="web-test-tools",
    )
    assert _deployment_container(web_tools)["image"] == f"aurum-ai-web-tools@{digest}"


@pytest.mark.parametrize(
    "malformed",
    (
        "1:2:3/64",
        "abcdef::/64",
        "deadbeef/64",
        "::::/64",
        ":::/64",
        "2001:db8:::1/64",
        ":1:2:3:4:5:6:7:8/64",
        "1:2:3:4:5:6:7:8:/64",
    ),
)
def test_web_helm_rejects_malformed_ipv6_cidrs(tmp_path: Path, malformed: str) -> None:
    values = {
        "web": {
            "enabled": True,
            "namespace": "web-test-tools",
            "networkPolicy": {
                "excludedIpv6Cidrs": [*REQUIRED_WEB_EXCLUDED_IPV6, malformed],
            },
        }
    }
    path = tmp_path / "malformed-ipv6.yaml"
    path.write_text(yaml.safe_dump(values))
    result = _helm_template_values_file(path)
    assert result.returncode != 0
    assert "invalid IPv6 CIDR" in result.stderr


def test_web_helm_accepts_valid_compressed_ipv6_cidr(tmp_path: Path) -> None:
    values = {
        "web": {
            "enabled": True,
            "namespace": "web-test-tools",
            "networkPolicy": {
                "excludedIpv6Cidrs": [
                    *REQUIRED_WEB_EXCLUDED_IPV6,
                    "2001:db8:abcd::/48",
                ],
            },
        }
    }
    path = tmp_path / "valid-ipv6.yaml"
    path.write_text(yaml.safe_dump(values))
    result = _helm_template_values_file(path)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("removed", ("169.254.0.0/16", "2001::/23", "3fff::/20", "2002::/16"))
def test_web_helm_rejects_removing_required_public_exclusion(tmp_path: Path, removed: str) -> None:
    values = {
        "web": {
            "enabled": True,
            "namespace": "web-test-tools",
            "networkPolicy": {
                "excludedIpv4Cidrs": [
                    cidr for cidr in REQUIRED_WEB_EXCLUDED_IPV4 if cidr != removed
                ],
                "excludedIpv6Cidrs": [
                    cidr for cidr in REQUIRED_WEB_EXCLUDED_IPV6 if cidr != removed
                ],
            },
        }
    }
    path = tmp_path / "removed-exclusion.yaml"
    path.write_text(yaml.safe_dump(values))
    result = _helm_template_values_file(path)
    assert result.returncode != 0
    assert "must retain required exclusion" in result.stderr


@pytest.mark.parametrize(
    "dns",
    (
        {"namespace": "", "podLabels": {"k8s-app": "kube-dns"}},
        {"namespace": "kube-system", "podLabels": {}},
        {"namespace": "kube-system", "podLabels": None},
        {"namespace": "kube-system", "podLabels": {"k8s-app": ""}},
    ),
)
def test_web_helm_rejects_broadened_dns_selector(tmp_path: Path, dns: object) -> None:
    values = {
        "web": {"enabled": True, "namespace": "web-test-tools"},
        "networkPolicies": {"dns": dns},
    }
    path = tmp_path / "unsafe-dns.yaml"
    path.write_text(yaml.safe_dump(values))
    result = _helm_template_values_file(path)
    assert result.returncode != 0
    assert "networkPolicies.dns" in result.stderr

    # Runner DNS is unconditional, so an unsafe selector must also fail while
    # the isolated web workload is disabled.
    values["web"] = {"enabled": False}
    disabled_path = tmp_path / "unsafe-dns-disabled-web.yaml"
    disabled_path.write_text(yaml.safe_dump(values))
    disabled_result = _helm_template_values_file(disabled_path)
    assert disabled_result.returncode != 0
    assert "networkPolicies.dns" in disabled_result.stderr


def test_web_deployments_roll_for_config_and_secret_rotation() -> None:
    managed_one = _helm_resources("web.enabled=true", "web.namespace=web-test-tools")
    managed_two = _helm_resources(
        "web.enabled=true",
        "web.namespace=web-test-tools",
        "web.serviceSecret=changed-web-service-secret",
        "web.pidLimit=65",
    )
    external_one = _helm_resources(
        "web.enabled=true",
        "web.namespace=web-test-tools",
        "web.serviceSecret=",
        "web.existingSecret=operator-web-credentials",
        "web.secretRotationToken=version-1",
        "web.createNamespace=false",
    )
    external_two = _helm_resources(
        "web.enabled=true",
        "web.namespace=web-test-tools",
        "web.serviceSecret=",
        "web.existingSecret=operator-web-credentials",
        "web.secretRotationToken=version-2",
        "web.createNamespace=false",
    )
    external_reference_changed = _helm_resources(
        "web.enabled=true",
        "web.namespace=web-test-tools",
        "web.serviceSecret=",
        "web.existingSecret=replaced-web-credentials",
        "web.secretRotationToken=version-2",
        "web.createNamespace=false",
    )

    for deployment_name in (
        "web-test-backend",
        "web-test-web-tools",
        "web-test-searxng",
    ):
        managed_annotations_one = _pod_template(managed_one, deployment_name)["metadata"][
            "annotations"
        ]
        managed_annotations_two = _pod_template(managed_two, deployment_name)["metadata"][
            "annotations"
        ]
        external_annotations_one = _pod_template(external_one, deployment_name)["metadata"][
            "annotations"
        ]
        external_annotations_two = _pod_template(external_two, deployment_name)["metadata"][
            "annotations"
        ]
        external_reference_annotations = _pod_template(external_reference_changed, deployment_name)[
            "metadata"
        ]["annotations"]
        assert (
            managed_annotations_one["checksum/web-secret"]
            != managed_annotations_two["checksum/web-secret"]
        )
        assert (
            managed_annotations_one["checksum/web-config"]
            != managed_annotations_two["checksum/web-config"]
        )
        assert (
            external_annotations_one["checksum/web-secret"]
            != external_annotations_two["checksum/web-secret"]
        )
        assert (
            external_annotations_two["checksum/web-secret"]
            != external_reference_annotations["checksum/web-secret"]
        )


def test_web_helm_exposes_explicit_operational_gates_without_sensitive_labels() -> None:
    resources = _helm_resources("web.enabled=true", "web.namespace=web-test-tools")
    config = _resource(
        resources,
        "ConfigMap",
        "web-test-web-config",
        namespace="web-test-tools",
    )
    gates = yaml.safe_load(config["data"]["operational-gates.yaml"])
    assert gates["cniNetworkPolicyEnforcementRequired"] is True
    assert gates["fqdnAllowlistClaimed"] is False
    assert gates["pidLimit"] == {
        "runtimeClassRequired": "aurum-ai-web-restricted",
        "kubeletPodPidsLimitMaximum": 64,
    }
    assert gates["rollout"] == [
        "migrations",
        "backend-gate-off",
        "web-workloads-and-policy-enforcement",
        "runner-and-ui",
        "operator-enable",
    ]
    assert gates["rollback"] == {
        "gateOffBlocksNewCalls": True,
        "existingTurnSettlementPreserved": True,
        "sourceReadsPreserved": True,
    }
    assert gates["metrics"]["allowedLabels"] == ["tool", "status"]
    assert set(gates["metrics"]["forbiddenLabels"]) == {
        "org",
        "query",
        "url",
        "content",
    }
    assert set(gates["metrics"]["alertStatuses"]) == {
        "web_timeout",
        "web_unavailable",
        "web_url_blocked",
        "web_too_large",
        "unsupported_document",
    }
    assert gates["metrics"]["alertConditions"] == {
        "boundedQueue": ["web_unavailable"],
        "timeout": ["web_timeout"],
        "upstream429": ["web_unavailable"],
        "policyDeny": ["web_url_blocked"],
        "parserFailure": [
            "web_unavailable",
            "web_timeout",
            "web_too_large",
            "unsupported_document",
        ],
    }
    alert_document = yaml.safe_load(config["data"]["prometheus-alerts.yml"])
    rules = alert_document["groups"][0]["rules"]
    assert {rule["alert"] for rule in rules} == {
        "WebToolsTimeouts",
        "WebToolsUnavailable",
        "WebToolsPolicyDenials",
        "WebToolsParserFailures",
    }
    assert all("sum by (tool, status)" in rule["expr"] for rule in rules)
    serialized_rules = yaml.safe_dump(alert_document).lower()
    for sensitive_label in ("org_id", "query=", "url=", "content="):
        assert sensitive_label not in serialized_rules
    unavailable = next(rule for rule in rules if rule["alert"] == "WebToolsUnavailable")
    assert "bounded queue" in unavailable["annotations"]["description"].lower()
    assert "upstream 429" in unavailable["annotations"]["description"].lower()
    parser = next(rule for rule in rules if rule["alert"] == "WebToolsParserFailures")
    assert 'tool="web_read"' in parser["expr"]
    parser_description = parser["annotations"]["description"].lower()
    assert "may include" in parser_description
    assert "isolated parser reported" not in parser_description


@pytest.mark.parametrize(
    ("changed_path", "expected_images"),
    (
        (
            "infra/web-egress/Dockerfile",
            {"web-tools", "infra/web-egress"},
        ),
        (
            "charts/aurum-ai/templates/web-network-policies.yaml",
            {"web-tools"},
        ),
    ),
)
def test_ci_routes_web_infrastructure_changes_through_boundary_jobs(
    tmp_path: Path,
    changed_path: str,
    expected_images: set[str],
) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    pick_step = next(
        step for step in workflow["jobs"]["changes"]["steps"] if step.get("id") == "pick"
    )
    script = pick_step["run"]

    repository = tmp_path / "repo"
    repository.mkdir()
    _must_run("git", "init", str(repository))
    marker = repository / "base"
    marker.write_text("base\n")
    _must_run("git", "-C", str(repository), "add", "base")
    _must_run(
        "git",
        "-C",
        str(repository),
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "base",
    )
    base_sha = _must_run("git", "-C", str(repository), "rev-parse", "HEAD")
    changed = repository / changed_path
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text("changed\n")
    _must_run("git", "-C", str(repository), "add", changed_path)
    _must_run(
        "git",
        "-C",
        str(repository),
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "change",
    )
    head_sha = _must_run("git", "-C", str(repository), "rev-parse", "HEAD")
    output_path = tmp_path / "github-output"
    environment = os.environ.copy()
    environment.update(
        {
            "BASE_SHA": base_sha,
            "EVENT": "pull_request",
            "HEAD_SHA": head_sha,
            "GITHUB_OUTPUT": str(output_path),
        }
    )
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=repository,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    outputs = {
        key: json.loads(value)
        for key, value in (line.split("=", 1) for line in output_path.read_text().splitlines())
    }
    assert set(outputs["python"]) == {"web-tools"}
    assert set(outputs["harness"]) == {"scripts"}
    assert set(outputs["images"]) == expected_images


def _compose(*args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "APP_SECRET_KEY": "deployment-contract-secret-not-for-runtime",
            "KEYCLOAK_ADMIN_PASSWORD": "deployment-contract-keycloak-not-for-runtime",
            "WEB_TOOLS_SERVICE_SECRET": "web-tools-contract-secret",
            "SEARXNG_SECRET": "searxng-contract-secret",
        }
    )
    return subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(BASE_COMPOSE),
            "-f",
            str(WEB_COMPOSE),
            "--profile",
            "web",
            "--profile",
            "build",
            *args,
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _probe_compose(*args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "APP_SECRET_KEY": "deployment-contract-secret-not-for-runtime",
            "KEYCLOAK_ADMIN_PASSWORD": "deployment-contract-keycloak-not-for-runtime",
            "WEB_TOOLS_SERVICE_SECRET": "web-tools-contract-secret",
            "SEARXNG_SECRET": "searxng-contract-secret",
        }
    )
    return subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(BASE_COMPOSE),
            "-f",
            str(WEB_COMPOSE),
            "-f",
            str(WEB_PROBES_COMPOSE),
            "--profile",
            "web",
            *args,
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _controlled_compose(*args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "APP_SECRET_KEY": "deployment-contract-secret-not-for-runtime",
            "KEYCLOAK_ADMIN_PASSWORD": "deployment-contract-keycloak-not-for-runtime",
            "WEB_TOOLS_SERVICE_SECRET": "web-tools-contract-secret",
            "SEARXNG_SECRET": "searxng-contract-secret",
        }
    )
    return subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(BASE_COMPOSE),
            "-f",
            str(WEB_COMPOSE),
            "-f",
            str(WEB_PROBES_COMPOSE),
            "-f",
            str(WEB_CONTROLLED_COMPOSE),
            "--profile",
            "web",
            *args,
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.fixture(scope="module")
def rendered_compose() -> dict[str, Any]:
    result = _compose("config", "--format", "json")
    if result.returncode != 0:
        pytest.skip(f"web compose does not render yet: {result.stderr}")
    document = json.loads(result.stdout)
    assert isinstance(document, dict)
    return document


def test_web_compose_renders_successfully() -> None:
    result = _compose("config", "--format", "json")

    assert result.returncode == 0, result.stderr
    assert WEB_SERVICES <= set(json.loads(result.stdout)["services"])


def test_acceptance_fixture_addresses_are_reserved_from_dynamic_ipam() -> None:
    result = _probe_compose("config", "--format", "json")

    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    fixture_networks = document["services"]["web-private-fixture"]["networks"]

    for network_name, attachment in fixture_networks.items():
        configs = document["networks"][network_name]["ipam"]["config"]
        for address_key in ("ipv4_address", "ipv6_address"):
            if address_key not in attachment:
                continue
            fixture_address = ipaddress.ip_address(attachment[address_key])
            matching_config = next(
                config
                for config in configs
                if fixture_address in ipaddress.ip_network(config["subnet"])
            )
            dynamic_pool = ipaddress.ip_network(
                matching_config.get("ip_range", matching_config["subnet"])
            )
            assert fixture_address not in dynamic_pool, (
                f"{network_name} fixture address {fixture_address} overlaps "
                f"dynamic IPAM pool {dynamic_pool}"
            )


def test_acceptance_guard_resolves_backend_only_on_ingress_network() -> None:
    result = _probe_compose("config", "--format", "json")

    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    backend_networks = services["backend"]["networks"]
    guard_networks = services["web-tools-egress"]["networks"]
    ingress_host = services["web-tools-egress"]["environment"]["WEB_EGRESS_INGRESS_HOST"]

    assert ingress_host != "backend"
    resolving_networks = [
        network_name
        for network_name, attachment in backend_networks.items()
        if ingress_host in (attachment or {}).get("aliases", [])
    ]
    assert resolving_networks == ["web-tools-ingress"]
    assert "web-tools-ingress" in guard_networks


def test_controlled_rebinding_resolver_is_mounted_into_requesting_application() -> None:
    public_result = _probe_compose("config", "--format", "json")
    controlled_result = _controlled_compose("config", "--format", "json")

    assert public_result.returncode == 0, public_result.stderr
    assert controlled_result.returncode == 0, controlled_result.stderr
    public_services = json.loads(public_result.stdout)["services"]
    controlled_services = json.loads(controlled_result.stdout)["services"]

    assert "dns" not in public_services["web-tools-egress"]
    assert "dns" not in public_services["web-tools"]
    assert controlled_services["web-tools-egress"]["dns"] == ["93.184.216.2"]
    assert "dns" not in controlled_services["web-tools"]
    assert not any(
        volume.get("target") == "/etc/resolv.conf"
        for volume in public_services["web-tools"].get("volumes", [])
        if isinstance(volume, dict)
    )
    assert not any(
        volume.get("target") == "/etc/resolv.conf"
        for volume in controlled_services["web-tools-egress"].get("volumes", [])
        if isinstance(volume, dict)
    )
    resolver = next(
        volume
        for volume in controlled_services["web-tools"]["volumes"]
        if volume.get("target") == "/etc/resolv.conf"
    )
    assert resolver["source"] == str(
        ROOT / "scripts" / "fixtures" / "web-tools-e2e-resolv.conf"
    )
    assert resolver["read_only"] is True
    assert (
        controlled_services["web-tools"]["environment"]["WEB_TOOLS_SEARXNG_URL"]
        == "http://93.184.216.2:8080"
    )
    assert (
        controlled_services["backend"]["environment"]["APP_WEB_TOOLS_URL"]
        == "http://web-tools:8120"
    )
    assert "WEB_TOOLS_URL" not in controlled_services["backend"]["environment"]
    assert (
        controlled_services["web-tools-egress"]["environment"][
            "WEB_EGRESS_INGRESS_HOST"
        ]
        == "web-tools-backend"
    )
    assert (
        controlled_services["web-tools-egress"]["environment"][
            "WEB_EGRESS_INTERNAL_HOST"
        ]
        == "web-fixture"
    )
    ingress_network = "web-tools-ingress"
    assert "ipv4_address" not in controlled_services["backend"]["networks"][
        ingress_network
    ]
    assert "ipv4_address" not in controlled_services["web-tools-egress"]["networks"][
        ingress_network
    ]


def _mount_source(volume: object) -> str:
    if isinstance(volume, str):
        return volume.split(":", 1)[0]
    assert isinstance(volume, dict)
    return str(volume.get("source", ""))


def _run(*command: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _must_run(*command: str, timeout: int = 60) -> str:
    result = _run(*command, timeout=timeout)
    assert (
        result.returncode == 0
    ), f"command failed: {command!r}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return result.stdout.strip()


def _connection(container: str, host: str, port: int) -> subprocess.CompletedProcess[str]:
    program = (
        "import socket; " f"sock=socket.create_connection(({host!r}, {port}), 2); " "sock.close()"
    )
    return _run("docker", "exec", container, "python3", "-c", program, timeout=10)


def _wait_connection(container: str, host: str, port: int, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        result = _connection(container, host, port)
        if result.returncode == 0:
            return
        last_error = result.stderr
        time.sleep(0.1)
    pytest.fail(f"{container} could not connect to {host}:{port}: {last_error}")


def _wait_healthy(container: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        state = _must_run("docker", "inspect", "--format", "{{.State.Health.Status}}", container)
        if state == "healthy":
            return
        if state == "unhealthy":
            logs = _must_run("docker", "logs", container)
            pytest.fail(f"{container} became unhealthy:\n{logs}")
        time.sleep(0.25)
    pytest.fail(f"{container} did not become healthy")


def _wait_state(container: str, expected: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    state = ""
    while time.monotonic() < deadline:
        state = _must_run("docker", "inspect", "--format", "{{.State.Status}}", container)
        if state == expected:
            return
        time.sleep(0.25)
    pytest.fail(f"{container} state remained {state!r}, expected {expected!r}")


def _wait_health(container: str, expected: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    health = ""
    while time.monotonic() < deadline:
        health = _must_run("docker", "inspect", "--format", "{{.State.Health.Status}}", container)
        if health == expected:
            return
        time.sleep(0.25)
    pytest.fail(f"{container} health remained {health!r}, expected {expected!r}")


class _DockerScope:
    def __init__(self) -> None:
        self.containers: list[str] = []
        self.networks: list[str] = []
        self.images: list[str] = []

    def __enter__(self) -> _DockerScope:
        return self

    def _list_command(self, kind: str, identity: str) -> tuple[str, ...]:
        if kind == "container":
            return (
                "docker",
                "container",
                "ls",
                "--all",
                "--filter",
                f"name={identity}",
                "--format",
                "{{.Names}}",
            )
        if kind == "network":
            return (
                "docker",
                "network",
                "ls",
                "--filter",
                f"name={identity}",
                "--format",
                "{{.Name}}",
            )
        if kind == "image":
            return (
                "docker",
                "image",
                "ls",
                "--filter",
                f"reference={identity}",
                "--format",
                "{{.Repository}}:{{.Tag}}",
            )
        raise AssertionError(f"unsupported Docker resource kind: {kind}")

    def _presence(self, kind: str, identity: str) -> tuple[bool | None, str | None]:
        command = self._list_command(kind, identity)
        result = _run(*command)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or "no diagnostic output"
            return None, (
                f"could not query owned {kind} {identity}: command {command!r} "
                f"exited {result.returncode}: {detail}"
            )
        return identity in result.stdout.splitlines(), None

    def _remove_if_present(self, kind: str, identity: str) -> list[str]:
        errors: list[str] = []
        present, query_error = self._presence(kind, identity)
        if query_error is not None:
            errors.append(query_error)
        if present is not False:
            remove = _run("docker", kind, "rm", "-f", identity)
            if remove.returncode != 0:
                detail = (remove.stderr or remove.stdout).strip() or "no diagnostic output"
                errors.append(
                    f"could not remove owned {kind} {identity}: command "
                    f"{remove.args!r} exited {remove.returncode}: {detail}"
                )

        remains, verify_error = self._presence(kind, identity)
        if verify_error is not None:
            errors.append(verify_error)
        elif remains:
            errors.append(f"owned {kind} still exists: {identity}")
        return errors

    def cleanup(self) -> list[str]:
        errors: list[str] = []
        for container in reversed(self.containers):
            errors.extend(self._remove_if_present("container", container))
        for network in reversed(self.networks):
            errors.extend(self._remove_if_present("network", network))
        for image in reversed(self.images):
            errors.extend(self._remove_if_present("image", image))
        return errors

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        errors = self.cleanup()
        if errors:
            message = "Docker cleanup incomplete:\n" + "\n".join(errors)
            if exc is not None:
                message += f"\nOriginal test failure: {exc!r}"
            raise AssertionError(message) from exc
        return False


def test_docker_cleanup_fails_closed_on_resource_list_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    successful_query_counts = {"owned-network": 0, "owned-image:test": 0}

    def fake_run(*command: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        del timeout
        calls.append(command)
        if any("owned-container" in argument for argument in command) and "rm" not in command:
            return subprocess.CompletedProcess(
                command,
                125,
                stdout="",
                stderr="permission denied while querying Docker API",
            )
        if "ls" in command:
            for identity in successful_query_counts:
                if any(identity in argument for argument in command):
                    successful_query_counts[identity] += 1
                    stdout = identity + "\n" if successful_query_counts[identity] == 1 else ""
                    return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(sys.modules[__name__], "_run", fake_run)
    scope = _DockerScope()
    scope.containers = ["owned-container"]
    scope.networks = ["owned-network"]
    scope.images = ["owned-image:test"]

    with pytest.raises(AssertionError) as failure:
        with scope:
            pass

    message = str(failure.value)
    assert "owned-container" in message
    assert "permission denied while querying Docker API" in message
    assert "docker" in message and "container" in message and "ls" in message
    container_calls = [
        command for command in calls if any("owned-container" in argument for argument in command)
    ]
    assert sum("ls" in command for command in container_calls) == 2
    assert any("rm" in command for command in container_calls)
    assert any(any("owned-network" in argument for argument in command) for command in calls)
    assert any(any("owned-image:test" in argument for argument in command) for command in calls)
    for identity in successful_query_counts:
        assert successful_query_counts[identity] == 2
        assert any(
            "rm" in command and any(identity in argument for argument in command)
            for command in calls
        )


def test_pinned_searxng_starts_with_rendered_nonroot_security() -> None:
    daemon = _run("docker", "info", timeout=10)
    if daemon.returncode != 0:
        pytest.skip(f"Docker daemon unavailable: {daemon.stderr.strip()}")

    resources = _helm_resources("web.enabled=true", "web.namespace=web-test-tools")
    deployment = _resource(
        resources,
        "Deployment",
        "web-test-searxng",
        namespace="web-test-tools",
    )
    pod_security = deployment["spec"]["template"]["spec"]["securityContext"]
    container = _deployment_container(deployment)
    assert container["image"] == PINNED_SEARXNG_IMAGE
    assert pod_security["runAsUser"] == 977
    assert pod_security["runAsGroup"] == 977
    assert pod_security["fsGroup"] == 977

    account = _run(
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "/bin/sh",
        PINNED_SEARXNG_IMAGE,
        "-c",
        "getent passwd searxng; getent group searxng",
        timeout=60,
    )
    assert account.returncode == 0, account.stderr
    assert "searxng:x:977:977:" in account.stdout
    assert "searxng:x:977:" in account.stdout
    image_metadata = json.loads(_must_run("docker", "image", "inspect", PINNED_SEARXNG_IMAGE))[0]
    # Docker omits an empty OCI Config.User on some API versions; both forms
    # mean the image itself does not select a non-root identity.
    assert image_metadata["Config"].get("User", "") == ""
    assert image_metadata["Config"]["Entrypoint"] == ["/usr/local/searxng/entrypoint.sh"]

    suffix = uuid.uuid4().hex[:12]
    name = f"searxng-nonroot-{suffix}"
    with _DockerScope() as scope:
        scope.containers.append(name)
        started = _run(
            "docker",
            "run",
            "--detach",
            "--name",
            name,
            "--user",
            "977:977",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--network",
            "none",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m,uid=0,gid=977,mode=0770",
            "--tmpfs",
            "/var/cache/searxng:rw,noexec,nosuid,size=32m,uid=0,gid=977,mode=0770",
            "--tmpfs",
            "/var/log/searxng:rw,noexec,nosuid,size=8m,uid=0,gid=977,mode=0770",
            "--env",
            "SEARXNG_SECRET=nonroot-startup-contract-secret",
            "--volume",
            f"{ROOT / 'infra/searxng/settings.yml'}:/etc/searxng/settings.yml:ro",
            PINNED_SEARXNG_IMAGE,
            timeout=60,
        )
        assert started.returncode == 0, started.stderr
        _wait_connection(name, "127.0.0.1", 8080, timeout=30)
        assert _must_run("docker", "inspect", "--format", "{{.State.Status}}", name) == "running"


def _kernel_image(scope: _DockerScope, suffix: str) -> str:
    image = f"web-egress-kernel-test:{suffix}"
    _must_run(
        "docker",
        "build",
        "-q",
        "-t",
        image,
        str(ROOT / "infra/web-egress"),
        timeout=120,
    )
    scope.images.append(image)
    return image


def test_web_workloads_are_opt_in_and_operator_gate_defaults_off(
    rendered_compose: dict[str, Any],
) -> None:
    services = rendered_compose["services"]

    assert all(services[name]["profiles"] == ["web"] for name in WEB_SERVICES)
    assert services["backend"]["environment"]["APP_WEB_ENABLED"] == "false"
    assert (
        "web"
        not in subprocess.run(
            ["docker", "compose", "-f", str(BASE_COMPOSE), "config", "--profiles"],
            cwd=ROOT,
            env={
                **os.environ,
                "APP_SECRET_KEY": os.environ.get("APP_SECRET_KEY", "deployment-contract-secret-not-for-runtime"),
                "KEYCLOAK_ADMIN_PASSWORD": "deployment-contract-keycloak-not-for-runtime",
            },
            text=True,
            capture_output=True,
            check=True,
        ).stdout.splitlines()
    )


def test_only_namespace_guards_receive_network_administration(
    rendered_compose: dict[str, Any],
) -> None:
    services = rendered_compose["services"]

    for name in WEB_SERVICES:
        service = services[name]
        assert service.get("privileged", False) is False
        assert service.get("network_mode") != "host"
        assert all(
            _mount_source(volume) != "/var/run/docker.sock" for volume in service.get("volumes", [])
        )
        capabilities = set(service.get("cap_add", []))
        if name in GUARDS:
            assert capabilities == {"NET_ADMIN"}
        else:
            assert capabilities == set()
        assert set(service.get("cap_drop", [])) == {"ALL"}


def test_applications_share_guard_namespaces_and_wait_for_enforcement(
    rendered_compose: dict[str, Any],
) -> None:
    services = rendered_compose["services"]

    for application, guard in (
        ("web-tools", "web-tools-egress"),
        ("searxng", "searxng-egress"),
    ):
        service = services[application]
        assert service["network_mode"] == f"service:{guard}"
        assert "networks" not in service
        assert service["depends_on"][guard]["condition"] == "service_healthy"
        assert service["depends_on"][guard]["required"] is True

        ready_port = services[guard]["environment"]["WEB_EGRESS_READY_PORT"]
        assert service["entrypoint"][:5] == [
            "python3",
            "/usr/local/bin/web-egress-readiness",
            "--wait",
            "--port",
            ready_port,
        ]
        readiness_mounts = [
            volume
            for volume in service.get("volumes", [])
            if _mount_source(volume) == str(READINESS_PROGRAM)
        ]
        assert len(readiness_mounts) == 1
        assert readiness_mounts[0]["read_only"] is True

        healthcheck = services[guard]["healthcheck"]["test"]
        assert healthcheck[:5] == [
            "CMD",
            "python3",
            "/usr/local/bin/web-egress-readiness",
            "--check",
            "--port",
        ]
        assert healthcheck[5] == ready_port


def test_readiness_wrapper_executes_each_application_command(
    rendered_compose: dict[str, Any],
) -> None:
    services = rendered_compose["services"]

    assert services["web-tools"]["entrypoint"][5:] == [
        "--",
        "python",
        "-m",
        "src.main",
    ]
    assert services["searxng"]["entrypoint"][5:] == [
        "--",
        "/usr/local/searxng/entrypoint.sh",
    ]


def test_external_web_workloads_have_no_host_or_runner_reachability(
    rendered_compose: dict[str, Any],
) -> None:
    services = rendered_compose["services"]
    networks = rendered_compose["networks"]

    assert all("ports" not in services[name] for name in WEB_SERVICES)
    assert set(services["backend"]["networks"]) >= {"web-tools-ingress"}
    assert set(services["web-tools-egress"]["networks"]) == {
        "web-tools-ingress",
        "web-search",
        "web-public",
    }
    assert set(services["searxng-egress"]["networks"]) == {
        "web-search",
        "web-public",
    }
    assert services["web-tools-egress"]["networks"]["web-public"].get("gw_priority") == 1
    assert services["searxng-egress"]["networks"]["web-public"].get("gw_priority") == 1
    assert networks["web-tools-ingress"]["internal"] is True
    assert networks["web-search"]["internal"] is True
    assert networks["web-public"].get("internal", False) is False
    assert networks["web-tools-ingress"]["enable_ipv6"] is True
    assert networks["web-search"]["enable_ipv6"] is True
    assert networks["web-public"]["enable_ipv6"] is True
    assert not (
        {"web-tools-ingress", "web-search", "web-public"}
        & set(services["runner"].get("networks", []))
    )


def test_kernel_readiness_rejects_empty_table_and_failed_install(
    tmp_path: Path,
) -> None:
    if os.environ.get("WEB_EGRESS_KERNEL_TEST") != "1":
        pytest.skip("set WEB_EGRESS_KERNEL_TEST=1 to run the disposable kernel acceptance")

    suffix = f"readiness-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    empty_guard = f"web-egress-empty-{suffix}"
    failed_guard = f"web-egress-failed-{suffix}"
    with _DockerScope() as scope:
        image = _kernel_image(scope, suffix)
        _must_run(
            "docker",
            "run",
            "-d",
            "--name",
            empty_guard,
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "NET_ADMIN",
            "--entrypoint",
            "sh",
            image,
            "-c",
            "nft add table inet web_egress; sleep 30",
        )
        scope.containers.append(empty_guard)

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            health = _must_run(
                "docker",
                "inspect",
                "--format",
                "{{.State.Health.Status}}",
                empty_guard,
            )
            assert health != "healthy", "an empty nft table must never satisfy readiness"
            if health == "unhealthy":
                break
            time.sleep(0.25)
        else:
            pytest.fail("empty-table guard did not become unhealthy")

        invalid_rules = tmp_path / "invalid-web-egress.nft"
        invalid_rules.write_text("this is not valid nft syntax\n")
        _must_run(
            "docker",
            "run",
            "-d",
            "--name",
            failed_guard,
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "NET_ADMIN",
            "--read-only",
            "--tmpfs",
            "/run:rw,noexec,nosuid,size=1m",
            "-v",
            f"{invalid_rules}:/etc/web-egress/rules.nft:ro",
            "-e",
            "WEB_EGRESS_MODE=web-tools",
            "-e",
            "WEB_EGRESS_LOCAL_PORT=8120",
            "-e",
            "WEB_EGRESS_READY_PORT=8190",
            "-e",
            "WEB_EGRESS_INGRESS_HOST=127.0.0.1",
            "-e",
            "WEB_EGRESS_INGRESS_PORT=8120",
            "-e",
            "WEB_EGRESS_INTERNAL_HOST=127.0.0.1",
            "-e",
            "WEB_EGRESS_INTERNAL_PORT=8080",
            image,
        )
        scope.containers.append(failed_guard)
        _wait_state(failed_guard, "exited")
        health = _must_run(
            "docker",
            "inspect",
            "--format",
            "{{.State.Health.Status}}",
            failed_guard,
        )
        assert health != "healthy"


def test_kernel_fresh_namespace_gates_application_until_policy_ready() -> None:
    if os.environ.get("WEB_EGRESS_KERNEL_TEST") != "1":
        pytest.skip("set WEB_EGRESS_KERNEL_TEST=1 to run the disposable kernel acceptance")

    suffix = f"lifecycle-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    ingress = f"web-egress-ingress-{suffix}"
    search = f"web-egress-search-{suffix}"
    backend = f"web-egress-backend-{suffix}"
    guard = f"web-egress-guard-{suffix}"
    application = f"web-egress-app-{suffix}"
    late_peer = f"web-egress-late-peer-{suffix}"
    ready_port = 8194
    with _DockerScope() as scope:
        image = _kernel_image(scope, suffix)
        for network_name, subnet in (
            (ingress, "172.31.244.0/24"),
            (search, "172.31.245.0/24"),
        ):
            _must_run("docker", "network", "create", "--subnet", subnet, network_name)
            scope.networks.append(network_name)

        _must_run(
            "docker",
            "run",
            "-d",
            "--name",
            backend,
            "--network",
            ingress,
            "--network-alias",
            "backend",
            "--entrypoint",
            "python3",
            image,
            "-m",
            "http.server",
            "8000",
        )
        scope.containers.append(backend)
        _wait_connection(backend, "127.0.0.1", 8000)
        backend_address = _must_run(
            "docker",
            "inspect",
            "--format",
            f'{{{{(index .NetworkSettings.Networks "{ingress}").IPAddress}}}}',
            backend,
        )
        _must_run(
            "docker",
            "create",
            "--name",
            guard,
            "--network",
            ingress,
            "--network-alias",
            "gated-web-tools",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "NET_ADMIN",
            "--read-only",
            "--tmpfs",
            "/run:rw,noexec,nosuid,size=1m",
            "-e",
            "WEB_EGRESS_MODE=web-tools",
            "-e",
            "WEB_EGRESS_LOCAL_PORT=8124",
            "-e",
            f"WEB_EGRESS_READY_PORT={ready_port}",
            "-e",
            "WEB_EGRESS_INGRESS_HOST=backend",
            "-e",
            "WEB_EGRESS_INGRESS_PORT=8124",
            "-e",
            "WEB_EGRESS_INTERNAL_HOST=late-search",
            "-e",
            "WEB_EGRESS_INTERNAL_PORT=8080",
            image,
        )
        scope.containers.append(guard)
        _must_run("docker", "network", "connect", search, guard)
        _must_run("docker", "start", guard)
        _wait_state(guard, "running")

        payload = (
            "import pathlib; "
            "pathlib.Path('/tmp/application-started').write_text('started'); "
            "import http.server; http.server.test(port=8124)"
        )
        _must_run(
            "docker",
            "run",
            "-d",
            "--name",
            application,
            "--network",
            f"container:{guard}",
            "--cap-drop",
            "ALL",
            "--entrypoint",
            "/usr/local/bin/web-egress-readiness",
            image,
            "--wait",
            "--port",
            str(ready_port),
            "--",
            "python3",
            "-c",
            payload,
        )
        scope.containers.append(application)
        time.sleep(1)
        assert (
            _run("docker", "exec", application, "test", "-e", "/tmp/application-started").returncode
            != 0
        )
        assert _connection(application, backend_address, 8000).returncode != 0

        _must_run(
            "docker",
            "run",
            "-d",
            "--name",
            late_peer,
            "--network",
            search,
            "--network-alias",
            "late-search",
            "--entrypoint",
            "python3",
            image,
            "-m",
            "http.server",
            "8080",
        )
        scope.containers.append(late_peer)
        _wait_healthy(guard)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (
                _run(
                    "docker", "exec", application, "test", "-e", "/tmp/application-started"
                ).returncode
                == 0
            ):
                break
            time.sleep(0.1)
        else:
            pytest.fail("application payload did not start after enforced readiness")


def test_kernel_ipv6_link_control_allows_exact_peer_and_denies_ula() -> None:
    if os.environ.get("WEB_EGRESS_KERNEL_TEST") != "1":
        pytest.skip("set WEB_EGRESS_KERNEL_TEST=1 to run the disposable kernel acceptance")

    suffix = f"ipv6-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    network = f"web-egress-ipv6-{suffix}"
    backend = f"web-egress-ipv6-backend-{suffix}"
    peer = f"web-egress-ipv6-peer-{suffix}"
    private = f"web-egress-ipv6-private-{suffix}"
    guard = f"web-egress-ipv6-guard-{suffix}"
    application = f"web-egress-ipv6-app-{suffix}"
    attacker = f"web-egress-ipv6-attacker-{suffix}"
    with _DockerScope() as scope:
        image = _kernel_image(scope, suffix)
        create_network = _run(
            "docker",
            "network",
            "create",
            "--ipv6",
            "--subnet",
            "172.31.246.0/24",
            "--subnet",
            "fd42:31:246::/64",
            network,
        )
        if create_network.returncode != 0:
            pytest.skip(
                "Docker IPv6 user-defined networks unavailable: "
                + (create_network.stderr or create_network.stdout).strip()
            )
        scope.networks.append(network)

        for name, alias, ipv4, ipv6, port_number in (
            (backend, "backend", "172.31.246.10", "fd42:31:246::10", 8000),
            (peer, "searxng", "172.31.246.20", "fd42:31:246::20", 8080),
            (private, "private-v6", "172.31.246.30", "fd42:31:246::30", 80),
        ):
            _must_run(
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--network",
                network,
                "--network-alias",
                alias,
                "--ip",
                ipv4,
                "--ip6",
                ipv6,
                "--entrypoint",
                "python3",
                image,
                "-m",
                "http.server",
                str(port_number),
                "--bind",
                "::",
            )
            scope.containers.append(name)

        _must_run(
            "docker",
            "run",
            "-d",
            "--name",
            guard,
            "--network",
            network,
            "--ip",
            "172.31.246.40",
            "--ip6",
            "fd42:31:246::40",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "NET_ADMIN",
            "--read-only",
            "--tmpfs",
            "/run:rw,noexec,nosuid,size=1m",
            "-e",
            "WEB_EGRESS_MODE=web-tools",
            "-e",
            "WEB_EGRESS_LOCAL_PORT=8120",
            "-e",
            "WEB_EGRESS_READY_PORT=8190",
            "-e",
            "WEB_EGRESS_INGRESS_HOST=backend",
            "-e",
            "WEB_EGRESS_INGRESS_PORT=8120",
            "-e",
            "WEB_EGRESS_INTERNAL_HOST=searxng",
            "-e",
            "WEB_EGRESS_INTERNAL_PORT=8080",
            image,
        )
        scope.containers.append(guard)
        _wait_healthy(guard)

        for name, namespace in ((application, f"container:{guard}"), (attacker, network)):
            _must_run(
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--network",
                namespace,
                "--cap-drop",
                "ALL",
                "--entrypoint",
                "tail",
                image,
                "-f",
                "/dev/null",
            )
            scope.containers.append(name)

        baseline_peer = _connection(attacker, "fd42:31:246::20", 8080)
        baseline_private = _connection(attacker, "fd42:31:246::30", 80)
        if baseline_peer.returncode != 0 or baseline_private.returncode != 0:
            pytest.skip(
                "Docker IPv6 data plane unavailable despite network creation: "
                f"peer={baseline_peer.stderr.strip()!r}, "
                f"private={baseline_private.stderr.strip()!r}"
            )
        guarded_peer = _connection(application, "fd42:31:246::20", 8080)
        if guarded_peer.returncode != 0:
            diagnostics = _must_run(
                "docker",
                "exec",
                guard,
                "sh",
                "-c",
                "ip -6 address; ip -6 route; ip -6 neigh; nft list table inet web_egress",
            )
            pytest.fail(f"exact IPv6 peer was blocked: {guarded_peer.stderr}\n{diagnostics}")
        assert baseline_private.returncode == 0
        assert _connection(application, "fd42:31:246::30", 80).returncode != 0


def test_kernel_namespace_blocks_bypasses_and_keeps_required_paths() -> None:
    if os.environ.get("WEB_EGRESS_KERNEL_TEST") != "1":
        pytest.skip("set WEB_EGRESS_KERNEL_TEST=1 to run the disposable kernel acceptance")

    suffix = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    image = f"web-egress-kernel-test:{suffix}"
    keys = (
        "backend",
        "postgres",
        "redis",
        "private80",
        "metadata",
        "hosteq",
        "public",
        "operator",
        "publichigh",
        "invalidguard",
        "webguard",
        "webapp",
        "searchguard",
        "searchapp",
        "replacementsearch",
        "attacker",
        "runner",
    )
    names = {key: f"web-egress-{key}-{suffix}" for key in keys}
    networks = {
        key: f"web-egress-{key}-{suffix}"
        for key in (
            "ingress",
            "search",
            "private",
            "metadata",
            "hosteq",
            "public",
            "runner",
        )
    }
    created_containers: list[str] = []
    created_networks: list[str] = []

    def network(key: str, subnet: str, gateway: str | None = None) -> None:
        command = ["docker", "network", "create", "--subnet", subnet]
        if gateway is not None:
            command.extend(["--gateway", gateway])
        command.append(networks[key])
        _must_run(*command)
        created_networks.append(networks[key])

    def server(key: str, network_key: str, alias: str, address: str, port: int) -> None:
        _must_run(
            "docker",
            "run",
            "-d",
            "--name",
            names[key],
            "--network",
            networks[network_key],
            "--network-alias",
            alias,
            "--ip",
            address,
            "--entrypoint",
            "python3",
            image,
            "-m",
            "http.server",
            str(port),
        )
        created_containers.append(names[key])

    def guard(
        key: str,
        initial_network: str,
        alias: str,
        mode: str,
        local_port: int,
        ingress_host: str,
        ingress_port: int,
        internal_host: str | None = None,
        internal_port: int | None = None,
        forbidden_cidrs: str = "",
    ) -> None:
        command = [
            "docker",
            "create",
            "--name",
            names[key],
            "--network",
            networks[initial_network],
            "--network-alias",
            alias,
            "--cap-drop",
            "ALL",
            "--cap-add",
            "NET_ADMIN",
            "--security-opt",
            "no-new-privileges:true",
            "--read-only",
            "--tmpfs",
            "/run:rw,noexec,nosuid,size=1m",
            "-e",
            f"WEB_EGRESS_MODE={mode}",
            "-e",
            f"WEB_EGRESS_LOCAL_PORT={local_port}",
            "-e",
            "WEB_EGRESS_READY_PORT=8190",
            "-e",
            f"WEB_EGRESS_INGRESS_HOST={ingress_host}",
            "-e",
            f"WEB_EGRESS_INGRESS_PORT={ingress_port}",
            "-e",
            f"WEB_EGRESS_FORBIDDEN_CIDRS={forbidden_cidrs}",
        ]
        if internal_host is not None and internal_port is not None:
            command.extend(
                [
                    "-e",
                    f"WEB_EGRESS_INTERNAL_HOST={internal_host}",
                    "-e",
                    f"WEB_EGRESS_INTERNAL_PORT={internal_port}",
                ]
            )
        command.append(image)
        _must_run(*command)
        created_containers.append(names[key])

    try:
        _must_run(
            "docker",
            "build",
            "-q",
            "-t",
            image,
            str(ROOT / "infra/web-egress"),
            timeout=120,
        )
        network("ingress", "172.31.240.0/24")
        network("search", "172.31.241.0/24")
        network("private", "172.31.247.0/24")
        network("metadata", "169.254.169.0/24")
        # The bridge gateway is deliberately .254, leaving the first usable
        # address as a controlled listener equivalent to the host-gateway
        # candidate denied by the guard.
        network("hosteq", "12.210.244.0/24", gateway="12.210.244.254")
        # This globally classified subnet exists only inside the disposable
        # bridge, so the public-HTTP allow path is deterministic and offline.
        network("public", "11.210.242.0/24")
        network("runner", "172.31.243.0/24")

        server("backend", "ingress", "backend", "172.31.240.10", 8000)
        server("postgres", "ingress", "postgres", "172.31.240.11", 5432)
        server("redis", "ingress", "redis", "172.31.240.12", 6379)
        server("private80", "private", "private-http", "172.31.247.20", 80)
        server("metadata", "metadata", "metadata-target", "169.254.169.254", 80)
        server("hosteq", "hosteq", "host-equivalent", "12.210.244.1", 80)
        server("public", "public", "public-target", "11.210.242.10", 80)
        server("operator", "public", "operator-target", "11.210.242.11", 80)
        server("publichigh", "public", "public-high", "11.210.242.12", 8080)

        guard(
            "webguard",
            "ingress",
            "web-tools",
            "web-tools",
            8120,
            "backend",
            8120,
            "searxng",
            8080,
            forbidden_cidrs="11.210.242.11/32",
        )
        _must_run(
            "docker",
            "network",
            "connect",
            "--alias",
            "web-tools",
            networks["search"],
            names["webguard"],
        )
        _must_run("docker", "network", "connect", networks["public"], names["webguard"])
        _must_run("docker", "network", "connect", networks["private"], names["webguard"])
        _must_run("docker", "network", "connect", networks["metadata"], names["webguard"])
        _must_run("docker", "network", "connect", networks["hosteq"], names["webguard"])

        guard(
            "searchguard",
            "search",
            "searxng",
            "searxng",
            8080,
            "web-tools",
            8080,
            forbidden_cidrs="11.210.242.11/32",
        )
        _must_run("docker", "network", "connect", networks["public"], names["searchguard"])
        _must_run("docker", "network", "connect", networks["private"], names["searchguard"])
        _must_run("docker", "network", "connect", networks["metadata"], names["searchguard"])
        _must_run("docker", "network", "connect", networks["hosteq"], names["searchguard"])
        guard(
            "invalidguard",
            "ingress",
            "invalid-web-tools",
            "web-tools",
            8120,
            "backend",
            8120,
            "searxng",
            8080,
            forbidden_cidrs="11.210.242.9/24",
        )
        _must_run("docker", "network", "connect", networks["search"], names["invalidguard"])
        _must_run("docker", "start", names["invalidguard"])
        deadline = time.monotonic() + 5
        invalid_state = "running"
        while time.monotonic() < deadline:
            invalid_state = _must_run(
                "docker", "inspect", "--format", "{{.State.Status}}", names["invalidguard"]
            )
            if invalid_state != "running":
                break
            time.sleep(0.1)
        assert invalid_state == "exited"
        invalid_logs = _run("docker", "logs", names["invalidguard"])
        assert invalid_logs.returncode == 0
        assert "canonical IPv4/IPv6 CIDRs" in (invalid_logs.stdout + invalid_logs.stderr)

        _must_run("docker", "start", names["webguard"], names["searchguard"])
        _wait_healthy(names["webguard"])
        _wait_healthy(names["searchguard"])

        for key, guard_key, app_port in (
            ("webapp", "webguard", 8120),
            ("searchapp", "searchguard", 8080),
        ):
            _must_run(
                "docker",
                "run",
                "-d",
                "--name",
                names[key],
                "--network",
                f"container:{names[guard_key]}",
                "--cap-drop",
                "ALL",
                "--entrypoint",
                "python3",
                image,
                "-m",
                "http.server",
                str(app_port),
            )
            created_containers.append(names[key])
            _wait_connection(names[key], "127.0.0.1", app_port)

        for key, network_key in (("attacker", "ingress"), ("runner", "runner")):
            _must_run(
                "docker",
                "run",
                "-d",
                "--name",
                names[key],
                "--network",
                networks[network_key],
                "--entrypoint",
                "tail",
                image,
                "-f",
                "/dev/null",
            )
            created_containers.append(names[key])

        # Required exact paths: Docker DNS, backend ingress, internal search,
        # and controlled public HTTP from both guarded namespaces.
        dns_probe = _run(
            "docker",
            "exec",
            names["webapp"],
            "python3",
            "-c",
            "import socket; assert socket.getaddrinfo('public-target', 80)",
        )
        if dns_probe.returncode != 0:
            guard_diagnostics = _must_run(
                "docker",
                "exec",
                names["webguard"],
                "sh",
                "-c",
                "cat /etc/resolv.conf; ip route; ss -lntu; nft -a list table inet web_egress",
            )
            app_resolver = _must_run("docker", "exec", names["webapp"], "cat", "/etc/resolv.conf")
            pytest.fail(
                f"DNS probe failed:\n{dns_probe.stderr}\n"
                f"app resolver:\n{app_resolver}\nnamespace diagnostics:\n{guard_diagnostics}"
            )
        assert _connection(names["backend"], "web-tools", 8120).returncode == 0
        search_connection = _connection(names["webapp"], "searxng", 8080)
        if search_connection.returncode != 0:
            web_diagnostics = _must_run(
                "docker",
                "exec",
                names["webguard"],
                "sh",
                "-c",
                "getent hosts searxng; ip route; nft -a list table inet web_egress",
            )
            search_diagnostics = _must_run(
                "docker",
                "exec",
                names["searchguard"],
                "sh",
                "-c",
                "ip address; nft -a list table inet web_egress",
            )
            pytest.fail(
                f"exact SearXNG path failed: {search_connection.stderr}\n"
                f"web namespace:\n{web_diagnostics}\n"
                f"search namespace:\n{search_diagnostics}"
            )
        assert _connection(names["webapp"], "public-target", 80).returncode == 0
        assert _connection(names["searchapp"], "public-target", 80).returncode == 0
        assert _connection(names["webapp"], "operator-target", 80).returncode != 0
        assert _connection(names["searchapp"], "operator-target", 80).returncode != 0

        for network_key in ("private", "metadata", "hosteq"):
            _must_run("docker", "network", "connect", networks[network_key], names["attacker"])
        # Each negative target has a real TCP/80 listener and an independent
        # unfiltered positive control, so failures cannot pass through a
        # closed port or absent route.
        for target in (
            "172.31.247.20",
            "169.254.169.254",
            "12.210.244.1",
        ):
            assert _connection(names["attacker"], target, 80).returncode == 0
            assert _connection(names["webapp"], target, 80).returncode != 0
            assert _connection(names["searchapp"], target, 80).returncode != 0

        # Direct IP bypasses to every private data-plane target and metadata
        # must be rejected by the kernel, independent of application policy.
        for host, target_port in (
            ("172.31.240.10", 8000),
            ("172.31.240.11", 5432),
            ("172.31.240.12", 6379),
            ("169.254.169.254", 80),
        ):
            assert _connection(names["webapp"], host, target_port).returncode != 0
        public_gateway = _must_run(
            "docker",
            "network",
            "inspect",
            "--format",
            "{{(index .IPAM.Config 0).Gateway}}",
            networks["public"],
        )
        assert _connection(names["webapp"], public_gateway, 80).returncode != 0
        assert _connection(names["attacker"], "web-tools", 8120).returncode != 0
        assert _connection(names["searchapp"], "172.31.240.10", 8000).returncode != 0
        assert _connection(names["webapp"], "public-high", 8080).returncode != 0
        _must_run("docker", "network", "connect", networks["search"], names["attacker"])
        assert _connection(names["attacker"], "searxng", 8080).returncode != 0

        # The application cannot remove its namespace policy. A guard restart
        # reapplies the transaction before returning healthy and stays closed.
        assert (
            _run(
                "docker", "exec", names["webapp"], "nft", "delete", "table", "inet", "web_egress"
            ).returncode
            != 0
        )
        old_search_address = _must_run(
            "docker",
            "inspect",
            "--format",
            f'{{{{(index .NetworkSettings.Networks "{networks["search"]}").IPAddress}}}}',
            names["searchguard"],
        )
        _must_run("docker", "network", "disconnect", networks["search"], names["searchguard"])
        _must_run(
            "docker",
            "network",
            "connect",
            "--ip",
            old_search_address,
            networks["search"],
            names["searchguard"],
        )
        server(
            "replacementsearch",
            "search",
            "searxng",
            "172.31.241.30",
            8080,
        )

        # Reapply in the live namespace with a changed DNS peer. The new tuple
        # must work and the still-reachable old listener must be revoked.
        _must_run(
            "docker",
            "exec",
            names["webguard"],
            "/usr/local/bin/web-egress-entrypoint",
            "--apply-only",
        )
        assert _connection(names["webapp"], "searxng", 8080).returncode == 0
        assert _connection(names["webapp"], old_search_address, 8080).returncode != 0

        _must_run("docker", "rm", "-f", names["webapp"])
        created_containers.remove(names["webapp"])
        _must_run("docker", "restart", names["webguard"])
        _wait_healthy(names["webguard"])
        _must_run(
            "docker",
            "run",
            "-d",
            "--name",
            names["webapp"],
            "--network",
            f"container:{names['webguard']}",
            "--cap-drop",
            "ALL",
            "--entrypoint",
            "python3",
            image,
            "-m",
            "http.server",
            "8120",
        )
        created_containers.append(names["webapp"])
        assert _connection(names["webapp"], "172.31.240.11", 5432).returncode != 0
        assert _connection(names["webapp"], "11.210.242.10", 80).returncode == 0
        assert _connection(names["webapp"], "searxng", 8080).returncode == 0
        assert _connection(names["webapp"], old_search_address, 8080).returncode != 0

        web_address = _must_run(
            "docker",
            "inspect",
            "--format",
            f'{{{{(index .NetworkSettings.Networks "{networks["ingress"]}").IPAddress}}}}',
            names["webguard"],
        )
        search_address = _must_run(
            "docker",
            "inspect",
            "--format",
            f'{{{{(index .NetworkSettings.Networks "{networks["search"]}").IPAddress}}}}',
            names["searchguard"],
        )
        assert _connection(names["runner"], web_address, 8120).returncode != 0
        assert _connection(names["runner"], search_address, 8080).returncode != 0
    finally:
        cleanup = _DockerScope()
        cleanup.containers = created_containers
        cleanup.networks = created_networks
        cleanup.images = [image]
        cleanup_errors = cleanup.cleanup()
        if cleanup_errors:
            pytest.fail("Docker cleanup incomplete:\n" + "\n".join(cleanup_errors))
