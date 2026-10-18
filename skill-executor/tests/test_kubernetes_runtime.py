from __future__ import annotations

import asyncio
import io
import tarfile
import zipfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client.exceptions import ApiException

from src.config import Settings
from src.sandbox_runtime import SessionNotFoundError, create_sandbox_runtime
from src.sandbox_runtime.kubernetes import (
    KubernetesSandboxPermanentError,
    KubernetesSandboxReadinessError,
    KubernetesSandboxRuntime,
)
from src.sandbox_runtime.kubernetes_exec import ExecResult


SESSION_ID = "0123456789abcdef0123456789abcdef"
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def bundle() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("SKILL.md", "# Skill\n")
        archive.writestr("scripts/run.py", "print('ok')\n")
    return output.getvalue()


def pod(
    *,
    session_id: str = SESSION_ID,
    uid: str = "uid-1",
    phase: str = "Running",
    ready: bool = True,
    created_at: datetime = NOW,
    last_active_at: datetime = NOW,
    resource_version: str = "7",
    labels: dict[str, str] | None = None,
    waiting_reason: str | None = None,
    terminating: bool = False,
) -> k8s_client.V1Pod:
    state = None
    if waiting_reason is not None:
        state = k8s_client.V1ContainerState(
            waiting=k8s_client.V1ContainerStateWaiting(
                reason=waiting_reason,
                message="https://token:secret@cluster must not leak",
            )
        )
    statuses = []
    if state is not None:
        statuses.append(
            k8s_client.V1ContainerStatus(
                name="sandbox",
                image="sandbox:test",
                image_id="sandbox:test",
                ready=False,
                restart_count=0,
                state=state,
            )
        )
    return k8s_client.V1Pod(
        metadata=k8s_client.V1ObjectMeta(
            name=f"skill-sandbox-{session_id[:32]}",
            namespace="test-sandboxes",
            uid=uid,
            resource_version=resource_version,
            deletion_timestamp=NOW if terminating else None,
            labels=labels
            or {
                "app.kubernetes.io/instance": "test-release",
                "aurum-ai.component": "skill-sandbox",
                "aurum-ai.session": session_id,
                "aurum-ai.network-policy": "none",
            },
            annotations={
                "aurum-ai/session-id": session_id,
                "aurum-ai/created-at": created_at.isoformat(),
                "aurum-ai/last-active-at": last_active_at.isoformat(),
                "aurum-ai/network-policy": "none",
            },
        ),
        status=k8s_client.V1PodStatus(
            phase=phase,
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
            create_namespaced_pod=AsyncMock(return_value=pod()),
            read_namespaced_pod=AsyncMock(return_value=pod()),
            delete_namespaced_pod=AsyncMock(),
            list_namespaced_pod=AsyncMock(
                return_value=k8s_client.V1PodList(items=[])
            ),
            patch_namespaced_pod=AsyncMock(),
        )
        self.authorization = SimpleNamespace(
            create_self_subject_access_review=AsyncMock(
                return_value=SimpleNamespace(status=SimpleNamespace(allowed=True))
            )
        )
        self.exec = SimpleNamespace(exec=AsyncMock(return_value=ExecResult("", "", 0, False)))
        self.api_client = SimpleNamespace(close=AsyncMock())

    def runtime(
        self,
        *,
        now: datetime = NOW,
        settings: Settings | None = None,
    ) -> KubernetesSandboxRuntime:
        return KubernetesSandboxRuntime(
            runtime_settings=settings
            or Settings(
                _env_file=None,
                runtime="kubernetes",
                kubernetes_release_name="test-release",
                kubernetes_namespace="test-sandboxes",
            ),
            core_v1_api_factory=lambda: self.core,
            authorization_v1_api_factory=lambda: self.authorization,
            exec_client=self.exec,
            api_client=self.api_client,
            ready_timeout_seconds=0.01,
            poll_interval_seconds=0,
            now=lambda: now,
            session_id_factory=lambda: SESSION_ID,
        )


def test_factory_selects_kubernetes_runtime() -> None:
    runtime = create_sandbox_runtime(Settings(_env_file=None, runtime="kubernetes"))

    assert isinstance(runtime, KubernetesSandboxRuntime)


def test_build_pod_is_hardened_and_contains_no_credentials() -> None:
    settings = Settings(
        _env_file=None,
        runtime="kubernetes",
        sandbox_image="registry/sandbox:v3",
        kubernetes_release_name="test-release",
        kubernetes_namespace="test-sandboxes",
        kubernetes_image_pull_policy="Always",
        kubernetes_image_pull_secrets=("registry-one", "registry-two"),
        kubernetes_cpu_request="125m",
        kubernetes_cpu_limit="750m",
        kubernetes_memory_request="96Mi",
        kubernetes_memory_limit="384Mi",
        sandbox_skill_tmpfs_mb=128,
        s3_endpoint="https://minio.internal",
        s3_access_key="minio-user",
        s3_secret_key="minio-secret",
    )

    manifest = ApiDoubles().runtime(settings=settings).build_pod(SESSION_ID, "none")
    container = manifest.spec.containers[0]

    assert manifest.metadata.name == f"skill-sandbox-{SESSION_ID}"
    assert manifest.metadata.namespace == "test-sandboxes"
    assert manifest.metadata.labels == {
        "app.kubernetes.io/instance": "test-release",
        "aurum-ai.component": "skill-sandbox",
        "aurum-ai.session": SESSION_ID,
        "aurum-ai.network-policy": "none",
    }
    assert manifest.metadata.annotations["aurum-ai/session-id"] == SESSION_ID
    assert manifest.metadata.annotations["aurum-ai/network-policy"] == "none"
    assert manifest.spec.automount_service_account_token is False
    assert manifest.spec.restart_policy == "Never"
    assert manifest.spec.security_context.run_as_non_root is True
    assert manifest.spec.security_context.run_as_user == 10001
    assert manifest.spec.security_context.run_as_group == 10001
    assert manifest.spec.security_context.fs_group == 10001
    assert manifest.spec.security_context.fs_group_change_policy == "OnRootMismatch"
    assert manifest.spec.security_context.seccomp_profile.type == "RuntimeDefault"
    assert manifest.spec.enable_service_links is False
    assert container.image == "registry/sandbox:v3"
    assert container.image_pull_policy == "Always"
    assert container.command == ["/bin/sh", "-c"]
    assert container.args == ["mkdir -p /tmp/home && exec sleep infinity"]
    assert container.security_context.allow_privilege_escalation is False
    assert container.security_context.read_only_root_filesystem is True
    assert container.security_context.capabilities.drop == ["ALL"]
    assert [(item.name, item.value) for item in container.env] == [("HOME", "/tmp/home")]
    assert container.env_from in (None, [])
    assert {mount.mount_path: mount.name for mount in container.volume_mounts} == {
        "/tmp": "tmp",
        "/skill": "skill",
    }
    volumes = {volume.name: volume for volume in manifest.spec.volumes}
    assert volumes["tmp"].empty_dir.medium is None
    assert volumes["skill"].empty_dir.medium == "Memory"
    assert volumes["skill"].empty_dir.size_limit == "128Mi"
    assert container.resources.requests == {"cpu": "125m", "memory": "96Mi"}
    assert container.resources.limits == {"cpu": "750m", "memory": "384Mi"}
    assert [secret.name for secret in manifest.spec.image_pull_secrets] == [
        "registry-one",
        "registry-two",
    ]
    rendered = repr(manifest)
    assert "minio.internal" not in rendered
    assert "minio-user" not in rendered
    assert "minio-secret" not in rendered


@pytest.mark.asyncio
async def test_create_waits_for_ready_and_streams_tar_over_exec_stdin() -> None:
    api = ApiDoubles()
    api.core.read_namespaced_pod.side_effect = [
        pod(phase="Pending", ready=False),
        pod(),
    ]
    raw_bundle = bundle()

    session = await api.runtime().create_session(raw_bundle, "instructions", "none")

    assert session.session_id == SESSION_ID
    assert session.resource_id == f"skill-sandbox-{SESSION_ID}"
    assert session.resource_uid == "uid-1"
    manifest = api.core.create_namespaced_pod.await_args.kwargs["body"]
    assert manifest.metadata.name == f"skill-sandbox-{SESSION_ID}"
    call = api.exec.exec.await_args.kwargs
    assert call["namespace"] == "test-sandboxes"
    assert call["pod"] == f"skill-sandbox-{SESSION_ID}"
    assert call["container"] == "sandbox"
    assert call["command"] == ["tar", "-x", "-C", "/skill", "-f", "-"]
    with tarfile.open(fileobj=io.BytesIO(call["stdin"])) as archive:
        assert sorted(archive.getnames()) == ["SKILL.md", "scripts/run.py"]


@pytest.mark.asyncio
async def test_create_deletes_only_created_uid_when_bootstrap_fails() -> None:
    api = ApiDoubles()
    api.exec.exec.return_value = ExecResult("", "tar failed", 2, False)

    with pytest.raises(Exception, match="bundle bootstrap"):
        await api.runtime().create_session(bundle(), "instructions")

    options = api.core.delete_namespaced_pod.await_args.kwargs["body"]
    assert options.preconditions.uid == "uid-1"


@pytest.mark.asyncio
async def test_create_retries_transient_kubernetes_api_failure() -> None:
    api = ApiDoubles()
    api.core.create_namespaced_pod.side_effect = [
        ApiException(status=503),
        pod(),
    ]

    await api.runtime().create_session(bundle(), "instructions")

    assert api.core.create_namespaced_pod.await_count == 2


@pytest.mark.asyncio
async def test_create_readiness_timeout_covers_a_hanging_api_read() -> None:
    api = ApiDoubles()

    async def hang(**kwargs: object) -> k8s_client.V1Pod:
        del kwargs
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    api.core.read_namespaced_pod.side_effect = hang

    with pytest.raises(KubernetesSandboxReadinessError, match="timed out"):
        await api.runtime().create_session(bundle(), "instructions")

    api.exec.exec.assert_not_awaited()
    options = api.core.delete_namespaced_pod.await_args.kwargs["body"]
    assert options.preconditions.uid == "uid-1"


@pytest.mark.asyncio
async def test_create_conflict_is_rejected_without_reusing_an_existing_pod() -> None:
    api = ApiDoubles()
    api.core.create_namespaced_pod.side_effect = ApiException(status=409)
    api.core.read_namespaced_pod.return_value = pod()

    with pytest.raises(KubernetesSandboxPermanentError, match="conflict"):
        await api.runtime().create_session(bundle(), "instructions", "egress")

    api.core.read_namespaced_pod.assert_not_awaited()
    api.exec.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_runtime_instance_finds_session_from_pod_metadata() -> None:
    api = ApiDoubles()
    first = api.runtime()
    second = api.runtime()

    await first.create_session(bundle(), "instructions")
    result = await second.exec_command(SESSION_ID, "printf ok", timeout=12)

    assert result == {"stdout": "", "stderr": "", "exit_code": 0, "timed_out": False}
    api.core.read_namespaced_pod.assert_awaited()
    call = api.exec.exec.await_args.kwargs
    assert call["command"] == [
        "timeout",
        "-k",
        "5",
        "12",
        "/bin/sh",
        "-c",
        "printf ok",
    ]


@pytest.mark.asyncio
async def test_exec_maps_timeout_and_patches_last_active_optimistically() -> None:
    api = ApiDoubles()
    api.exec.exec.return_value = ExecResult("partial", "", None, True)

    result = await api.runtime().exec_command(SESSION_ID, "sleep 20", timeout=2.5)

    assert result == {
        "stdout": "partial",
        "stderr": "",
        "exit_code": None,
        "timed_out": True,
    }
    patch = api.core.patch_namespaced_pod.await_args.kwargs["body"]
    assert patch == {
        "metadata": {
            "resourceVersion": "7",
            "annotations": {"aurum-ai/last-active-at": NOW.isoformat()},
        }
    }


@pytest.mark.asyncio
async def test_last_active_patch_reconciles_resource_version_conflict() -> None:
    api = ApiDoubles()
    api.core.read_namespaced_pod.side_effect = [
        pod(resource_version="7", last_active_at=NOW - timedelta(seconds=2)),
        pod(resource_version="8", last_active_at=NOW - timedelta(seconds=1)),
    ]
    api.core.patch_namespaced_pod.side_effect = [ApiException(status=409), None]

    await api.runtime().exec_command(SESSION_ID, "true")

    assert api.core.patch_namespaced_pod.await_count == 2
    retry = api.core.patch_namespaced_pod.await_args.kwargs["body"]
    assert retry["metadata"]["resourceVersion"] == "8"
    assert retry["metadata"]["annotations"]["aurum-ai/last-active-at"] == NOW.isoformat()


@pytest.mark.asyncio
async def test_last_active_conflict_preserves_session_loss_during_reconciliation() -> None:
    api = ApiDoubles()
    api.core.read_namespaced_pod.side_effect = [pod(), ApiException(status=404)]
    api.core.patch_namespaced_pod.side_effect = ApiException(status=409)

    with pytest.raises(SessionNotFoundError):
        await api.runtime().exec_command(SESSION_ID, "true")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reconciled_pod",
    [pod(phase="Failed", ready=False), pod(terminating=True)],
)
async def test_last_active_conflict_rejects_unusable_reconciled_pod(
    reconciled_pod: k8s_client.V1Pod,
) -> None:
    api = ApiDoubles()
    api.core.read_namespaced_pod.side_effect = [pod(), reconciled_pod]
    api.core.patch_namespaced_pod.side_effect = ApiException(status=409)

    with pytest.raises(SessionNotFoundError):
        await api.runtime().exec_command(SESSION_ID, "true")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_session_id",
    ["UPPERCASE", "../escape", "contains-hyphen", ""],
)
async def test_exec_rejects_non_lowercase_hex_session_ids(bad_session_id: str) -> None:
    api = ApiDoubles()

    with pytest.raises(SessionNotFoundError):
        await api.runtime().exec_command(bad_session_id, "true")

    api.core.read_namespaced_pod.assert_not_awaited()


@pytest.mark.asyncio
async def test_exec_returns_unknown_session_for_missing_or_foreign_pod() -> None:
    missing = ApiDoubles()
    missing.core.read_namespaced_pod.side_effect = ApiException(status=404)
    with pytest.raises(SessionNotFoundError):
        await missing.runtime().exec_command(SESSION_ID, "true")

    foreign = ApiDoubles()
    foreign.core.read_namespaced_pod.return_value = pod(
        labels={
            "app.kubernetes.io/instance": "another-release",
            "aurum-ai.component": "skill-sandbox",
            "aurum-ai.session": SESSION_ID,
            "aurum-ai.network-policy": "none",
        }
    )
    with pytest.raises(SessionNotFoundError):
        await foreign.runtime().exec_command(SESSION_ID, "true")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unusable_pod",
    [pod(phase="Failed", ready=False), pod(terminating=True)],
)
async def test_exec_rejects_non_ready_or_terminating_pod(
    unusable_pod: k8s_client.V1Pod,
) -> None:
    api = ApiDoubles()
    api.core.read_namespaced_pod.return_value = unusable_pod

    with pytest.raises(SessionNotFoundError):
        await api.runtime().exec_command(SESSION_ID, "true")

    api.exec.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_exec_maps_pod_disappearance_during_upgrade_to_unknown_session() -> None:
    api = ApiDoubles()
    api.core.read_namespaced_pod.side_effect = [pod(), ApiException(status=404)]
    api.exec.exec.side_effect = RuntimeError("websocket upgrade failed")

    with pytest.raises(SessionNotFoundError):
        await api.runtime().exec_command(SESSION_ID, "true")


@pytest.mark.asyncio
async def test_exec_does_not_convert_unrelated_transport_failure_for_live_pod() -> None:
    api = ApiDoubles()
    api.core.read_namespaced_pod.side_effect = [pod(), pod()]
    api.exec.exec.side_effect = RuntimeError("websocket framing failed")

    with pytest.raises(RuntimeError, match="framing failed"):
        await api.runtime().exec_command(SESSION_ID, "true")


@pytest.mark.asyncio
async def test_exec_maps_pod_disappearance_during_activity_patch_to_unknown_session() -> None:
    api = ApiDoubles()
    api.core.patch_namespaced_pod.side_effect = ApiException(status=404)

    with pytest.raises(SessionNotFoundError):
        await api.runtime().exec_command(SESSION_ID, "true")


@pytest.mark.asyncio
async def test_close_is_idempotent_and_uses_current_pod_uid() -> None:
    api = ApiDoubles()

    await api.runtime().close_session(SESSION_ID)

    options = api.core.delete_namespaced_pod.await_args.kwargs["body"]
    assert options.preconditions.uid == "uid-1"
    api.core.delete_namespaced_pod.side_effect = ApiException(status=404)
    await api.runtime().close_session(SESSION_ID)


@pytest.mark.asyncio
async def test_reaper_filters_release_and_deletes_absolute_and_idle_expiry() -> None:
    api = ApiDoubles()
    fresh = pod(
        session_id="1" * 32,
        uid="fresh",
        created_at=NOW - timedelta(seconds=10),
        last_active_at=NOW - timedelta(seconds=10),
    )
    absolute = pod(
        session_id="2" * 32,
        uid="absolute",
        created_at=NOW - timedelta(seconds=1801),
        last_active_at=NOW,
    )
    idle = pod(
        session_id="3" * 32,
        uid="idle",
        created_at=NOW - timedelta(seconds=60),
        last_active_at=NOW - timedelta(seconds=301),
    )
    api.core.list_namespaced_pod.return_value = k8s_client.V1PodList(
        items=[fresh, absolute, idle]
    )

    reaped = await api.runtime().reap_expired()

    assert reaped == 2
    assert api.core.list_namespaced_pod.await_args.kwargs["label_selector"] == (
        "app.kubernetes.io/instance=test-release,"
        "aurum-ai.component=skill-sandbox"
    )
    deletes = api.core.delete_namespaced_pod.await_args_list
    assert {(call.kwargs["name"], call.kwargs["body"].preconditions.uid) for call in deletes} == {
        (f"skill-sandbox-{'2' * 32}", "absolute"),
        (f"skill-sandbox-{'3' * 32}", "idle"),
    }


@pytest.mark.asyncio
async def test_shutdown_closes_kubernetes_client_without_deleting_live_pods() -> None:
    api = ApiDoubles()
    runtime = api.runtime()

    await runtime.shutdown()

    api.api_client.close.assert_awaited_once()
    api.core.delete_namespaced_pod.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_checks_namespace_and_required_pod_permissions() -> None:
    api = ApiDoubles()

    await api.runtime().ready()

    api.core.list_namespaced_pod.assert_awaited_once_with(
        namespace="test-sandboxes", limit=1
    )
    attributes = [
        call.args[0].spec.resource_attributes
        for call in api.authorization.create_self_subject_access_review.await_args_list
    ]
    assert {
        (item.namespace, item.resource, item.subresource, item.verb)
        for item in attributes
    } == {
        ("test-sandboxes", "pods", None, verb)
        for verb in ("get", "list", "create", "delete", "patch")
    } | {
        ("test-sandboxes", "pods", "exec", verb) for verb in ("create", "get")
    }


@pytest.mark.asyncio
async def test_ready_fails_when_an_ssar_is_denied() -> None:
    api = ApiDoubles()
    api.authorization.create_self_subject_access_review.return_value = SimpleNamespace(
        status=SimpleNamespace(allowed=False)
    )

    with pytest.raises(Exception, match="RBAC"):
        await api.runtime().ready()
