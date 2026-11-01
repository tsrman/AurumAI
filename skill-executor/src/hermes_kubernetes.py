"""Hermes Agent sandboxes on Kubernetes (docs/HERMES_AGENT.md, phase 2d).

The same lifecycle as src/hermes_runtime.py, with a Pod per session in the
chart's Hermes namespace instead of a container, a network and a volume:

* the Pod's only way out is the `hermes-egress` Service, which it reaches
  through a hostAliases entry (no DNS at all: the resolver is a dead address);
  the chart's NetworkPolicies allow nothing else, so sandboxes cannot reach each
  other, the platform or the nodes;
* the workspace is an emptyDir (memory-backed in tmpfs mode); a Pod cannot be
  stopped and kept, so a retired sandbox is snapshotted from its running Pod
  and the Pod deleted, and a sandbox whose snapshot failed is kept running but
  refuses work until it is retried or its retention runs out;
* every file goes in and out through `pods/exec` (tar over stdin and stdout), and
  the relay's output is untrusted exactly as on Docker.

Ownership is in Pod labels, so a restarted executor re-adopts live sandboxes.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable

import aiohttp
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio import config as k8s_config
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.config.config_exception import ConfigException

from src.config import Settings
from src.hermes_runtime import (
    _CHUNK,
    CONVERSATION_LABEL,
    CREATED_LABEL,
    LABEL,
    ORG_LABEL,
    SANDBOX_UID,
    SESSION_LABEL,
    HermesSandbox,
    HermesSandboxError,
    HermesSandboxes,
    resource_name,
)
from src.hermes_snapshots import SnapshotError, SnapshotStore, to_thread_sanitize
from src.sandbox_runtime.base import SandboxExecError
from src.sandbox_runtime.kubernetes_exec import ExecStream, KubernetesExecClient

logger = logging.getLogger(__name__)

RELEASE_LABEL = "app.kubernetes.io/instance"
COMPONENT_LABEL = "aurum-ai.component"
COMPONENT_VALUE = "hermes-sandbox"
CONTAINER = "sandbox"
# A resolver nobody answers (TEST-NET-1): a Pod's dnsPolicy None needs one.
DEAD_RESOLVER = "192.0.2.1"
_TERMINAL_WAITING_REASONS = frozenset(
    {"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "CreateContainerConfigError", "CreateContainerError"}
)
_TERMINAL_PHASES = frozenset({"Succeeded", "Failed"})


@dataclass(frozen=True)
class RelayMessage:
    """What the preview route reads from a stream: standard output only."""

    stream: int
    data: bytes


@dataclass(frozen=True)
class PodIdentity:
    """Ownership observed on an acknowledged creation or scoped discovery."""

    name: str
    uid: str
    session_id: str
    org_id: str
    conversation_id: str


class PreviewStream:
    """`hermes-relay connect` as the preview route drives it: write_in, read_out, close."""

    def __init__(self, stream: ExecStream) -> None:
        self._stream = stream

    async def write_in(self, data: bytes) -> None:
        if data:
            await self._stream.write(data)

    async def read_out(self) -> RelayMessage | None:
        data = await self._stream.read()
        return None if data is None else RelayMessage(1, data)

    async def close(self) -> None:
        try:
            # End of input ends the relay; then the socket goes.
            await self._stream.close_stdin()
        except Exception:  # noqa: BLE001 - the other side may already be gone
            pass
        await self._stream.close()


class KubernetesHermesSandboxes(HermesSandboxes):
    _backend_errors = (ApiException, SandboxExecError, aiohttp.ClientError, OSError)
    # A Pod cannot be stopped and started again, so a stopped sandbox is retired.
    _resumable = False

    def __init__(
        self,
        settings: Settings,
        store: SnapshotStore | None = None,
        *,
        core_v1_api_factory: Callable[[], Any] | None = None,
        exec_client: Any | None = None,
        poll_interval_seconds: float = 0.5,
    ) -> None:
        super().__init__(settings, store)
        self._core_factory = core_v1_api_factory
        self._core: Any | None = None
        self._api_client: Any | None = None
        self._exec_client = exec_client or KubernetesExecClient()
        self._poll_interval = poll_interval_seconds
        self._pod_identities: dict[str, PodIdentity] = {}

    @property
    def namespace(self) -> str:
        return self._settings.hermes_namespace

    async def _api(self) -> Any:
        if self._core is not None:
            return self._core
        if self._core_factory is not None:
            self._core = self._core_factory()
            return self._core
        try:
            if os.getenv("KUBERNETES_SERVICE_HOST") and os.getenv("KUBERNETES_SERVICE_PORT"):
                k8s_config.load_incluster_config()  # type: ignore[no-untyped-call]
            else:
                await k8s_config.load_kube_config()
        except ConfigException:
            logger.warning("Kubernetes configuration for Hermes sandboxes is unavailable", exc_info=True)
            raise HermesSandboxError("hermes_kubernetes_unavailable") from None
        self._api_client = k8s_client.ApiClient()
        self._core = k8s_client.CoreV1Api(self._api_client)
        return self._core

    async def shutdown(self) -> None:
        if self._api_client is not None:
            await self._api_client.close()
            self._api_client = None
            self._core = None

    # Pod construction -------------------------------------------------------

    def _pod_labels(self, session_id: str, org_id: str, conversation_id: str, created: float) -> dict[str, str]:
        return {
            **self._labels(session_id, org_id, conversation_id, created),
            RELEASE_LABEL: self._settings.kubernetes_release_name,
            COMPONENT_LABEL: COMPONENT_VALUE,
        }

    @property
    def _selector(self) -> str:
        return f"{LABEL}=1,{RELEASE_LABEL}={self._settings.kubernetes_release_name},{COMPONENT_LABEL}={COMPONENT_VALUE}"

    def _pod_identity(self, pod: k8s_client.V1Pod) -> PodIdentity | None:
        metadata = pod.metadata
        labels = metadata.labels or {}
        session_id = labels.get(SESSION_LABEL, "")
        org_id = labels.get(ORG_LABEL, "")
        conversation_id = labels.get(CONVERSATION_LABEL, "")
        if (
            not metadata.uid
            or not session_id
            or not org_id
            or not conversation_id
            or metadata.name != resource_name(session_id)
            or labels.get(LABEL) != "1"
            or labels.get(RELEASE_LABEL) != self._settings.kubernetes_release_name
            or labels.get(COMPONENT_LABEL) != COMPONENT_VALUE
        ):
            return None
        return PodIdentity(metadata.name, metadata.uid, session_id, org_id, conversation_id)

    def build_pod(
        self, *, session_id: str, org_id: str, conversation_id: str, created: float, guard_address: str
    ) -> k8s_client.V1Pod:
        settings = self._settings
        name = resource_name(session_id)
        workspace_limit = settings.hermes_workspace_max_bytes
        if self._tmpfs:
            # Memory-backed pages are charged to the Pod, so the limit is its memory too.
            memory_limit = settings.hermes_memory_bytes + workspace_limit
            workspace = k8s_client.V1EmptyDirVolumeSource(medium="Memory", size_limit=str(workspace_limit))
        else:
            memory_limit = settings.hermes_memory_bytes
            workspace = k8s_client.V1EmptyDirVolumeSource(
                size_limit=str(int(workspace_limit * settings.hermes_workspace_backstop_factor))
            )
        container = k8s_client.V1Container(
            name=CONTAINER,
            image=settings.hermes_sandbox_image,
            image_pull_policy=settings.kubernetes_image_pull_policy,
            security_context=k8s_client.V1SecurityContext(
                allow_privilege_escalation=False,
                read_only_root_filesystem=True,
                capabilities=k8s_client.V1Capabilities(drop=["ALL"]),
            ),
            volume_mounts=[
                k8s_client.V1VolumeMount(name="workspace", mount_path="/workspace"),
                k8s_client.V1VolumeMount(name="tmp", mount_path="/tmp"),
            ],
            resources=k8s_client.V1ResourceRequirements(
                requests={"cpu": settings.hermes_cpu_request, "memory": settings.hermes_memory_request},
                limits={"cpu": str(settings.hermes_cpus), "memory": str(memory_limit)},
            ),
        )
        return k8s_client.V1Pod(
            metadata=k8s_client.V1ObjectMeta(
                name=name,
                namespace=self.namespace,
                labels=self._pod_labels(session_id, org_id, conversation_id, created),
            ),
            spec=k8s_client.V1PodSpec(
                # In place of Docker's init: orphaned processes (apps the agent
                # started) are reaped by the pause process.
                share_process_namespace=True,
                # The container may restart in place and keeps its workspace.
                restart_policy="Always",
                termination_grace_period_seconds=settings.hermes_stop_timeout_seconds,
                automount_service_account_token=False,
                enable_service_links=False,
                host_network=False,
                host_pid=False,
                host_ipc=False,
                # No DNS: the guard is named by an address, and the proxy resolves
                # internet hosts. The resolver below answers nobody.
                dns_policy="None",
                dns_config=k8s_client.V1PodDNSConfig(
                    nameservers=[DEAD_RESOLVER],
                    options=[
                        k8s_client.V1PodDNSConfigOption(name="timeout", value="1"),
                        k8s_client.V1PodDNSConfigOption(name="attempts", value="1"),
                    ],
                ),
                host_aliases=[k8s_client.V1HostAlias(ip=guard_address, hostnames=["hermes-egress"])],
                runtime_class_name=settings.hermes_runtime_class or None,
                security_context=k8s_client.V1PodSecurityContext(
                    run_as_non_root=True,
                    run_as_user=SANDBOX_UID,
                    run_as_group=SANDBOX_UID,
                    fs_group=SANDBOX_UID,
                    seccomp_profile=k8s_client.V1SeccompProfile(type="RuntimeDefault"),
                ),
                containers=[container],
                volumes=[
                    k8s_client.V1Volume(name="workspace", empty_dir=workspace),
                    k8s_client.V1Volume(
                        name="tmp",
                        empty_dir=k8s_client.V1EmptyDirVolumeSource(
                            medium="Memory", size_limit=f"{settings.hermes_tmp_mb}Mi"
                        ),
                    ),
                ],
                image_pull_secrets=[
                    k8s_client.V1LocalObjectReference(name=secret) for secret in settings.kubernetes_image_pull_secrets
                ],
            ),
        )

    # Readiness, adoption and discovery --------------------------------------

    async def _guard_address(self) -> str:
        """The guard Service's address; sandboxes name it through hostAliases."""
        core = await self._api()
        try:
            service = await core.read_namespaced_service(self._settings.hermes_guard_service, self.namespace)
        except ApiException as exc:
            if exc.status == 404:
                raise HermesSandboxError("hermes_egress_guard_missing") from None
            logger.warning("Could not read the Hermes guard Service", exc_info=True)
            raise HermesSandboxError("hermes_kubernetes_unavailable") from None
        address = getattr(service.spec, "cluster_ip", None)
        if not address or address == "None":
            raise HermesSandboxError("hermes_egress_guard_missing")
        return str(address)

    async def _check_backend(self) -> None:
        await self._guard_address()

    async def _live_pods(self) -> list[k8s_client.V1Pod]:
        core = await self._api()
        listed = await core.list_namespaced_pod(self.namespace, label_selector=self._selector)
        return [
            pod
            for pod in listed.items
            if pod.metadata.deletion_timestamp is None and pod.status.phase not in _TERMINAL_PHASES
        ]

    async def _discover(self) -> list[tuple[str, dict[str, str], bool]]:
        discovered = []
        for pod in await self._live_pods():
            identity = self._pod_identity(pod)
            if identity is None or self._pod_identities.get(identity.name, identity) != identity:
                continue
            self._pod_identities[identity.name] = identity
            discovered.append((identity.name, dict(pod.metadata.labels or {}), pod.status.phase in ("Pending", "Running")))
        return discovered

    # Creation ---------------------------------------------------------------

    async def _wait_running(self, name: str) -> None:
        """Until the container runs: scheduling and an image pull come first."""
        core = await self._api()
        deadline = time.monotonic() + self._settings.hermes_pod_start_timeout_seconds
        while time.monotonic() < deadline:
            try:
                pod = await core.read_namespaced_pod(name, self.namespace)
            except ApiException:
                raise HermesSandboxError("hermes_start_failed") from None
            if pod.status.phase in _TERMINAL_PHASES:
                raise HermesSandboxError("hermes_start_failed")
            for status in pod.status.container_statuses or []:
                if status.state is not None and status.state.running is not None:
                    return
                waiting = status.state.waiting if status.state is not None else None
                if waiting is not None and waiting.reason in _TERMINAL_WAITING_REASONS:
                    raise HermesSandboxError("hermes_image_unavailable")
            await asyncio.sleep(self._poll_interval)
        raise HermesSandboxError("hermes_start_timeout")

    async def _provision(
        self,
        *,
        session_id: str,
        org_id: str,
        conversation_id: str,
        token: str,
        model: str,
        snapshot_sha256: str | None,
    ) -> tuple[HermesSandbox, bool]:
        name = resource_name(session_id)
        created = time.time()
        guard = await self._guard_address()
        pod = self.build_pod(
            session_id=session_id, org_id=org_id, conversation_id=conversation_id, created=created, guard_address=guard
        )
        sandbox = HermesSandbox(
            session_id=session_id,
            org_id=org_id,
            conversation_id=conversation_id,
            container_id=name,
            network=name,
            volume=name,
            created_at=created,
        )
        restored = False
        core = await self._api()
        # An uncertain create or a conflict is not proof this attempt owns a Pod.
        identity = await self._create_pod(core, pod)
        self._pod_identities[name] = identity
        try:
            await self._wait_running(name)
            if snapshot_sha256 is not None:
                await self._restore(sandbox, org_id, conversation_id, snapshot_sha256)
                restored = True
            # After the restore, so the platform's files are always the current
            # ones; start.sh waits for them.
            await self._put_archive(sandbox, self._secrets_archive(token=token, model=model))
        except SnapshotError as exc:
            await self._remove(name)
            raise HermesSandboxError(exc.code) from None
        except TimeoutError:
            await self._remove(name)
            raise HermesSandboxError("hermes_snapshot_timeout") from None
        except Exception:
            await self._remove(name)
            raise
        self._sandboxes[session_id] = sandbox
        return sandbox, restored

    async def _create_pod(self, core: Any, pod: k8s_client.V1Pod) -> PodIdentity:
        """Create the Pod. One of the same name that is still terminating (deleted
        while its image was being pulled, it can outlast the removal's own wait)
        is waited for, so a session started again at once is not refused."""
        for attempt in range(2):
            try:
                created = await core.create_namespaced_pod(self.namespace, pod)
                identity = self._pod_identity(created)
                if identity is None or any(
                    (created.metadata.labels or {}).get(key) != value
                    for key, value in (pod.metadata.labels or {}).items()
                ):
                    raise HermesSandboxError("hermes_sandbox_owner_mismatch")
                return identity
            except ApiException as exc:
                if exc.status != 409:
                    logger.warning("Could not create the Hermes sandbox Pod", exc_info=True)
                    raise HermesSandboxError("hermes_kubernetes_failed") from None
                if attempt or not await self._wait_gone(core, pod.metadata.name):
                    raise HermesSandboxError("hermes_sandbox_exists") from None
        raise HermesSandboxError("hermes_sandbox_exists")

    async def _wait_gone(self, core: Any, name: str) -> bool:
        """True once a Pod that is being deleted has gone; False for one that is not
        being deleted, or that outlasts the start timeout."""
        deadline = time.monotonic() + self._settings.hermes_pod_start_timeout_seconds
        while time.monotonic() < deadline:
            try:
                pod = await core.read_namespaced_pod(name, self.namespace)
            except ApiException as exc:
                return exc.status == 404
            if getattr(pod.metadata, "deletion_timestamp", None) is None:
                return False
            await asyncio.sleep(self._poll_interval)
        return False

    async def _resume(self, sandbox: HermesSandbox, *, token: str, model: str) -> HermesSandbox:
        raise HermesSandboxError("hermes_resume_failed")

    # Exec relay -------------------------------------------------------------

    async def _exec(self, sandbox: HermesSandbox, cmd: list[str]) -> tuple[int | None, bytes]:
        result = await self._exec_client.exec(
            namespace=self.namespace,
            pod=sandbox.container_id,
            container=CONTAINER,
            command=cmd,
            timeout_seconds=self._settings.hermes_exec_timeout_seconds,
            max_bytes=self._settings.hermes_event_line_max_bytes,
        )
        return (None if result.timed_out else result.exit_code), result.stdout.encode()

    async def _open_stream(self, sandbox: HermesSandbox, cmd: list[str]) -> AsyncGenerator[bytes, None]:
        stream = await self._exec_client.open(
            namespace=self.namespace, pod=sandbox.container_id, container=CONTAINER, command=cmd
        )
        try:
            while (chunk := await stream.read()) is not None:
                yield chunk
        finally:
            await stream.close()

    async def _upload(self, sandbox: HermesSandbox, source: BinaryIO) -> None:
        """Unpack a tar archive into /workspace: it goes in on the command's stdin."""
        stream = await self._exec_client.open(
            namespace=self.namespace,
            pod=sandbox.container_id,
            container=CONTAINER,
            command=["tar", "-x", "--no-same-owner", "-C", "/workspace", "-f", "-"],
            stdin=True,
        )
        try:
            while block := source.read(_CHUNK):
                await stream.write(block)
            await stream.close_stdin()
            while await stream.read() is not None:
                pass
            if stream.exit_code != 0:
                raise HermesSandboxError("hermes_upload_failed")
        finally:
            await stream.close()

    async def _put_archive(self, sandbox: HermesSandbox, data: bytes) -> None:
        async with asyncio.timeout(self._settings.hermes_exec_timeout_seconds):
            await self._upload(sandbox, io.BytesIO(data))

    async def connect(self, session_id: str, port: int) -> Any:
        """A raw byte stream to 127.0.0.1:port in the sandbox, for a preview:
        `hermes-relay connect` on stdin and stdout. Loopback only, port 1024
        or above; preview traffic never counts as the sandbox being in use."""
        if not 1024 <= port <= 65535:
            raise HermesSandboxError("hermes_preview_port_invalid")
        sandbox = self.get_running(session_id)
        stream = await self._exec_client.open(
            namespace=self.namespace,
            pod=sandbox.container_id,
            container=CONTAINER,
            command=["hermes-relay", "connect", str(port)],
            stdin=True,
        )
        return PreviewStream(stream)

    # Snapshots --------------------------------------------------------------

    async def _workspace_archive(self, container_id: str, path: Path) -> None:
        """`tar` of /workspace from the running Pod, written to `path` within the bound."""
        limit, size = self._archive_limit(), 0
        stream = await self._exec_client.open(
            namespace=self.namespace,
            pod=container_id,
            container=CONTAINER,
            command=["tar", "-C", "/", "-cf", "-", "workspace"],
        )
        try:
            with path.open("xb") as target:
                path.chmod(0o600)
                while (block := await stream.read()) is not None:
                    size += len(block)
                    if size > limit:
                        raise SnapshotError("hermes_snapshot_too_large")
                    target.write(block)
            # 1 is "a file changed as it was read": a live workspace does that.
            if stream.exit_code not in (0, 1):
                raise HermesSandboxError("hermes_snapshot_failed")
        finally:
            await stream.close()

    async def _restore(self, sandbox: HermesSandbox, org_id: str, conversation_id: str, sha256: str) -> None:
        """Verify the snapshot, rebuild it as a clean archive and unpack that."""
        async with asyncio.timeout(self._settings.hermes_snapshot_timeout_seconds):
            with self._tempdir() as directory:
                fetched, clean = Path(directory, "fetched.tar"), Path(directory, "restore.tar")
                await self._store.fetch(org_id, conversation_id, sha256, fetched, self._archive_limit())
                await to_thread_sanitize(fetched, clean, strip=None, settings=self._settings)
                fetched.unlink()
                with clean.open("rb") as archive:
                    await self._upload(sandbox, archive)

    async def _stop_container(self, sandbox: HermesSandbox) -> None:
        """A Pod cannot be stopped and kept: it is snapshotted while it runs, and
        its state "retiring" already refuses new work."""

    # How long past its grace period a deleted Pod may stay Terminating before
    # it is deleted by force, and how long a forced deletion is waited for.
    _remove_wait_seconds = 30.0
    _force_wait_seconds = 10.0

    async def _remove(self, name: str, *, force: bool = False) -> None:
        """Delete the Pod and wait until it is gone: a Terminating Pod still holds
        its name, so a session created again at once would be refused. One whose
        node is lost never confirms its termination, so after the wait (at once
        with `force`) the Pod object is deleted by force: its workspace was only a
        cache of the last snapshot, and a node that returns kills what remains."""
        core = await self._api()
        identity = self._pod_identities.get(name)
        if identity is None:
            return
        grace = 0 if force else self._settings.hermes_stop_timeout_seconds
        if not await self._delete_pod(core, identity, grace):
            return
        wait = self._force_wait_seconds if force else grace + self._remove_wait_seconds
        gone = await self._wait_deleted(core, identity, wait)
        if gone is False and not force:
            logger.warning("Hermes sandbox Pod %s is still terminating; deleting it by force", name)
            if await self._force_delete(core, identity):
                gone = await self._wait_deleted(core, identity, self._force_wait_seconds)
        if gone and self._pod_identities.get(name) == identity:
            self._pod_identities.pop(name, None)

    async def _delete_pod(self, core: Any, identity: PodIdentity, grace: int) -> bool:
        """Recheck ownership and fence the API mutation against replacement."""
        try:
            pod = await core.read_namespaced_pod(identity.name, self.namespace)
            if self._pod_identity(pod) != identity:
                logger.warning("Refusing to delete Hermes Pod %s: ownership changed", identity.name)
                return False
            await core.delete_namespaced_pod(
                identity.name,
                self.namespace,
                grace_period_seconds=grace,
                body=k8s_client.V1DeleteOptions(preconditions=k8s_client.V1Preconditions(uid=identity.uid)),
            )
        except ApiException as exc:
            if exc.status == 404:
                return True
            logger.warning("Could not delete Hermes sandbox Pod %s", identity.name, exc_info=True)
            return False
        return True

    async def _force_delete(self, core: Any, identity: PodIdentity) -> bool:
        return await self._delete_pod(core, identity, 0)

    async def _wait_deleted(self, core: Any, identity: PodIdentity, seconds: float) -> bool | None:
        """True if the UID is gone, False on timeout, None when the API is unsure."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                pod = await core.read_namespaced_pod(identity.name, self.namespace)
            except ApiException as exc:
                if exc.status != 404:
                    logger.warning("Could not check Hermes sandbox Pod %s", identity.name, exc_info=True)
                    return None
                return True
            if pod.metadata.uid != identity.uid:
                return True
            await asyncio.sleep(self._poll_interval)
        return False

    async def _lost(self, sandbox: HermesSandbox) -> bool:
        """The Pod is gone, being deleted, or reported not Ready by the node's own
        controller (which is how a lost node's Pods look). An unreadable Pod is not
        called lost."""
        core = await self._api()
        try:
            pod = await core.read_namespaced_pod(sandbox.container_id, self.namespace)
        except ApiException as exc:
            return exc.status == 404
        if getattr(pod.metadata, "deletion_timestamp", None) is not None:
            return True
        for condition in getattr(pod.status, "conditions", None) or []:
            if condition.type == "Ready":
                return bool(condition.status != "True")
        return False

    async def _discard(self, sandbox: HermesSandbox) -> None:
        await self._remove(sandbox.network, force=True)

    # Reaping ----------------------------------------------------------------

    async def _volume_usage(self) -> dict[str, int]:
        """Each running sandbox's workspace size, measured in its own Pod."""
        gate = asyncio.Semaphore(4)

        async def measure(sandbox: HermesSandbox) -> tuple[str, int]:
            async with gate:
                try:
                    code, output = await self._exec(sandbox, ["du", "-sb", "/workspace"])
                except self._backend_errors:
                    return sandbox.volume, -1
            match = re.match(rb"\s*(\d+)", output)
            return sandbox.volume, int(match.group(1)) if code == 0 and match else -1

        running = [sandbox for sandbox in self._sandboxes.values() if sandbox.state == "running"]
        return dict(await asyncio.gather(*(measure(sandbox) for sandbox in running)))

    async def _reap_leftovers(self, now: float) -> None:
        """Delete Pods that ended, never started, or belong to no known session; and
        forget a sandbox whose Pod is gone (evicted, or its node lost)."""
        core = await self._api()
        listed = await core.list_namespaced_pod(self.namespace, label_selector=self._selector)
        grace = self._settings.hermes_orphan_grace_seconds
        present: set[str] = set()
        for pod in listed.items:
            identity = self._pod_identity(pod)
            if identity is None or self._pod_identities.get(identity.name, identity) != identity:
                continue
            self._pod_identities[identity.name] = identity
            labels = pod.metadata.labels or {}
            name = pod.metadata.name
            if pod.metadata.deletion_timestamp is not None:
                # A Pod whose node is lost stays Terminating for good.
                since = pod.metadata.deletion_timestamp
                if isinstance(since, datetime) and (
                    datetime.now(UTC) - since
                ).total_seconds() > self._settings.hermes_stop_timeout_seconds + self._remove_wait_seconds + 30:
                    logger.warning("Hermes sandbox Pod %s has been terminating for long; deleting it by force", name)
                    await self._force_delete(core, identity)
                continue
            session_id = labels.get(SESSION_LABEL, "")
            age = now - float(labels.get(CREATED_LABEL, "0") or 0)
            if pod.status.phase in _TERMINAL_PHASES:
                await self._remove(name)
            elif session_id not in self._sandboxes and age >= grace:
                # Adoption just ran, so an unknown Pod is not one a session owns.
                await self._remove(name)
            else:
                present.add(session_id)
        for session_id, sandbox in list(self._sandboxes.items()):
            if session_id not in present and now - sandbox.created_at >= grace:
                async with self._lock(session_id):
                    if self._sandboxes.get(session_id) is sandbox:
                        logger.warning("Hermes sandbox %s lost its Pod", session_id)
                        self._sandboxes.pop(session_id, None)
