"""Stateless Kubernetes implementation of the code-skill sandbox runtime."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable, Literal, TypeVar, cast

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio import config as k8s_config
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.config.config_exception import ConfigException

from src.config import Settings, settings
from src.sandbox_runtime.base import (
    BundleExtractionError,
    SandboxSession,
    SessionNotFoundError,
    _bundle_to_tar,
)
from src.sandbox_runtime.kubernetes_exec import ExecResult, KubernetesExecClient

logger = logging.getLogger(__name__)

RELEASE_LABEL = "app.kubernetes.io/instance"
COMPONENT_LABEL = "aurum-ai.component"
COMPONENT_VALUE = "skill-sandbox"
SESSION_LABEL = "aurum-ai.session"
NETWORK_POLICY_LABEL = "aurum-ai.network-policy"
SESSION_ANNOTATION = "aurum-ai/session-id"
CREATED_AT_ANNOTATION = "aurum-ai/created-at"
LAST_ACTIVE_AT_ANNOTATION = "aurum-ai/last-active-at"
NETWORK_POLICY_ANNOTATION = "aurum-ai/network-policy"
_SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
_TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
_TERMINAL_WAITING_REASONS = {
    "ImagePullBackOff",
    "ErrImagePull",
    "InvalidImageName",
    "CreateContainerConfigError",
    "CreateContainerError",
    "CrashLoopBackOff",
}
T = TypeVar("T")


class KubernetesSandboxError(RuntimeError):
    """A safe, diagnostic Kubernetes sandbox runtime failure."""


class KubernetesSandboxPermanentError(KubernetesSandboxError):
    """A Kubernetes sandbox failure that retrying cannot repair."""


class KubernetesSandboxReadinessError(KubernetesSandboxError):
    """A sandbox Pod did not become usable."""


def _as_utc_timestamp(value: object) -> float | None:
    if isinstance(value, datetime):
        timestamp = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return timestamp.astimezone(UTC).timestamp()
    if isinstance(value, str):
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return timestamp.astimezone(UTC).timestamp()
    return None


def _utc_text(value: datetime) -> str:
    timestamp = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC).isoformat()


class KubernetesSandboxRuntime:
    """Create, find and reap sandbox Pods without process-local session state."""

    kind: Literal["kubernetes"] = "kubernetes"

    def __init__(
        self,
        runtime_settings: Settings | None = None,
        *,
        core_v1_api_factory: Callable[[], Any] | None = None,
        authorization_v1_api_factory: Callable[[], Any] | None = None,
        exec_client: Any | None = None,
        api_client: Any | None = None,
        ready_timeout_seconds: float | None = None,
        poll_interval_seconds: float = 0.25,
        retry_attempts: int | None = None,
        now: Callable[[], datetime] | None = None,
        session_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._settings = runtime_settings or settings
        self._core_factory = core_v1_api_factory
        self._authorization_factory = authorization_v1_api_factory
        self._core: Any | None = None
        self._authorization: Any | None = None
        self._api_client: Any | None = api_client
        self._exec = exec_client or KubernetesExecClient()
        self._ready_timeout = (
            float(ready_timeout_seconds)
            if ready_timeout_seconds is not None
            else float(self._settings.kubernetes_ready_timeout_seconds)
        )
        self._poll_interval = poll_interval_seconds
        self._retry_attempts = retry_attempts or self._settings.kubernetes_api_retry_attempts
        self._now = now or (lambda: datetime.now(UTC))
        self._session_id_factory = session_id_factory or (lambda: uuid.uuid4().hex)

    async def _clients(self) -> tuple[Any, Any]:
        if self._core is not None and self._authorization is not None:
            return self._core, self._authorization
        if self._core_factory is not None:
            if self._authorization_factory is None:
                raise KubernetesSandboxPermanentError(
                    "Kubernetes authorization API factory is unavailable"
                )
            self._core = self._core_factory()
            self._authorization = self._authorization_factory()
            return self._core, self._authorization
        try:
            if os.getenv("KUBERNETES_SERVICE_HOST") and os.getenv(
                "KUBERNETES_SERVICE_PORT"
            ):
                k8s_config.load_incluster_config()  # type: ignore[no-untyped-call]
            else:
                await k8s_config.load_kube_config()
        except ConfigException as exc:
            logger.warning("Kubernetes sandbox configuration loading failed", exc_info=exc)
            raise KubernetesSandboxPermanentError(
                "Kubernetes configuration is unavailable"
            ) from None
        self._api_client = k8s_client.ApiClient()
        self._core = k8s_client.CoreV1Api(self._api_client)
        self._authorization = k8s_client.AuthorizationV1Api(self._api_client)
        return self._core, self._authorization

    def _pod_name(self, session_id: str) -> str:
        if _SESSION_ID.fullmatch(session_id) is None:
            raise SessionNotFoundError(f"Unknown or expired session: {session_id}")
        return f"skill-sandbox-{session_id[:32]}"

    def _labels(self, session_id: str, network_policy: str) -> dict[str, str]:
        return {
            RELEASE_LABEL: self._settings.kubernetes_release_name,
            COMPONENT_LABEL: COMPONENT_VALUE,
            SESSION_LABEL: session_id,
            NETWORK_POLICY_LABEL: network_policy,
        }

    def build_pod(self, session_id: str, network_policy: str) -> k8s_client.V1Pod:
        if network_policy not in {"none", "egress"}:
            raise KubernetesSandboxPermanentError("Unsupported sandbox network policy")
        name = self._pod_name(session_id)
        now = _utc_text(self._now())
        security = k8s_client.V1SecurityContext(
            allow_privilege_escalation=False,
            read_only_root_filesystem=True,
            capabilities=k8s_client.V1Capabilities(drop=["ALL"]),
        )
        container = k8s_client.V1Container(
            name="sandbox",
            image=self._settings.sandbox_image,
            image_pull_policy=self._settings.kubernetes_image_pull_policy,
            command=["/bin/sh", "-c"],
            args=["mkdir -p /tmp/home && exec sleep infinity"],
            env=[k8s_client.V1EnvVar(name="HOME", value="/tmp/home")],
            security_context=security,
            volume_mounts=[
                k8s_client.V1VolumeMount(name="tmp", mount_path="/tmp"),
                k8s_client.V1VolumeMount(name="skill", mount_path="/skill"),
            ],
            resources=k8s_client.V1ResourceRequirements(
                requests={
                    "cpu": self._settings.kubernetes_cpu_request,
                    "memory": self._settings.kubernetes_memory_request,
                },
                limits={
                    "cpu": self._settings.kubernetes_cpu_limit,
                    "memory": self._settings.kubernetes_memory_limit,
                },
            ),
        )
        return k8s_client.V1Pod(
            metadata=k8s_client.V1ObjectMeta(
                name=name,
                namespace=self._settings.kubernetes_namespace,
                labels=self._labels(session_id, network_policy),
                annotations={
                    SESSION_ANNOTATION: session_id,
                    CREATED_AT_ANNOTATION: now,
                    LAST_ACTIVE_AT_ANNOTATION: now,
                    NETWORK_POLICY_ANNOTATION: network_policy,
                },
            ),
            spec=k8s_client.V1PodSpec(
                restart_policy="Never",
                automount_service_account_token=False,
                enable_service_links=False,
                security_context=k8s_client.V1PodSecurityContext(
                    run_as_non_root=True,
                    run_as_user=10001,
                    run_as_group=10001,
                    fs_group=10001,
                    fs_group_change_policy="OnRootMismatch",
                    seccomp_profile=k8s_client.V1SeccompProfile(type="RuntimeDefault"),
                ),
                containers=[container],
                volumes=[
                    k8s_client.V1Volume(
                        name="tmp", empty_dir=k8s_client.V1EmptyDirVolumeSource()
                    ),
                    k8s_client.V1Volume(
                        name="skill",
                        empty_dir=k8s_client.V1EmptyDirVolumeSource(
                            medium="Memory",
                            size_limit=f"{self._settings.sandbox_skill_tmpfs_mb}Mi",
                        ),
                    ),
                ],
                image_pull_secrets=[
                    k8s_client.V1LocalObjectReference(name=name)
                    for name in self._settings.kubernetes_image_pull_secrets
                ],
            ),
        )

    async def _api_call(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        allow_conflict: bool = False,
        raise_not_found: bool = False,
        missing_is_none: bool = False,
    ) -> T | None:
        for attempt in range(1, self._retry_attempts + 1):
            try:
                return await operation()
            except ApiException as exc:
                if allow_conflict and exc.status == 409:
                    raise
                if raise_not_found and exc.status == 404:
                    raise
                if missing_is_none and exc.status == 404:
                    return None
                if exc.status not in _TRANSIENT_STATUSES or attempt == self._retry_attempts:
                    self._raise_api_error(exc)
                await asyncio.sleep(min(0.25 * 2 ** (attempt - 1), 2.0))
        raise AssertionError("unreachable")

    @staticmethod
    def _raise_api_error(exc: ApiException) -> None:
        logger.warning("Kubernetes sandbox API operation failed", exc_info=exc)
        if 400 <= exc.status < 500 and exc.status != 429:
            raise KubernetesSandboxPermanentError(
                f"Kubernetes API request failed (HTTP {exc.status})"
            ) from None
        raise KubernetesSandboxError(
            f"Kubernetes API request failed (HTTP {exc.status})"
        ) from None

    def _owns_pod(self, pod: k8s_client.V1Pod, session_id: str) -> bool:
        labels = pod.metadata.labels or {}
        annotations = pod.metadata.annotations or {}
        return (
            labels.get(RELEASE_LABEL) == self._settings.kubernetes_release_name
            and labels.get(COMPONENT_LABEL) == COMPONENT_VALUE
            and labels.get(SESSION_LABEL) == session_id
            and annotations.get(SESSION_ANNOTATION) == session_id
        )

    async def _read_session_pod(self, session_id: str) -> k8s_client.V1Pod:
        name = self._pod_name(session_id)
        core, _ = await self._clients()
        result = await self._api_call(
            lambda: core.read_namespaced_pod(
                name=name, namespace=self._settings.kubernetes_namespace
            ),
            missing_is_none=True,
        )
        pod = cast(k8s_client.V1Pod | None, result)
        if pod is None or not self._owns_pod(pod, session_id):
            raise SessionNotFoundError(f"Unknown or expired session: {session_id}")
        return pod

    @staticmethod
    def _is_ready(pod: k8s_client.V1Pod) -> bool:
        return pod.status.phase == "Running" and any(
            condition.type == "Ready" and condition.status == "True"
            for condition in pod.status.conditions or []
        )

    @staticmethod
    def _terminal_diagnostic(pod: k8s_client.V1Pod) -> str | None:
        if pod.status.phase == "Failed":
            return "Failed"
        for container in pod.status.container_statuses or []:
            state = container.state
            if state is None:
                continue
            if (
                state.waiting is not None
                and state.waiting.reason in _TERMINAL_WAITING_REASONS
            ):
                return state.waiting.reason
            if state.terminated is not None and state.terminated.reason:
                return state.terminated.reason
        return None

    async def _wait_for_ready(self, session_id: str) -> k8s_client.V1Pod:
        last_phase = "Unknown"
        try:
            async with asyncio.timeout(self._ready_timeout):
                while True:
                    pod = await self._read_session_pod(session_id)
                    last_phase = pod.status.phase or last_phase
                    diagnostic = self._terminal_diagnostic(pod)
                    if diagnostic is not None:
                        raise KubernetesSandboxReadinessError(
                            f"Kubernetes sandbox readiness failed: {diagnostic}"
                        )
                    if self._is_ready(pod):
                        return pod
                    await asyncio.sleep(self._poll_interval)
        except TimeoutError:
            raise KubernetesSandboxReadinessError(
                "timed out waiting for Kubernetes sandbox readiness "
                f"(phase {last_phase})"
            ) from None

    def _session(self, pod: k8s_client.V1Pod, skill_md: str = "") -> SandboxSession:
        annotations = pod.metadata.annotations or {}
        session_id = annotations.get(SESSION_ANNOTATION, "")
        created = _as_utc_timestamp(annotations.get(CREATED_AT_ANNOTATION))
        last_active = _as_utc_timestamp(annotations.get(LAST_ACTIVE_AT_ANNOTATION))
        return SandboxSession(
            session_id=session_id,
            resource_id=pod.metadata.name or self._pod_name(session_id),
            resource_uid=pod.metadata.uid or "",
            resource_name=pod.metadata.name or self._pod_name(session_id),
            skill_md=skill_md,
            created_at=created or 0.0,
            last_active=last_active or created or 0.0,
        )

    async def create_session(
        self,
        bundle_bytes: bytes,
        skill_md: str,
        network_policy: str = "none",
    ) -> SandboxSession:
        tar_bytes = _bundle_to_tar(bundle_bytes, self._settings)
        session_id = self._session_id_factory()
        name = self._pod_name(session_id)
        manifest = self.build_pod(session_id, network_policy)
        core, _ = await self._clients()
        created_uid: str | None = None
        try:
            try:
                created = cast(
                    k8s_client.V1Pod,
                    await self._api_call(
                        lambda: core.create_namespaced_pod(
                        namespace=self._settings.kubernetes_namespace, body=manifest
                        ),
                        allow_conflict=True,
                    ),
                )
                created_uid = created.metadata.uid
            except ApiException as exc:
                if exc.status != 409:
                    self._raise_api_error(exc)
                raise KubernetesSandboxPermanentError(
                    "Kubernetes sandbox create conflict"
                ) from None
            pod = await self._wait_for_ready(session_id)
            result: ExecResult = await self._exec.exec(
                namespace=self._settings.kubernetes_namespace,
                pod=name,
                container="sandbox",
                command=["tar", "-x", "-C", "/skill", "-f", "-"],
                stdin=tar_bytes,
                timeout_seconds=float(self._settings.exec_timeout_seconds),
                max_bytes=self._settings.exec_output_max_bytes,
            )
            if result.timed_out or result.exit_code != 0:
                raise BundleExtractionError("Kubernetes bundle bootstrap failed")
            return self._session(pod, skill_md)
        except BaseException:
            if created_uid is not None:
                try:
                    await self._delete_pod(name, created_uid)
                except Exception:
                    logger.warning(
                        "Kubernetes sandbox bootstrap cleanup failed", exc_info=True
                    )
            raise

    async def exec_command(
        self,
        session_id: str,
        command: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        pod = await self._read_session_pod(session_id)
        if pod.metadata.deletion_timestamp is not None or not self._is_ready(pod):
            raise SessionNotFoundError(f"Unknown or expired session: {session_id}")
        exec_timeout = timeout or float(self._settings.exec_timeout_seconds)
        try:
            result: ExecResult = await self._exec.exec(
                namespace=self._settings.kubernetes_namespace,
                pod=pod.metadata.name,
                container="sandbox",
                command=[
                    "timeout",
                    "-k",
                    "5",
                    str(int(exec_timeout)),
                    "/bin/sh",
                    "-c",
                    command,
                ],
                timeout_seconds=exec_timeout + 10,
                max_bytes=self._settings.exec_output_max_bytes,
            )
        except ApiException as exc:
            if exc.status == 404:
                raise SessionNotFoundError(
                    f"Unknown or expired session: {session_id}"
                ) from None
            raise
        except Exception:
            try:
                current = await self._read_session_pod(session_id)
            except SessionNotFoundError:
                raise SessionNotFoundError(
                    f"Unknown or expired session: {session_id}"
                ) from None
            if (
                current.metadata.deletion_timestamp is not None
                or not self._is_ready(current)
            ):
                raise SessionNotFoundError(
                    f"Unknown or expired session: {session_id}"
                ) from None
            raise
        timed_out = result.timed_out or result.exit_code == 124
        await self._patch_last_active(pod)
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
            "timed_out": timed_out,
        }

    async def _patch_last_active(self, pod: k8s_client.V1Pod) -> None:
        core, _ = await self._clients()
        completed_at = self._now()
        completed_timestamp = completed_at.timestamp()
        completed_text = _utc_text(completed_at)
        current = pod
        for _ in range(self._retry_attempts):
            body = {
                "metadata": {
                    "resourceVersion": current.metadata.resource_version,
                    "annotations": {LAST_ACTIVE_AT_ANNOTATION: completed_text},
                }
            }
            try:
                await self._api_call(
                    lambda: core.patch_namespaced_pod(
                        name=current.metadata.name,
                        namespace=self._settings.kubernetes_namespace,
                        body=body,
                    ),
                    allow_conflict=True,
                    raise_not_found=True,
                )
                return
            except ApiException as exc:
                if exc.status == 404:
                    session_id = (current.metadata.annotations or {}).get(
                        SESSION_ANNOTATION, ""
                    )
                    raise SessionNotFoundError(
                        f"Unknown or expired session: {session_id}"
                    ) from None
                if exc.status != 409:
                    self._raise_api_error(exc)
                try:
                    current = await self._read_session_pod(
                        (current.metadata.annotations or {}).get(SESSION_ANNOTATION, "")
                    )
                except SessionNotFoundError:
                    raise
                except KubernetesSandboxError:
                    logger.warning(
                        "Kubernetes sandbox last-active reconciliation failed",
                        exc_info=True,
                    )
                    return
                if (
                    current.metadata.deletion_timestamp is not None
                    or not self._is_ready(current)
                ):
                    session_id = (current.metadata.annotations or {}).get(
                        SESSION_ANNOTATION, ""
                    )
                    raise SessionNotFoundError(
                        f"Unknown or expired session: {session_id}"
                    )
                current_last_active = _as_utc_timestamp(
                    (current.metadata.annotations or {}).get(LAST_ACTIVE_AT_ANNOTATION)
                )
                if (
                    current_last_active is not None
                    and current_last_active >= completed_timestamp
                ):
                    return
            except KubernetesSandboxError:
                logger.warning("Kubernetes sandbox last-active patch failed", exc_info=True)
                return
        logger.warning("Kubernetes sandbox last-active patch conflict persisted")

    async def _delete_pod(self, name: str, uid: str) -> None:
        core, _ = await self._clients()
        await self._api_call(
            lambda: core.delete_namespaced_pod(
                name=name,
                namespace=self._settings.kubernetes_namespace,
                body=k8s_client.V1DeleteOptions(
                    preconditions=k8s_client.V1Preconditions(uid=uid)
                ),
            ),
            missing_is_none=True,
        )

    async def close_session(self, session_id: str) -> None:
        try:
            pod = await self._read_session_pod(session_id)
        except SessionNotFoundError:
            return
        await self._delete_pod(pod.metadata.name, pod.metadata.uid or "")

    async def _list_owned_pods(self) -> list[k8s_client.V1Pod]:
        core, _ = await self._clients()
        selector = (
            f"{RELEASE_LABEL}={self._settings.kubernetes_release_name},"
            f"{COMPONENT_LABEL}={COMPONENT_VALUE}"
        )
        result = await self._api_call(
            lambda: core.list_namespaced_pod(
                namespace=self._settings.kubernetes_namespace,
                label_selector=selector,
            )
        )
        return list(cast(k8s_client.V1PodList, result).items)

    async def reap_expired(self) -> int:
        now = self._now().timestamp()
        expired: list[tuple[str, str]] = []
        for pod in await self._list_owned_pods():
            annotations = pod.metadata.annotations or {}
            created = _as_utc_timestamp(annotations.get(CREATED_AT_ANNOTATION))
            last_active = _as_utc_timestamp(annotations.get(LAST_ACTIVE_AT_ANNOTATION))
            session_id = annotations.get(SESSION_ANNOTATION)
            if (
                created is None
                or last_active is None
                or session_id is None
                or not self._owns_pod(pod, session_id)
            ):
                continue
            if (
                now - created >= self._settings.session_ttl_seconds
                or now - last_active >= self._settings.session_idle_ttl_seconds
            ):
                expired.append((pod.metadata.name, pod.metadata.uid or ""))
        for name, uid in expired:
            logger.info("Reaping expired Kubernetes skill sandbox %s", name)
            await self._delete_pod(name, uid)
        return len(expired)

    async def reap_orphans(self) -> int:
        return await self.reap_expired()

    async def run_reaper(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._settings.reap_interval_seconds)
                await self.reap_expired()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Kubernetes sandbox reaper iteration failed", exc_info=True)

    async def ready(self) -> None:
        core, authorization = await self._clients()
        await self._api_call(
            lambda: core.list_namespaced_pod(
                namespace=self._settings.kubernetes_namespace, limit=1
            )
        )
        checks = [
            ("pods", None, verb)
            for verb in ("get", "list", "create", "delete", "patch")
        ] + [("pods", "exec", verb) for verb in ("create", "get")]
        for resource, subresource, verb in checks:
            review = k8s_client.V1SelfSubjectAccessReview(
                spec=k8s_client.V1SelfSubjectAccessReviewSpec(
                    resource_attributes=k8s_client.V1ResourceAttributes(
                        namespace=self._settings.kubernetes_namespace,
                        resource=resource,
                        subresource=subresource,
                        verb=verb,
                    )
                )
            )

            async def submit_review(
                current: k8s_client.V1SelfSubjectAccessReview = review,
            ) -> Any:
                return await authorization.create_self_subject_access_review(current)

            result = await self._api_call(submit_review)
            if not getattr(getattr(result, "status", None), "allowed", False):
                raise KubernetesSandboxPermanentError(
                    "Kubernetes sandbox RBAC permission is missing"
                )

    async def shutdown(self) -> None:
        self._core = None
        self._authorization = None
        if self._api_client is not None:
            await self._api_client.close()
            self._api_client = None
