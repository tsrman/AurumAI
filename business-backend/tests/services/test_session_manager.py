import asyncio
import json
import time
from typing import Any, Literal, Mapping
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import httpx
import pytest
from aiodocker.exceptions import DockerError

from src.core.cache import redis_client
from src.core.config import settings as config_settings
from src.services.runner_runtime import (
    DockerRunnerRuntime,
    RunnerBinding,
    RunnerResource,
    RunnerRuntimeError,
)
from src.services.session_manager import POOL_REDIS_KEY, ContainerRef, SessionManager
from src.services.session_token import session_token_service


class FakeHttpResponse:
    def __init__(self, status_code: int = 200, json_data: dict | None = None) -> None:
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}

    def json(self) -> dict:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)  # type: ignore[arg-type]


def fake_async_client_factory(
    get_response: FakeHttpResponse | None = None,
    post_response: FakeHttpResponse | None = None,
    post_side_effect: Exception | None = None,
) -> tuple[MagicMock, AsyncMock]:
    """Builds a patchable replacement for `httpx.AsyncClient(...)` used as an
    `async with` context manager, matching session_manager.py's usage."""
    instance = AsyncMock()
    instance.get = AsyncMock(return_value=get_response or FakeHttpResponse(200))
    if post_side_effect is not None:
        instance.post = AsyncMock(side_effect=post_side_effect)
    else:
        instance.post = AsyncMock(return_value=post_response or FakeHttpResponse(200))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=instance)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)
    return factory, instance


class FakeRedis:
    def __init__(self) -> None:
        self._hash: dict[str, str] = {}
        self._lists: dict[str, list[str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.lock_calls: list[tuple[str, float | None, float | None]] = []

    async def hget(self, key: str, field: str) -> str | None:
        return self._hash.get(field)

    async def hset(self, key: str, field: str, value: str) -> None:
        self._hash[field] = value

    async def hdel(self, key: str, field: str) -> None:
        self._hash.pop(field, None)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._hash)

    async def lpop(self, key: str) -> str | None:
        items = self._lists.get(key) or []
        if not items:
            return None
        return items.pop(0)

    async def rpush(self, key: str, value: str) -> int:
        self._lists.setdefault(key, []).append(value)
        return len(self._lists[key])

    async def llen(self, key: str) -> int:
        return len(self._lists.get(key) or [])

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        items = self._lists.get(key) or []
        if end == -1:
            return items[start:]
        return items[start : end + 1]

    async def lrem(self, key: str, count: int, value: str) -> int:
        items = self._lists.get(key) or []
        try:
            items.remove(value)
            return 1
        except ValueError:
            return 0

    def lock(self, name: str, timeout: float | None = None, blocking_timeout: float | None = None) -> "FakeLock":
        # A real asyncio.Lock per name gives genuine mutual exclusion within
        # the test's event loop — enough to test real interleaving via
        # asyncio.gather, unlike a no-op stand-in.
        self.lock_calls.append((name, timeout, blocking_timeout))
        return FakeLock(self._locks.setdefault(name, asyncio.Lock()))


class FakeLock:
    """Stands in for redis.asyncio.lock.Lock's `async with lock:` protocol."""

    def __init__(self, inner: asyncio.Lock) -> None:
        self._inner = inner

    async def __aenter__(self) -> "FakeLock":
        await self._inner.acquire()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._inner.release()


class FakeContainer:
    def __init__(
        self,
        cid: str,
        status: str = "running",
        health: dict[str, Any] | None = None,
    ) -> None:
        self.id = cid
        self._status = status
        self._health = health
        self.start = AsyncMock()
        self.kill = AsyncMock()
        self.delete = AsyncMock()
        self.rename = AsyncMock()

    async def show(self) -> dict[str, Any]:
        return {
            "State": {
                "Status": self._status,
                "Running": self._status == "running",
                "Health": self._health,
            },
            "NetworkSettings": {"Networks": {"aurum-ai_runner-net": {"IPAddress": "172.20.0.5"}}},
        }

    def __getitem__(self, key: str) -> Any:
        if key == "State":
            return {"Status": self._status}
        raise KeyError(key)


class FakeListedContainer:
    """Shaped like an entry from Docker's `containers/json` (what
    containers.list() returns), not like an inspect result: `State` is a bare
    string there, `Names` are "/"-prefixed, and keys are simply absent rather
    than null — which is what sweep_orphans has to cope with."""

    def __init__(
        self,
        cid: str,
        name: str,
        state: str = "running",
        labels: dict[str, str] | None = None,
        created: float | None = None,
    ) -> None:
        self.id = cid
        self.kill = AsyncMock()
        self.delete = AsyncMock()
        self._payload: dict[str, Any] = {
            "Id": cid,
            "Names": [f"/{name}"],
            "State": state,
            "Created": created if created is not None else time.time(),
        }
        if labels is not None:
            self._payload["Labels"] = labels

    def __getitem__(self, key: str) -> Any:
        return self._payload[key]


RUNNER_LABELS = {"aurum-ai.component": "conversation-runner"}
KUBERNETES_RUNNER_LABELS = {
    "app.kubernetes.io/instance": "aurum-ai",
    "aurum-ai.component": "conversation-runner",
}


class FakeRunnerRuntime:
    def __init__(
        self,
        *,
        kind: Literal["docker", "kubernetes"] = "kubernetes",
        supports_warm_pool: bool = False,
    ) -> None:
        self.kind = kind
        self.supports_warm_pool = supports_warm_pool
        self.resources: dict[str, RunnerResource] = {}
        self.created: list[tuple[str, Mapping[str, str], RunnerBinding]] = []
        self.unbound_created: list[tuple[str, Mapping[str, str]]] = []
        self.binds: list[tuple[RunnerResource, str, RunnerBinding]] = []
        self.refreshes: list[tuple[RunnerResource, str]] = []
        self.get_calls: list[str] = []
        self.deleted: list[tuple[str, str | None]] = []
        self.closed = False

    async def create_bound(
        self, name: str, static_env: Mapping[str, str], binding: RunnerBinding
    ) -> RunnerResource:
        self.created.append((name, dict(static_env), binding))
        resource = RunnerResource(
            resource_id=name,
            name=name,
            uid=f"uid-created-{len(self.created)}",
            ip_address=f"10.42.0.{10 + len(self.created)}",
            state="running",
            ready=True,
            created_at=time.time(),
            labels={
                **KUBERNETES_RUNNER_LABELS,
                "aurum-ai.conversation": binding.conversation_id,
            },
        )
        self.resources[name] = resource
        return resource

    async def create_unbound(
        self, name: str, static_env: Mapping[str, str]
    ) -> RunnerResource:
        self.unbound_created.append((name, dict(static_env)))
        raise AssertionError("Kubernetes orchestration must not create pool resources")

    async def bind(
        self, resource: RunnerResource, new_name: str, binding: RunnerBinding
    ) -> RunnerResource:
        self.binds.append((resource, new_name, binding))
        raise AssertionError("Kubernetes orchestration must not bind pool resources")

    async def refresh_token(self, resource: RunnerResource, token: str) -> bool:
        self.refreshes.append((resource, token))
        return True

    async def pool_is_unbound(self, resource: RunnerResource) -> bool:
        return False

    async def get(self, resource_id: str) -> RunnerResource | None:
        self.get_calls.append(resource_id)
        return self.resources.get(resource_id)

    async def list(self) -> list[RunnerResource]:
        return list(self.resources.values())

    async def delete(self, resource_id: str, uid: str | None = None) -> None:
        self.deleted.append((resource_id, uid))
        current = self.resources.get(resource_id)
        if current is not None and (uid is None or current.uid == uid):
            self.resources.pop(resource_id)

    async def close(self) -> None:
        self.closed = True

    async def ready(self) -> None:
        return None


class FakeContainers:
    def __init__(self, container: FakeContainer | None = None) -> None:
        self.create = AsyncMock(return_value=container)
        self.get = AsyncMock(return_value=container)
        self.list = AsyncMock(return_value=[])


class FakeDocker:
    def __init__(self, container: FakeContainer | None = None) -> None:
        self.containers = FakeContainers(container)
        self.close = AsyncMock()


@pytest.fixture
def fake_redis() -> FakeRedis:
    original = redis_client._client
    redis_client._client = FakeRedis()
    yield redis_client._client  # type: ignore[misc]
    redis_client._client = original


def make_manager(container: FakeContainer | None = None) -> SessionManager:
    return SessionManager(docker=FakeDocker(container))  # type: ignore[arg-type]


def kubernetes_resource(
    resource_id: str,
    *,
    uid: str,
    ip_address: str | None = "10.42.0.20",
    state: str = "running",
    ready: bool = True,
    created_at: float | None = None,
    labels: dict[str, str] | None = None,
) -> RunnerResource:
    return RunnerResource(
        resource_id=resource_id,
        name=resource_id,
        uid=uid,
        ip_address=ip_address,
        state=state,
        ready=ready,
        created_at=time.time() if created_at is None else created_at,
        labels=labels
        if labels is not None
        else {
            **KUBERNETES_RUNNER_LABELS,
            "aurum-ai.conversation": resource_id.removeprefix("runner-"),
        },
    )


def test_factory_selects_kubernetes(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.services.runner_runtime import (
        KubernetesRunnerRuntime,
        create_runner_runtime,
    )

    monkeypatch.setattr(config_settings, "RUNNER_RUNTIME", "kubernetes")

    runtime = create_runner_runtime(config_settings)

    assert isinstance(runtime, KubernetesRunnerRuntime)


def test_factory_keeps_docker_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.services.runner_runtime import create_runner_runtime

    monkeypatch.setattr(config_settings, "RUNNER_RUNTIME", "docker")

    assert isinstance(create_runner_runtime(config_settings), DockerRunnerRuntime)


@pytest.mark.asyncio
async def test_get_or_create_creates_container(fake_redis: FakeRedis) -> None:
    container = FakeContainer("c123")
    manager = make_manager(container)

    ref = await manager.get_or_create("conv-1", "user-1", "org-1")

    assert isinstance(ref, ContainerRef)
    assert ref.container_id == "c123"
    assert ref.conversation_id == "conv-1"
    assert ref.ip_address == "172.20.0.5"
    container.start.assert_awaited_once()

    config = manager.docker.containers.create.call_args.kwargs["config"]
    assert config["Image"] == "business-runner:latest"
    # The label is what lets sweep_orphans() recognise the container as ours
    # from Docker alone, with no Redis bookkeeping to consult.
    assert config["Labels"] == RUNNER_LABELS
    assert config["HostConfig"]["Memory"] == 512 * 1024 * 1024
    assert config["HostConfig"]["CpuQuota"] == 100000
    env_dict = {k: v for k, v in (item.split("=", 1) for item in config["Env"])}
    assert env_dict["CONVERSATION_ID"] == "conv-1"
    assert env_dict["USER_ID"] == "user-1"
    assert env_dict["ORG_ID"] == "org-1"
    assert env_dict["TASK_QUEUE"] == "session-conv-1"
    decoded = session_token_service.decode(env_dict["SESSION_TOKEN"])
    assert decoded["conversation_id"] == "conv-1"
    assert decoded["sub"] == "user-1"
    assert decoded["org_id"] == "org-1"
    assert decoded["type"] == "session"

    stored = json.loads(fake_redis._hash["org-1:conv-1"])
    assert stored["container_id"] == "c123"
    assert stored["conversation_id"] == "conv-1"


@pytest.mark.asyncio
async def test_get_or_create_stores_versioned_docker_runner_record(
    fake_redis: FakeRedis,
) -> None:
    manager = make_manager(FakeContainer("cold-c1"))

    await manager.get_or_create("conv-1", "user-1", "org-1")

    stored = json.loads(fake_redis._hash["org-1:conv-1"])
    assert stored == {
        "version": 2,
        "generation": 1,
        "runtime": "docker",
        "resource_id": "cold-c1",
        "resource_name": "runner-conv-1",
        "resource_uid": "cold-c1",
        "container_id": "cold-c1",
        "conversation_id": "conv-1",
        "ip_address": "172.20.0.5",
        "last_active": ANY,
        "token_issued_at": ANY,
    }


@pytest.mark.asyncio
async def test_get_or_create_returns_existing_container(fake_redis: FakeRedis) -> None:
    container = FakeContainer("c456")
    manager = make_manager(container)
    fake_redis._hash["-:conv-2"] = json.dumps(
        {
            "container_id": "c456",
            "conversation_id": "conv-2",
            "ip_address": "10.0.0.2",
            "token_issued_at": time.time(),
        }
    )

    ref = await manager.get_or_create("conv-2", "user-2", None)

    assert ref.container_id == "c456"
    assert ref.ip_address == "10.0.0.2"
    manager.docker.containers.create.assert_not_called()


@pytest.mark.asyncio
async def test_get_or_create_reuses_legacy_docker_runner_record(
    fake_redis: FakeRedis,
) -> None:
    manager = make_manager(FakeContainer("legacy-c1"))
    fake_redis._hash["-:conv-legacy"] = json.dumps(
        {
            "container_id": "legacy-c1",
            "conversation_id": "conv-legacy",
            "ip_address": "10.0.0.2",
            "token_issued_at": time.time(),
        }
    )

    ref = await manager.get_or_create("conv-legacy", "user-1", None)

    assert ref == ContainerRef("legacy-c1", "conv-legacy", "10.0.0.2")
    manager.docker.containers.create.assert_not_called()


@pytest.mark.asyncio
async def test_get_or_create_refreshes_stale_session_token(fake_redis: FakeRedis) -> None:
    """Regression test: a session token (30-min TTL) was only ever issued at
    container create/claim time — a conversation active long enough to keep
    the container alive past that TTL used to just start 401ing. See
    the security review."""
    container = FakeContainer("c-longlived")
    manager = make_manager(container)
    stale_issued_at = time.time() - (config_settings.RUNNER_TOKEN_REFRESH_AFTER_SECONDS + 1)
    fake_redis._hash["org-1:conv-longlived"] = json.dumps(
        {
            "container_id": "c-longlived",
            "conversation_id": "conv-longlived",
            "ip_address": "10.0.0.9",
            "token_issued_at": stale_issued_at,
        }
    )
    factory, http_client = fake_async_client_factory(post_response=FakeHttpResponse(200))

    with patch("src.services.runner_runtime.docker.httpx.AsyncClient", factory):
        ref = await manager.get_or_create("conv-longlived", "user-1", "org-1")

    assert ref.container_id == "c-longlived"
    refresh_url = http_client.post.call_args.args[0]
    assert refresh_url == "http://runner-conv-longlived:8080/refresh-token"
    refreshed_token = http_client.post.call_args.kwargs["json"]["session_token"]
    decoded = session_token_service.decode(refreshed_token)
    assert decoded["conversation_id"] == "conv-longlived"

    stored = json.loads(fake_redis._hash["org-1:conv-longlived"])
    assert stored["token_issued_at"] > stale_issued_at


@pytest.mark.asyncio
async def test_get_or_create_cleans_stale_redis_entry(fake_redis: FakeRedis) -> None:
    container = FakeContainer("c999")
    manager = make_manager(container)
    fake_redis._hash["org-1:conv-stale"] = json.dumps(
        {
            "container_id": "c-stale",
            "conversation_id": "conv-stale",
            "ip_address": "10.0.0.3",
        }
    )
    manager.docker.containers.get.side_effect = DockerError(404, "No such container")

    ref = await manager.get_or_create("conv-stale", "user-1", "org-1")

    assert ref.container_id == "c999"
    stored = json.loads(fake_redis._hash["org-1:conv-stale"])
    assert stored["container_id"] == "c999"
    assert stored["ip_address"] == "172.20.0.5"
    manager.docker.containers.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_or_create_removes_dead_container_before_recreate(fake_redis: FakeRedis) -> None:
    """Regression test: a found-but-not-running container used to only be
    dropped from Redis, never actually deleted from Docker — the cold-start
    path then tried to create a new container under the same fixed name
    (f"runner-{conversation_id}") and hit a 409 Conflict forever. See
    the security review."""
    old_container = FakeContainer("c-old", status="exited")
    new_container = FakeContainer("c-new")
    manager = make_manager()
    manager.docker.containers.get = AsyncMock(return_value=old_container)
    manager.docker.containers.create = AsyncMock(return_value=new_container)
    fake_redis._hash["org-1:conv-dead"] = json.dumps(
        {"container_id": "c-old", "conversation_id": "conv-dead", "ip_address": "10.0.0.9"}
    )

    ref = await manager.get_or_create("conv-dead", "user-1", "org-1")

    old_container.kill.assert_awaited_once()
    old_container.delete.assert_awaited_once_with(force=True)
    assert ref.container_id == "c-new"
    stored = json.loads(fake_redis._hash["org-1:conv-dead"])
    assert stored["container_id"] == "c-new"


@pytest.mark.asyncio
async def test_get_or_create_serializes_concurrent_calls_for_same_conversation(
    fake_redis: FakeRedis,
) -> None:
    """Regression test: two concurrent first messages
    for the same conversation both used to pass the "no existing container"
    check and both hit Docker's create — the loser got a 409 Conflict (cold
    path) after the message was already saved to the DB, or (pool path)
    leaked a zombie container that no reaper ever finds. A per-conversation
    Redis lock now serializes get_or_create, so the second caller sees the
    first one's result instead of racing it."""
    container = FakeContainer("c-shared")
    manager = make_manager(container)

    ref1, ref2 = await asyncio.gather(
        manager.get_or_create("conv-race", "user-1", "org-1"),
        manager.get_or_create("conv-race", "user-1", "org-1"),
    )

    assert ref1.container_id == ref2.container_id == "c-shared"
    manager.docker.containers.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_or_create_handles_corrupted_redis_entry(fake_redis: FakeRedis) -> None:
    container = FakeContainer("c888")
    manager = make_manager(container)
    fake_redis._hash["org-1:conv-bad"] = "not-json"

    ref = await manager.get_or_create("conv-bad", "user-1", "org-1")

    assert ref.container_id == "c888"
    stored = json.loads(fake_redis._hash["org-1:conv-bad"])
    assert stored["container_id"] == "c888"
    assert stored["ip_address"] == "172.20.0.5"
    manager.docker.containers.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_or_create_uses_env_resource_limits(fake_redis: FakeRedis) -> None:
    container = FakeContainer("c777")
    manager = make_manager(container)
    with patch.object(config_settings, "RUNNER_MEMORY_LIMIT", 268435456), patch.object(
        config_settings, "RUNNER_CPU_QUOTA", 50000
    ):
        await manager.get_or_create("conv-env", "user-1", "org-1")

    config = manager.docker.containers.create.call_args.kwargs["config"]
    assert config["HostConfig"]["Memory"] == 268435456
    assert config["HostConfig"]["CpuQuota"] == 50000


@pytest.mark.asyncio
async def test_close_closes_owned_docker_client() -> None:
    docker = FakeDocker()
    runtime = DockerRunnerRuntime(docker)  # type: ignore[arg-type]
    runtime._owns_docker = True
    manager = SessionManager(runtime=runtime)

    await manager.close()

    docker.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_does_not_close_injected_docker_client() -> None:
    docker = FakeDocker()
    manager = SessionManager(docker=docker)  # type: ignore[arg-type]
    assert isinstance(manager.runtime, DockerRunnerRuntime)
    assert manager.runtime._owns_docker is False

    await manager.close()

    docker.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_kubernetes_get_or_create_creates_bound_resource_and_versioned_record(
    fake_redis: FakeRedis,
) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)
    manager = SessionManager(runtime=runtime)

    ref = await manager.get_or_create("conv-k8s-create", "user-1", "org-1")

    assert ref == ContainerRef("runner-conv-k8s-create", "conv-k8s-create", "10.42.0.11")
    name, static_env, binding = runtime.created[0]
    assert name == "runner-conv-k8s-create"
    assert "SESSION_TOKEN" not in static_env
    assert binding.conversation_id == "conv-k8s-create"
    assert binding.user_id == "user-1"
    assert binding.org_id == "org-1"
    assert binding.task_queue == "session-conv-k8s-create"
    decoded = session_token_service.decode(binding.session_token)
    assert decoded["conversation_id"] == "conv-k8s-create"
    assert set(decoded) == {"sub", "conversation_id", "org_id", "exp", "type", "generation"}

    stored = json.loads(fake_redis._hash["org-1:conv-k8s-create"])
    assert stored == {
        "version": 2,
        "generation": 1,
        "runtime": "kubernetes",
        "resource_id": "runner-conv-k8s-create",
        "resource_name": "runner-conv-k8s-create",
        "resource_uid": "uid-created-1",
        "conversation_id": "conv-k8s-create",
        "ip_address": "10.42.0.11",
        "last_active": ANY,
        "token_issued_at": ANY,
    }
    assert "container_id" not in stored


@pytest.mark.asyncio
async def test_kubernetes_binding_uses_identity_only_token(
    fake_redis: FakeRedis,
) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)
    manager = SessionManager(runtime=runtime)

    await manager.get_or_create("conv-k8s-live", "user-live", "org-live")

    decoded = session_token_service.decode(runtime.created[0][2].session_token)
    assert set(decoded) == {"sub", "conversation_id", "org_id", "exp", "type", "generation"}


@pytest.mark.asyncio
async def test_kubernetes_get_or_create_lock_covers_full_provisioning_budget(
    fake_redis: FakeRedis,
) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)
    manager = SessionManager(runtime=runtime)

    with patch.object(
        config_settings, "KUBERNETES_RUNNER_READY_TIMEOUT_SECONDS", 60
    ):
        await manager.get_or_create("conv-lock-budget", "user-1", "org-1")

    _, timeout, blocking_timeout = fake_redis.lock_calls[-1]
    assert timeout is not None and timeout >= 134.75
    assert blocking_timeout is not None and blocking_timeout >= timeout


@pytest.mark.asyncio
async def test_kubernetes_lock_cleanup_margin_scales_with_api_retry_override(
    fake_redis: FakeRedis,
) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)
    manager = SessionManager(runtime=runtime)

    with (
        patch.object(config_settings, "KUBERNETES_RUNNER_READY_TIMEOUT_SECONDS", 60),
        patch.object(config_settings, "KUBERNETES_API_RETRY_ATTEMPTS", 6),
    ):
        await manager.get_or_create("conv-lock-retries", "user-1", "org-1")

    _, timeout, blocking_timeout = fake_redis.lock_calls[-1]
    # operation 110s + cleanup: 6*3s API attempts, 5.75s backoff,
    # 10s deletion wait and 5s scheduling slack = 148.75s minimum.
    assert timeout is not None and timeout >= 148.75
    assert blocking_timeout is not None and blocking_timeout >= timeout


@pytest.mark.asyncio
async def test_kubernetes_get_or_create_bounds_hung_existing_record_lookup_inside_lease(
    fake_redis: FakeRedis,
) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)

    async def hang_forever(resource_id: str) -> RunnerResource | None:
        await asyncio.sleep(3600)
        raise AssertionError(resource_id)

    runtime.get = hang_forever  # type: ignore[method-assign]
    manager = SessionManager(runtime=runtime)
    fake_redis._hash["org-1:conv-hung-get"] = json.dumps(
        {
            "version": 2,
        "generation": 1,
            "runtime": "kubernetes",
            "resource_id": "runner-conv-hung-get",
            "resource_name": "runner-conv-hung-get",
            "resource_uid": "uid-hung-get",
            "conversation_id": "conv-hung-get",
        }
    )

    with (
        patch.object(config_settings, "KUBERNETES_RUNNER_READY_TIMEOUT_SECONDS", 0.01),
        patch(
            "src.services.session_manager._KUBERNETES_RECONCILIATION_BUDGET_SECONDS",
            0.01,
        ),
        pytest.raises(RunnerRuntimeError, match="timed out"),
    ):
        await asyncio.wait_for(
            manager.get_or_create("conv-hung-get", "user-1", "org-1"),
            timeout=0.2,
        )

    _, lock_timeout, _ = fake_redis.lock_calls[-1]
    assert lock_timeout is not None and lock_timeout > 0.02
    assert runtime.created == []


@pytest.mark.asyncio
async def test_kubernetes_get_or_create_bounds_hung_stale_delete_inside_lease(
    fake_redis: FakeRedis,
) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)
    runtime.resources["runner-conv-hung-delete"] = kubernetes_resource(
        "runner-conv-hung-delete",
        uid="uid-hung-delete",
        state="failed",
        ready=False,
    )

    async def hang_forever(resource_id: str, uid: str | None = None) -> None:
        await asyncio.sleep(3600)
        raise AssertionError((resource_id, uid))

    runtime.delete = hang_forever  # type: ignore[method-assign]
    manager = SessionManager(runtime=runtime)
    fake_redis._hash["org-1:conv-hung-delete"] = json.dumps(
        {
            "version": 2,
        "generation": 1,
            "runtime": "kubernetes",
            "resource_id": "runner-conv-hung-delete",
            "resource_name": "runner-conv-hung-delete",
            "resource_uid": "uid-hung-delete",
            "conversation_id": "conv-hung-delete",
        }
    )

    with (
        patch.object(config_settings, "KUBERNETES_RUNNER_READY_TIMEOUT_SECONDS", 0.01),
        patch(
            "src.services.session_manager._KUBERNETES_RECONCILIATION_BUDGET_SECONDS",
            0.01,
        ),
        pytest.raises(RunnerRuntimeError, match="timed out"),
    ):
        await asyncio.wait_for(
            manager.get_or_create("conv-hung-delete", "user-1", "org-1"),
            timeout=0.2,
        )

    assert runtime.created == []


@pytest.mark.asyncio
async def test_kubernetes_hung_reconciliation_releases_before_concurrent_create(
    fake_redis: FakeRedis,
) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)
    get_calls = 0

    async def hang_once(resource_id: str) -> RunnerResource | None:
        nonlocal get_calls
        get_calls += 1
        if get_calls == 1:
            await asyncio.sleep(3600)
        return runtime.resources.get(resource_id)

    runtime.get = hang_once  # type: ignore[method-assign]
    manager = SessionManager(runtime=runtime)
    fake_redis._hash["org-1:conv-reconcile-race"] = json.dumps(
        {
            "version": 2,
        "generation": 1,
            "runtime": "kubernetes",
            "resource_id": "runner-conv-reconcile-race",
            "resource_name": "runner-conv-reconcile-race",
            "resource_uid": "uid-missing",
            "conversation_id": "conv-reconcile-race",
        }
    )

    with (
        patch.object(config_settings, "KUBERNETES_RUNNER_READY_TIMEOUT_SECONDS", 0.01),
        patch(
            "src.services.session_manager._KUBERNETES_RECONCILIATION_BUDGET_SECONDS",
            0.01,
        ),
    ):
        results = await asyncio.gather(
            manager.get_or_create("conv-reconcile-race", "user-1", "org-1"),
            manager.get_or_create("conv-reconcile-race", "user-1", "org-1"),
            return_exceptions=True,
        )

    assert sum(isinstance(result, RunnerRuntimeError) for result in results) == 1
    assert sum(isinstance(result, ContainerRef) for result in results) == 1
    assert len(runtime.created) == 1


@pytest.mark.asyncio
async def test_kubernetes_get_or_create_reuse_verifies_uid_and_refreshes_token(
    fake_redis: FakeRedis,
) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)
    runtime.resources["runner-conv-k8s-reuse"] = kubernetes_resource(
        "runner-conv-k8s-reuse", uid="uid-k8s-reuse", ip_address="10.42.0.88"
    )
    manager = SessionManager(runtime=runtime)
    stale_last_active = time.time() - 10_000
    stale_token_issued_at = (
        time.time() - config_settings.RUNNER_TOKEN_REFRESH_AFTER_SECONDS - 10
    )
    fake_redis._hash["org-1:conv-k8s-reuse"] = json.dumps(
        {
            "version": 2,
        "generation": 1,
            "runtime": "kubernetes",
            "resource_id": "runner-conv-k8s-reuse",
            "resource_name": "runner-conv-k8s-reuse",
            "resource_uid": "uid-k8s-reuse",
            "conversation_id": "conv-k8s-reuse",
            "ip_address": "10.42.0.12",
            "last_active": stale_last_active,
            "token_issued_at": stale_token_issued_at,
        }
    )

    ref = await manager.get_or_create("conv-k8s-reuse", "user-1", "org-1")

    assert ref == ContainerRef("runner-conv-k8s-reuse", "conv-k8s-reuse", "10.42.0.88")
    assert runtime.created == []
    refreshed_resource, refreshed_token = runtime.refreshes[0]
    assert refreshed_resource.uid == "uid-k8s-reuse"
    decoded = session_token_service.decode(refreshed_token)
    assert decoded["conversation_id"] == "conv-k8s-reuse"

    stored = json.loads(fake_redis._hash["org-1:conv-k8s-reuse"])
    assert stored["last_active"] > stale_last_active
    assert stored["token_issued_at"] > stale_token_issued_at
    assert stored["resource_uid"] == "uid-k8s-reuse"


@pytest.mark.asyncio
async def test_refresh_owned_waiting_runtime_rotates_token_without_creating_resource(
    fake_redis: FakeRedis,
) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)
    runtime.resources["runner-conv-waiting"] = kubernetes_resource(
        "runner-conv-waiting", uid="uid-waiting"
    )
    manager = SessionManager(runtime=runtime)
    stale_issued_at = (
        time.time() - config_settings.RUNNER_TOKEN_REFRESH_AFTER_SECONDS - 10
    )
    fake_redis._hash["org-1:conv-waiting"] = json.dumps(
        {
            "version": 2,
        "generation": 1,
            "runtime": "kubernetes",
            "resource_id": "runner-conv-waiting",
            "resource_name": "runner-conv-waiting",
            "resource_uid": "uid-waiting",
            "conversation_id": "conv-waiting",
            "last_active": time.time() - 10_000,
            "token_issued_at": stale_issued_at,
        }
    )

    maintained = await manager.refresh_owned_waiting_runtime(
        "conv-waiting", "user-1", "org-1"
    )

    assert maintained is True
    assert runtime.created == []
    assert len(runtime.refreshes) == 1
    resource, token = runtime.refreshes[0]
    assert resource.uid == "uid-waiting"
    assert session_token_service.decode(token)["sub"] == "user-1"
    stored = json.loads(fake_redis._hash["org-1:conv-waiting"])
    assert stored["last_active"] > time.time() - 10
    assert stored["token_issued_at"] > stale_issued_at


@pytest.mark.asyncio
async def test_refresh_owned_waiting_runtime_does_not_recreate_missing_resource(
    fake_redis: FakeRedis,
) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)
    manager = SessionManager(runtime=runtime)
    fake_redis._hash["org-1:conv-missing"] = json.dumps(
        {
            "version": 2,
        "generation": 1,
            "runtime": "kubernetes",
            "resource_id": "runner-conv-missing",
            "resource_name": "runner-conv-missing",
            "resource_uid": "uid-missing",
            "conversation_id": "conv-missing",
            "token_issued_at": 0,
        }
    )

    maintained = await manager.refresh_owned_waiting_runtime(
        "conv-missing", "user-1", "org-1"
    )

    assert maintained is False
    assert runtime.created == []
    assert runtime.refreshes == []


@pytest.mark.asyncio
async def test_refresh_owned_waiting_runtime_rejects_reused_name_with_wrong_uid(
    fake_redis: FakeRedis,
) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)
    runtime.resources["runner-conv-reused"] = kubernetes_resource(
        "runner-conv-reused", uid="uid-new"
    )
    manager = SessionManager(runtime=runtime)
    fake_redis._hash["org-1:conv-reused"] = json.dumps(
        {
            "version": 2,
        "generation": 1,
            "runtime": "kubernetes",
            "resource_id": "runner-conv-reused",
            "resource_name": "runner-conv-reused",
            "resource_uid": "uid-old",
            "conversation_id": "conv-reused",
            "token_issued_at": 0,
        }
    )

    maintained = await manager.refresh_owned_waiting_runtime(
        "conv-reused", "user-1", "org-1"
    )

    assert maintained is False
    assert runtime.created == []
    assert runtime.refreshes == []


@pytest.mark.asyncio
async def test_refresh_owned_waiting_runtime_rejects_wrong_kubernetes_label(
    fake_redis: FakeRedis,
) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)
    runtime.resources["runner-conv-wrong-label"] = kubernetes_resource(
        "runner-conv-wrong-label",
        uid="uid-shared",
        labels={
            **KUBERNETES_RUNNER_LABELS,
            "aurum-ai.conversation": "another-conversation",
        },
    )
    manager = SessionManager(runtime=runtime)
    fake_redis._hash["org-1:conv-wrong-label"] = json.dumps(
        {
            "version": 2,
        "generation": 1,
            "runtime": "kubernetes",
            "resource_id": "runner-conv-wrong-label",
            "resource_name": "runner-conv-wrong-label",
            "resource_uid": "uid-shared",
            "conversation_id": "conv-wrong-label",
            "token_issued_at": 0,
        }
    )

    maintained = await manager.refresh_owned_waiting_runtime(
        "conv-wrong-label", "user-1", "org-1"
    )

    assert maintained is False
    assert runtime.refreshes == []


@pytest.mark.asyncio
async def test_docker_refresh_owned_waiting_runtime_never_creates_container(
    fake_redis: FakeRedis,
) -> None:
    container = FakeContainer("c-docker-wait")
    manager = make_manager(container)
    fake_redis._hash["org-1:conv-docker-wait"] = json.dumps(
        {
            "version": 2,
        "generation": 1,
            "runtime": "docker",
            "resource_id": "c-docker-wait",
            "resource_name": "runner-conv-docker-wait",
            "resource_uid": "c-docker-wait",
            "container_id": "c-docker-wait",
            "conversation_id": "conv-docker-wait",
            "token_issued_at": 0,
        }
    )
    factory, http_client = fake_async_client_factory(
        post_response=FakeHttpResponse(200)
    )

    with patch("src.services.runner_runtime.docker.httpx.AsyncClient", factory):
        maintained = await manager.refresh_owned_waiting_runtime(
            "conv-docker-wait", "user-1", "org-1"
        )

    assert maintained is True
    manager.docker.containers.create.assert_not_called()
    assert http_client.post.call_args.args[0] == (
        "http://runner-conv-docker-wait:8080/refresh-token"
    )


@pytest.mark.asyncio
async def test_kubernetes_get_or_create_recreates_when_stored_uid_mismatches(
    fake_redis: FakeRedis,
) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)
    runtime.resources["runner-conv-k8s-stale"] = kubernetes_resource(
        "runner-conv-k8s-stale", uid="uid-reused-name"
    )
    manager = SessionManager(runtime=runtime)
    fake_redis._hash["org-1:conv-k8s-stale"] = json.dumps(
        {
            "version": 2,
        "generation": 1,
            "runtime": "kubernetes",
            "resource_id": "runner-conv-k8s-stale",
            "resource_name": "runner-conv-k8s-stale",
            "resource_uid": "uid-stale-record",
            "conversation_id": "conv-k8s-stale",
            "ip_address": "10.42.0.12",
            "last_active": time.time(),
            "token_issued_at": time.time(),
        }
    )

    ref = await manager.get_or_create("conv-k8s-stale", "user-1", "org-1")

    assert ref.container_id == "runner-conv-k8s-stale"
    # A same-name Pod with a new UID is not ours: a UID-precondition delete
    # would conflict in Kubernetes, so only the stale Redis record is replaced.
    assert runtime.deleted == []
    assert len(runtime.created) == 1
    stored = json.loads(fake_redis._hash["org-1:conv-k8s-stale"])
    assert stored["resource_uid"] == "uid-created-1"
    assert stored["resource_uid"] != "uid-stale-record"


@pytest.mark.asyncio
async def test_kubernetes_get_or_create_reconciles_running_not_ready_pod_without_delete(
    fake_redis: FakeRedis,
) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)
    resource = kubernetes_resource("runner-conv-k8s-recover", uid="uid-recover")
    resource = RunnerResource(
        resource.resource_id,
        resource.name,
        resource.uid,
        resource.ip_address,
        "running",
        False,
        resource.created_at,
        resource.labels,
    )
    runtime.resources[resource.resource_id] = resource
    manager = SessionManager(runtime=runtime)
    fake_redis._hash["org-1:conv-k8s-recover"] = json.dumps(
        {
            "version": 2,
        "generation": 1,
            "runtime": "kubernetes",
            "resource_id": resource.resource_id,
            "resource_name": resource.name,
            "resource_uid": resource.uid,
            "conversation_id": "conv-k8s-recover",
            "ip_address": resource.ip_address,
            "last_active": time.time(),
            "token_issued_at": time.time(),
        }
    )

    await manager.get_or_create("conv-k8s-recover", "user-1", "org-1")

    assert runtime.deleted == []


@pytest.mark.asyncio
async def test_kubernetes_get_or_create_recreates_instead_of_interpreting_docker_record(
    fake_redis: FakeRedis,
) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)
    runtime.resources["docker-container-id"] = kubernetes_resource(
        "docker-container-id", uid="uid-accidental-name"
    )
    manager = SessionManager(runtime=runtime)
    fake_redis._hash["org-1:conv-k8s-legacy"] = json.dumps(
        {
            "container_id": "docker-container-id",
            "conversation_id": "conv-k8s-legacy",
            "ip_address": "172.20.0.5",
            "last_active": time.time(),
            "token_issued_at": time.time(),
        }
    )

    ref = await manager.get_or_create("conv-k8s-legacy", "user-1", "org-1")

    assert ref.container_id == "runner-conv-k8s-legacy"
    assert "docker-container-id" not in runtime.get_calls
    assert len(runtime.created) == 1
    assert runtime.deleted == []
    stored = json.loads(fake_redis._hash["org-1:conv-k8s-legacy"])
    assert stored["runtime"] == "kubernetes"
    assert stored["resource_id"] == "runner-conv-k8s-legacy"


@pytest.mark.asyncio
async def test_kubernetes_maintain_pool_never_creates_pool_resources(
    fake_redis: FakeRedis,
) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)
    manager = SessionManager(runtime=runtime)

    with patch.object(config_settings, "RUNNER_POOL_SIZE", 2), patch.object(
        config_settings, "RUNNER_POOL_MAINTAIN_INTERVAL_SECONDS", 60
    ):
        task = asyncio.create_task(manager.maintain_pool())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert runtime.unbound_created == []
    assert runtime.binds == []


@pytest.mark.asyncio
async def test_kubernetes_sweep_orphans_tracks_uids_not_reusable_names(
    fake_redis: FakeRedis,
) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)
    keep = kubernetes_resource(
        "runner-conv-keep",
        uid="uid-keep",
        created_at=time.time() - config_settings.RUNNER_ORPHAN_GRACE_SECONDS - 60,
    )
    same_name_new_uid = kubernetes_resource(
        "runner-conv-reused-name",
        uid="uid-new-untracked",
        created_at=time.time() - config_settings.RUNNER_ORPHAN_GRACE_SECONDS - 60,
    )
    young_untracked = kubernetes_resource(
        "runner-conv-young",
        uid="uid-young",
        created_at=time.time(),
    )
    not_runner = kubernetes_resource(
        "postgres",
        uid="uid-postgres",
        created_at=time.time() - config_settings.RUNNER_ORPHAN_GRACE_SECONDS - 60,
        labels={"app.kubernetes.io/instance": "aurum-ai", "app": "postgres"},
    )
    runtime.resources = {
        item.resource_id: item
        for item in (keep, same_name_new_uid, young_untracked, not_runner)
    }
    manager = SessionManager(runtime=runtime)
    fake_redis._hash["org-1:conv-keep"] = json.dumps(
        {
            "version": 2,
        "generation": 1,
            "runtime": "kubernetes",
            "resource_id": "runner-conv-keep",
            "resource_name": "runner-conv-keep",
            "resource_uid": "uid-keep",
            "conversation_id": "conv-keep",
            "last_active": time.time(),
            "token_issued_at": time.time(),
        }
    )
    fake_redis._hash["org-1:conv-reused-name"] = json.dumps(
        {
            "version": 2,
        "generation": 1,
            "runtime": "kubernetes",
            "resource_id": "runner-conv-reused-name",
            "resource_name": "runner-conv-reused-name",
            "resource_uid": "uid-old-tracked",
            "conversation_id": "conv-reused-name",
            "last_active": time.time(),
            "token_issued_at": time.time(),
        }
    )

    removed = await manager.sweep_orphans()

    assert removed == 1
    assert runtime.deleted == [("runner-conv-reused-name", "uid-new-untracked")]
    assert "runner-conv-keep" in runtime.resources
    assert "runner-conv-young" in runtime.resources
    assert "postgres" in runtime.resources


@pytest.mark.asyncio
async def test_kubernetes_terminate_uses_stored_uid(fake_redis: FakeRedis) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)
    runtime.resources["runner-conv-k8s-terminate"] = kubernetes_resource(
        "runner-conv-k8s-terminate", uid="uid-stored"
    )
    manager = SessionManager(runtime=runtime)
    fake_redis._hash["org-1:conv-k8s-terminate"] = json.dumps(
        {
            "version": 2,
        "generation": 1,
            "runtime": "kubernetes",
            "resource_id": "runner-conv-k8s-terminate",
            "resource_name": "runner-conv-k8s-terminate",
            "resource_uid": "uid-stored",
            "conversation_id": "conv-k8s-terminate",
            "last_active": time.time(),
            "token_issued_at": time.time(),
        }
    )

    await manager.terminate("org-1:conv-k8s-terminate")

    assert runtime.deleted == [("runner-conv-k8s-terminate", "uid-stored")]
    assert "org-1:conv-k8s-terminate" not in fake_redis._hash


@pytest.mark.asyncio
async def test_kubernetes_cleanup_drops_legacy_docker_record_without_lookup(
    fake_redis: FakeRedis,
) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)
    runtime.resources["docker-container-id"] = kubernetes_resource(
        "docker-container-id", uid="uid-unrelated"
    )
    manager = SessionManager(runtime=runtime)
    fake_redis._hash["org-1:conv-legacy-cleanup"] = json.dumps(
        {
            "container_id": "docker-container-id",
            "conversation_id": "conv-legacy-cleanup",
            "last_active": time.time(),
        }
    )

    removed = await manager.cleanup_stale()

    assert removed == 1
    assert runtime.get_calls == []
    assert runtime.deleted == []
    assert "org-1:conv-legacy-cleanup" not in fake_redis._hash


@pytest.mark.asyncio
async def test_kubernetes_cleanup_never_deletes_same_name_pod_with_new_uid(
    fake_redis: FakeRedis,
) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)
    runtime.resources["runner-conv-cleanup"] = kubernetes_resource(
        "runner-conv-cleanup", uid="uid-new"
    )
    manager = SessionManager(runtime=runtime)
    fake_redis._hash["org-1:conv-cleanup"] = json.dumps(
        {
            "version": 2,
        "generation": 1,
            "runtime": "kubernetes",
            "resource_id": "runner-conv-cleanup",
            "resource_name": "runner-conv-cleanup",
            "resource_uid": "uid-old",
            "conversation_id": "conv-cleanup",
            "last_active": time.time(),
        }
    )

    removed = await manager.cleanup_stale()

    assert removed == 1
    assert runtime.deleted == []
    assert "runner-conv-cleanup" in runtime.resources


@pytest.mark.asyncio
async def test_kubernetes_cleanup_never_deletes_wrong_label_with_same_uid(
    fake_redis: FakeRedis,
) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)
    runtime.resources["runner-conv-cleanup-label"] = kubernetes_resource(
        "runner-conv-cleanup-label",
        uid="uid-shared",
        labels={
            **KUBERNETES_RUNNER_LABELS,
            "aurum-ai.conversation": "different-conversation",
        },
    )
    manager = SessionManager(runtime=runtime)
    fake_redis._hash["org-1:conv-cleanup-label"] = json.dumps(
        {
            "version": 2,
        "generation": 1,
            "runtime": "kubernetes",
            "resource_id": "runner-conv-cleanup-label",
            "resource_name": "runner-conv-cleanup-label",
            "resource_uid": "uid-shared",
            "conversation_id": "conv-cleanup-label",
            "last_active": time.time(),
        }
    )

    removed = await manager.cleanup_stale()

    assert removed == 1
    assert runtime.deleted == []


@pytest.mark.asyncio
async def test_kubernetes_terminate_drops_legacy_record_without_runtime_delete(
    fake_redis: FakeRedis,
) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)
    runtime.resources["docker-container-id"] = kubernetes_resource(
        "docker-container-id", uid="uid-unrelated"
    )
    manager = SessionManager(runtime=runtime)
    fake_redis._hash["org-1:conv-legacy-terminate"] = json.dumps(
        {"container_id": "docker-container-id", "conversation_id": "conv-legacy-terminate"}
    )

    await manager.terminate("org-1:conv-legacy-terminate")

    assert runtime.get_calls == []
    assert runtime.deleted == []
    assert "org-1:conv-legacy-terminate" not in fake_redis._hash


@pytest.mark.asyncio
async def test_kubernetes_terminate_never_deletes_wrong_label_resource(
    fake_redis: FakeRedis,
) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)
    runtime.resources["runner-conv-terminate-label"] = kubernetes_resource(
        "runner-conv-terminate-label",
        uid="uid-shared",
        labels={
            **KUBERNETES_RUNNER_LABELS,
            "aurum-ai.conversation": "different-conversation",
        },
    )
    manager = SessionManager(runtime=runtime)
    fake_redis._hash["org-1:conv-terminate-label"] = json.dumps(
        {
            "version": 2,
        "generation": 1,
            "runtime": "kubernetes",
            "resource_id": "runner-conv-terminate-label",
            "resource_name": "runner-conv-terminate-label",
            "resource_uid": "uid-shared",
            "conversation_id": "conv-terminate-label",
        }
    )

    await manager.terminate("org-1:conv-terminate-label")

    assert runtime.deleted == []


@pytest.mark.asyncio
async def test_kubernetes_reuse_rejects_wrong_conversation_label(
    fake_redis: FakeRedis,
) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)
    runtime.resources["runner-conv-label"] = kubernetes_resource(
        "runner-conv-label",
        uid="uid-label",
        labels={
            **KUBERNETES_RUNNER_LABELS,
            "aurum-ai.conversation": "another-conversation",
        },
    )
    manager = SessionManager(runtime=runtime)
    fake_redis._hash["org-1:conv-label"] = json.dumps(
        {
            "version": 2,
        "generation": 1,
            "runtime": "kubernetes",
            "resource_id": "runner-conv-label",
            "resource_name": "runner-conv-label",
            "resource_uid": "uid-label",
            "conversation_id": "conv-label",
            "last_active": time.time(),
            "token_issued_at": time.time(),
        }
    )

    await manager.get_or_create("conv-label", "user-1", "org-1")

    assert runtime.refreshes == []
    assert runtime.deleted == []
    assert len(runtime.created) == 1

@pytest.mark.asyncio
async def test_reap_idle_terminates_idle_container(fake_redis: FakeRedis) -> None:
    import time

    container = FakeContainer("c-idle")
    manager = make_manager(container)
    fake_redis._hash["org-1:conv-idle"] = json.dumps(
        {
            "container_id": "c-idle",
            "conversation_id": "conv-idle",
            "ip_address": None,
            "last_active": time.time() - 10_000,
        }
    )

    reaped = await manager.reap_idle(idle_seconds=1800)

    assert reaped == 1
    container.kill.assert_awaited_once()
    container.delete.assert_awaited_once_with(force=True)
    assert "conv-idle" not in fake_redis._hash


@pytest.mark.asyncio
async def test_reap_idle_keeps_active_container(fake_redis: FakeRedis) -> None:
    import time

    container = FakeContainer("c-active")
    manager = make_manager(container)
    fake_redis._hash["org-1:conv-active"] = json.dumps(
        {
            "container_id": "c-active",
            "conversation_id": "conv-active",
            "ip_address": None,
            "last_active": time.time(),
        }
    )

    reaped = await manager.reap_idle(idle_seconds=1800)

    assert reaped == 0
    container.kill.assert_not_awaited()
    assert "org-1:conv-active" in fake_redis._hash


@pytest.mark.asyncio
async def test_reap_idle_keeps_durable_waiting_turn(fake_redis: FakeRedis) -> None:
    container = FakeContainer("c-waiting")
    wait_guard = AsyncMock(return_value=True)
    manager = SessionManager(docker=FakeDocker(container), wait_guard=wait_guard)  # type: ignore[arg-type]
    fake_redis._hash["org-1:conv-waiting"] = json.dumps(
        {
            "container_id": "c-waiting",
            "conversation_id": "conv-waiting",
            "last_active": time.time() - 10_000,
        }
    )

    reaped = await manager.reap_idle(idle_seconds=1800)

    assert reaped == 0
    wait_guard.assert_awaited_once_with("org-1", "conv-waiting")
    container.delete.assert_not_awaited()
    assert "org-1:conv-waiting" in fake_redis._hash


@pytest.mark.asyncio
async def test_reap_idle_fails_closed_when_wait_state_is_unavailable(
    fake_redis: FakeRedis,
) -> None:
    container = FakeContainer("c-unconfirmed")
    wait_guard = AsyncMock(side_effect=ConnectionError("database unavailable"))
    manager = SessionManager(docker=FakeDocker(container), wait_guard=wait_guard)  # type: ignore[arg-type]
    fake_redis._hash["org-1:conv-unconfirmed"] = json.dumps(
        {
            "container_id": "c-unconfirmed",
            "conversation_id": "conv-unconfirmed",
            "last_active": time.time() - 10_000,
        }
    )

    reaped = await manager.reap_idle(idle_seconds=1800)

    assert reaped == 0
    container.delete.assert_not_awaited()
    assert "org-1:conv-unconfirmed" in fake_redis._hash


@pytest.mark.asyncio
async def test_reap_idle_backfills_missing_timestamp(fake_redis: FakeRedis) -> None:
    container = FakeContainer("c-legacy")
    manager = make_manager(container)
    fake_redis._hash["org-1:conv-legacy"] = json.dumps(
        {
            "container_id": "c-legacy",
            "conversation_id": "conv-legacy",
            "ip_address": None,
        }
    )

    reaped = await manager.reap_idle(idle_seconds=1800)

    assert reaped == 0
    container.kill.assert_not_awaited()
    stored = json.loads(fake_redis._hash["org-1:conv-legacy"])
    assert "last_active" in stored


@pytest.mark.asyncio
async def test_terminate_removes_container(fake_redis: FakeRedis) -> None:
    container = FakeContainer("c789")
    manager = make_manager(container)
    fake_redis._hash["org-1:conv-3"] = json.dumps(
        {
            "container_id": "c789",
            "conversation_id": "conv-3",
            "ip_address": None,
        }
    )

    # terminate consumes the hash field, not conversation_id alone.
    await manager.terminate("org-1:conv-3")

    container.kill.assert_awaited_once()
    container.delete.assert_awaited_once_with(force=True)
    assert "org-1:conv-3" not in fake_redis._hash


# --------------------------------------------------------------------
# Warm pool
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_from_pool_returns_none_when_empty(fake_redis: FakeRedis) -> None:
    manager = make_manager(FakeContainer("unused"))

    result = await manager.claim_from_pool("conv-1", "user-1", "org-1")

    assert result is None


@pytest.mark.asyncio
async def test_claim_from_pool_binds_and_registers_container(fake_redis: FakeRedis) -> None:
    container = FakeContainer("pool-c1")
    manager = make_manager(container)
    fake_redis._lists[POOL_REDIS_KEY] = [
        json.dumps({"container_id": "pool-c1", "container_name": "runner-pool-abc", "created_at": 1.0})
    ]
    factory, http_client = fake_async_client_factory(post_response=FakeHttpResponse(200))

    with patch("src.services.runner_runtime.docker.httpx.AsyncClient", factory):
        ref = await manager.claim_from_pool("conv-1", "user-1", "org-1")

    assert ref is not None
    assert ref.container_id == "pool-c1"
    assert ref.conversation_id == "conv-1"
    manager.docker.containers.create.assert_not_called()

    bind_url, bind_payload = http_client.post.call_args.args[0], http_client.post.call_args.kwargs["json"]
    assert bind_url == "http://runner-pool-abc:8080/bind"
    assert bind_payload["conversation_id"] == "conv-1"
    assert bind_payload["task_queue"] == "session-conv-1"

    # Renamed away from the pool-style name so `docker ps` doesn't read as
    # "still idle" once it's actually serving a conversation.
    container.rename.assert_awaited_once_with("runner-conv-1")
    stored = json.loads(fake_redis._hash["org-1:conv-1"])
    assert stored["container_id"] == "pool-c1"
    assert stored["resource_name"] == "runner-conv-1"
    # Popped off the pool list, not left there for a second claim.
    assert await fake_redis.llen(POOL_REDIS_KEY) == 0


@pytest.mark.asyncio
async def test_claim_from_pool_discards_container_on_bind_failure(fake_redis: FakeRedis) -> None:
    container = FakeContainer("pool-c2")
    manager = make_manager(container)
    fake_redis._lists[POOL_REDIS_KEY] = [
        json.dumps({"container_id": "pool-c2", "container_name": "runner-pool-def", "created_at": 1.0})
    ]
    factory, _ = fake_async_client_factory(
        post_side_effect=httpx.ConnectError("connection refused")
    )

    with patch("src.services.runner_runtime.docker.httpx.AsyncClient", factory):
        ref = await manager.claim_from_pool("conv-2", "user-1", "org-1")

    assert ref is None
    container.kill.assert_awaited_once()
    container.delete.assert_awaited_once_with(force=True)
    assert "conv-2" not in fake_redis._hash


@pytest.mark.asyncio
async def test_claim_from_pool_ignores_malformed_created_at(fake_redis: FakeRedis) -> None:
    manager = make_manager(FakeContainer("unused"))
    fake_redis._lists[POOL_REDIS_KEY] = [
        json.dumps(
            {
                "container_id": "pool-bad-time",
                "container_name": "runner-pool-bad-time",
                "created_at": "not-a-timestamp",
            }
        )
    ]

    assert await manager.claim_from_pool("conv-bad", "user-1", "org-1") is None
    assert "org-1:conv-bad" not in fake_redis._hash


@pytest.mark.asyncio
async def test_get_or_create_uses_pool_when_enabled(fake_redis: FakeRedis) -> None:
    container = FakeContainer("pool-c3")
    manager = make_manager(container)
    fake_redis._lists[POOL_REDIS_KEY] = [
        json.dumps({"container_id": "pool-c3", "container_name": "runner-pool-ghi", "created_at": 1.0})
    ]
    factory, _ = fake_async_client_factory(post_response=FakeHttpResponse(200))

    with patch.object(config_settings, "RUNNER_POOL_SIZE", 2):
        with patch("src.services.runner_runtime.docker.httpx.AsyncClient", factory):
            ref = await manager.get_or_create("conv-3", "user-1", "org-1")

    assert ref.container_id == "pool-c3"
    manager.docker.containers.create.assert_not_called()


@pytest.mark.asyncio
async def test_get_or_create_falls_back_to_cold_start_when_pool_empty(
    fake_redis: FakeRedis,
) -> None:
    container = FakeContainer("cold-c1")
    manager = make_manager(container)

    with patch.object(config_settings, "RUNNER_POOL_SIZE", 2):
        ref = await manager.get_or_create("conv-4", "user-1", "org-1")

    assert ref.container_id == "cold-c1"
    manager.docker.containers.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_or_create_skips_pool_when_disabled(fake_redis: FakeRedis) -> None:
    container = FakeContainer("cold-c2")
    manager = make_manager(container)
    fake_redis._lists[POOL_REDIS_KEY] = [
        json.dumps({"container_id": "pool-unused", "container_name": "runner-pool-x", "created_at": 1.0})
    ]

    with patch.object(config_settings, "RUNNER_POOL_SIZE", 0):
        ref = await manager.get_or_create("conv-5", "user-1", "org-1")

    # Pool untouched — still has its entry — and the cold-start path ran.
    assert ref.container_id == "cold-c2"
    assert await fake_redis.llen(POOL_REDIS_KEY) == 1


@pytest.mark.asyncio
async def test_top_up_pool_creates_missing_containers(fake_redis: FakeRedis) -> None:
    container = FakeContainer("new-pool-c")
    manager = make_manager(container)
    factory, _ = fake_async_client_factory(get_response=FakeHttpResponse(200))

    with patch.object(config_settings, "RUNNER_POOL_SIZE", 2):
        with patch("src.services.runner_runtime.docker.httpx.AsyncClient", factory):
            created = await manager.top_up_pool()

    assert created == 2
    assert manager.docker.containers.create.await_count == 2
    assert await fake_redis.llen(POOL_REDIS_KEY) == 2


@pytest.mark.asyncio
async def test_top_up_pool_stores_docker_legacy_aliases(fake_redis: FakeRedis) -> None:
    manager = make_manager(FakeContainer("new-pool-c"))
    factory, _ = fake_async_client_factory(get_response=FakeHttpResponse(200))

    with patch.object(config_settings, "RUNNER_POOL_SIZE", 1):
        with patch("src.services.runner_runtime.docker.httpx.AsyncClient", factory):
            assert await manager.top_up_pool() == 1

    stored = json.loads(fake_redis._lists[POOL_REDIS_KEY][0])
    assert stored["resource_id"] == "new-pool-c"
    assert stored["resource_name"].startswith("runner-pool-")
    assert stored["container_id"] == "new-pool-c"
    assert stored["container_name"] == stored["resource_name"]


@pytest.mark.asyncio
async def test_top_up_pool_noop_when_disabled(fake_redis: FakeRedis) -> None:
    manager = make_manager(FakeContainer("unused"))

    with patch.object(config_settings, "RUNNER_POOL_SIZE", 0):
        created = await manager.top_up_pool()

    assert created == 0
    manager.docker.containers.create.assert_not_called()


@pytest.mark.asyncio
async def test_top_up_pool_tops_up_partial_pool(fake_redis: FakeRedis) -> None:
    container = FakeContainer("new-pool-c2")
    manager = make_manager(container)
    fake_redis._lists[POOL_REDIS_KEY] = [
        json.dumps({"container_id": "existing", "container_name": "runner-pool-existing", "created_at": 1.0})
    ]
    factory, _ = fake_async_client_factory(get_response=FakeHttpResponse(200))

    with patch.object(config_settings, "RUNNER_POOL_SIZE", 3):
        with patch("src.services.runner_runtime.docker.httpx.AsyncClient", factory):
            created = await manager.top_up_pool()

    assert created == 2
    assert await fake_redis.llen(POOL_REDIS_KEY) == 3


@pytest.mark.asyncio
async def test_top_up_pool_trims_excess_over_target(fake_redis: FakeRedis) -> None:
    container = FakeContainer("excess-c")
    manager = make_manager(container)
    fake_redis._lists[POOL_REDIS_KEY] = [
        json.dumps({"container_id": "e1", "container_name": "runner-pool-e1", "created_at": 1.0}),
        json.dumps({"container_id": "e2", "container_name": "runner-pool-e2", "created_at": 1.0}),
        json.dumps({"container_id": "e3", "container_name": "runner-pool-e3", "created_at": 1.0}),
        json.dumps({"container_id": "e4", "container_name": "runner-pool-e4", "created_at": 1.0}),
    ]

    with patch.object(config_settings, "RUNNER_POOL_SIZE", 2):
        created = await manager.top_up_pool()

    assert created == -2
    assert await fake_redis.llen(POOL_REDIS_KEY) == 2
    assert container.kill.await_count == 2
    assert container.delete.await_count == 2
    manager.docker.containers.create.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_stale_pool_removes_dead_containers(fake_redis: FakeRedis) -> None:
    manager = make_manager(FakeContainer("unused"))
    fake_redis._lists[POOL_REDIS_KEY] = [
        json.dumps({"container_id": "dead", "container_name": "runner-pool-dead", "created_at": 1.0})
    ]
    manager.docker.containers.get = AsyncMock(
        side_effect=DockerError(404, "No such container")
    )

    removed = await manager.cleanup_stale_pool()

    assert removed == 1
    assert await fake_redis.llen(POOL_REDIS_KEY) == 0


@pytest.mark.asyncio
async def test_cleanup_stale_pool_removes_already_bound_container(fake_redis: FakeRedis) -> None:
    container = FakeContainer("bound-c")
    manager = make_manager(container)
    fake_redis._lists[POOL_REDIS_KEY] = [
        json.dumps({"container_id": "bound-c", "container_name": "runner-pool-bound", "created_at": 1.0})
    ]
    factory, _ = fake_async_client_factory(
        get_response=FakeHttpResponse(200, {"bound": True})
    )

    with patch("src.services.runner_runtime.docker.httpx.AsyncClient", factory):
        removed = await manager.cleanup_stale_pool()

    assert removed == 1
    assert await fake_redis.llen(POOL_REDIS_KEY) == 0


@pytest.mark.asyncio
async def test_cleanup_stale_pool_keeps_healthy_unbound_container(fake_redis: FakeRedis) -> None:
    container = FakeContainer("healthy-c")
    manager = make_manager(container)
    fake_redis._lists[POOL_REDIS_KEY] = [
        json.dumps({"container_id": "healthy-c", "container_name": "runner-pool-healthy", "created_at": 1.0})
    ]
    factory, _ = fake_async_client_factory(
        get_response=FakeHttpResponse(200, {"bound": False})
    )

    with patch("src.services.runner_runtime.docker.httpx.AsyncClient", factory):
        removed = await manager.cleanup_stale_pool()

    assert removed == 0
    assert await fake_redis.llen(POOL_REDIS_KEY) == 1


@pytest.mark.asyncio
async def test_drain_pool_terminates_all_containers(fake_redis: FakeRedis) -> None:
    container = FakeContainer("drain-c")
    manager = make_manager(container)
    fake_redis._lists[POOL_REDIS_KEY] = [
        json.dumps({"container_id": "drain-c", "container_name": "runner-pool-1", "created_at": 1.0}),
        json.dumps({"container_id": "drain-c", "container_name": "runner-pool-2", "created_at": 1.0}),
    ]

    removed = await manager.drain_pool()

    assert removed == 2
    assert container.kill.await_count == 2
    assert container.delete.await_count == 2
    assert await fake_redis.llen(POOL_REDIS_KEY) == 0


@pytest.mark.asyncio
async def test_drain_pool_noop_when_empty(fake_redis: FakeRedis) -> None:
    manager = make_manager(FakeContainer("unused"))

    removed = await manager.drain_pool()

    assert removed == 0


@pytest.mark.asyncio
async def test_sweep_orphans_removes_dead_container_redis_forgot(fake_redis: FakeRedis) -> None:
    """The post-reboot case. The `redis` service has no volume, so a host
    reboot leaves Redis empty while Docker still holds the runner containers
    as Exited — and every other cleanup path starts from a Redis record, so
    none of them can even see those containers."""
    orphan = FakeListedContainer(
        "c-dead", "runner-pool-abc123", state="exited", labels=RUNNER_LABELS
    )
    manager = make_manager(FakeContainer("unused"))
    manager.docker.containers.list = AsyncMock(return_value=[orphan])
    manager.docker.containers.get = AsyncMock(return_value=orphan)

    removed = await manager.sweep_orphans()

    assert removed == 1
    orphan.delete.assert_awaited_once()
    assert manager.docker.containers.list.await_args.kwargs == {"all": True}


@pytest.mark.asyncio
async def test_sweep_orphans_keeps_containers_redis_still_tracks(fake_redis: FakeRedis) -> None:
    """A container is an orphan only because nothing points at it — both the
    per-conversation hash and the warm-pool list count as pointing at it."""
    in_session = FakeListedContainer("c-session", "runner-conv-1", labels=RUNNER_LABELS)
    in_pool = FakeListedContainer("c-pool", "runner-pool-xyz", labels=RUNNER_LABELS)
    manager = make_manager(FakeContainer("unused"))
    manager.docker.containers.list = AsyncMock(return_value=[in_session, in_pool])
    fake_redis._hash["org-1:conv-1"] = json.dumps(
        {"container_id": "c-session", "conversation_id": "conv-1", "last_active": time.time()}
    )
    fake_redis._lists[POOL_REDIS_KEY] = [
        json.dumps({"container_id": "c-pool", "container_name": "runner-pool-xyz", "created_at": 1.0})
    ]

    removed = await manager.sweep_orphans()

    assert removed == 0
    in_session.delete.assert_not_awaited()
    in_pool.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_orphans_spares_a_container_still_being_created(fake_redis: FakeRedis) -> None:
    """Both creation paths write their Redis record only after the container
    reports ready, so a young running container with no record may be one
    that get_or_create/_create_pool_container is still waiting on — deleting
    it would break the very request that created it."""
    just_created = FakeListedContainer(
        "c-young", "runner-pool-fresh", labels=RUNNER_LABELS, created=time.time()
    )
    manager = make_manager(FakeContainer("unused"))
    manager.docker.containers.list = AsyncMock(return_value=[just_created])

    removed = await manager.sweep_orphans()

    assert removed == 0
    just_created.kill.assert_not_awaited()
    just_created.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_orphans_removes_long_running_untracked_container(
    fake_redis: FakeRedis,
) -> None:
    """Past the grace period an untracked running container is a genuine
    zombie: get_or_create() only ever finds a conversation's container through
    its Redis record, so one nothing points at can never serve a chat again."""
    zombie = FakeListedContainer(
        "c-zombie",
        "runner-conv-lost",
        labels=RUNNER_LABELS,
        created=time.time() - config_settings.RUNNER_ORPHAN_GRACE_SECONDS - 60,
    )
    manager = make_manager(FakeContainer("unused"))
    manager.docker.containers.list = AsyncMock(return_value=[zombie])
    manager.docker.containers.get = AsyncMock(return_value=zombie)

    removed = await manager.sweep_orphans()

    assert removed == 1
    zombie.kill.assert_awaited_once()
    zombie.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_sweep_orphans_keeps_running_wait_after_redis_binding_expires(
    fake_redis: FakeRedis,
) -> None:
    waiting = FakeListedContainer(
        "c-waiting-orphan",
        "runner-conv-waiting-orphan",
        labels=RUNNER_LABELS,
        created=time.time() - config_settings.RUNNER_ORPHAN_GRACE_SECONDS - 60,
    )
    wait_guard = AsyncMock(return_value=True)
    manager = SessionManager(
        docker=FakeDocker(FakeContainer("unused")), wait_guard=wait_guard
    )  # type: ignore[arg-type]
    manager.docker.containers.list = AsyncMock(return_value=[waiting])

    removed = await manager.sweep_orphans()

    assert removed == 0
    wait_guard.assert_awaited_once_with(None, "conv-waiting-orphan")
    waiting.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_orphans_fails_closed_when_durable_wait_is_unavailable(
    fake_redis: FakeRedis,
) -> None:
    unconfirmed = FakeListedContainer(
        "c-unconfirmed-orphan",
        "runner-conv-unconfirmed-orphan",
        labels=RUNNER_LABELS,
        created=time.time() - config_settings.RUNNER_ORPHAN_GRACE_SECONDS - 60,
    )
    wait_guard = AsyncMock(side_effect=ConnectionError("database unavailable"))
    manager = SessionManager(
        docker=FakeDocker(FakeContainer("unused")), wait_guard=wait_guard
    )  # type: ignore[arg-type]
    manager.docker.containers.list = AsyncMock(return_value=[unconfirmed])

    removed = await manager.sweep_orphans()

    assert removed == 0
    unconfirmed.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_orphans_retains_bound_pool_name_when_conversation_is_unrecoverable(
    fake_redis: FakeRedis,
) -> None:
    # Docker labels are immutable. If a pool claim succeeded but rename
    # failed, losing Redis leaves a bound runtime named runner-pool-* and no
    # safe conversation ID with which to check durable wait state.
    unidentifiable = FakeListedContainer(
        "c-bound-pool-name",
        "runner-pool-old-name",
        labels=RUNNER_LABELS,
        created=time.time() - config_settings.RUNNER_ORPHAN_GRACE_SECONDS - 60,
    )
    wait_guard = AsyncMock(return_value=False)
    manager = SessionManager(
        docker=FakeDocker(FakeContainer("unused")), wait_guard=wait_guard
    )  # type: ignore[arg-type]
    manager.docker.containers.list = AsyncMock(return_value=[unidentifiable])
    manager.runtime.pool_is_unbound = AsyncMock(return_value=False)  # type: ignore[method-assign]

    removed = await manager.sweep_orphans()

    assert removed == 0
    wait_guard.assert_not_awaited()
    unidentifiable.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_kubernetes_sweep_keeps_running_wait_after_redis_binding_expires(
    fake_redis: FakeRedis,
) -> None:
    runtime = FakeRunnerRuntime(kind="kubernetes", supports_warm_pool=False)
    runtime.resources["runner-conv-k8s-orphan"] = kubernetes_resource(
        "runner-conv-k8s-orphan",
        uid="uid-k8s-orphan",
        created_at=time.time() - config_settings.RUNNER_ORPHAN_GRACE_SECONDS - 60,
    )
    wait_guard = AsyncMock(return_value=True)
    manager = SessionManager(runtime=runtime, wait_guard=wait_guard)

    removed = await manager.sweep_orphans()

    assert removed == 0
    wait_guard.assert_awaited_once_with(None, "conv-k8s-orphan")
    assert runtime.deleted == []


@pytest.mark.asyncio
async def test_sweep_orphans_ignores_containers_that_are_not_runners(
    fake_redis: FakeRedis,
) -> None:
    """The backend shares its Docker socket with the whole stack — the sweep
    must only ever touch containers it created itself."""
    postgres = FakeListedContainer("c-pg", "aurum-ai-postgres", state="exited")
    manager = make_manager(FakeContainer("unused"))
    manager.docker.containers.list = AsyncMock(return_value=[postgres])

    removed = await manager.sweep_orphans()

    assert removed == 0
    postgres.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_orphans_recognises_unlabelled_runner_by_name(
    fake_redis: FakeRedis,
) -> None:
    """Containers created before RUNNER_LABEL existed carry no label, and they
    are exactly the ones already stranded on a host — the name both creation
    paths use keeps them sweepable."""
    legacy = FakeListedContainer("c-legacy", "runner-pool-legacy", state="exited")
    manager = make_manager(FakeContainer("unused"))
    manager.docker.containers.list = AsyncMock(return_value=[legacy])
    manager.docker.containers.get = AsyncMock(return_value=legacy)

    removed = await manager.sweep_orphans()

    assert removed == 1
    legacy.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_sweep_orphans_does_nothing_when_redis_is_unreadable(
    fake_redis: FakeRedis,
) -> None:
    """Without the Redis side there is no way to tell an orphan from a live
    session's container, and deleting a live one mid-chat is far worse than
    leaving an orphan for the next pass."""
    container = FakeListedContainer("c-any", "runner-conv-9", labels=RUNNER_LABELS)
    manager = make_manager(FakeContainer("unused"))
    manager.docker.containers.list = AsyncMock(return_value=[container])
    fake_redis.hgetall = AsyncMock(side_effect=ConnectionError("redis down"))  # type: ignore[method-assign]

    removed = await manager.sweep_orphans()

    assert removed == 0
    container.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_orgs_with_the_same_conversation_id_do_not_collide(
    fake_redis: FakeRedis,
) -> None:
    """Tenant-prefixed fields prevent same-ID conversations from colliding.

    A bare conversation_id used to allow one tenant record to replace another;
    each organization now has its own field and owned container identity.
    """
    manager_a = make_manager(FakeContainer("container-org-a"))
    manager_b = make_manager(FakeContainer("container-org-b"))

    ref_a = await manager_a.get_or_create("shared-id", "user-a", "org-a")
    ref_b = await manager_b.get_or_create("shared-id", "user-b", "org-b")

    assert ref_a.container_id != ref_b.container_id
    assert set(fake_redis._hash) == {"org-a:shared-id", "org-b:shared-id"}
    assert json.loads(fake_redis._hash["org-a:shared-id"])["container_id"] == "container-org-a"
    assert json.loads(fake_redis._hash["org-b:shared-id"])["container_id"] == "container-org-b"
