#!/usr/bin/env python3
"""Fresh k3d web acceptance with C5 Temporal and UID-precondition recovery.

Shared Compose infrastructure is never restarted. Secrets use stdin and private
ephemeral kubeconfig, never journals/logs. Temporal history is retained.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import ipaddress
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any
import urllib.request

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from web_tools_e2e import (  # noqa: E402
    WebFixtureController,
    Config,
    FixtureError,
    private_probe_targets,
    socket_probe_code,
    barrier_program,
)
from workflow_templates_ui_e2e import canonical, load_state, private_parent  # noqa: E402

OWNER = "aurum-ai.web-e2e/owner"
ID = re.compile(r"[a-f0-9]{64}")
K8S_FIXTURE_IPV4_ADDRESS = os.getenv(
    "WEB_ACCEPTANCE_FIXTURE_IPV4", "93.184.217.2"
)
K8S_FIXTURE_IPV6_ADDRESS = os.getenv(
    "WEB_ACCEPTANCE_FIXTURE_IPV6", "2606:4700:ffff:217::2"
)
K8S_FIXTURE_IPV4_NETWORK = str(
    ipaddress.ip_network(K8S_FIXTURE_IPV4_ADDRESS + "/29", strict=False)
)
K8S_FIXTURE_IPV6_NETWORK = str(
    ipaddress.ip_network(K8S_FIXTURE_IPV6_ADDRESS + "/64", strict=False)
)


def run(command: list[str], *, data: str | None = None, timeout: int = 60,
        env: dict[str, str] | None = None) -> str:
    try:
        result = subprocess.run(command, input=data, capture_output=True, text=True,
                                timeout=timeout, env=env, cwd=ROOT)
    except subprocess.TimeoutExpired:
        raise FixtureError(f"{command[0]} operation timed out; raw output suppressed") from None
    if result.returncode:
        raise FixtureError(f"{command[0]} operation failed (exit {result.returncode}); raw output suppressed")
    return result.stdout


def cni_diagnostic_code(targets: list[tuple[str, str, int]]) -> str:
    for label, address, port in targets:
        if not re.fullmatch(r"[a-z0-9-]+", label):
            raise FixtureError("invalid CNI diagnostic label")
        ipaddress.ip_address(address)
        if type(port) is not int or port not in {53, 80, 443, 5432, 6379, 7233, 8000, 9000}:
            raise FixtureError("invalid CNI diagnostic port")
    payload = json.dumps(targets, separators=(",", ":"))
    return """import errno
import json
import socket
report = {}
for label, address, port in json.loads(""" + repr(payload) + """):
 try:
  with socket.create_connection((address, port), timeout=2): pass
  outcome = 'connected'
 except OSError as error:
  if isinstance(error, TimeoutError) or error.errno in (errno.ETIMEDOUT, errno.ECONNREFUSED, errno.EACCES, errno.EPERM): outcome = 'denied'
  else: outcome = 'route-unavailable'
 report[label] = outcome
print(json.dumps(report, sort_keys=True, separators=(',', ':')))
"""


def private_json(path: Path, data: dict[str, Any]) -> None:
    private_parent(path)
    fd, name = tempfile.mkstemp(prefix=".infra-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class InfraJournal:
    """Strict metadata-only sidecar to the unchanged C5 journal."""

    def __init__(self, path: Path, marker: str):
        self.path, self.marker = path, marker
        self.name = "web-e2e-" + marker.rsplit("-", 1)[-1]
        if not re.fullmatch(r"workflow-templates-ui-e2e-[a-f0-9]{16}", marker):
            raise FixtureError("invalid web ownership marker")

    def init(self) -> None:
        if self.path.exists():
            raise FixtureError("infrastructure journal already exists")
        private_json(self.path, dict(marker=self.marker, name=self.name, node_ids={},
            cluster_uid=None, cluster_network_id=None, fixture_network_id=None, fixture_container_id=None,
            runner_pods={}, release_installed=False, cleanup_verified=False,
            cluster_started=False, cluster_deleted=False))

    def validate(self, data: Any) -> None:
        expected = {"marker", "name", "node_ids", "cluster_uid", "cluster_network_id", "fixture_network_id",
                    "fixture_container_id", "runner_pods", "release_installed", "cleanup_verified",
                    "cluster_started", "cluster_deleted"}
        if not isinstance(data, dict) or set(data) != expected or data["marker"] != self.marker or data["name"] != self.name:
            raise FixtureError("invalid infrastructure journal schema")
        for key in ("release_installed", "cleanup_verified", "cluster_started", "cluster_deleted"):
            if type(data[key]) is not bool:
                raise FixtureError("invalid infrastructure journal flags")
        if data["cluster_uid"] is not None:
            canonical(data["cluster_uid"])
        for key in ("cluster_network_id", "fixture_network_id", "fixture_container_id"):
            if data[key] is not None and (not isinstance(data[key], str) or not ID.fullmatch(data[key])):
                raise FixtureError("invalid Docker resource identity")
        if not isinstance(data["node_ids"], dict) or set(data["node_ids"]) - {
            f"k3d-{self.name}-server-0", f"k3d-{self.name}-agent-0", f"k3d-{self.name}-serverlb"
        }:
            raise FixtureError("invalid cluster node identity")
        if any(not isinstance(value, str) or not ID.fullmatch(value) for value in data["node_ids"].values()):
            raise FixtureError("invalid cluster container identity")
        if not isinstance(data["runner_pods"], dict):
            raise FixtureError("invalid runner identity journal")
        for conversation, pod in data["runner_pods"].items():
            canonical(conversation)
            if not isinstance(pod, dict) or set(pod) != {"kind", "name", "namespace", "release", "uid"}:
                raise FixtureError("invalid runner identity")
            canonical(pod["uid"])
            if pod != {"kind": "kubernetes", "name": f"runner-{conversation}",
                       "namespace": self.name + "-runners", "release": self.name, "uid": pod["uid"]}:
                raise FixtureError("runner journal ownership mismatch")

    def load(self) -> dict[str, Any]:
        private_parent(self.path)
        if stat.S_IMODE(self.path.stat().st_mode) != 0o600 or self.path.stat().st_uid != os.getuid():
            raise FixtureError("infrastructure journal requires private mode600")
        data = json.loads(self.path.read_text())
        self.validate(data)
        return data

    def update(self, **changes: Any) -> None:
        data = self.load()
        data.update(changes)
        self.validate(data)
        private_json(self.path, data)


def runner_control(infra: InfraJournal, state_path: Path, **kwargs: Any):
    # Reuse existing read/labels/delete/UID precondition. Only journal storage
    # differs from the question acceptance; no copied deletion implementation.
    from user_questions_k8s_e2e import KubernetesPodControl

    class WebPodControl(KubernetesPodControl):
        async def _pin_owned_async(self, conversation_id: str) -> None:
            if conversation_id not in load_state(state_path).conversation_ids:
                raise FixtureError("runner conversation is not journal-owned")
            pod = await self._read_async(conversation_id)
            if pod is None:
                return
            resource = self._resource(pod, conversation_id)
            data = infra.load()
            previous = data["runner_pods"].get(conversation_id)
            if previous is not None and previous != resource:
                raise FixtureError("runner Pod UID changed; recovery refused")
            data["runner_pods"][conversation_id] = resource
            infra.update(runner_pods=data["runner_pods"])

        def _pinned(self, conversation_id: str):
            return infra.load()["runner_pods"].get(conversation_id)

    return WebPodControl(namespace=infra.name + "-runners", release=infra.name,
                         state_path=state_path, **kwargs)


class KubernetesWebFixture(WebFixtureController):
    def __init__(self, config: Config, state_path: Path, marker: str, infra: InfraJournal,
                 **kwargs: Any):
        super().__init__(config, state_path, marker, **kwargs)
        self.infra = infra
        from user_questions_k8s_e2e import KubernetesTemporalTransport
        self.temporal_transport = KubernetesTemporalTransport("127.0.0.1:7233", infra.name)
        self.runner_cleaner = runner_control(infra, state_path)

    def cleanup(self):
        state = self._state()
        try:
            for conversation in state.conversation_ids:
                self.runner_cleaner.pin_owned(conversation)
        except Exception as exc:
            self.restore(state)
            raise FixtureError("runner identity validation failed; permissions restoration attempted") from exc
        super().cleanup()


class ReleaseLifecycle:
    def __init__(self, state_path: Path, marker: str):
        self.state_path, self.marker = state_path, marker
        self.infra = InfraJournal(state_path.parent / "infra.json", marker)
        self.name = self.infra.name
        self.namespace = self.name
        self.web_namespace = self.name + "-web"
        # Each CLI has its own ephemeral kubeconfig; nested browser CLI calls
        # cannot unlink the parent's port-forward configuration.
        self.kubeconfig = state_path.parent / f"kubeconfig-{os.getpid()}"
        self.environment = {**os.environ, "KUBECONFIG": str(self.kubeconfig)}

    def expected_node_names(self) -> tuple[str, str, str]:
        return tuple(
            f"k3d-{self.name}-{suffix}"
            for suffix in ("server-0", "agent-0", "serverlb")
        )

    def create_cluster_network(self) -> None:
        name = self.name + "-cluster"
        if self.docker_object(name, network=True) is not None:
            raise FixtureError("cluster network ownership collision")
        suffix = self.marker.rsplit("-", 1)[-1]
        subnet = f"fd42:{suffix[:4]}:{suffix[4:8]}:{suffix[8:12]}::/64"
        network_id = run([
            "docker", "network", "create", "--ipv6", "--subnet", subnet,
            "--label", f"{OWNER}={self.marker}", name,
        ]).strip()
        if not ID.fullmatch(network_id):
            raise FixtureError("invalid cluster network identity")
        self.infra.update(cluster_network_id=network_id)

    def cluster_rows(self) -> list[dict[str, Any]]:
        rows = json.loads(run(["k3d", "cluster", "list", "-o", "json"]))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise FixtureError("invalid k3d cluster inventory")
        return rows

    def pin_partial_cluster_identity(self) -> None:
        data = self.infra.load()
        if not data["cluster_started"] or not data["cluster_network_id"]:
            raise FixtureError("partial cluster ownership proof incomplete")
        matches = [row for row in self.cluster_rows() if row.get("name") == self.name]
        if len(matches) != 1:
            raise FixtureError("partial cluster ownership is ambiguous")
        expected = set(self.expected_node_names())
        names = {
            node.get("name")
            for node in matches[0].get("nodes", [])
            if isinstance(node, dict)
        }
        if not names or not names.issubset(expected):
            raise FixtureError("partial cluster node ownership mismatch")
        pinned: dict[str, str] = {}
        for name in names:
            row = self.docker_object(name)
            labels = row.get("Config", {}).get("Labels", {}) if row else {}
            if (not row or labels.get("k3d.cluster") != self.name
                    or labels.get(OWNER) != self.marker):
                raise FixtureError("partial cluster node ownership mismatch")
            pinned[name] = row["Id"]
        if data["node_ids"] and data["node_ids"] != pinned:
            raise FixtureError("partial cluster node identity changed")
        self.infra.update(node_ids=pinned)

    def assert_partial_cluster_identity(self) -> None:
        data = self.infra.load()
        state = load_state(self.state_path)
        if (data["cluster_uid"] is not None or data["release_installed"]
                or state.organization_id or not data["node_ids"]):
            raise FixtureError("partial cluster ownership proof incomplete")
        pinned = dict(data["node_ids"])
        self.pin_partial_cluster_identity()
        if self.infra.load()["node_ids"] != pinned:
            raise FixtureError("partial cluster node identity changed")

    def kubectl(
        self, *args: str, data: Any = None, timeout: int = 60,
        phase: str | None = None,
    ) -> str:
        if phase is not None and not re.fullmatch(r"[a-z0-9-]+", phase):
            raise FixtureError("invalid kubectl phase")
        try:
            return run(["kubectl", *args], data=json.dumps(data) if data is not None else None,
                       timeout=timeout, env=self.environment)
        except FixtureError:
            if phase is None:
                raise
            raise FixtureError(f"kubectl {phase} failed; raw output suppressed") from None

    def cluster_access(self) -> None:
        content = run(["k3d", "kubeconfig", "get", self.name])
        private_parent(self.kubeconfig)
        fd = os.open(self.kubeconfig, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(content)

    def temporal_cli(self, *args: str) -> str:
        compose_project = os.getenv("K8S_TEST_COMPOSE_PROJECT", "aurum-ai")
        return run([
            "docker", "compose", "-p", compose_project, "run", "--rm",
            "--no-deps", "--entrypoint", "temporal", "temporal-admin-tools",
            "--address", "temporal:7233", *args,
        ])

    def configure_ingress_node_port(self) -> None:
        for attempt in range(120):
            try:
                self.kubectl("-n", "kube-system", "get", "service", "traefik")
                break
            except FixtureError:
                if attempt == 119:
                    raise FixtureError(
                        "Traefik service did not become ready; raw output suppressed"
                    ) from None
                time.sleep(1)
        patch = (
            '{"spec":{"type":"NodePort","ports":['
            '{"name":"web","port":80,"protocol":"TCP","targetPort":"web","nodePort":30080},'
            '{"name":"websecure","port":443,"protocol":"TCP","targetPort":"websecure"}]}}'
        )
        self.kubectl(
            "-n", "kube-system", "patch", "service", "traefik",
            "--type", "merge", "-p", patch,
        )
        self.kubectl(
            "-n", "kube-system", "rollout", "status", "deployment/traefik",
            "--timeout=180s",
        )

    def dispose_access(self) -> None:
        self.kubeconfig.unlink(missing_ok=True)

    def docker_object(self, name: str, *, network: bool = False):
        command = ["docker", "network", "inspect", name] if network else ["docker", "inspect", name]
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        if result.returncode:
            if "no such" in result.stderr.lower() or "not found" in result.stderr.lower():
                return None
            raise FixtureError("Docker ownership inspection unavailable")
        rows = json.loads(result.stdout)
        if not isinstance(rows, list) or len(rows) != 1:
            raise FixtureError("ambiguous Docker resource")
        return rows[0]

    def assert_cluster_identity(self) -> None:
        data = self.infra.load()
        if not data["cluster_started"] or not data["cluster_uid"] or not data["node_ids"]:
            raise FixtureError("cluster ownership proof incomplete; retain journal")
        for name, saved_id in data["node_ids"].items():
            current = self.docker_object(name)
            if not current or current["Id"] != saved_id or current["Config"]["Labels"].get("k3d.cluster") != self.name:
                raise FixtureError("cluster node identity changed")
        actual = json.loads(self.kubectl("get", "namespace", "kube-system", "-o", "json"))
        if actual["metadata"]["uid"] != data["cluster_uid"]:
            raise FixtureError("cluster UID changed")

    def init(self) -> None:
        self.infra.init()
        WebFixtureController(Config(), self.state_path, self.marker).init()

    def deploy(self) -> None:
        if self.infra.load()["cluster_started"]:
            raise FixtureError("deploy is not retryable; use exact recovery")
        clusters = self.cluster_rows()
        if any(row.get("name") == self.name for row in clusters):
            raise FixtureError("fresh owned cluster required")
        for name, network in ((self.name + "-cluster", True), (self.name + "-public", True),
                              (self.name + "-fixture", False)):
            if self.docker_object(name, network=network) is not None:
                raise FixtureError("fixture ownership collision")
        network = os.getenv("K8S_TEST_DOCKER_NETWORK", "aurum-ai_default")
        addresses = {}
        for service in ("postgres", "redis", "keycloak", "temporal", "minio"):
            container = os.getenv("E2E_POSTGRES_CONTAINER", "aurum-ai-postgres") if service == "postgres" else f"aurum-ai-{service}"
            row = self.docker_object(container)
            if not row or not row["State"]["Running"] or row["Config"]["Labels"].get("com.docker.compose.service") != service:
                raise FixtureError("BLOCKED: shared data-plane prerequisite unavailable")
            addresses[service] = row["NetworkSettings"]["Networks"][network]["IPAddress"]
        subnet = self.docker_object(network, network=True)["IPAM"]["Config"][0]["Subnet"]
        self.create_cluster_network()
        self.infra.update(cluster_started=True)
        try:
            run(["k3d", "cluster", "create", self.name, "--network", self.name + "-cluster",
                 "-p", "127.0.0.1:8081:30080@loadbalancer",
                 "--agents", "1", "--wait", "--kubeconfig-update-default=false",
                 "--kubeconfig-switch-context=false", "--runtime-label",
                 f"{OWNER}={self.marker}@server:*;agent:*;loadbalancer",
                 "--k3s-arg", "--disable=servicelb@server:*",
                 "--k3s-arg", "--cluster-cidr=10.42.0.0/16,fd42:216::/56@server:*",
                 "--k3s-arg", "--service-cidr=10.43.0.0/16,fd43:216::/112@server:*",
                 "--k3s-arg", "--flannel-ipv6-masq@server:*",
                 "--k3s-arg", "--kubelet-arg=pod-max-pids=64@server:*",
                 "--k3s-arg", "--kubelet-arg=pod-max-pids=64@agent:*"], timeout=300)
        except Exception:
            self.pin_partial_cluster_identity()
            raise
        self.pin_partial_cluster_identity()
        self.cluster_access()
        self.configure_ingress_node_port()
        nodes = self.infra.load()["node_ids"]
        uid = json.loads(self.kubectl("get", "namespace", "kube-system", "-o", "json"))["metadata"]["uid"]
        self.infra.update(node_ids=nodes, cluster_uid=canonical(uid))
        for name, node_id in nodes.items():
            if not name.endswith("serverlb"):
                run(["docker", "network", "connect", network, node_id])
        net_id = run(["docker", "network", "create", "--internal", "--ipv6",
                      "--subnet", K8S_FIXTURE_IPV4_NETWORK,
                      "--subnet", K8S_FIXTURE_IPV6_NETWORK,
                      "--label", f"{OWNER}={self.marker}", self.name + "-public"]).strip()
        self.infra.update(fixture_network_id=net_id)
        ipv4_network = ipaddress.ip_network(K8S_FIXTURE_IPV4_NETWORK)
        ipv6_network = ipaddress.ip_network(K8S_FIXTURE_IPV6_NETWORK)
        for index, (name, node_id) in enumerate(nodes.items()):
            if not name.endswith("serverlb"):
                run(["docker", "network", "connect", "--ip",
                     str(ipv4_network.network_address + index + 3), "--ip6",
                     str(ipv6_network.network_address + index + 3), net_id, node_id])
        images = []
        for folder, image in (("business-backend", "backend"), ("business-ui", "frontend"),
                              ("admin-ui", "admin-ui"), ("tool-worker", "tool-worker"),
                              ("skill-executor", "skill-executor"), ("conversation-runner", "conversation-runner"),
                              ("skill-sandbox", "skill-sandbox"), ("web-tools", "web-tools")):
            tag = f"aurum-ai-{image}:latest"
            run(["docker", "build", "-t", tag, str(ROOT / folder)], timeout=900)
            images.append(tag)
        # Eight images exceed five minutes on a cold WSL Docker image store.
        # This is an operator acceptance timeout, not workflow-clock behavior.
        run(["k3d", "image", "import", "-c", self.name, *images], timeout=900)
        fixture_id = run(["docker", "run", "-d", "--name", self.name + "-fixture", "--network", net_id,
                          "--ip", K8S_FIXTURE_IPV4_ADDRESS, "--ip6", K8S_FIXTURE_IPV6_ADDRESS,
                          "--env", "WEB_ACCEPTANCE_FIXTURE_IPV4=" + K8S_FIXTURE_IPV4_ADDRESS,
                          "--env", "WEB_ACCEPTANCE_FIXTURE_IPV6=" + K8S_FIXTURE_IPV6_ADDRESS,
                          "--env", "WEB_FIXTURE_IPV6_CONTROL=1", "--label", f"{OWNER}={self.marker}",
                          "--read-only", "--cap-drop=ALL", "--cap-add=NET_BIND_SERVICE",
                          "--security-opt=no-new-privileges:true", "--user", "0", "--entrypoint", "python3",
                          "-v", f"{SCRIPT_DIR / 'web_tools_e2e.py'}:/fixture.py:ro",
                          "-v", f"{SCRIPT_DIR / 'web_tools_fault_fixture.py'}:/web_tools_fault_fixture.py:ro", "aurum-ai-backend:latest",
                          "/fixture.py", "fixture"]).strip()
        self.infra.update(fixture_container_id=fixture_id)
        # Only the fresh test cluster's DNS configuration changes. Web Pods
        # still use the production DNS NetworkPolicy and private CIDR denials.
        dns = json.loads(self.kubectl("-n", "kube-system", "get", "configmap", "coredns", "-o", "json",
                                      phase="dns-read"))
        dns["data"]["Corefile"] += (
            "\nrebind.web-acceptance.test:53 {\n forward . "
            + K8S_FIXTURE_IPV4_ADDRESS + "\n}\n"
        )
        self.kubectl("replace", "-f", "-", data=dns, phase="dns-replace")
        self.kubectl("-n", "kube-system", "rollout", "restart", "deployment/coredns",
                     phase="dns-restart")
        self.kubectl("create", "-f", "-", data={"apiVersion": "node.k8s.io/v1", "kind": "RuntimeClass",
            "metadata": {"name": "aurum-ai-web-restricted", "labels": {OWNER: self.marker}}, "handler": "runc"},
            phase="runtimeclass-create")
        self.temporal_cli(
            "operator", "namespace", "create", "--namespace", self.name,
            "--retention", "1d",
        )
        self.kubectl("create", "namespace", self.namespace, phase="namespace-create")
        self.gateway()
        self.private_listener()
        values = self.helm_values(addresses, subnet)
        self.infra.update(release_installed=True)
        for action in ("install", "upgrade"):
            self.apply_release(action, values)
        self.kubectl("-n", self.web_namespace, "create", "-f", "-", data={
            "apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "web-fixture", "labels": {OWNER: self.marker}},
            "data": {"fixture.py": (SCRIPT_DIR / "web_tools_e2e.py").read_text(),
                     "web_tools_fault_fixture.py": (SCRIPT_DIR / "web_tools_fault_fixture.py").read_text()}})
        patch = self.searxng_fixture_patch()
        self.kubectl("-n", self.web_namespace, "patch", "deployment", self.name + "-searxng",
                     "--type=strategic", "-p", json.dumps(patch))
        self.kubectl("-n", self.web_namespace, "rollout", "status", "deployment/" + self.name + "-searxng", "--timeout=120s", timeout=130)

    def helm_values(self, addresses: dict[str, str], subnet: str) -> dict[str, Any]:
        return {
            "image": {"tag": "latest", "pullPolicy": "IfNotPresent"},
            "secrets": {"appSecretKey": os.environ["APP_SECRET_KEY"], "gatewayApiKey": "web-e2e-deterministic",
                        "databaseRolePassword": os.getenv("DATABASE_ROLE_PASSWORD", "aurum_ai_app"),
                        "redisPassword": os.getenv("REDIS_PASSWORD", "redis_dev_password")},
            "backend": {"webEnabled": True, "userQuestionsEnabled": True, "debug": True, "replicaCount": 1, "autoscaling": {"enabled": False},
                        "evaluator": {"enabled": False}, "rateLimitPerMinute": 3000, "rateLimitPerMinuteStream": 500},
            "backendAdmin": {"debug": True, "replicaCount": 1, "autoscaling": {"enabled": False}},
            "frontend": {"replicaCount": 1, "autoscaling": {"enabled": False}, "runtimeConfig": {
                "apiUrl": "http://backend.127.0.0.1.nip.io:8081/v1", "keycloakUrl": os.getenv("E2E_KEYCLOAK_URL", "http://localhost:8080")}},
            "adminUi": {"replicaCount": 1, "autoscaling": {"enabled": False}},
            "ingress": {"enabled": True, "externalPort": 8081},
            "runtimeNamespaces": {"create": True, "runner": self.name + "-runners", "sandbox": self.name + "-sandboxes"},
            "runnerRuntime": {"type": "kubernetes", "idleReapSeconds": 900},
            "web": {"enabled": True, "namespace": self.web_namespace,
                    "serviceSecret": os.environ["WEB_TOOLS_SERVICE_SECRET"], "searxngSecret": os.environ["SEARXNG_SECRET"],
                    "webTools": {"replicaCount": 1}, "searxng": {"replicaCount": 1}},
            "external": {"postgres": {"host": addresses["postgres"]}, "redis": {"host": addresses["redis"]},
                         "keycloak": {"url": "http://" + addresses["keycloak"] + ":8080",
                                      "issuer": os.getenv("E2E_KEYCLOAK_URL", "http://localhost:8080") + "/realms/aurum-ai"},
                         "temporal": {"host": addresses["temporal"] + ":7233", "namespace": self.name},
                         "minio": {"endpoint": "http://" + addresses["minio"] + ":9000"}, "langfuse": {"enabled": False},
                         "llmGateway": {"url": "http://web-test-gateway:8080", "verifySsl": False}},
            "networkPolicies": {"runner": {"destinations": [{"cidr": subnet,
                "ports": [{"protocol": "TCP", "port": 6379}, {"protocol": "TCP", "port": 7233}]}]}},
        }

    def searxng_fixture_patch(self) -> dict[str, Any]:
        return {"spec": {"template": {"spec": {
            "containers": [{"name": "searxng",
                            "command": ["python3", "/fixture/fixture.py", "fixture-search"],
                            "env": [{"name": "WEB_ACCEPTANCE_FIXTURE_IPV4",
                                     "value": K8S_FIXTURE_IPV4_ADDRESS}],
                            "volumeMounts": [{"name": "fixture", "mountPath": "/fixture", "readOnly": True}]}],
            "volumes": [{"name": "fixture", "configMap": {"name": "web-fixture"}}]}}}}

    def release_failure_reason(self) -> str:
        allowed = {
            "CrashLoopBackOff", "CreateContainerConfigError", "ErrImagePull",
            "ImagePullBackOff", "InvalidImageName", "Pending", "Failed", "Unknown",
        }
        reasons: set[str] = set()
        try:
            for namespace in (self.namespace, self.web_namespace):
                rows = json.loads(self.kubectl("-n", namespace, "get", "pods", "-o", "json"))
                for pod in rows.get("items", []):
                    status = pod.get("status", {})
                    phase = status.get("phase")
                    if phase in allowed:
                        reasons.add(phase)
                    for key in ("initContainerStatuses", "containerStatuses"):
                        for container in status.get(key, []):
                            state = container.get("state", {})
                            for kind in ("waiting", "terminated"):
                                reason = state.get(kind, {}).get("reason")
                                if reason in allowed:
                                    reasons.add(reason)
        except (FixtureError, json.JSONDecodeError, AttributeError, TypeError):
            return "diagnostics-unavailable"
        priority = (
            "CreateContainerConfigError", "InvalidImageName", "ErrImagePull",
            "ImagePullBackOff", "CrashLoopBackOff", "Failed", "Pending", "Unknown",
        )
        return next((reason for reason in priority if reason in reasons), "not-ready")

    def apply_release(self, action: str, values: dict[str, Any]) -> None:
        if action not in {"install", "upgrade"}:
            raise FixtureError("invalid Helm action")
        try:
            run(["helm", action, self.name, str(ROOT / "charts/aurum-ai"), "-n", self.namespace,
                 "-f", "-", "--wait", "--timeout", "8m"], data=json.dumps(values), timeout=540,
                env=self.environment)
        except FixtureError:
            reason = self.release_failure_reason()
            raise FixtureError(
                f"helm-{action} failed ({reason}); raw output suppressed"
            ) from None

    def gateway(self) -> None:
        labels = {OWNER: self.marker, "app": "web-test-gateway"}
        self.kubectl("-n", self.namespace, "create", "-f", "-", data={"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "web-test-gateway", "labels": labels},
            "data": {"gateway.py": (SCRIPT_DIR / "k8s-test-gateway.py").read_text()}},
            phase="gateway-config-create")
        self.kubectl("-n", self.namespace, "create", "-f", "-", data={"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "web-test-gateway", "labels": labels}, "spec": {"replicas": 1,
            "selector": {"matchLabels": {"app": "web-test-gateway"}}, "template": {"metadata": {"labels": labels},
            "spec": {"automountServiceAccountToken": False, "containers": [{"name": "gateway", "image": "aurum-ai-backend:latest",
                "imagePullPolicy": "IfNotPresent", "command": ["python", "/gateway/gateway.py"],
                "env": [{"name": "TEST_GATEWAY_MODEL_ID", "value": self.marker + "-model"},
                        {"name": "WEB_ACCEPTANCE_FIXTURE_IPV4", "value": K8S_FIXTURE_IPV4_ADDRESS}],
                "ports": [{"containerPort": 8080}], "volumeMounts": [{"name": "source", "mountPath": "/gateway", "readOnly": True}]}],
                "volumes": [{"name": "source", "configMap": {"name": "web-test-gateway"}}]}}}},
            phase="gateway-deployment-create")
        self.kubectl("-n", self.namespace, "create", "-f", "-", data={"apiVersion": "v1", "kind": "Service",
            "metadata": {"name": "web-test-gateway", "labels": labels}, "spec": {"selector": {"app": "web-test-gateway"},
            "ports": [{"port": 8080, "targetPort": 8080}]}}, phase="gateway-service-create")

    def private_listener(self) -> None:
        nodes = json.loads(self.kubectl("get", "nodes", "-o", "json", phase="private-nodes-read")).get("items", [])
        if not nodes or any({ipaddress.ip_network(cidr).version for cidr in node.get("spec", {}).get("podCIDRs", [])} != {4, 6} for node in nodes):
            raise FixtureError("BLOCKED: Kubernetes IPv4/IPv6 pod-network coverage unavailable; no dual-stack acceptance")
        labels = {OWNER: self.marker, "app": "web-private-fixture"}
        self.kubectl("-n", self.namespace, "create", "-f", "-", data={"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "web-private-fixture", "labels": labels},
            "data": {"fixture.py": (SCRIPT_DIR / "web_tools_e2e.py").read_text()}},
            phase="private-config-create")
        self.kubectl("-n", self.namespace, "create", "-f", "-", data={"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "web-private-fixture", "labels": labels}, "spec": {"replicas": 1,
            "selector": {"matchLabels": {"app": "web-private-fixture"}}, "template": {"metadata": {"labels": labels},
            "spec": {"automountServiceAccountToken": False, "containers": [{"name": "listener", "image": "aurum-ai-backend:latest",
                "imagePullPolicy": "IfNotPresent",
                "command": ["python3", "/fixture/fixture.py", "private-listener"],
                "securityContext": {"runAsUser": 0, "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"], "add": ["NET_BIND_SERVICE"]}},
                "readinessProbe": {"tcpSocket": {"port": 80}},
                "volumeMounts": [{"name": "source", "mountPath": "/fixture", "readOnly": True}]}],
                "volumes": [{"name": "source", "configMap": {"name": "web-private-fixture"}}]}}}},
            phase="private-deployment-create")
        self.kubectl("-n", self.namespace, "create", "-f", "-", data={"apiVersion": "v1", "kind": "Service",
            "metadata": {"name": "web-private-fixture", "labels": labels},
            "spec": {"ipFamilyPolicy": "RequireDualStack", "ipFamilies": ["IPv4", "IPv6"],
                "selector": {"app": "web-private-fixture"},
                "ports": [{"name": "http", "port": 80}, {"name": "https-tcp", "port": 443}]}},
            phase="private-service-create")
        self.kubectl("-n", self.namespace, "rollout", "status", "deployment/web-private-fixture", "--timeout=120s",
                     timeout=130, phase="private-rollout")

    @contextmanager
    def forwards(self, *, ui: bool = False):
        self.cluster_access()
        self.assert_cluster_identity()
        processes = []
        try:
            services = [(self.name + "-backend", "18000:8000"),
                        (self.name + "-backend-admin", "18001:8000")]
            urls = ["http://127.0.0.1:18000/ready", "http://127.0.0.1:18001/ready"]
            if ui:
                # Browser SSE must not share a kubectl forwarding session:
                # a remote broken pipe can terminate all its connections.
                # Keep REST-only recovery forwards compatible with old journals.
                urls.extend([
                    "http://backend.127.0.0.1.nip.io:8081/ready",
                    "http://backend-admin.127.0.0.1.nip.io:8081/ready",
                    "http://chat.127.0.0.1.nip.io:8081/login",
                ])
            for service, mapping in services:
                processes.append(subprocess.Popen(["kubectl", "-n", self.namespace, "port-forward",
                    "--address=127.0.0.1", "service/" + service, mapping], env=self.environment,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            for url in urls:
                for _ in range(120):
                    if any(process.poll() is not None for process in processes):
                        raise FixtureError("owned port-forward failed")
                    try:
                        with urllib.request.urlopen(url, timeout=2):
                            break
                    except OSError:
                        time.sleep(0.5)
                else:
                    raise FixtureError("owned release readiness failed")
            yield
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    def config(self) -> Config:
        return Config(business_url="http://127.0.0.1:18000", admin_url="http://127.0.0.1:18001")

    def fixture(self) -> KubernetesWebFixture:
        os.environ["KUBECONFIG"] = str(self.kubeconfig)
        return KubernetesWebFixture(self.config(), self.state_path, self.marker, self.infra)

    def scenario(self) -> None:
        with self.forwards(ui=True):
            self.verify_pid_budget()
            self.network_prerequisites()
            self.fixture().prepare()
            environment = {**self.environment, "E2E_WEB_TOOLS": "true", "E2E_WEB_TOOLS_RUNTIME": "kubernetes",
                "PLAYWRIGHT_NO_COPY_PROMPT": "1",
                "E2E_WEB_TOOLS_STATE": str(self.state_path), "E2E_WEB_TOOLS_MARKER": self.marker,
                "E2E_WEB_TOOLS_FIXTURE_SCRIPT": str(SCRIPT_DIR / "web_tools_k8s_e2e.py"),
                "E2E_WEB_TOOLS_PYTHON": sys.executable, "WEB_ACCEPTANCE_KIND": "controlled",
                "E2E_BUSINESS_UI_URL": "http://chat.127.0.0.1.nip.io:8081",
                "E2E_BUSINESS_BACKEND_URL": "http://backend.127.0.0.1.nip.io:8081",
                "E2E_ADMIN_BACKEND_URL": "http://backend-admin.127.0.0.1.nip.io:8081"}
            result = subprocess.run(["npm", "exec", "--", "playwright", "test", "e2e/web-tools.real.spec.ts",
                "--config=playwright.web-tools.config.ts", "--project=chromium", "--workers=1", "--trace=off"], cwd=ROOT / "business-ui", env=environment)
            if result.returncode:
                raise FixtureError("Kubernetes browser scenario failed")
            self.network_probes()

    def verify_pid_budget(self) -> None:
        nodes = json.loads(self.kubectl("get", "nodes", "-o", "json")).get("items", [])
        if not nodes:
            raise FixtureError("PID budget requires observed kubelet configuration")
        for node in nodes:
            name = node.get("metadata", {}).get("name", "")
            if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", name):
                raise FixtureError("PID budget node identity invalid")
            config = json.loads(self.kubectl("get", "--raw", f"/api/v1/nodes/{name}/proxy/configz"))
            limit = config.get("kubeletconfig", {}).get("podPidsLimit")
            if type(limit) is not int or not 1 <= limit <= 64:
                raise FixtureError("PID budget must be observed between 1 and 64 on every node")

    def cni_failure_detail(
        self,
        namespace: str,
        workload: str,
        targets: list[tuple[str, str, int]],
        *,
        allowed: bool,
    ) -> str:
        component = workload.removeprefix(self.name + "-")
        diagnostic_targets = [
            ("public-v6-80", K8S_FIXTURE_IPV6_ADDRESS, 80),
            ("public-v6-443", K8S_FIXTURE_IPV6_ADDRESS, 443),
            *targets,
        ]
        try:
            output = self.kubectl(
                "-n", namespace, "exec", "deployment/" + workload, "--",
                "python3", "-c", cni_diagnostic_code(diagnostic_targets), timeout=40,
            )
            report = json.loads(output)
            expected_labels = {label for label, _, _ in diagnostic_targets}
            if (not isinstance(report, dict) or set(report) != expected_labels
                    or set(report.values()) - {"connected", "denied", "route-unavailable"}):
                raise ValueError
            failures = []
            for label in sorted(report):
                expected = "connected" if label.startswith("public-") or allowed else "denied"
                if report[label] != expected:
                    failures.append(f"{label}-{report[label]}")
            reason = ",".join(failures) if failures else "probe-disagreed"
        except (FixtureError, json.JSONDecodeError, TypeError, ValueError):
            reason = "diagnostics-unavailable"
        gate = "positive reachability coverage unavailable" if allowed else "private/internal denial failed"
        return f"CNI {component} {gate} ({reason})"

    def network_prerequisites(self) -> None:
        targets = []
        diagnostic_targets: list[tuple[str, str, int]] = []
        network = os.getenv("K8S_TEST_DOCKER_NETWORK", "aurum-ai_default")
        for service, port in (("postgres", 5432), ("redis", 6379), ("temporal", 7233), ("minio", 9000)):
            row = self.docker_object(os.getenv("E2E_POSTGRES_CONTAINER", "aurum-ai-postgres") if service == "postgres" else "aurum-ai-" + service)
            address = row["NetworkSettings"]["Networks"][network]["IPAddress"]
            targets.append([address, port])
            diagnostic_targets.append(("shared-" + service + "-v4", address, port))
        backend = json.loads(self.kubectl("-n", self.namespace, "get", "service", self.name + "-backend", "-o", "json"))
        backend_address = backend["spec"]["clusterIP"]
        targets.append([backend_address, 8000])
        diagnostic_targets.append(("backend-service-v4", backend_address, 8000))
        private = json.loads(self.kubectl("-n", self.namespace, "get", "service", "web-private-fixture", "-o", "json"))
        private_targets = private_probe_targets(private["spec"].get("clusterIPs", []))
        targets += private_targets
        for address, port in private_targets:
            family = ipaddress.ip_address(address).version
            diagnostic_targets.append((f"private-v{family}-{port}", address, port))
        # Controls prove the five listeners are reachable before real CNI
        # denial checks in both isolated web workloads (not just URL policy).
        for namespace, workload, allowed in ((self.namespace, self.name + "-backend", True),
                (self.web_namespace, self.name + "-web-tools", False), (self.web_namespace, self.name + "-searxng", False)):
            code = socket_probe_code(
                targets,
                allowed=allowed,
                ipv6_control_address=K8S_FIXTURE_IPV6_ADDRESS,
            )
            try:
                self.kubectl("-n", namespace, "exec", "deployment/" + workload, "--", "python3", "-c", code, timeout=40)
            except FixtureError:
                raise FixtureError(self.cni_failure_detail(
                    namespace, workload, diagnostic_targets, allowed=allowed,
                )) from None
        for workload in (self.name + "-web-tools", self.name + "-searxng"):
            code = socket_probe_code(
                [[K8S_FIXTURE_IPV4_ADDRESS, 80], [K8S_FIXTURE_IPV4_ADDRESS, 443]],
                allowed=True,
            )
            code += "\nassert socket.getaddrinfo(" + repr(self.name + "-searxng." + self.web_namespace + ".svc.cluster.local") + ",8080)\n"
            self.kubectl("-n", self.web_namespace, "exec", "deployment/" + workload, "--", "python3", "-c", code)
        code = "import urllib.request; urllib.request.urlopen(" + repr("http://" + self.name + "-searxng:8080/search?q=IANA%20reserved%20domains&format=json") + ",timeout=5).read()"
        self.kubectl("-n", self.web_namespace, "exec", "deployment/" + self.name + "-web-tools", "--", "python3", "-c", code)
        code = """
import json, urllib.request, urllib.error
from src.core.config import settings
for url in (""" + repr("http://" + K8S_FIXTURE_IPV4_ADDRESS + "/redirect-internal") + """,'http://rebind.web-acceptance.test/rebind-start','http://169.254.169.254/'):
 request=urllib.request.Request(settings.WEB_TOOLS_URL+'/read',data=json.dumps({'url':url}).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+settings.WEB_TOOLS_SERVICE_SECRET.get_secret_value()})
 try:
  urllib.request.urlopen(request,timeout=40)
  raise SystemExit(1)
 except urllib.error.HTTPError as error:
  if error.code != 422 or json.loads(error.read()).get('detail',{}).get('code') != 'web_url_blocked': raise SystemExit(1)
"""
        self.kubectl("-n", self.namespace, "exec", "deployment/" + self.name + "-backend", "--", "python", "-c", code, timeout=130)
        fixture_id = self.infra.load()["fixture_container_id"]
        run(["docker", "exec", fixture_id, "python3", "-c", "import urllib.request; n=int(urllib.request.urlopen('http://127.0.0.1/dns-count',timeout=3).read()); raise SystemExit(0 if n>=2 else 1)"])
        service = json.loads(self.kubectl("-n", self.web_namespace, "get", "service", self.name + "-web-tools", "-o", "json"))
        address = service["spec"]["clusterIP"]
        for namespace, workload in ((self.namespace, "deployment/" + self.name + "-backend-admin"),
                (self.web_namespace, "deployment/" + self.name + "-searxng")):
            code = socket_probe_code([[address, 8120]], allowed=False)
            self.kubectl("-n", namespace, "exec", workload, "--", "python3", "-c", code, timeout=15)
        print("Kubernetes pre-grant CNI private IPv4/IPv6 TCP80/443, public/DNS/search controls, ingress and rebinding: PASS")

    def network_probes(self) -> None:
        state = load_state(self.state_path)
        if not state.conversation_ids:
            raise FixtureError("owned runner required for ingress negatives")
        service = json.loads(self.kubectl("-n", self.web_namespace, "get", "service", self.name + "-web-tools", "-o", "json"))
        code = socket_probe_code([[service["spec"]["clusterIP"], 8120]], allowed=False)
        self.kubectl("-n", self.name + "-runners", "exec", "runner-" + state.conversation_ids[0], "--", "python", "-c", code, timeout=15)
        print("Kubernetes exact-owned runner ingress negative: PASS")

    def reconcile_deleted_cluster(self) -> None:
        """Resume a verified teardown before any recovery port-forward attempt."""
        data = self.infra.load()
        if not data["cluster_started"] or data["cluster_deleted"] or not data["cleanup_verified"]:
            return
        clusters = self.cluster_rows()
        if any(row.get("name") == self.name for row in clusters):
            return
        for name in self.expected_node_names():
            if self.docker_object(name) is not None:
                raise FixtureError("partial cluster remains; retain journal")
        state = load_state(self.state_path)
        if not data["cluster_uid"] and (state.organization_id or data["release_installed"]):
            raise FixtureError("cluster ownership proof incomplete; retain journal")
        self.infra.update(cluster_deleted=True)

    def cleanup(self) -> None:
        self.reconcile_deleted_cluster()
        if not load_state(self.state_path).organization_id:
            return
        if self.infra.load()["cluster_deleted"]:
            self.verify_clean()
            return
        with self.forwards():
            self.fixture().cleanup()

    def verify_clean(self) -> None:
        self.reconcile_deleted_cluster()
        if load_state(self.state_path).organization_id:
            if self.infra.load()["cluster_deleted"]:
                self.assert_cluster_absent()
                controller = self.fixture()
                # A verified-absent entire pinned cluster cannot contain a Pod.
                lifecycle = self
                class AbsentCluster:
                    def assert_absent(self, conversation):
                        if conversation not in load_state(lifecycle.state_path).conversation_ids:
                            raise FixtureError("runner is not journal-owned")
                        lifecycle.assert_cluster_absent()
                controller.runner_cleaner = AbsentCluster()
                controller.verify_clean()
            else:
                with self.forwards():
                    self.fixture().verify_clean()
        self.infra.update(cleanup_verified=True)

    def teardown(self) -> None:
        data = self.infra.load()
        if not data["cleanup_verified"]:
            raise FixtureError("exact Temporal/DB/capability verification required before release teardown")
        if data["cluster_started"] and not data["cluster_deleted"]:
            if not data["node_ids"]:
                self.pin_partial_cluster_identity()
                data = self.infra.load()
            clusters = self.cluster_rows()
            if not any(row.get("name") == self.name for row in clusters) and data["node_ids"]:
                if any(self.docker_object(name) is not None for name in data["node_ids"]):
                    raise FixtureError("partial cluster remains; retain journal")
            else:
                if data["cluster_uid"]:
                    self.cluster_access()
                    self.assert_cluster_identity()
                else:
                    self.assert_partial_cluster_identity()
                if data["release_installed"]:
                    releases = json.loads(run(["helm", "list", "-n", self.namespace, "--all", "-o", "json"], env=self.environment))
                    if any(row.get("name") == self.name for row in releases):
                        run(["helm", "uninstall", self.name, "-n", self.namespace, "--wait", "--timeout", "3m"],
                            env=self.environment, timeout=190)
                    self.infra.update(release_installed=False)
                run(["k3d", "cluster", "delete", self.name], timeout=180)
            self.infra.update(cluster_deleted=True)
        if data["fixture_container_id"]:
            row = self.docker_object(self.name + "-fixture")
            if row:
                if row["Id"] != data["fixture_container_id"] or row["Config"]["Labels"].get(OWNER) != self.marker:
                    raise FixtureError("fixture container identity changed")
                run(["docker", "rm", "-f", row["Id"]])
        if data["fixture_network_id"]:
            row = self.docker_object(self.name + "-public", network=True)
            if row:
                if row["Id"] != data["fixture_network_id"] or row["Labels"].get(OWNER) != self.marker or row.get("Containers"):
                    raise FixtureError("fixture network identity changed or foreign endpoint present")
                run(["docker", "network", "rm", row["Id"]])
        if data["cluster_network_id"]:
            row = self.docker_object(self.name + "-cluster", network=True)
            if row:
                if (row["Id"] != data["cluster_network_id"]
                        or row["Labels"].get(OWNER) != self.marker or row.get("Containers")):
                    raise FixtureError("cluster network identity changed or foreign endpoint present")
                run(["docker", "network", "rm", row["Id"]])

    def assert_cluster_absent(self) -> None:
        data = self.infra.load()
        state = load_state(self.state_path)
        if not data["cleanup_verified"] or not data["cluster_deleted"]:
            raise FixtureError("verified cluster deletion proof required")
        if (not data["cluster_uid"]
                and (state.organization_id or data["release_installed"] or not data["cluster_network_id"])):
            raise FixtureError("verified cluster deletion proof required")
        if data["cluster_uid"] and not data["node_ids"]:
            raise FixtureError("verified cluster deletion proof required")
        clusters = self.cluster_rows()
        if any(row.get("name") == self.name for row in clusters):
            raise FixtureError("cluster name reappeared; recovery refused")
        for name in self.expected_node_names():
            if self.docker_object(name) is not None:
                raise FixtureError("owned cluster remains")

    def verify_infra(self) -> None:
        data = self.infra.load()
        if not data["cleanup_verified"]:
            raise FixtureError("runtime cleanup was not verified")
        if data["cluster_started"]:
            self.assert_cluster_absent()
        if (self.docker_object(self.name + "-fixture")
                or self.docker_object(self.name + "-public", network=True)
                or self.docker_object(self.name + "-cluster", network=True)):
            raise FixtureError("owned fixture infrastructure remains")


def preflight() -> None:
    if os.getenv("WEB_ACCEPTANCE_EXCLUSIVE") != "1":
        raise FixtureError("BLOCKED: exclusive acknowledgement required")
    for name in ("APP_SECRET_KEY", "WEB_TOOLS_SERVICE_SECRET", "SEARXNG_SECRET"):
        if len(os.getenv(name, "")) < 16:
            raise FixtureError("BLOCKED: missing or short " + name)
    for name in ("docker", "kubectl", "helm", "k3d", "npm"):
        if shutil.which(name) is None:
            raise FixtureError("BLOCKED: missing " + name)
    run(["docker", "info"])
    import socket
    for port in (18000, 18001, 8081):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                raise FixtureError("BLOCKED: web K8s loopback port occupied") from None
    from kubernetes_asyncio import client  # noqa: F401
    from temporalio.client import Client  # noqa: F401


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "init", "deploy", "scenario", "cleanup", "verify-clean",
        "teardown", "verify-infra", "record", "pin", "grant", "revoke", "network-probes", "barrier-arm", "barrier-status", "barrier-release", "fault", "turn-proof", "prepare-workflow", "update-workflow"))
    parser.add_argument("--state", type=Path)
    parser.add_argument("--marker")
    parser.add_argument("--id")
    parser.add_argument("--action")
    args = parser.parse_args()
    lifecycle = None
    try:
        if args.command == "preflight":
            preflight()
            return 0
        if args.state is None or args.marker is None:
            raise FixtureError("private journal and marker required")
        lifecycle = ReleaseLifecycle(args.state, args.marker)
        if args.command in {"prepare-workflow", "update-workflow"}:
            lifecycle.cluster_access()
            lifecycle.assert_cluster_identity()
            controller = lifecycle.fixture()
            if args.command == "prepare-workflow":
                controller.prepare_workflow()
            else:
                controller.update_workflow(args.action, args.id)
        elif args.command == "fault":
            from web_tools_fault_fixture import control_program
            lifecycle.cluster_access()
            lifecycle.assert_cluster_identity()
            data = lifecycle.infra.load()
            row = lifecycle.docker_object(lifecycle.name + "-fixture")
            if not row or row["Id"] != data["fixture_container_id"] or row["Config"]["Labels"].get(OWNER) != lifecycle.marker:
                raise FixtureError("upstream fixture identity changed")
            print(run(["docker", "exec", row["Id"], "python3", "-c", control_program(args.id, args.action)]).strip())
        elif args.command == "turn-proof":
            lifecycle.cluster_access()
            lifecycle.assert_cluster_identity()
            lifecycle.fixture().turn_proof(args.id)
        elif args.command.startswith("barrier-"):
            lifecycle.cluster_access()
            lifecycle.assert_cluster_identity()
            print(lifecycle.kubectl("-n", lifecycle.namespace, "exec", "deployment/" + lifecycle.name + "-backend", "--", "python3", "-c", barrier_program(args.command.removeprefix("barrier-"))).strip())
        elif args.command in {"record", "pin", "grant", "revoke"}:
            lifecycle.cluster_access()
            lifecycle.assert_cluster_identity()
            controller = lifecycle.fixture()
            if args.command == "record":
                controller.record("conversation", args.id)
            elif args.command == "pin":
                controller.runner_cleaner.pin_owned(canonical(args.id))
            else:
                controller.capabilities(args.command == "grant")
        else:
            getattr(lifecycle, args.command.replace("-", "_"))()
    except Exception as exc:
        detail = str(exc) if isinstance(exc, FixtureError) else "operation failed; raw diagnostics suppressed"
        print("[web-tools-k8s] " + detail, file=sys.stderr)
        return 2 if args.command == "preflight" else 1
    finally:
        if lifecycle is not None:
            lifecycle.dispose_access()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
