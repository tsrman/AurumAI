from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client.exceptions import ApiException

from src.services.runner_runtime import (
    KubernetesRunnerRuntime,
    RunnerBinding,
    RunnerPermanentError,
    RunnerReadinessError,
    RunnerResource,
    RunnerRuntimeError,
)


STATIC_ENV = {
    "TEMPORAL_HOST": "temporal.example:7233",
    "TEMPORAL_NAMESPACE": "default",
    "REDIS_URL": "redis://user:secret@redis.example/0",
    "LLM_GATEWAY_URL": "http://backend:8000",
    "LANGFUSE_ENABLED": "True",
    "LANGFUSE_PUBLIC_KEY": "public-key",
    "LANGFUSE_SECRET_KEY": "langfuse-secret",
    "LANGFUSE_HOST": "http://langfuse.example",
}


def binding() -> RunnerBinding:
    return RunnerBinding(
        conversation_id="abc",
        user_id="user-secret",
        org_id="org-secret",
        session_token="token-secret",
        task_queue="session-abc",
    )


def pod(
    *,
    name: str = "runner-abc",
    uid: str = "uid-abc",
    phase: str = "Running",
    ip: str | None = "10.42.0.12",
    ready: bool = True,
    labels: dict[str, str] | None = None,
    waiting_reason: str | None = None,
    terminated_reason: str | None = None,
    status_reason: str | None = None,
    terminating: bool = False,
) -> k8s_client.V1Pod:
    state = None
    if waiting_reason is not None:
        state = k8s_client.V1ContainerState(
            waiting=k8s_client.V1ContainerStateWaiting(
                reason=waiting_reason, message="token-secret must never leak"
            )
        )
    if terminated_reason is not None:
        state = k8s_client.V1ContainerState(
            terminated=k8s_client.V1ContainerStateTerminated(
                exit_code=137,
                reason=terminated_reason,
                message="token-secret must never leak",
            )
        )
    statuses = (
        [
            k8s_client.V1ContainerStatus(
                name="runner",
                image="runner:test",
                image_id="docker://runner:test",
                container_id="containerd://runner-abc",
                ready=False,
                restart_count=0,
                state=state,
            )
        ]
        if state is not None
        else []
    )
    return k8s_client.V1Pod(
        metadata=k8s_client.V1ObjectMeta(
            name=name,
            namespace="aurum-ai-runners",
            uid=uid,
            labels=labels
            or {
                "app.kubernetes.io/instance": "aurum-ai",
                "aurum-ai.component": "conversation-runner",
                "aurum-ai.conversation": "abc",
            },
            creation_timestamp="2026-08-30T12:34:56Z",
            deletion_timestamp=(
                datetime(2026, 8, 30, 12, 35, tzinfo=timezone.utc)
                if terminating
                else None
            ),
        ),
        status=k8s_client.V1PodStatus(
            phase=phase,
            pod_ip=ip,
            reason=status_reason,
            message="token-secret must never leak",
            conditions=[
                k8s_client.V1PodCondition(
                    type="Ready", status="True" if ready else "False"
                )
            ],
            container_statuses=statuses,
        ),
    )


class ApiDoubles:
    def __init__(self) -> None:
        self.core = SimpleNamespace(
            create_namespaced_pod=AsyncMock(),
            read_namespaced_pod=AsyncMock(),
            delete_namespaced_pod=AsyncMock(),
            list_namespaced_pod=AsyncMock(),
        )
        self.authorization = SimpleNamespace(
            create_self_subject_access_review=AsyncMock()
        )
        self.response = Mock()
        self.response.raise_for_status = Mock()
        self.http = SimpleNamespace(post=AsyncMock(return_value=self.response))

    def runtime(self, **kwargs: Any) -> KubernetesRunnerRuntime:
        kwargs.setdefault("ready_timeout_seconds", 0.01)
        kwargs.setdefault("provision_timeout_seconds", 0.05)
        kwargs.setdefault("api_call_timeout_seconds", 0.01)
        kwargs.setdefault("poll_interval_seconds", 0)
        return KubernetesRunnerRuntime(
            core_v1_api_factory=lambda: self.core,
            authorization_v1_api_factory=lambda: self.authorization,
            http_client_factory=lambda: self.http,
            **kwargs,
        )


def test_build_pod_uses_hardened_manifest_and_static_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.services.runner_runtime.kubernetes.settings.RUNNER_IMAGE",
        "registry/runner:v7",
    )
    monkeypatch.setattr(
        "src.services.runner_runtime.kubernetes.settings.KUBERNETES_RUNNER_IMAGE_PULL_POLICY",
        "Always",
    )
    monkeypatch.setattr(
        "src.services.runner_runtime.kubernetes.settings.KUBERNETES_RUNNER_IMAGE_PULL_SECRETS",
        ("registry-one", "registry-two"),
    )
    runtime = ApiDoubles().runtime()

    manifest = runtime.build_pod("runner-abc", STATIC_ENV)
    container = manifest.spec.containers[0]

    assert manifest.metadata.namespace == "aurum-ai-runners"
    assert manifest.spec.automount_service_account_token is False
    assert manifest.spec.security_context.run_as_non_root is True
    assert manifest.spec.security_context.run_as_user == 10001
    assert manifest.spec.security_context.run_as_group == 10001
    assert manifest.spec.security_context.seccomp_profile.type == "RuntimeDefault"
    assert container.security_context.allow_privilege_escalation is False
    assert container.security_context.read_only_root_filesystem is True
    assert container.security_context.capabilities.drop == ["ALL"]
    assert {mount.mount_path for mount in container.volume_mounts} == {"/tmp"}
    assert manifest.spec.volumes[0].empty_dir is not None
    assert container.image == "registry/runner:v7"
    assert container.image_pull_policy == "Always"
    assert [item.name for item in manifest.spec.image_pull_secrets] == [
        "registry-one",
        "registry-two",
    ]
    assert container.readiness_probe.http_get.path == "/health"
    assert container.readiness_probe.http_get.port == 8080
    assert container.liveness_probe.http_get.path == "/health"
    assert container.liveness_probe.http_get.port == 8080


def test_build_pod_keeps_secrets_and_binding_data_out_of_loggable_manifest() -> None:
    runtime = ApiDoubles().runtime()
    env = {
        **STATIC_ENV,
        "SESSION_TOKEN": "token-secret",
        "USER_ID": "user-secret",
        "ORG_ID": "org-secret",
        "CONVERSATION_ID": "abc",
        "TASK_QUEUE": "session-abc",
    }

    manifest = runtime.build_pod("runner-abc", env)
    values = {item.name: item.value for item in manifest.spec.containers[0].env}
    secret_refs = {
        item.name: item.value_from.secret_key_ref
        for item in manifest.spec.containers[0].env
        if item.value_from is not None
    }

    assert not {
        "SESSION_TOKEN",
        "USER_ID",
        "ORG_ID",
        "CONVERSATION_ID",
        "TASK_QUEUE",
    } & values.keys()
    assert set(secret_refs) == {"REDIS_URL", "LANGFUSE_SECRET_KEY"}
    assert {ref.name for ref in secret_refs.values()} == {
        "aurum-ai-runner-secrets"
    }
    assert {name: ref.key for name, ref in secret_refs.items()} == {
        "REDIS_URL": "REDIS_URL",
        "LANGFUSE_SECRET_KEY": "LANGFUSE_SECRET_KEY",
    }
    assert "redis://user:secret" not in repr(manifest)
    assert "langfuse-secret" not in repr(manifest)
    assert manifest.metadata.labels == {
        "app.kubernetes.io/instance": "aurum-ai",
        "aurum-ai.component": "conversation-runner",
        "aurum-ai.conversation": "abc",
    }
    assert not any(
        fragment in key.lower()
        for key in manifest.metadata.labels
        for fragment in ("user", "org", "token")
    )


@pytest.mark.asyncio
async def test_create_bound_waits_for_ready_then_posts_binding() -> None:
    api = ApiDoubles()
    api.core.create_namespaced_pod.return_value = pod(
        phase="Pending", ip=None, ready=False
    )
    api.core.read_namespaced_pod.return_value = pod()

    resource = await api.runtime().create_bound("runner-abc", STATIC_ENV, binding())

    assert resource == RunnerResource(
        resource_id="runner-abc",
        name="runner-abc",
        uid="uid-abc",
        ip_address="10.42.0.12",
        state="running",
        ready=True,
        created_at=1788093296.0,
        labels={
            "app.kubernetes.io/instance": "aurum-ai",
            "aurum-ai.component": "conversation-runner",
            "aurum-ai.conversation": "abc",
        },
    )
    api.http.post.assert_awaited_once_with(
        "http://10.42.0.12:8080/bind",
        json={
            "conversation_id": "abc",
            "user_id": "user-secret",
            "org_id": "org-secret",
            "session_token": "token-secret",
            "task_queue": "session-abc",
        },
    )


@pytest.mark.asyncio
async def test_create_conflict_reconciles_matching_existing_pod() -> None:
    api = ApiDoubles()
    api.core.create_namespaced_pod.side_effect = ApiException(status=409)
    api.core.read_namespaced_pod.return_value = pod()

    resource = await api.runtime().create_bound("runner-abc", STATIC_ENV, binding())

    assert resource.uid == "uid-abc"
    assert resource.ready is True
    api.core.delete_namespaced_pod.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_conflict_rejects_pod_with_mismatching_runtime_labels() -> None:
    api = ApiDoubles()
    api.core.create_namespaced_pod.side_effect = ApiException(status=409)
    api.core.read_namespaced_pod.return_value = pod(
        labels={
            "app.kubernetes.io/instance": "another-release",
            "aurum-ai.component": "conversation-runner",
            "aurum-ai.conversation": "abc",
        }
    )

    with pytest.raises(RunnerPermanentError, match="conflict"):
        await api.runtime().create_bound("runner-abc", STATIC_ENV, binding())

    api.http.post.assert_not_awaited()
    api.core.delete_namespaced_pod.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_reads_current_pod_ip_instead_of_a_cached_address() -> None:
    api = ApiDoubles()
    api.core.read_namespaced_pod.return_value = pod(ip="10.42.0.99")

    resource = await api.runtime().get("runner-abc")

    assert resource is not None
    assert resource.ip_address == "10.42.0.99"


@pytest.mark.asyncio
async def test_refresh_token_posts_to_current_pod_ip() -> None:
    api = ApiDoubles()
    api.core.read_namespaced_pod.return_value = pod(ip="10.42.0.99")
    cached = RunnerResource(
        "runner-abc",
        "runner-abc",
        "uid-abc",
        "10.42.0.12",
        "running",
        True,
        0,
        {},
    )

    refreshed = await api.runtime().refresh_token(cached, "new-token")

    assert refreshed is True
    api.http.post.assert_awaited_once_with(
        "http://10.42.0.99:8080/refresh-token",
        json={"session_token": "new-token"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failed_pod", "diagnostic"),
    [
        (
            pod(
                phase="Pending",
                ip=None,
                ready=False,
                waiting_reason="ImagePullBackOff",
            ),
            "ImagePullBackOff",
        ),
        (
            pod(phase="Failed", ip=None, ready=False, status_reason="Evicted"),
            "Failed",
        ),
        (
            pod(phase="Running", ready=False, terminated_reason="OOMKilled"),
            "OOMKilled",
        ),
    ],
)
async def test_readiness_terminal_failures_report_safe_diagnostics(
    failed_pod: k8s_client.V1Pod, diagnostic: str
) -> None:
    api = ApiDoubles()
    api.core.create_namespaced_pod.return_value = pod(
        phase="Pending", ip=None, ready=False
    )
    api.core.read_namespaced_pod.return_value = failed_pod

    with pytest.raises(RunnerReadinessError) as exc_info:
        await api.runtime().create_bound("runner-abc", STATIC_ENV, binding())

    assert diagnostic in str(exc_info.value)
    assert "token-secret" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_readiness_timeout_reports_phase_without_unsafe_status_message() -> None:
    api = ApiDoubles()
    api.core.create_namespaced_pod.return_value = pod(
        phase="Pending", ip=None, ready=False
    )
    api.core.read_namespaced_pod.return_value = pod(
        phase="Pending", ip=None, ready=False
    )

    with pytest.raises(RunnerReadinessError) as exc_info:
        await api.runtime(
            provision_timeout_seconds=0.5,
            api_call_timeout_seconds=0.1,
        ).create_bound("runner-abc", STATIC_ENV, binding())

    assert "timed out" in str(exc_info.value)
    assert "Pending" in str(exc_info.value)
    assert "token-secret" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_readiness_keeps_polling_for_normal_container_creation() -> None:
    api = ApiDoubles()
    api.core.create_namespaced_pod.return_value = pod(
        phase="Pending", ip=None, ready=False
    )
    api.core.read_namespaced_pod.side_effect = [
        pod(phase="Pending", ip=None, ready=False, waiting_reason="ContainerCreating"),
        pod(),
    ]

    resource = await api.runtime().create_bound("runner-abc", STATIC_ENV, binding())

    assert resource.ready is True
    assert api.core.read_namespaced_pod.await_count == 2


@pytest.mark.asyncio
async def test_failed_create_cleanup_uses_only_uid_returned_by_create() -> None:
    api = ApiDoubles()
    api.core.create_namespaced_pod.return_value = pod(
        uid="created-uid", phase="Pending", ip=None, ready=False
    )
    api.core.read_namespaced_pod.return_value = pod(
        uid="replacement-uid",
        phase="Pending",
        ip=None,
        ready=False,
        waiting_reason="ImagePullBackOff",
    )

    with pytest.raises(RunnerReadinessError):
        await api.runtime().create_bound("runner-abc", STATIC_ENV, binding())

    options = api.core.delete_namespaced_pod.await_args.kwargs["body"]
    assert options.preconditions.uid == "created-uid"


@pytest.mark.asyncio
async def test_bind_failure_deletes_new_pod_with_uid_precondition() -> None:
    api = ApiDoubles()
    api.core.create_namespaced_pod.return_value = pod()
    api.core.read_namespaced_pod.return_value = pod()
    api.response.raise_for_status.side_effect = RuntimeError("token-secret")

    with pytest.raises(RunnerRuntimeError) as exc_info:
        await api.runtime().create_bound("runner-abc", STATIC_ENV, binding())

    options = api.core.delete_namespaced_pod.await_args.kwargs["body"]
    assert options.preconditions.uid == "uid-abc"
    assert "token-secret" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_delete_uses_uid_precondition_and_treats_not_found_as_success() -> None:
    api = ApiDoubles()
    api.core.delete_namespaced_pod.side_effect = ApiException(status=404)

    await api.runtime().delete("runner-abc", "uid-abc")

    options = api.core.delete_namespaced_pod.await_args.kwargs["body"]
    assert options.preconditions.uid == "uid-abc"


@pytest.mark.asyncio
async def test_delete_waits_until_target_uid_releases_deterministic_name() -> None:
    api = ApiDoubles()
    api.core.read_namespaced_pod.side_effect = [
        pod(uid="uid-abc", ready=False),
        ApiException(status=404),
    ]

    await api.runtime().delete("runner-abc", "uid-abc")

    assert api.core.read_namespaced_pod.await_count == 2
    options = api.core.delete_namespaced_pod.await_args.kwargs["body"]
    assert options.grace_period_seconds == 0


@pytest.mark.asyncio
async def test_wait_for_ready_bounds_a_hung_kubernetes_read() -> None:
    api = ApiDoubles()

    async def hang_forever(**_: Any) -> k8s_client.V1Pod:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    api.core.read_namespaced_pod.side_effect = hang_forever

    with pytest.raises(RunnerReadinessError, match="timed out"):
        await asyncio.wait_for(api.runtime()._wait_for_ready("runner-abc"), timeout=0.2)


@pytest.mark.asyncio
async def test_create_bound_bounds_a_hung_create_api_call() -> None:
    api = ApiDoubles()

    async def hang_forever(**_: Any) -> k8s_client.V1Pod:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    api.core.create_namespaced_pod.side_effect = hang_forever

    with pytest.raises(RunnerReadinessError, match="timed out"):
        await asyncio.wait_for(
            api.runtime().create_bound("runner-abc", STATIC_ENV, binding()), timeout=0.2
        )


@pytest.mark.asyncio
async def test_create_bound_waits_for_terminating_conflict_then_recreates() -> None:
    api = ApiDoubles()
    api.core.create_namespaced_pod.side_effect = [
        ApiException(status=409),
        pod(uid="uid-new"),
    ]
    api.core.read_namespaced_pod.side_effect = [
        pod(uid="uid-old", ready=False, terminating=True),
        ApiException(status=404),
        pod(uid="uid-new"),
    ]

    resource = await api.runtime(ready_timeout_seconds=0.2).create_bound(
        "runner-abc", STATIC_ENV, binding()
    )

    assert resource.uid == "uid-new"
    assert api.core.create_namespaced_pod.await_count == 2


@pytest.mark.asyncio
async def test_transport_failure_is_sanitized_as_typed_runtime_error() -> None:
    api = ApiDoubles()
    api.core.read_namespaced_pod.side_effect = RuntimeError(
        "TLS failure at https://token:secret@kubernetes.default"
    )

    with pytest.raises(RunnerRuntimeError) as exc_info:
        await api.runtime(retry_attempts=1).get("runner-abc")

    assert "token:secret" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_bounds_a_hung_kubernetes_api_call() -> None:
    api = ApiDoubles()

    async def hang_forever(**_: Any) -> k8s_client.V1Pod:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    api.core.read_namespaced_pod.side_effect = hang_forever

    with pytest.raises(RunnerRuntimeError, match="transport failed"):
        await asyncio.wait_for(
            api.runtime(retry_attempts=1).get("runner-abc"), timeout=0.2
        )


@pytest.mark.asyncio
async def test_delete_bounds_a_hung_kubernetes_api_call() -> None:
    api = ApiDoubles()

    async def hang_forever(**_: Any) -> None:
        await asyncio.sleep(3600)

    api.core.delete_namespaced_pod.side_effect = hang_forever

    with pytest.raises(RunnerRuntimeError, match="transport failed"):
        await asyncio.wait_for(
            api.runtime(retry_attempts=1).delete("runner-abc", "uid-abc"),
            timeout=0.2,
        )


@pytest.mark.asyncio
async def test_cancelled_provisioning_cleans_created_pod_before_returning() -> None:
    api = ApiDoubles()
    api.core.create_namespaced_pod.return_value = pod(
        uid="uid-cancelled", phase="Pending", ip=None, ready=False
    )
    read_started = asyncio.Event()
    read_count = 0

    async def read_then_release(**_: Any) -> k8s_client.V1Pod:
        nonlocal read_count
        read_count += 1
        if read_count == 1:
            read_started.set()
            await asyncio.sleep(3600)
        raise ApiException(status=404)

    api.core.read_namespaced_pod.side_effect = read_then_release
    task = asyncio.create_task(
        api.runtime(
            ready_timeout_seconds=10,
            provision_timeout_seconds=20,
            api_call_timeout_seconds=10,
        ).create_bound("runner-abc", STATIC_ENV, binding())
    )
    await asyncio.wait_for(read_started.wait(), timeout=0.2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    options = api.core.delete_namespaced_pod.await_args.kwargs["body"]
    assert options.preconditions.uid == "uid-cancelled"


@pytest.mark.asyncio
async def test_list_is_scoped_to_release_and_component_labels() -> None:
    api = ApiDoubles()
    api.core.list_namespaced_pod.return_value = k8s_client.V1PodList(items=[pod()])

    resources = await api.runtime().list()

    assert [resource.uid for resource in resources] == ["uid-abc"]
    assert api.core.list_namespaced_pod.await_args.kwargs["label_selector"] == (
        "app.kubernetes.io/instance=aurum-ai,"
        "aurum-ai.component=conversation-runner"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_transient_api_failures_retry_exactly_configured_attempts(
    status: int,
) -> None:
    api = ApiDoubles()
    api.core.read_namespaced_pod.side_effect = ApiException(status=status)

    with pytest.raises(RunnerRuntimeError):
        await api.runtime(retry_attempts=3).get("runner-abc")

    assert api.core.read_namespaced_pod.await_count == 3


@pytest.mark.asyncio
async def test_ready_checks_namespace_access_and_every_required_pod_permission() -> None:
    api = ApiDoubles()
    api.core.list_namespaced_pod.return_value = k8s_client.V1PodList(items=[])
    api.authorization.create_self_subject_access_review.return_value = SimpleNamespace(
        status=SimpleNamespace(allowed=True)
    )

    await api.runtime().ready()

    assert api.core.list_namespaced_pod.await_args.kwargs["limit"] == 1
    reviews = [
        call.args[0].spec.resource_attributes
        for call in api.authorization.create_self_subject_access_review.await_args_list
    ]
    assert {(review.namespace, review.resource, review.verb) for review in reviews} == {
        ("aurum-ai-runners", "pods", verb)
        for verb in ("get", "list", "watch", "create", "delete")
    }


@pytest.mark.asyncio
async def test_ready_rejects_missing_required_rbac_permission() -> None:
    api = ApiDoubles()
    api.core.list_namespaced_pod.return_value = k8s_client.V1PodList(items=[])
    api.authorization.create_self_subject_access_review.return_value = SimpleNamespace(
        status=SimpleNamespace(allowed=False)
    )

    with pytest.raises(RunnerPermanentError, match="RBAC"):
        await api.runtime().ready()


@pytest.mark.asyncio
async def test_public_warm_pool_operations_are_permanently_disabled() -> None:
    runtime = ApiDoubles().runtime()
    resource = RunnerResource(
        "runner-abc", "runner-abc", "uid", None, "running", True, 0, {}
    )

    with pytest.raises(RunnerPermanentError, match="warm pool"):
        await runtime.create_unbound("runner-abc", STATIC_ENV)
    with pytest.raises(RunnerPermanentError, match="warm pool"):
        await runtime.bind(resource, "runner-renamed", binding())
    assert await runtime.pool_is_unbound(resource) is False


@pytest.mark.asyncio
async def test_client_loading_never_falls_back_to_kubeconfig_inside_a_pod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.runner_runtime import kubernetes

    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    in_cluster = Mock(side_effect=kubernetes.ConfigException("missing token"))
    kubeconfig = AsyncMock()
    monkeypatch.setattr(kubernetes.k8s_config, "load_incluster_config", in_cluster)
    monkeypatch.setattr(kubernetes.k8s_config, "load_kube_config", kubeconfig)

    with pytest.raises(RunnerPermanentError, match="in-cluster"):
        await KubernetesRunnerRuntime()._clients()

    in_cluster.assert_called_once_with()
    kubeconfig.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_loading_uses_kubeconfig_only_outside_a_pod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.runner_runtime import kubernetes

    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_PORT", raising=False)
    in_cluster = Mock()
    kubeconfig = AsyncMock()
    api_client = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(kubernetes.k8s_config, "load_incluster_config", in_cluster)
    monkeypatch.setattr(kubernetes.k8s_config, "load_kube_config", kubeconfig)
    monkeypatch.setattr(kubernetes.k8s_client, "ApiClient", Mock(return_value=api_client))
    monkeypatch.setattr(kubernetes.k8s_client, "CoreV1Api", Mock())
    monkeypatch.setattr(kubernetes.k8s_client, "AuthorizationV1Api", Mock())
    monkeypatch.setattr(
        kubernetes.httpx, "AsyncClient", Mock(return_value=SimpleNamespace(aclose=AsyncMock()))
    )

    runtime = KubernetesRunnerRuntime()
    await runtime._clients()
    await runtime.close()

    in_cluster.assert_not_called()
    kubeconfig.assert_awaited_once_with()
