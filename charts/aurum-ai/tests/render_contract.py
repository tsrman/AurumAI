# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "PyYAML==6.0.3",
# ]
# ///

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml


def fail(message: str) -> None:
    raise AssertionError(message)


def load(path: str) -> list[dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as stream:
        documents = [document for document in yaml.safe_load_all(stream) if document]
    for document in documents:
        if not isinstance(document, dict):
            fail(f"{path}: every YAML document must be a mapping")
    return documents


def index(documents: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for document in documents:
        metadata = document.get("metadata")
        if not isinstance(metadata, dict):
            fail("resource metadata must be a mapping")
        key = (document.get("kind"), metadata.get("name"))
        if not all(isinstance(part, str) for part in key):
            fail("resource kind and metadata.name must be strings")
        if key in result:
            fail(f"duplicate resource {key[0]}/{key[1]}")
        result[key] = document
    return result


def resource(
    resources: dict[tuple[str, str], dict[str, Any]], kind: str, name: str
) -> dict[str, Any]:
    try:
        return resources[(kind, name)]
    except KeyError:
        fail(f"missing {kind}/{name}")


def assert_namespace(document: dict[str, Any], namespace: str) -> None:
    actual = document["metadata"].get("namespace")
    if actual != namespace:
        fail(f"{document['kind']}/{document['metadata']['name']} namespace: {actual!r} != {namespace!r}")


def assert_role(
    document: dict[str, Any],
    namespace: str,
    expected_rules: list[tuple[set[str], set[str], set[str]]],
) -> None:
    assert_namespace(document, namespace)
    rules = document.get("rules")
    if not isinstance(rules, list) or len(rules) != len(expected_rules):
        fail(f"Role/{document['metadata']['name']} must have exactly {len(expected_rules)} rules")
    actual: list[tuple[set[str], set[str], set[str]]] = []
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"apiGroups", "resources", "verbs"}:
            fail(f"Role/{document['metadata']['name']} rule has unexpected fields: {rule!r}")
        actual.append(
            (set(rule["apiGroups"]), set(rule["resources"]), set(rule["verbs"]))
        )
    if actual != expected_rules:
        fail(f"Role/{document['metadata']['name']} rules differ: {actual!r}")


def assert_role_binding(
    document: dict[str, Any], namespace: str, role_name: str, subject_name: str, subject_namespace: str
) -> None:
    assert_namespace(document, namespace)
    expected_role_ref = {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": role_name,
    }
    if document.get("roleRef") != expected_role_ref:
        fail(f"RoleBinding/{document['metadata']['name']} roleRef differs")
    expected_subjects = [
        {
            "kind": "ServiceAccount",
            "name": subject_name,
            "namespace": subject_namespace,
        }
    ]
    if document.get("subjects") != expected_subjects:
        fail(f"RoleBinding/{document['metadata']['name']} must have exactly the dedicated subject")


def assert_runner_secret(
    document: dict[str, Any], namespace: str, *, redis_url: str, langfuse_key: str
) -> None:
    assert_namespace(document, namespace)
    if document.get("type") != "Opaque":
        fail(f"Secret/{document['metadata']['name']} must be Opaque")
    if "data" in document or "binaryData" in document:
        fail(f"Secret/{document['metadata']['name']} must use only stringData")
    expected = {"REDIS_URL": redis_url, "LANGFUSE_SECRET_KEY": langfuse_key}
    if document.get("stringData") != expected:
        fail(
            f"Secret/{document['metadata']['name']} must contain exactly REDIS_URL and LANGFUSE_SECRET_KEY"
        )


def peer(namespace: str, pod_labels: dict[str, str]) -> dict[str, Any]:
    return {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": namespace}},
        "podSelector": {"matchLabels": pod_labels},
    }


def assert_policy(
    resources: dict[tuple[str, str], dict[str, Any]],
    name: str,
    namespace: str,
    expected_spec: dict[str, Any],
) -> None:
    document = resource(resources, "NetworkPolicy", name)
    assert_namespace(document, namespace)
    if document.get("spec") != expected_spec:
        fail(f"NetworkPolicy/{name} spec differs: {document.get('spec')!r}")


def assert_no_unsafe_pod_fields(documents: list[dict[str, Any]]) -> None:
    rendered = yaml.safe_dump_all(documents)
    if "/var/run/docker.sock" in rendered or "privileged: true" in rendered:
        fail("render must not contain a Docker socket or privileged container")


def pod_spec(document: dict[str, Any]) -> dict[str, Any]:
    spec = document.get("spec", {}).get("template", {}).get("spec")
    if not isinstance(spec, dict):
        fail(f"{document['kind']}/{document['metadata']['name']} must contain a Pod spec")
    return spec


def only_container(document: dict[str, Any]) -> dict[str, Any]:
    containers = pod_spec(document).get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        fail(f"{document['kind']}/{document['metadata']['name']} must have one container")
    container = containers[0]
    if not isinstance(container, dict):
        fail(f"{document['kind']}/{document['metadata']['name']} container must be a mapping")
    return container


def env_by_name(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    env = only_container(document).get("env")
    if not isinstance(env, list):
        fail(f"Deployment/{document['metadata']['name']} must define env")
    result: dict[str, dict[str, Any]] = {}
    for entry in env:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            fail(f"Deployment/{document['metadata']['name']} has malformed env")
        if entry["name"] in result:
            fail(f"Deployment/{document['metadata']['name']} repeats env {entry['name']}")
        result[entry["name"]] = entry
    return result


def assert_env_values(document: dict[str, Any], expected: dict[str, str]) -> None:
    env = env_by_name(document)
    for name, value in expected.items():
        if env.get(name) != {"name": name, "value": value}:
            fail(
                f"Deployment/{document['metadata']['name']} env {name}: "
                f"{env.get(name)!r} != {value!r}"
            )


def assert_secret_env(
    document: dict[str, Any], env_name: str, secret_name: str, key: str
) -> None:
    actual = env_by_name(document).get(env_name)
    expected = {
        "name": env_name,
        "valueFrom": {"secretKeyRef": {"name": secret_name, "key": key}},
    }
    if actual != expected:
        fail(f"Deployment/{document['metadata']['name']} env {env_name} differs: {actual!r}")


def assert_probe(
    document: dict[str, Any], probe_name: str, path: str, port: str
) -> None:
    probe = only_container(document).get(probe_name)
    if not isinstance(probe, dict):
        fail(f"Deployment/{document['metadata']['name']} missing {probe_name}")
    if probe.get("httpGet") != {"path": path, "port": port}:
        fail(f"Deployment/{document['metadata']['name']} {probe_name} target differs")


def assert_pull_secrets(document: dict[str, Any], expected: list[str]) -> None:
    actual = pod_spec(document).get("imagePullSecrets", [])
    if actual != [{"name": name} for name in expected]:
        fail(
            f"Deployment/{document['metadata']['name']} imagePullSecrets: "
            f"{actual!r} != {expected!r}"
        )


def assert_pull_policy(document: dict[str, Any], expected: str) -> None:
    actual = only_container(document).get("imagePullPolicy")
    if actual != expected:
        fail(
            f"{document['kind']}/{document['metadata']['name']} imagePullPolicy: "
            f"{actual!r} != {expected!r}"
        )


created_docs = load(sys.argv[1])
created = index(created_docs)

deployment_names = {
    document["metadata"]["name"]
    for document in created_docs
    if document.get("kind") == "Deployment"
}
expected_deployments = {
    "test-backend",
    "test-backend-admin",
    "test-frontend",
    "test-admin-ui",
    "test-tool-worker",
    "test-skill-executor",
}
if deployment_names != expected_deployments:
    fail(f"Deployment set differs: {deployment_names!r}")
obsolete_authorization_prefix = "APP_AUTHORIZATION" + "_V2_"
for deployment_name in expected_deployments:
    environment = env_by_name(resource(created, "Deployment", deployment_name))
    if any(name.startswith(obsolete_authorization_prefix) for name in environment):
        fail(f"Deployment/{deployment_name} still exposes authorization transition flags")
for deployment_name in expected_deployments:
    assert_pull_policy(
        resource(created, "Deployment", deployment_name), "IfNotPresent"
    )
assert_pull_policy(resource(created, "Job", "test-migrations"), "IfNotPresent")

for deployment_name in ("test-backend-admin", "test-frontend", "test-admin-ui"):
    unprivileged_spec = pod_spec(resource(created, "Deployment", deployment_name))
    if "serviceAccountName" in unprivileged_spec:
        fail(f"Deployment/{deployment_name} must not use a runtime API ServiceAccount")
    if unprivileged_spec.get("automountServiceAccountToken") is not False:
        fail(f"Deployment/{deployment_name} must disable ServiceAccount token automount")
if pod_spec(resource(created, "Job", "test-migrations")).get(
    "automountServiceAccountToken"
) is not False:
    fail("Job/test-migrations must disable ServiceAccount token automount")

backend = resource(created, "Deployment", "test-backend")
backend_spec = pod_spec(backend)
if backend_spec.get("serviceAccountName") != "test-backend":
    fail("Deployment/test-backend must use its dedicated ServiceAccount")
if backend_spec.get("automountServiceAccountToken") is not True:
    fail("Deployment/test-backend must mount its scoped Kubernetes API token")
assert_env_values(
    backend,
    {
        "APP_WORKFLOW_MAX_NODES": "20",
        "APP_WORKFLOW_MAX_PARALLELISM": "4",
        "APP_WORKFLOW_DIAGNOSTIC_RETENTION_DAYS": "30",
        "APP_WORKFLOW_RETENTION_SWEEP_INTERVAL_SECONDS": "3600",
        "APP_RUNNER_RUNTIME": "kubernetes",
        "APP_KUBERNETES_RELEASE_NAME": "test",
        "APP_KUBERNETES_RELEASE_NAMESPACE": "platform",
        "APP_KUBERNETES_RUNNER_NAMESPACE": "test-runners",
        "APP_KUBERNETES_RUNNER_SECRET_NAME": "test-runner-secrets",
        "APP_RUNNER_IMAGE": "aurum-ai-conversation-runner:latest",
        "APP_KUBERNETES_RUNNER_IMAGE_PULL_POLICY": "IfNotPresent",
        "APP_KUBERNETES_RUNNER_IMAGE_PULL_SECRETS": "[]",
        "APP_KUBERNETES_RUNNER_READY_TIMEOUT_SECONDS": "60",
        "APP_KUBERNETES_API_RETRY_ATTEMPTS": "3",
        "APP_KUBERNETES_RUNNER_CPU_REQUEST": "100m",
        "APP_KUBERNETES_RUNNER_CPU_LIMIT": "1",
        "APP_KUBERNETES_RUNNER_MEMORY_REQUEST": "256Mi",
        "APP_KUBERNETES_RUNNER_MEMORY_LIMIT": "512Mi",
        "APP_RUNNER_BACKEND_URL": "http://test-backend.platform.svc:8000",
        "APP_RUNNER_IDLE_REAP_SECONDS": "1800",
        "APP_RUNNER_REAP_INTERVAL_SECONDS": "300",
        "APP_RUNNER_ORPHAN_GRACE_SECONDS": "600",
        "APP_RUNNER_TOKEN_REFRESH_AFTER_SECONDS": "1200",
        "APP_INTERACTION_MAINTENANCE_INTERVAL_SECONDS": "30",
        "APP_INTERACTION_SETTLEMENT_GRACE_SECONDS": "300",
        "APP_USER_QUESTIONS_ENABLED": "false",
        "APP_RUNNER_POOL_SIZE": "0",
    },
)

backend_admin = resource(created, "Deployment", "test-backend-admin")
assert_env_values(
    backend_admin,
    {
        "APP_WORKFLOW_MAX_NODES": "20",
        "APP_WORKFLOW_MAX_PARALLELISM": "4",
        "APP_WORKFLOW_DIAGNOSTIC_RETENTION_DAYS": "30",
        "APP_WORKFLOW_RETENTION_SWEEP_INTERVAL_SECONDS": "3600",
    },
)

tool_worker = resource(created, "Deployment", "test-tool-worker")
tool_worker_spec = pod_spec(tool_worker)
if "serviceAccountName" in tool_worker_spec:
    fail("Deployment/test-tool-worker must not use a Kubernetes API ServiceAccount")
if tool_worker_spec.get("automountServiceAccountToken") is not False:
    fail("Deployment/test-tool-worker must disable ServiceAccount token automount")
assert_env_values(
    tool_worker,
    {
        "TOOL_WORKER_TEMPORAL_HOST": "aurum-ai-temporal:7233",
        "TOOL_WORKER_TEMPORAL_NAMESPACE": "default",
        "TOOL_WORKER_TASK_QUEUE": "tools",
        "TOOL_WORKER_BACKEND_API_URL": "http://test-backend:8000",
        "TOOL_WORKER_REDIS_URL": "redis://:$(REDIS_PASSWORD)@aurum-ai-redis:6379/0",
        "TOOL_WORKER_SKILL_EXECUTOR_URL": "http://test-skill-executor:8090",
        "TOOL_WORKER_OTEL_EXPORTER_OTLP_ENDPOINT": "console",
    },
)
assert_secret_env(tool_worker, "REDIS_PASSWORD", "aurum-ai-secrets", "REDIS_PASSWORD")
assert_probe(tool_worker, "livenessProbe", "/health", "health")
assert_probe(tool_worker, "readinessProbe", "/health", "health")
if only_container(tool_worker).get("resources") != {
    "requests": {"cpu": "100m", "memory": "128Mi"},
    "limits": {"cpu": "500m", "memory": "256Mi"},
}:
    fail("Deployment/test-tool-worker resources differ")
if ("Service", "test-tool-worker") in created:
    fail("tool-worker health port must not be exposed by a Service")

skill_executor = resource(created, "Deployment", "test-skill-executor")
skill_executor_spec = pod_spec(skill_executor)
if skill_executor_spec.get("serviceAccountName") != "test-skill-executor":
    fail("Deployment/test-skill-executor must use its dedicated ServiceAccount")
if skill_executor_spec.get("automountServiceAccountToken") is not True:
    fail("Deployment/test-skill-executor must mount its scoped Kubernetes API token")
assert_env_values(
    skill_executor,
    {
        "SKILL_EXECUTOR_RUNTIME": "kubernetes",
        "SKILL_EXECUTOR_SANDBOX_IMAGE": "aurum-ai-skill-sandbox:latest",
        "SKILL_EXECUTOR_KUBERNETES_RELEASE_NAME": "test",
        "SKILL_EXECUTOR_KUBERNETES_RELEASE_NAMESPACE": "platform",
        "SKILL_EXECUTOR_KUBERNETES_NAMESPACE": "test-sandboxes",
        "SKILL_EXECUTOR_KUBERNETES_READY_TIMEOUT_SECONDS": "60",
        "SKILL_EXECUTOR_KUBERNETES_API_RETRY_ATTEMPTS": "3",
        "SKILL_EXECUTOR_KUBERNETES_IMAGE_PULL_POLICY": "IfNotPresent",
        "SKILL_EXECUTOR_KUBERNETES_IMAGE_PULL_SECRETS": "[]",
        "SKILL_EXECUTOR_KUBERNETES_CPU_REQUEST": "100m",
        "SKILL_EXECUTOR_KUBERNETES_CPU_LIMIT": "500m",
        "SKILL_EXECUTOR_KUBERNETES_MEMORY_REQUEST": "128Mi",
        "SKILL_EXECUTOR_KUBERNETES_MEMORY_LIMIT": "256Mi",
        "SKILL_EXECUTOR_SESSION_TTL_SECONDS": "1800",
        "SKILL_EXECUTOR_SESSION_IDLE_TTL_SECONDS": "300",
        "SKILL_EXECUTOR_REAP_INTERVAL_SECONDS": "15",
        "SKILL_EXECUTOR_S3_ENDPOINT": "http://aurum-ai-minio:9000",
        "SKILL_EXECUTOR_S3_ACCESS_KEY": "minio",
        "SKILL_EXECUTOR_S3_BUCKET": "aurum-ai-business",
    },
)
assert_secret_env(
    skill_executor,
    "SKILL_EXECUTOR_S3_SECRET_KEY",
    "aurum-ai-secrets",
    "APP_S3_SECRET_KEY",
)
assert_probe(skill_executor, "livenessProbe", "/health", "http")
assert_probe(skill_executor, "readinessProbe", "/ready", "http")
if only_container(skill_executor).get("resources") != {
    "requests": {"cpu": "100m", "memory": "128Mi"},
    "limits": {"cpu": "500m", "memory": "256Mi"},
}:
    fail("Deployment/test-skill-executor resources differ")

skill_service = resource(created, "Service", "test-skill-executor")
if skill_service.get("spec") != {
    "selector": {"app.kubernetes.io/name": "skill-executor", "app.kubernetes.io/instance": "test"},
    "ports": [{"name": "http", "port": 8090, "targetPort": "http"}],
}:
    fail("Service/test-skill-executor contract differs")

namespace_names = {
    document["metadata"]["name"] for document in created_docs if document.get("kind") == "Namespace"
}
if namespace_names != {"test-runners", "test-sandboxes"}:
    fail(f"runtime Namespaces differ: {namespace_names!r}")

for service_account in ("test-backend", "test-skill-executor"):
    document = resource(created, "ServiceAccount", service_account)
    assert_namespace(document, "platform")
    if document.get("automountServiceAccountToken") is not True:
        fail(f"ServiceAccount/{service_account} must automount its scoped API token")

assert_role(
    resource(created, "Role", "test-runner-manager"),
    "test-runners",
    [({""}, {"pods"}, {"get", "list", "watch", "create", "delete"})],
)
assert_role(
    resource(created, "Role", "test-sandbox-manager"),
    "test-sandboxes",
    [
        ({""}, {"pods"}, {"get", "list", "watch", "create", "delete", "patch"}),
        ({""}, {"pods/exec"}, {"create", "get"}),
    ],
)
assert_role_binding(
    resource(created, "RoleBinding", "test-runner-manager"),
    "test-runners",
    "test-runner-manager",
    "test-backend",
    "platform",
)
assert_role_binding(
    resource(created, "RoleBinding", "test-sandbox-manager"),
    "test-sandboxes",
    "test-sandbox-manager",
    "test-skill-executor",
    "platform",
)
assert_runner_secret(
    resource(created, "Secret", "test-runner-secrets"),
    "test-runners",
    redis_url="redis://:redis_dev_password@aurum-ai-redis:6379/0",
    langfuse_key="sk-lf-550e8400-e29b-41d4-a716-446655440001",
)

runner_labels = {
    "app.kubernetes.io/instance": "test",
    "aurum-ai.component": "conversation-runner",
}
sandbox_none_labels = {
    "app.kubernetes.io/instance": "test",
    "aurum-ai.component": "skill-sandbox",
    "aurum-ai.network-policy": "none",
}
sandbox_egress_labels = {
    "app.kubernetes.io/instance": "test",
    "aurum-ai.component": "skill-sandbox",
    "aurum-ai.network-policy": "egress",
}
backend_peer = peer(
    "platform", {"app.kubernetes.io/instance": "test", "app.kubernetes.io/name": "backend"}
)
dns_peer = peer("kube-system", {"k8s-app": "kube-dns"})
dns_ports = [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]
excluded_cidrs = [
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
]

network_policy_names = {
    document["metadata"]["name"]
    for document in created_docs
    if document.get("kind") == "NetworkPolicy"
}
expected_policy_names = {
    "test-skill-executor-ingress",
    "test-runners-default-deny-ingress",
    "test-runner-ingress",
    "test-runner-egress",
    "test-sandboxes-default-deny-ingress",
    "test-sandbox-none-egress",
    "test-sandbox-public-egress",
}
if network_policy_names != expected_policy_names:
    fail(f"NetworkPolicy set differs: {network_policy_names!r}")

assert_policy(
    created,
    "test-skill-executor-ingress",
    "platform",
    {
        "podSelector": {
            "matchLabels": {
                "app.kubernetes.io/instance": "test",
                "app.kubernetes.io/name": "skill-executor",
            }
        },
        "policyTypes": ["Ingress"],
        "ingress": [
            {
                "from": [
                    {
                        "podSelector": {
                            "matchLabels": {
                                "app.kubernetes.io/instance": "test",
                                "app.kubernetes.io/name": "tool-worker",
                            }
                        }
                    }
                ],
                "ports": [{"protocol": "TCP", "port": 8090}],
            }
        ],
    },
)

assert_policy(
    created,
    "test-runners-default-deny-ingress",
    "test-runners",
    {"podSelector": {}, "policyTypes": ["Ingress"]},
)
assert_policy(
    created,
    "test-sandboxes-default-deny-ingress",
    "test-sandboxes",
    {"podSelector": {}, "policyTypes": ["Ingress"]},
)
assert_policy(
    created,
    "test-runner-ingress",
    "test-runners",
    {
        "podSelector": {"matchLabels": runner_labels},
        "policyTypes": ["Ingress"],
        "ingress": [{"from": [backend_peer], "ports": [{"protocol": "TCP", "port": 8080}]}],
    },
)
assert_policy(
    created,
    "test-runner-egress",
    "test-runners",
    {
        "podSelector": {"matchLabels": runner_labels},
        "policyTypes": ["Egress"],
        "egress": [
            {"to": [backend_peer], "ports": [{"protocol": "TCP", "port": 8000}]},
            {"to": [dns_peer], "ports": dns_ports},
            {
                "to": [{"ipBlock": {"cidr": "198.51.100.0/24"}}],
                "ports": [{"protocol": "TCP", "port": 7443}],
            },
        ],
    },
)
assert_policy(
    created,
    "test-sandbox-none-egress",
    "test-sandboxes",
    {
        "podSelector": {"matchLabels": sandbox_none_labels},
        "policyTypes": ["Egress"],
        "egress": [],
    },
)
assert_policy(
    created,
    "test-sandbox-public-egress",
    "test-sandboxes",
    {
        "podSelector": {"matchLabels": sandbox_egress_labels},
        "policyTypes": ["Egress"],
        "egress": [
            {"to": [dns_peer], "ports": dns_ports},
            {
                "to": [
                    {"ipBlock": {"cidr": "0.0.0.0/0", "except": excluded_cidrs}}
                ]
            },
        ],
    },
)
assert_no_unsafe_pod_fields(created_docs)

existing_docs = load(sys.argv[2])
existing = index(existing_docs)
if any(document.get("kind") == "Namespace" for document in existing_docs):
    fail("runtimeNamespaces.create=false must not render Namespace resources")
if any(document.get("kind") == "Secret" for document in existing_docs):
    fail("externally managed namespaces and secrets must not render Secret resources")
assert_role_binding(
    resource(existing, "RoleBinding", "custom-runner-manager"),
    "existing-runners",
    "custom-runner-manager",
    "custom-backend",
    "tenant-app",
)
assert_role_binding(
    resource(existing, "RoleBinding", "custom-sandbox-manager"),
    "existing-sandboxes",
    "custom-sandbox-manager",
    "custom-skill-executor",
    "tenant-app",
)
assert_namespace(resource(existing, "Role", "custom-runner-manager"), "existing-runners")
assert_namespace(resource(existing, "Role", "custom-sandbox-manager"), "existing-sandboxes")
assert_env_values(
    resource(existing, "Deployment", "custom-backend"),
    {
        "APP_KUBERNETES_RUNNER_NAMESPACE": "existing-runners",
        "APP_KUBERNETES_RUNNER_SECRET_NAME": "custom-runner-secrets",
        "APP_RUNNER_BACKEND_URL": "http://custom-backend.tenant-app.svc:8000",
    },
)
assert_env_values(
    resource(existing, "Deployment", "custom-skill-executor"),
    {"SKILL_EXECUTOR_KUBERNETES_NAMESPACE": "existing-sandboxes"},
)

no_secret_docs = load(sys.argv[3])
if any(document.get("kind") == "Secret" for document in no_secret_docs):
    fail("secrets.create=false must suppress both main and runtime Secrets")
no_secret_namespaces = {
    document["metadata"]["name"]
    for document in no_secret_docs
    if document.get("kind") == "Namespace"
}
if no_secret_namespaces != {"no-secrets-runners", "no-secrets-sandboxes"}:
    fail("secrets.create=false must not suppress Helm-owned runtime Namespaces")

no_langfuse_docs = load(sys.argv[4])
no_langfuse = index(no_langfuse_docs)
# KubernetesRunnerRuntime always materializes both secretKeyRef entries. Keeping the
# two-key Secret when telemetry is disabled preserves Pod creation semantics while
# LANGFUSE_ENABLED=false prevents the value from being used.
assert_runner_secret(
    resource(no_langfuse, "Secret", "no-langfuse-runner-secrets"),
    "no-langfuse-runners",
    redis_url="redis://:redis_dev_password@aurum-ai-redis:6379/0",
    langfuse_key="sk-lf-550e8400-e29b-41d4-a716-446655440001",
)

custom_image_docs = load(sys.argv[5])
custom_images = index(custom_image_docs)
expected_images = {
    ("Deployment", "images-backend"): "registry.example.test/team/aurum-ai-backend:latest",
    ("Deployment", "images-backend-admin"): "registry.example.test/team/aurum-ai-backend:latest",
    ("Deployment", "images-frontend"): "registry.example.test/team/aurum-ai-frontend:latest",
    ("Deployment", "images-admin-ui"): "registry.example.test/team/aurum-ai-admin-ui:latest",
    ("Deployment", "images-tool-worker"): "registry.example.test/team/aurum-ai-tool-worker:latest",
    ("Deployment", "images-skill-executor"): "registry.example.test/team/aurum-ai-skill-executor:latest",
    ("Job", "images-migrations"): "registry.example.test/team/aurum-ai-backend:latest",
}
for key, expected_image in expected_images.items():
    document = resource(custom_images, *key)
    actual_image = document["spec"]["template"]["spec"]["containers"][0]["image"]
    if actual_image != expected_image:
        fail(f"{key[0]}/{key[1]} image: {actual_image!r} != {expected_image!r}")
custom_pull_policies = {
    ("Deployment", "images-backend"): "Always",
    ("Deployment", "images-backend-admin"): "IfNotPresent",
    ("Deployment", "images-frontend"): "Always",
    ("Deployment", "images-admin-ui"): "IfNotPresent",
    ("Deployment", "images-tool-worker"): "Always",
    ("Deployment", "images-skill-executor"): "IfNotPresent",
    ("Job", "images-migrations"): "Always",
}
for key, expected_policy in custom_pull_policies.items():
    assert_pull_policy(resource(custom_images, *key), expected_policy)
for deployment_name in (
    "images-backend",
    "images-backend-admin",
    "images-frontend",
    "images-admin-ui",
    "images-tool-worker",
    "images-skill-executor",
):
    assert_pull_secrets(resource(custom_images, "Deployment", deployment_name), ["release-pull"])
assert_pull_secrets(resource(custom_images, "Job", "images-migrations"), ["release-pull"])
assert_env_values(
    resource(custom_images, "Deployment", "images-backend"),
    {
        "APP_RUNNER_IMAGE": "registry.example.test/team/aurum-ai-conversation-runner:latest",
        "APP_KUBERNETES_RUNNER_IMAGE_PULL_SECRETS": '["runner-pull"]',
    },
)
assert_env_values(
    resource(custom_images, "Deployment", "images-skill-executor"),
    {
        "SKILL_EXECUTOR_SANDBOX_IMAGE": "registry.example.test/team/aurum-ai-skill-sandbox:latest",
        "SKILL_EXECUTOR_KUBERNETES_IMAGE_PULL_SECRETS": '["sandbox-pull"]',
    },
)

no_langfuse_tool_worker = resource(no_langfuse, "Deployment", "no-langfuse-tool-worker")
assert_env_values(no_langfuse_tool_worker, {"LANGFUSE_ENABLED": "false"})
for deployment_name in ("no-langfuse-backend", "no-langfuse-tool-worker"):
    langfuse_env = env_by_name(resource(no_langfuse, "Deployment", deployment_name))
    if any(
        name.startswith("LANGFUSE_") and name != "LANGFUSE_ENABLED"
        for name in langfuse_env
    ):
        fail(
            f"Langfuse-disabled Deployment/{deployment_name} must not reference "
            "Langfuse configuration or Secrets"
        )

no_dns_docs = load(sys.argv[6])
no_dns = index(no_dns_docs)
no_dns_runner_labels = {
    "app.kubernetes.io/instance": "no-dns",
    "aurum-ai.component": "conversation-runner",
}
no_dns_backend_peer = peer(
    "platform", {"app.kubernetes.io/instance": "no-dns", "app.kubernetes.io/name": "backend"}
)
assert_policy(
    no_dns,
    "no-dns-runner-egress",
    "no-dns-runners",
    {
        "podSelector": {"matchLabels": no_dns_runner_labels},
        "policyTypes": ["Egress"],
        "egress": [
            {"to": [no_dns_backend_peer], "ports": [{"protocol": "TCP", "port": 8000}]},
            {"to": [dns_peer], "ports": dns_ports},
            {
                "to": [{"ipBlock": {"cidr": "203.0.113.0/24"}}],
                "ports": [{"protocol": "TCP", "port": 9443}],
            },
        ],
    },
)
no_dns_sandbox_labels = {
    "app.kubernetes.io/instance": "no-dns",
    "aurum-ai.component": "skill-sandbox",
    "aurum-ai.network-policy": "egress",
}
no_dns_sandbox_policy = resource(no_dns, "NetworkPolicy", "no-dns-sandbox-public-egress")
assert_policy(
    no_dns,
    "no-dns-sandbox-public-egress",
    "no-dns-sandboxes",
    {
        "podSelector": {"matchLabels": no_dns_sandbox_labels},
        "policyTypes": ["Egress"],
        "egress": [
            {
                "to": [
                    {"ipBlock": {"cidr": "0.0.0.0/0", "except": excluded_cidrs}}
                ]
            }
        ],
    },
)
no_dns_sandbox_rendered = yaml.safe_dump(no_dns_sandbox_policy)
if "kube-dns" in no_dns_sandbox_rendered or "port: 53" in no_dns_sandbox_rendered:
    fail("networkPolicies.dns.enabled=false must remove sandbox DNS egress only")

print("Helm runtime structural render contract passed")
