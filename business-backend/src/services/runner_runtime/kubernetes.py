"""Kubernetes implementation of the per-conversation runner runtime."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict
from datetime import datetime
from typing import Any, Awaitable, Callable, Literal, Mapping, TypeVar, cast

import httpx
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio import config as k8s_config
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.config.config_exception import ConfigException

from src.core.config import settings
from src.services.runner_runtime.base import (
    RunnerBinding,
    RunnerPermanentError,
    RunnerReadinessError,
    RunnerResource,
    RunnerRuntimeError,
)

logger = logging.getLogger(__name__)

RUNNER_LABEL = "aurum-ai.component"
RUNNER_LABEL_VALUE = "conversation-runner"
RELEASE_LABEL = "app.kubernetes.io/instance"
CONVERSATION_LABEL = "aurum-ai.conversation"
_SECRET_ENV = {"REDIS_URL", "LANGFUSE_SECRET_KEY"}
_BINDING_ENV = {
    "SESSION_TOKEN",
    "USER_ID",
    "ORG_ID",
    "CONVERSATION_ID",
    "TASK_QUEUE",
}
T = TypeVar("T")


def _timestamp(value: object) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


class KubernetesRunnerRuntime:
    """Create and supervise isolated conversation-runner Pods."""

    kind: Literal["kubernetes"] = "kubernetes"
    supports_warm_pool = False

    def __init__(
        self,
        *,
        core_v1_api_factory: Callable[[], Any] | None = None,
        authorization_v1_api_factory: Callable[[], Any] | None = None,
        http_client_factory: Callable[[], Any] | None = None,
        ready_timeout_seconds: float | None = None,
        provision_timeout_seconds: float | None = None,
        api_call_timeout_seconds: float | None = None,
        poll_interval_seconds: float = 0.25,
        retry_attempts: int | None = None,
    ) -> None:
        self._core_factory = core_v1_api_factory
        self._authorization_factory = authorization_v1_api_factory
        self._http_factory = http_client_factory
        self._core: Any | None = None
        self._authorization: Any | None = None
        self._http: Any | None = None
        self._api_client: k8s_client.ApiClient | None = None
        self._ready_timeout = (
            float(ready_timeout_seconds)
            if ready_timeout_seconds is not None
            else float(settings.KUBERNETES_RUNNER_READY_TIMEOUT_SECONDS)
        )
        self._poll_interval = poll_interval_seconds
        self._retry_attempts = retry_attempts or settings.KUBERNETES_API_RETRY_ATTEMPTS
        self._deletion_timeout = min(self._ready_timeout, 10.0)
        self._api_call_timeout = (
            float(api_call_timeout_seconds)
            if api_call_timeout_seconds is not None
            else min(self._ready_timeout, 3.0)
        )
        self._provision_timeout = (
            float(provision_timeout_seconds)
            if provision_timeout_seconds is not None
            else self._ready_timeout + 10.0
        )

    async def _clients(self) -> tuple[Any, Any, Any]:
        if self._core is not None and self._authorization is not None and self._http is not None:
            return self._core, self._authorization, self._http
        if self._core_factory is not None:
            self._core = self._core_factory()
            self._authorization = cast(Callable[[], Any], self._authorization_factory)()
            self._http = cast(Callable[[], Any], self._http_factory)()
            return self._core, self._authorization, self._http
        if os.getenv("KUBERNETES_SERVICE_HOST") and os.getenv("KUBERNETES_SERVICE_PORT"):
            try:
                k8s_config.load_incluster_config()  # type: ignore[no-untyped-call]
            except ConfigException as exc:
                logger.warning("Kubernetes in-cluster configuration failed", exc_info=exc)
                raise RunnerPermanentError(
                    "Kubernetes in-cluster configuration is unavailable"
                ) from None
        else:
            try:
                await k8s_config.load_kube_config()
            except ConfigException as exc:
                logger.warning("Kubernetes kubeconfig loading failed", exc_info=exc)
                raise RunnerPermanentError("Kubernetes kubeconfig is unavailable") from None
        self._api_client = k8s_client.ApiClient()
        self._core = k8s_client.CoreV1Api(self._api_client)
        self._authorization = k8s_client.AuthorizationV1Api(self._api_client)
        self._http = httpx.AsyncClient(timeout=10.0)
        return self._core, self._authorization, self._http

    def _labels(self, conversation_id: str) -> dict[str, str]:
        return {
            RELEASE_LABEL: settings.KUBERNETES_RELEASE_NAME,
            RUNNER_LABEL: RUNNER_LABEL_VALUE,
            CONVERSATION_LABEL: conversation_id,
        }

    def build_pod(self, name: str, static_env: Mapping[str, str]) -> k8s_client.V1Pod:
        env: list[k8s_client.V1EnvVar] = []
        for key, value in static_env.items():
            if key in _BINDING_ENV:
                continue
            if key in _SECRET_ENV:
                env.append(
                    k8s_client.V1EnvVar(
                        name=key,
                        value_from=k8s_client.V1EnvVarSource(
                            secret_key_ref=k8s_client.V1SecretKeySelector(
                                name=settings.KUBERNETES_RUNNER_SECRET_NAME, key=key
                            )
                        ),
                    )
                )
            else:
                env.append(k8s_client.V1EnvVar(name=key, value=value))
        security = k8s_client.V1SecurityContext(
            allow_privilege_escalation=False,
            read_only_root_filesystem=True,
            capabilities=k8s_client.V1Capabilities(drop=["ALL"]),
        )
        probe = k8s_client.V1Probe(
            http_get=k8s_client.V1HTTPGetAction(path="/health", port=8080),
            initial_delay_seconds=1,
            period_seconds=2,
        )
        container = k8s_client.V1Container(
            name="runner",
            image=settings.RUNNER_IMAGE,
            image_pull_policy=settings.KUBERNETES_RUNNER_IMAGE_PULL_POLICY,
            env=env,
            security_context=security,
            volume_mounts=[k8s_client.V1VolumeMount(name="tmp", mount_path="/tmp")],
            resources=k8s_client.V1ResourceRequirements(
                requests={
                    "cpu": settings.KUBERNETES_RUNNER_CPU_REQUEST,
                    "memory": settings.KUBERNETES_RUNNER_MEMORY_REQUEST,
                },
                limits={
                    "cpu": settings.KUBERNETES_RUNNER_CPU_LIMIT,
                    "memory": settings.KUBERNETES_RUNNER_MEMORY_LIMIT,
                },
            ),
            readiness_probe=probe,
            liveness_probe=probe,
        )
        return k8s_client.V1Pod(
            metadata=k8s_client.V1ObjectMeta(
                name=name,
                namespace=settings.KUBERNETES_RUNNER_NAMESPACE,
                labels=self._labels(name.removeprefix("runner-")),
            ),
            spec=k8s_client.V1PodSpec(
                restart_policy="Never",
                automount_service_account_token=False,
                security_context=k8s_client.V1PodSecurityContext(
                    run_as_non_root=True,
                    run_as_user=10001,
                    run_as_group=10001,
                    seccomp_profile=k8s_client.V1SeccompProfile(type="RuntimeDefault"),
                ),
                containers=[container],
                volumes=[k8s_client.V1Volume(name="tmp", empty_dir=k8s_client.V1EmptyDirVolumeSource())],
                image_pull_secrets=[
                    k8s_client.V1LocalObjectReference(name=secret)
                    for secret in settings.KUBERNETES_RUNNER_IMAGE_PULL_SECRETS
                ],
            ),
        )

    async def _api_call(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        allow_conflict: bool = False,
        missing_is_none: bool = False,
    ) -> T | None:
        for attempt in range(1, self._retry_attempts + 1):
            try:
                async with asyncio.timeout(self._api_call_timeout):
                    return await operation()
            except ApiException as exc:
                if allow_conflict and exc.status == 409:
                    raise
                if missing_is_none and exc.status == 404:
                    return None
                if exc.status not in {429, 500, 502, 503, 504} or attempt == self._retry_attempts:
                    self._raise_api_error(exc)
                await asyncio.sleep(min(0.25 * 2 ** (attempt - 1), 2.0))
            except Exception as exc:
                logger.warning("Kubernetes runner API transport failed", exc_info=exc)
                if attempt == self._retry_attempts:
                    raise RunnerRuntimeError("Kubernetes API transport failed") from None
                await asyncio.sleep(min(0.25 * 2 ** (attempt - 1), 2.0))
        raise AssertionError("unreachable")

    def _raise_api_error(self, exc: ApiException) -> None:
        logger.warning("Kubernetes runner API operation failed", exc_info=exc)
        if 400 <= exc.status < 500 and exc.status != 429:
            raise RunnerPermanentError(f"Kubernetes API request failed (HTTP {exc.status})") from None
        raise RunnerRuntimeError(f"Kubernetes API request failed (HTTP {exc.status})") from None

    @staticmethod
    def _resource(pod: k8s_client.V1Pod) -> RunnerResource:
        metadata = pod.metadata
        status = pod.status
        conditions = status.conditions or []
        ready = status.phase == "Running" and bool(status.pod_ip) and any(
            condition.type == "Ready" and condition.status == "True" for condition in conditions
        )
        return RunnerResource(
            resource_id=metadata.name or "",
            name=metadata.name or "",
            uid=metadata.uid or "",
            ip_address=status.pod_ip,
            state=(status.phase or "unknown").lower(),
            ready=ready,
            created_at=_timestamp(metadata.creation_timestamp),
            labels=dict(metadata.labels or {}),
        )

    def _has_expected_labels(self, pod: k8s_client.V1Pod, name: str) -> bool:
        return (pod.metadata.labels or {}) == self._labels(name.removeprefix("runner-"))

    async def create_bound(
        self, name: str, static_env: Mapping[str, str], binding: RunnerBinding
    ) -> RunnerResource:
        core, _, http = await self._clients()
        manifest = self.build_pod(name, static_env)
        created_uid: str | None = None
        try:
            try:
                async with asyncio.timeout(self._provision_timeout):
                    for _ in range(self._retry_attempts):
                        try:
                            created = await self._api_call(
                                lambda: core.create_namespaced_pod(
                                    namespace=settings.KUBERNETES_RUNNER_NAMESPACE,
                                    body=manifest,
                                ),
                                allow_conflict=True,
                            )
                            created_uid = cast(k8s_client.V1Pod, created).metadata.uid
                            break
                        except ApiException as exc:
                            if exc.status != 409:
                                self._raise_api_error(exc)
                            existing = await self._read_pod(name)
                            if existing is None:
                                continue
                            if not self._has_expected_labels(existing, name):
                                raise RunnerPermanentError(
                                    "Kubernetes runner create conflict"
                                )
                            if existing.metadata.deletion_timestamp is not None:
                                await self._wait_for_deletion(
                                    name, existing.metadata.uid or ""
                                )
                                continue
                            # A previous backend may have created and bound this owned
                            # Pod before crashing ahead of Redis bookkeeping. Adopt it;
                            # /bind is idempotent for the same immutable identity.
                            created_uid = existing.metadata.uid
                            break
                    if created_uid is None:
                        raise RunnerRuntimeError(
                            "Kubernetes runner create conflict persisted"
                        )
                    resource = await self._wait_for_ready(name)
                    try:
                        response = await http.post(
                            f"http://{resource.ip_address}:8080/bind",
                            json=asdict(binding),
                        )
                        response.raise_for_status()
                    except Exception as exc:
                        logger.warning(
                            "Kubernetes runner bind request failed", exc_info=exc
                        )
                        raise RunnerRuntimeError(
                            "Kubernetes runner bind request failed"
                        ) from None
                    return resource
            except TimeoutError:
                raise RunnerReadinessError(
                    "timed out provisioning Kubernetes runner"
                ) from None
        except asyncio.CancelledError:
            if created_uid is not None:
                try:
                    await asyncio.shield(self.delete(name, created_uid))
                except RunnerRuntimeError:
                    logger.warning(
                        "Kubernetes runner cancelled-bootstrap cleanup failed",
                        exc_info=True,
                    )
            raise
        except RunnerRuntimeError:
            if created_uid is not None:
                try:
                    await self.delete(name, created_uid)
                except RunnerRuntimeError:
                    logger.warning(
                        "Kubernetes runner bootstrap cleanup failed", exc_info=True
                    )
            raise

    async def create_unbound(
        self, name: str, static_env: Mapping[str, str]
    ) -> RunnerResource:
        del name, static_env
        raise RunnerPermanentError("Kubernetes runner warm pool is disabled")

    async def bind(
        self, resource: RunnerResource, new_name: str, binding: RunnerBinding
    ) -> RunnerResource:
        del resource, new_name, binding
        raise RunnerPermanentError("Kubernetes runner warm pool is disabled")

    async def pool_is_unbound(self, resource: RunnerResource) -> bool:
        del resource
        return False

    async def _read_pod(self, name: str) -> k8s_client.V1Pod | None:
        core, _, _ = await self._clients()
        result = await self._api_call(
            lambda: core.read_namespaced_pod(
                name=name, namespace=settings.KUBERNETES_RUNNER_NAMESPACE
            ),
            missing_is_none=True,
        )
        return cast(k8s_client.V1Pod | None, result)

    async def get(self, resource_id: str) -> RunnerResource | None:
        pod = await self._read_pod(resource_id)
        return self._resource(pod) if pod is not None else None

    async def refresh_token(self, resource: RunnerResource, token: str) -> bool:
        current = await self.get(resource.resource_id)
        if current is None or not current.ready or current.ip_address is None:
            return False
        _, _, http = await self._clients()
        try:
            response = await http.post(
                f"http://{current.ip_address}:8080/refresh-token", json={"session_token": token}
            )
            response.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("Kubernetes runner token refresh failed", exc_info=exc)
            return False

    async def _wait_for_ready(self, name: str) -> RunnerResource:
        last_phase = "Unknown"
        try:
            async with asyncio.timeout(self._ready_timeout):
                while True:
                    pod = await self._read_pod(name)
                    if pod is None:
                        raise RunnerReadinessError(
                            "Kubernetes runner Pod disappeared before readiness"
                        )
                    resource = self._resource(pod)
                    last_phase = pod.status.phase or last_phase
                    diagnostic = self._terminal_diagnostic(pod)
                    if diagnostic is not None:
                        raise RunnerReadinessError(
                            f"Kubernetes runner readiness failed: {diagnostic}"
                        )
                    if resource.ready:
                        return resource
                    await asyncio.sleep(self._poll_interval)
        except TimeoutError:
            raise RunnerReadinessError(
                f"timed out waiting for Kubernetes runner readiness (phase {last_phase})"
            ) from None

    async def _wait_for_deletion(self, name: str, uid: str) -> None:
        try:
            async with asyncio.timeout(self._deletion_timeout):
                while True:
                    pod = await self._read_pod(name)
                    if pod is None:
                        return
                    if uid and (pod.metadata.uid or "") != uid:
                        return
                    await asyncio.sleep(self._poll_interval)
        except TimeoutError:
            raise RunnerRuntimeError(
                "timed out waiting for Kubernetes runner deletion"
            ) from None

    @staticmethod
    def _terminal_diagnostic(pod: k8s_client.V1Pod) -> str | None:
        status = pod.status
        if status.phase == "Failed":
            return "Failed"
        for container in status.container_statuses or []:
            state = container.state
            if state is None:
                continue
            if (
                state.waiting is not None
                and state.waiting.reason
                in {
                    "ImagePullBackOff",
                    "ErrImagePull",
                    "InvalidImageName",
                    "CreateContainerConfigError",
                    "CreateContainerError",
                    "CrashLoopBackOff",
                }
            ):
                return state.waiting.reason
            if state.terminated is not None and state.terminated.reason:
                return state.terminated.reason
        return None

    async def delete(self, resource_id: str, uid: str | None = None) -> None:
        core, _, _ = await self._clients()
        body = k8s_client.V1DeleteOptions(
            grace_period_seconds=0,
            preconditions=k8s_client.V1Preconditions(uid=uid) if uid else None,
        )
        result = await self._api_call(
            lambda: core.delete_namespaced_pod(
                name=resource_id,
                namespace=settings.KUBERNETES_RUNNER_NAMESPACE,
                body=body,
            ),
            missing_is_none=True,
        )
        if result is not None:
            await self._wait_for_deletion(resource_id, uid or "")

    async def list(self) -> list[RunnerResource]:
        core, _, _ = await self._clients()
        selector = f"{RELEASE_LABEL}={settings.KUBERNETES_RELEASE_NAME},{RUNNER_LABEL}={RUNNER_LABEL_VALUE}"
        result = await self._api_call(
            lambda: core.list_namespaced_pod(
                namespace=settings.KUBERNETES_RUNNER_NAMESPACE, label_selector=selector
            )
        )
        return [self._resource(pod) for pod in cast(k8s_client.V1PodList, result).items]

    async def ready(self) -> None:
        core, authorization, _ = await self._clients()
        await self._api_call(
            lambda: core.list_namespaced_pod(
                namespace=settings.KUBERNETES_RUNNER_NAMESPACE, limit=1
            )
        )
        for verb in ("get", "list", "watch", "create", "delete"):
            review = k8s_client.V1SelfSubjectAccessReview(
                spec=k8s_client.V1SelfSubjectAccessReviewSpec(
                    resource_attributes=k8s_client.V1ResourceAttributes(
                        namespace=settings.KUBERNETES_RUNNER_NAMESPACE,
                        resource="pods",
                        verb=verb,
                    )
                )
            )
            async def submit_review(
                current_review: k8s_client.V1SelfSubjectAccessReview = review,
            ) -> Any:
                return await authorization.create_self_subject_access_review(current_review)

            result = await self._api_call(submit_review)
            if not getattr(getattr(result, "status", None), "allowed", False):
                raise RunnerPermanentError("Kubernetes runner RBAC permission is missing")

    async def close(self) -> None:
        if self._http is not None and self._http_factory is None:
            await self._http.aclose()
        self._http = None
        self._core = None
        self._authorization = None
        if self._api_client is not None:
            await self._api_client.close()
            self._api_client = None
