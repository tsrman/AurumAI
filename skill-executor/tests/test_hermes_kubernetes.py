"""Hermes sandboxes on a fake Kubernetes API: the Pod's isolation, files by exec, adoption and reaping."""

from __future__ import annotations

import io
import json
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from src.config import Settings
from src.hermes_kubernetes import (
    COMPONENT_LABEL,
    DEAD_RESOLVER,
    RELEASE_LABEL,
    KubernetesHermesSandboxes,
    PreviewStream,
)
from src.hermes_runtime import (
    CREATED_LABEL,
    LABEL,
    SESSION_LABEL,
    HermesSandbox,
    HermesSandboxError,
    resource_name,
)
from src.sandbox_runtime.base import SandboxExecError
from tests.test_hermes_snapshots import CONVERSATION, ORG, WORKSPACE, MemoryStore

SESSION = "a" * 32
TOKEN = "hermes-session-token-0123456789"
NAMESPACE = "hermes-agents"
GUARD = "10.43.0.77"


def _api_error(status: int) -> ApiException:
    return ApiException(status=status, reason=str(status))


def _tar_members(data: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        return {m.name: archive.extractfile(m).read() for m in archive if m.isfile()}  # type: ignore[union-attr]


class FakeCore:
    def __init__(self) -> None:
        self.services: dict[str, Any] = {
            "hermes-egress": SimpleNamespace(spec=SimpleNamespace(cluster_ip=GUARD)),
        }
        self.pods: dict[str, Any] = {}
        self.deleted: list[str] = []
        self.waiting_reason: str | None = None
        self.phase = "Running"
        # Reads a deleted Pod stays visible (Terminating) before it is gone.
        self.linger = 0
        self._terminating: dict[str, int] = {}
        self.forced: list[str] = []
        # Set to make the Pod report a Ready condition (the node's word about it).
        self.ready: str | None = None

    async def read_namespaced_service(self, name: str, namespace: str) -> Any:
        assert namespace == NAMESPACE
        if name not in self.services:
            raise _api_error(404)
        return self.services[name]

    def _status(self) -> Any:
        if self.waiting_reason:
            state = SimpleNamespace(running=None, waiting=SimpleNamespace(reason=self.waiting_reason))
        else:
            state = SimpleNamespace(running=object(), waiting=None)
        conditions = [SimpleNamespace(type="Ready", status=self.ready)] if self.ready else []
        return SimpleNamespace(phase=self.phase, container_statuses=[SimpleNamespace(state=state)], conditions=conditions)

    async def create_namespaced_pod(self, namespace: str, pod: Any) -> Any:
        assert namespace == NAMESPACE
        if pod.metadata.name in self.pods:
            raise _api_error(409)
        pod.status = self._status()
        self.pods[pod.metadata.name] = pod
        return pod

    async def read_namespaced_pod(self, name: str, namespace: str) -> Any:
        if name not in self.pods:
            raise _api_error(404)
        if name in self._terminating:
            self._terminating[name] -= 1
            if self._terminating[name] <= 0:
                del self._terminating[name], self.pods[name]
                raise _api_error(404)
        pod = self.pods[name]
        pod.status = self._status()
        return pod

    async def list_namespaced_pod(self, namespace: str, label_selector: str = "") -> Any:
        wanted = dict(item.split("=", 1) for item in label_selector.split(",") if item)
        items = [
            pod
            for pod in self.pods.values()
            if all((pod.metadata.labels or {}).get(key) == value for key, value in wanted.items())
        ]
        return SimpleNamespace(items=items)

    async def delete_namespaced_pod(self, name: str, namespace: str, grace_period_seconds: int | None = None) -> None:
        if name not in self.pods:
            raise _api_error(404)
        self.deleted.append(name)
        if grace_period_seconds == 0:
            # Forced: the API object goes at once, whatever the node says.
            self.forced.append(name)
            self._terminating.pop(name, None)
            del self.pods[name]
        elif self.linger:
            self._terminating[name] = self.linger
            self.pods[name].metadata.deletion_timestamp = "t"
        else:
            del self.pods[name]


class FakeStream:
    def __init__(self, chunks: list[bytes], exit_code: int, *, stdin: bool) -> None:
        self.chunks = list(chunks)
        self._exit = exit_code
        self.stdin = stdin
        self.written: list[bytes] = []
        self.stdin_closed = False
        self.closed = False
        self._ended = False

    @property
    def exit_code(self) -> int | None:
        return self._exit if self._ended else None

    async def read(self) -> bytes | None:
        if self.chunks:
            return self.chunks.pop(0)
        self._ended = True
        return None

    async def write(self, data: bytes) -> None:
        self.written.append(data)

    async def close_stdin(self) -> None:
        self.stdin_closed = True

    async def close(self) -> None:
        self.closed = True


class FakeExec:
    """Answers `exec` and `open` by command; records every call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.streams: list[FakeStream] = []
        self.uploads: list[bytes] = []
        self.outputs: dict[tuple[str, ...], tuple[list[bytes], int]] = {}
        self.fail_upload = False

    def _answer(self, command: list[str]) -> tuple[list[bytes], int]:
        for prefix, answer in self.outputs.items():
            if tuple(command[: len(prefix)]) == prefix:
                return answer
        if command[:2] == ["hermes-relay", "health"]:
            return [], 0
        return [], 0

    async def exec(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        chunks, code = self._answer(kwargs["command"])
        return SimpleNamespace(stdout=b"".join(chunks).decode(), stderr="", exit_code=code, timed_out=False)

    async def open(self, **kwargs: Any) -> FakeStream:
        self.calls.append(kwargs)
        command = kwargs["command"]
        chunks, code = self._answer(command)
        stream = FakeStream(chunks, 2 if (self.fail_upload and command[0] == "tar" and "-x" in command) else code, stdin=kwargs.get("stdin", False))
        self.streams.append(stream)
        return stream

    def uploaded_archives(self) -> list[dict[str, bytes]]:
        return [
            _tar_members(b"".join(stream.written))
            for stream in self.streams
            if stream.stdin and stream.written
        ]


@pytest.fixture
def cluster(tmp_path: Path) -> tuple[KubernetesHermesSandboxes, FakeCore, FakeExec, MemoryStore]:
    core, exec_client, store = FakeCore(), FakeExec(), MemoryStore()
    settings = Settings(
        hermes_enabled=True,
        runtime="kubernetes",
        hermes_namespace=NAMESPACE,
        kubernetes_release_name="rel",
        hermes_snapshot_dir=str(tmp_path),
        hermes_pod_start_timeout_seconds=2,
        hermes_exec_timeout_seconds=2,
    )
    sandboxes = KubernetesHermesSandboxes(
        settings,
        store=store,  # type: ignore[arg-type]
        core_v1_api_factory=lambda: core,
        exec_client=exec_client,
        poll_interval_seconds=0.01,
    )
    return sandboxes, core, exec_client, store


async def _create(sandboxes: KubernetesHermesSandboxes, session: str = SESSION, **kwargs: Any) -> Any:
    return await sandboxes.create(
        session_id=session, org_id=ORG, conversation_id=CONVERSATION, token=TOKEN, model="m", **kwargs
    )


# Pod construction ---------------------------------------------------------


def test_the_pod_has_no_way_out_but_the_guard_and_no_privileges(cluster) -> None:
    sandboxes, *_ = cluster
    pod = sandboxes.build_pod(
        session_id=SESSION, org_id=ORG, conversation_id=CONVERSATION, created=1_700_000_000.0, guard_address=GUARD
    )
    spec = pod.spec
    assert pod.metadata.name == resource_name(SESSION) and pod.metadata.namespace == NAMESPACE
    assert pod.metadata.labels[LABEL] == "1" and pod.metadata.labels[SESSION_LABEL] == SESSION
    assert pod.metadata.labels[RELEASE_LABEL] == "rel" and pod.metadata.labels[COMPONENT_LABEL] == "hermes-sandbox"
    # The labels are what the chart's NetworkPolicies select.
    assert pod.metadata.labels["app.kubernetes.io/instance"] == "rel"
    assert spec.dns_policy == "None" and spec.dns_config.nameservers == [DEAD_RESOLVER]
    assert [(h.ip, h.hostnames) for h in spec.host_aliases] == [(GUARD, ["hermes-egress"])]
    assert spec.automount_service_account_token is False and spec.enable_service_links is False
    assert not (spec.host_network or spec.host_pid or spec.host_ipc)
    assert spec.security_context.run_as_non_root and spec.security_context.run_as_user == 10001
    assert spec.security_context.seccomp_profile.type == "RuntimeDefault"
    (container,) = spec.containers
    assert container.security_context.allow_privilege_escalation is False
    assert container.security_context.read_only_root_filesystem is True
    assert container.security_context.capabilities.drop == ["ALL"]
    assert container.env is None and container.command is None  # the image's own start script
    assert {m.mount_path for m in container.volume_mounts} == {"/workspace", "/tmp"}


def test_the_pod_is_bounded_and_the_workspace_is_a_volume_with_a_backstop(cluster) -> None:
    sandboxes, *_ = cluster
    pod = sandboxes.build_pod(
        session_id=SESSION, org_id=ORG, conversation_id=CONVERSATION, created=1.0, guard_address=GUARD
    )
    (container,) = pod.spec.containers
    assert container.resources.limits == {"cpu": "2.0", "memory": str(2 * 1024**3)}
    assert container.resources.requests == {"cpu": "250m", "memory": "512Mi"}
    volumes = {v.name: v.empty_dir for v in pod.spec.volumes}
    assert volumes["workspace"].medium is None
    assert volumes["workspace"].size_limit == str(int(1024**3 * 1.5))
    assert volumes["tmp"].medium == "Memory" and volumes["tmp"].size_limit == "256Mi"
    assert pod.spec.share_process_namespace is True


def test_a_tmpfs_workspace_is_memory_and_charged_to_the_pod(tmp_path: Path) -> None:
    sandboxes = KubernetesHermesSandboxes(
        Settings(hermes_enabled=True, hermes_workspace_mode="tmpfs", hermes_namespace=NAMESPACE),
        core_v1_api_factory=lambda: FakeCore(),
        exec_client=FakeExec(),
    )
    pod = sandboxes.build_pod(
        session_id=SESSION, org_id=ORG, conversation_id=CONVERSATION, created=1.0, guard_address=GUARD
    )
    (container,) = pod.spec.containers
    workspace = next(v for v in pod.spec.volumes if v.name == "workspace").empty_dir
    assert workspace.medium == "Memory" and workspace.size_limit == str(1024**3)
    assert container.resources.limits["memory"] == str(2 * 1024**3 + 1024**3)


def test_pull_secrets_and_a_runtime_class_are_passed_through() -> None:
    sandboxes = KubernetesHermesSandboxes(
        Settings(
            hermes_enabled=True,
            hermes_namespace=NAMESPACE,
            kubernetes_image_pull_secrets=("pull",),
            hermes_runtime_class="gvisor",
        ),
        core_v1_api_factory=lambda: FakeCore(),
        exec_client=FakeExec(),
    )
    spec = sandboxes.build_pod(
        session_id=SESSION, org_id=ORG, conversation_id=CONVERSATION, created=1.0, guard_address=GUARD
    ).spec
    assert [s.name for s in spec.image_pull_secrets] == ["pull"] and spec.runtime_class_name == "gvisor"


# Readiness ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readiness_needs_the_guard_service(cluster) -> None:
    sandboxes, core, *_ = cluster
    await sandboxes.ready()
    del core.services["hermes-egress"]
    with pytest.raises(HermesSandboxError) as excinfo:
        await sandboxes.ready()
    assert excinfo.value.code == "hermes_egress_guard_missing"
    core.services["hermes-egress"] = SimpleNamespace(spec=SimpleNamespace(cluster_ip="None"))
    with pytest.raises(HermesSandboxError) as excinfo:
        await sandboxes.ready()
    assert excinfo.value.code == "hermes_egress_guard_missing"


# Creation ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_starts_a_pod_and_writes_the_platform_files_by_exec(cluster) -> None:
    sandboxes, core, exec_client, _ = cluster
    sandbox, outcome = await _create(sandboxes)

    assert outcome == "created" and sandbox.container_id == resource_name(SESSION)
    assert list(core.pods) == [resource_name(SESSION)]
    (archive,) = exec_client.uploaded_archives()
    assert set(archive) == {".hermes/config.yaml", ".aurum/proxy", ".aurum/platform-skills/.platform"}
    config = json.loads(archive[".hermes/config.yaml"])
    assert config["model"]["api_key"] == TOKEN and config["platforms"]["api_server"]["extra"]["host"] == "127.0.0.1"
    assert archive[".aurum/proxy"] == f"http://hermes:{TOKEN}@hermes-egress:3128".encode()
    upload = next(c for c in exec_client.calls if c.get("stdin"))
    assert upload["command"] == ["tar", "-x", "--no-same-owner", "-C", "/workspace", "-f", "-"]
    assert upload["namespace"] == NAMESPACE and upload["container"] == "sandbox"
    assert any(c["command"][:2] == ["hermes-relay", "health"] for c in exec_client.calls)
    # The token is in a file only: never in the Pod object.
    assert TOKEN not in json.dumps(core.pods[resource_name(SESSION)].to_dict(), default=str)


@pytest.mark.asyncio
async def test_create_is_idempotent_and_refuses_another_owner(cluster) -> None:
    sandboxes, core, *_ = cluster
    first, _ = await _create(sandboxes)
    again, outcome = await _create(sandboxes)
    assert outcome == "existing" and again is first and len(core.pods) == 1
    with pytest.raises(HermesSandboxError) as excinfo:
        await sandboxes.create(
            session_id=SESSION, org_id=ORG, conversation_id="other", token=TOKEN, model="m"
        )
    assert excinfo.value.code == "hermes_sandbox_owner_mismatch"


@pytest.mark.asyncio
async def test_create_restores_the_latest_snapshot_before_the_platform_files(cluster) -> None:
    sandboxes, _, exec_client, store = cluster
    snapshot = await _snapshot_of(sandboxes, store)
    exec_client.streams.clear()
    await sandboxes.close(SESSION)

    sandbox, outcome = await _create(sandboxes, snapshot_sha256=snapshot)

    assert outcome == "restored" and sandbox.session_id == SESSION
    restore, platform = exec_client.uploaded_archives()
    assert restore[".hermes/state.db"] == b"state" and restore["home/work.py"] == b"print(1)"
    assert ".aurum/proxy" in platform and ".hermes/config.yaml" in platform


async def _snapshot_of(sandboxes: KubernetesHermesSandboxes, store: MemoryStore) -> str:
    await _create(sandboxes)
    exec_client: FakeExec = sandboxes._exec_client
    exec_client.outputs[("tar", "-C", "/", "-cf")] = ([WORKSPACE], 0)
    snapshot = await sandboxes.snapshot(SESSION)
    assert store.latest_sha[(ORG, CONVERSATION)] == snapshot.sha256
    return snapshot.sha256


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "code"),
    [("ImagePullBackOff", "hermes_image_unavailable"), ("CreateContainerConfigError", "hermes_image_unavailable")],
)
async def test_a_pod_that_cannot_start_is_removed(cluster, reason: str, code: str) -> None:
    sandboxes, core, *_ = cluster
    core.waiting_reason = reason
    with pytest.raises(HermesSandboxError) as excinfo:
        await _create(sandboxes)
    assert excinfo.value.code == code
    assert core.deleted == [resource_name(SESSION)] and not core.pods
    with pytest.raises(HermesSandboxError):
        sandboxes.get(SESSION)


@pytest.mark.asyncio
async def test_a_pod_that_never_runs_times_out_and_a_failed_pod_is_reported(cluster) -> None:
    sandboxes, core, *_ = cluster
    core.waiting_reason = "ContainerCreating"
    with pytest.raises(HermesSandboxError) as excinfo:
        await _create(sandboxes)
    assert excinfo.value.code == "hermes_start_timeout" and not core.pods
    core.waiting_reason, core.phase = None, "Failed"
    with pytest.raises(HermesSandboxError) as excinfo:
        await _create(sandboxes)
    assert excinfo.value.code == "hermes_start_failed"


@pytest.mark.asyncio
async def test_an_existing_pod_name_and_a_failed_upload_are_errors_that_clean_up(cluster) -> None:
    sandboxes, core, exec_client, _ = cluster
    core.pods[resource_name(SESSION)] = SimpleNamespace(metadata=SimpleNamespace(name="x", labels={}))
    with pytest.raises(HermesSandboxError) as excinfo:
        await _create(sandboxes)
    assert excinfo.value.code == "hermes_sandbox_exists"
    core.pods.clear()
    exec_client.fail_upload = True
    with pytest.raises(HermesSandboxError) as excinfo:
        await _create(sandboxes)
    assert excinfo.value.code == "hermes_upload_failed" and not core.pods


@pytest.mark.asyncio
async def test_an_unhealthy_relay_removes_the_new_pod(cluster) -> None:
    sandboxes, core, exec_client, _ = cluster
    sandboxes._settings.hermes_start_timeout_seconds = 0.05
    exec_client.outputs[("hermes-relay", "health")] = ([], 1)
    with pytest.raises(HermesSandboxError) as excinfo:
        await _create(sandboxes)
    assert excinfo.value.code == "hermes_start_timeout" and not core.pods


# The relay, files and previews -------------------------------------------------


@pytest.mark.asyncio
async def test_a_run_streams_the_relays_lines_after_the_request_is_written(cluster) -> None:
    sandboxes, _, exec_client, _ = cluster
    await _create(sandboxes)
    exec_client.outputs[("hermes-relay", "run")] = (
        [b'{"type":"started","run_id":"r1"}\n{"type":"mess', b'age"}\n{"type":"done"}\n'],
        0,
    )
    lines = [line async for line in sandboxes.run(SESSION, {"key": "k", "input": "hi"})]
    assert [line["type"] for line in lines] == ["started", "message", "done"]
    request = next(a for a in exec_client.uploaded_archives() if any(n.startswith(".aurum/requests/") for n in a))
    name = next(n for n in request if n.startswith(".aurum/requests/"))
    assert json.loads(request[name])["input"] == "hi"
    run = next(c for c in exec_client.calls if c["command"][:2] == ["hermes-relay", "run"])
    assert run["command"][2] == f"/workspace/{name}"


@pytest.mark.asyncio
async def test_a_file_the_agent_attaches_is_checked_against_its_hash(cluster) -> None:
    import base64
    import hashlib

    sandboxes, _, exec_client, _ = cluster
    await _create(sandboxes)
    data = b"report"
    chunk = json.dumps({"type": "chunk", "data": base64.b64encode(data).decode()}).encode()
    done = json.dumps({"type": "file", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}).encode()
    exec_client.outputs[("hermes-relay", "file-read")] = ([chunk + b"\n" + done + b"\n"], 0)
    assert await sandboxes.read_file(SESSION, "/workspace/report.txt") == data


@pytest.mark.asyncio
async def test_a_preview_stream_pipes_bytes_through_exec_and_half_closes(cluster) -> None:
    sandboxes, _, exec_client, _ = cluster
    await _create(sandboxes)
    exec_client.outputs[("hermes-relay", "connect")] = ([b"HTTP/1.1 200 OK\r\n\r\n"], 0)
    stream = await sandboxes.connect(SESSION, 8899)

    assert isinstance(stream, PreviewStream)
    await stream.write_in(b"GET / HTTP/1.1\r\n\r\n")
    await stream.write_in(b"")
    message = await stream.read_out()
    assert message is not None and (message.stream, message.data) == (1, b"HTTP/1.1 200 OK\r\n\r\n")
    assert await stream.read_out() is None
    await stream.close()

    pipe = exec_client.streams[-1]
    assert pipe.stdin and pipe.written == [b"GET / HTTP/1.1\r\n\r\n"] and pipe.stdin_closed and pipe.closed
    call = exec_client.calls[-1]
    assert call["command"] == ["hermes-relay", "connect", "8899"] and call["stdin"] is True
    for port in (80, 1023, 70000):
        with pytest.raises(HermesSandboxError) as excinfo:
            await sandboxes.connect(SESSION, port)
        assert excinfo.value.code == "hermes_preview_port_invalid"


# Snapshots -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_workspace_archive_is_bounded_and_tolerates_a_changing_file(cluster, tmp_path: Path) -> None:
    sandboxes, _, exec_client, _ = cluster
    sandbox, _ = await _create(sandboxes)
    target = tmp_path / "w.tar"
    exec_client.outputs[("tar", "-C", "/", "-cf")] = ([b"abc", b"def"], 1)
    await sandboxes._workspace_archive(sandbox.container_id, target)
    assert target.read_bytes() == b"abcdef"
    call = exec_client.calls[-1]
    assert call["command"] == ["tar", "-C", "/", "-cf", "-", "workspace"] and call["pod"] == sandbox.container_id

    exec_client.outputs[("tar", "-C", "/", "-cf")] = ([b"x"], 2)
    with pytest.raises(HermesSandboxError) as excinfo:
        await sandboxes._workspace_archive(sandbox.container_id, tmp_path / "bad.tar")
    assert excinfo.value.code == "hermes_snapshot_failed"

    sandboxes._settings.hermes_snapshot_max_bytes = 4
    sandboxes._settings.hermes_snapshot_max_entries = 0
    exec_client.outputs[("tar", "-C", "/", "-cf")] = ([b"123", b"45"], 0)
    from src.hermes_snapshots import SnapshotError

    with pytest.raises(SnapshotError) as snapshot_error:
        await sandboxes._workspace_archive(sandbox.container_id, tmp_path / "big.tar")
    assert snapshot_error.value.code == "hermes_snapshot_too_large"


def _idle(sandboxes: KubernetesHermesSandboxes, seconds: float = 4000) -> None:
    sandbox = sandboxes.get(SESSION)
    sandboxes._sandboxes[SESSION] = HermesSandbox(**{**sandbox.__dict__, "last_active": time.time() - seconds})


@pytest.mark.asyncio
async def test_an_idle_sandbox_is_snapshotted_from_its_running_pod_and_deleted(cluster) -> None:
    sandboxes, core, exec_client, store = cluster
    await _create(sandboxes)
    exec_client.outputs[("tar", "-C", "/", "-cf")] = ([WORKSPACE], 0)
    _idle(sandboxes)

    assert await sandboxes.reap() == 1

    assert core.deleted == [resource_name(SESSION)] and not core.pods
    assert (ORG, CONVERSATION) in store.latest_sha
    with pytest.raises(HermesSandboxError):
        sandboxes.get(SESSION)


@pytest.mark.asyncio
async def test_a_failed_snapshot_keeps_the_pod_and_refuses_work_until_retried(cluster) -> None:
    sandboxes, core, exec_client, store = cluster
    await _create(sandboxes)
    exec_client.outputs[("tar", "-C", "/", "-cf")] = ([WORKSPACE], 0)
    store.fail_put = True
    _idle(sandboxes)

    assert await sandboxes.reap() == 0

    assert resource_name(SESSION) in core.pods and not core.deleted
    assert sandboxes.get(SESSION).state == "stopped"
    with pytest.raises(HermesSandboxError) as excinfo:
        sandboxes.get_running(SESSION)
    assert excinfo.value.code == "hermes_sandbox_stopped"


@pytest.mark.asyncio
async def test_a_stopped_sandbox_is_never_resumed_with_its_old_token(cluster) -> None:
    sandboxes, core, exec_client, store = cluster
    await _create(sandboxes)
    exec_client.outputs[("tar", "-C", "/", "-cf")] = ([WORKSPACE], 0)
    store.fail_put = True
    _idle(sandboxes)
    await sandboxes.reap()
    store.fail_put = False

    sandbox, outcome = await _create(sandboxes)

    assert outcome == "restored" and sandbox.state == "running"
    # Retired (snapshotted, deleted) and created anew, with a new Pod.
    assert core.deleted == [resource_name(SESSION)] and list(core.pods) == [resource_name(SESSION)]


# Adoption and reaping --------------------------------------------------------


def _pod(session: str, *, phase: str = "Running", created: float | None = None, deleting: bool = False) -> Any:
    labels = {
        LABEL: "1",
        SESSION_LABEL: session,
        RELEASE_LABEL: "rel",
        COMPONENT_LABEL: "hermes-sandbox",
        "aurum-ai.hermes-org": ORG,
        "aurum-ai.hermes-conversation": CONVERSATION,
        CREATED_LABEL: str(int(created if created is not None else time.time())),
    }
    return SimpleNamespace(
        metadata=SimpleNamespace(name=resource_name(session), labels=labels, deletion_timestamp="t" if deleting else None),
        status=SimpleNamespace(phase=phase, container_statuses=[]),
    )


@pytest.mark.asyncio
async def test_a_restarted_executor_adopts_live_pods_only(cluster) -> None:
    sandboxes, core, *_ = cluster
    for session, kwargs in {
        "1" * 32: {},
        "2" * 32: {"phase": "Pending"},
        "3" * 32: {"phase": "Failed"},
        "4" * 32: {"phase": "Succeeded"},
        "5" * 32: {"deleting": True},
    }.items():
        pod = _pod(session, **kwargs)
        core.pods[pod.metadata.name] = pod
    stranger = _pod("6" * 32)
    stranger.metadata.labels[RELEASE_LABEL] = "other-release"
    core.pods["stranger"] = stranger

    assert await sandboxes.adopt() == 2
    assert sandboxes.get("1" * 32).org_id == ORG and sandboxes.get("1" * 32).state == "running"
    assert sandboxes.get("1" * 32).container_id == resource_name("1" * 32)
    assert await sandboxes.adopt() == 0
    for session in ("3" * 32, "4" * 32, "5" * 32, "6" * 32):
        with pytest.raises(HermesSandboxError):
            sandboxes.get(session)


@pytest.mark.asyncio
async def test_leftover_pods_are_removed_and_a_lost_pod_is_forgotten(cluster) -> None:
    sandboxes, core, *_ = cluster
    old = time.time() - 4000
    ended = _pod("3" * 32, phase="Failed")
    orphan = _pod("7" * 32, created=old)
    fresh_orphan = _pod("8" * 32)
    kept = _pod("1" * 32)
    for pod in (ended, orphan, fresh_orphan, kept):
        core.pods[pod.metadata.name] = pod
    await sandboxes.adopt()  # adopts the live ones, 7, 8 and 1
    # 7 and 8 were adopted: only an unknown or ended pod is a leftover, so make 7 unknown again.
    sandboxes._sandboxes.pop("7" * 32)
    lost = HermesSandbox(
        session_id="9" * 32, org_id=ORG, conversation_id=CONVERSATION, container_id="x", network="x", volume="x",
        created_at=old,
    )
    sandboxes._sandboxes["9" * 32] = lost

    await sandboxes._reap_leftovers(time.time())

    assert set(core.deleted) == {resource_name("3" * 32), resource_name("7" * 32)}
    assert set(sandboxes._sandboxes) == {"1" * 32, "8" * 32}


@pytest.mark.asyncio
async def test_workspace_usage_is_measured_in_each_pod_and_over_the_limit_stops_runs(cluster) -> None:
    sandboxes, _, exec_client, _ = cluster
    await _create(sandboxes)
    exec_client.outputs[("du", "-sb")] = ([b"2147483648\t/workspace\n"], 0)
    exec_client.outputs[("hermes-relay", "active")] = ([b'{"type":"active","run_ids":["r1"]}\n'], 0)
    sandboxes._disk_checked_at = 0

    await sandboxes._check_disk()

    assert sandboxes.get(SESSION).workspace_full is True
    assert any(c["command"] == ["hermes-relay", "stop", "r1"] for c in exec_client.calls)
    with pytest.raises(HermesSandboxError) as excinfo:
        async for _ in sandboxes.run(SESSION, {"key": "k"}):
            pass
    assert excinfo.value.code == "hermes_workspace_full"

    exec_client.outputs[("du", "-sb")] = ([b"10\t/workspace\n"], 0)
    sandboxes._disk_checked_at = 0
    await sandboxes._check_disk()
    assert sandboxes.get(SESSION).workspace_full is False


@pytest.mark.asyncio
async def test_an_unmeasurable_workspace_is_not_called_full(cluster) -> None:
    sandboxes, _, exec_client, _ = cluster
    await _create(sandboxes)
    exec_client.outputs[("du", "-sb")] = ([], 1)
    sandboxes._disk_checked_at = 0
    await sandboxes._check_disk()
    assert sandboxes.get(SESSION).workspace_full is False


@pytest.mark.asyncio
async def test_close_deletes_the_pod_and_purge_removes_a_conversation(cluster) -> None:
    sandboxes, core, _, store = cluster
    await _create(sandboxes)
    assert await sandboxes.purge_conversation(ORG, CONVERSATION) == {"sandboxes": 1, "snapshots": 0}
    assert core.deleted == [resource_name(SESSION)]
    await sandboxes.close(SESSION)  # idempotent: the pod is already gone
    assert not core.pods


@pytest.mark.asyncio
async def test_exec_failures_are_the_backends_errors(cluster) -> None:
    sandboxes, *_ = cluster
    assert SandboxExecError in sandboxes._backend_errors and ApiException in sandboxes._backend_errors


@pytest.mark.asyncio
async def test_a_removed_pod_is_gone_before_its_name_is_used_again(cluster) -> None:
    sandboxes, core, *_ = cluster
    await _create(sandboxes)
    core.linger = 3  # the Pod stays Terminating for a few reads

    await sandboxes.close(SESSION)
    assert resource_name(SESSION) not in core.pods

    core.linger = 0
    sandbox, outcome = await _create(sandboxes)
    assert outcome == "created" and sandbox.session_id == SESSION


@pytest.mark.asyncio
async def test_a_pod_that_will_not_go_does_not_hang_the_caller(cluster) -> None:
    sandboxes, core, *_ = cluster
    await _create(sandboxes)
    core.linger = 10**6
    sandboxes._settings.hermes_stop_timeout_seconds = 0
    sandboxes._remove_wait_seconds = 0.05
    await sandboxes.close(SESSION)  # returns: a Pod stuck Terminating is the cluster's to finish


def _terminating_pod(name: str) -> Any:
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels={}, deletion_timestamp="t"),
        status=SimpleNamespace(phase="Running", container_statuses=[]),
    )


@pytest.mark.asyncio
async def test_create_waits_for_a_terminating_pod_of_the_same_name(cluster) -> None:
    sandboxes, core, *_ = cluster
    name = resource_name(SESSION)
    # A Pod deleted while its image was still being pulled keeps the name for a while.
    core.pods[name] = _terminating_pod(name)
    core._terminating[name] = 3

    sandbox, outcome = await _create(sandboxes)

    assert outcome == "created" and sandbox.container_id == name
    assert core.pods[name].spec is not None  # the new Pod, not the old one


@pytest.mark.asyncio
async def test_a_pod_that_stays_terminating_past_the_start_timeout_is_a_conflict(cluster) -> None:
    sandboxes, core, *_ = cluster
    sandboxes._settings.hermes_pod_start_timeout_seconds = 0.1
    name = resource_name(SESSION)
    core.pods[name] = _terminating_pod(name)
    core._terminating[name] = 10**6

    with pytest.raises(HermesSandboxError) as excinfo:
        await _create(sandboxes)

    assert excinfo.value.code == "hermes_sandbox_exists"


# A node that is lost ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_pod_stuck_terminating_is_force_deleted_after_the_wait(cluster) -> None:
    sandboxes, core, *_ = cluster
    await _create(sandboxes)
    core.linger = 10**6  # its node is gone: nothing confirms the termination
    sandboxes._settings.hermes_stop_timeout_seconds = 1
    sandboxes._remove_wait_seconds = 0.05

    await sandboxes.close(SESSION)

    # A graceful delete first, then a forced one once the wait is over.
    assert core.deleted == [resource_name(SESSION)] * 2
    assert core.forced == [resource_name(SESSION)] and not core.pods


@pytest.mark.asyncio
@pytest.mark.parametrize("ready", ["Unknown", "False"])
async def test_a_sandbox_whose_node_reports_it_not_ready_is_started_anew(cluster, ready: str) -> None:
    sandboxes, core, exec_client, store = cluster
    snapshot = await _snapshot_of(sandboxes, store)
    exec_client.streams.clear()
    core.ready = ready  # the node controller marks its Pods so when the node is lost

    sandbox, outcome = await _create(sandboxes)

    assert outcome == "restored" and sandbox.session_id == SESSION
    assert core.forced == [resource_name(SESSION)]
    assert list(core.pods) == [resource_name(SESSION)]
    restore, platform = exec_client.uploaded_archives()
    assert ".hermes/state.db" in restore and ".aurum/proxy" in platform
    assert snapshot


@pytest.mark.asyncio
async def test_a_ready_sandbox_is_left_alone(cluster) -> None:
    sandboxes, core, *_ = cluster
    first, _ = await _create(sandboxes)
    core.ready = "True"
    again, outcome = await _create(sandboxes)
    assert outcome == "existing" and again is first and not core.forced


@pytest.mark.asyncio
async def test_a_sandbox_whose_pod_is_gone_or_terminating_is_started_anew(cluster) -> None:
    sandboxes, core, *_ = cluster
    await _create(sandboxes)
    name = resource_name(SESSION)
    del core.pods[name]  # evicted and collected, or lost with its node

    sandbox, outcome = await _create(sandboxes)
    assert outcome == "created" and name in core.pods

    core.pods[name].metadata.deletion_timestamp = "t"  # marked for deletion, never confirmed
    core._terminating[name] = 10**6
    sandbox, outcome = await _create(sandboxes)
    assert outcome == "created" and core.forced == [name]


@pytest.mark.asyncio
async def test_a_stopped_sandbox_on_a_lost_node_is_not_stuck_on_its_snapshot(cluster) -> None:
    sandboxes, core, exec_client, store = cluster
    await _create(sandboxes)
    exec_client.outputs[("tar", "-C", "/", "-cf")] = ([WORKSPACE], 0)
    store.fail_put = True
    _idle(sandboxes)
    await sandboxes.reap()  # the snapshot fails: kept, stopped
    assert sandboxes.get(SESSION).state == "stopped"
    core.ready = "Unknown"  # and then its node is lost; nothing can be snapshotted any more
    store.fail_put = False
    exec_client.outputs[("tar", "-C", "/", "-cf")] = ([], 2)

    sandbox, outcome = await _create(sandboxes)

    assert sandbox.state == "running" and outcome in ("created", "restored")


@pytest.mark.asyncio
async def test_the_reaper_force_deletes_pods_terminating_for_long(cluster) -> None:
    from datetime import UTC, datetime, timedelta

    sandboxes, core, *_ = cluster
    long_ago = datetime.now(UTC) - timedelta(minutes=10)
    just_now = datetime.now(UTC)
    for session, when in (("3" * 32, long_ago), ("4" * 32, just_now)):
        pod = _pod(session)
        pod.metadata.deletion_timestamp = when
        core.pods[pod.metadata.name] = pod
    await sandboxes._reap_leftovers(time.time())
    assert core.forced == [resource_name("3" * 32)]
