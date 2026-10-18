import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, TypeVar, cast

from src.core.cache import redis_client
from src.core.config import settings
from src.services.runner_runtime import (
    DockerRunnerRuntime,
    RunnerBinding,
    RunnerResource,
    RunnerRuntime,
    RunnerRuntimeError,
    create_runner_runtime,
)
from src.services.session_token import session_token_service

logger = logging.getLogger(__name__)

POOL_REDIS_KEY = "runner_pool"

# One session_containers hash serves all organizations, but each field is
# "{org_id}:{conversation_id}", never conversation_id alone, so tenant
# bookkeeping cannot collide with another organization.
# Keep the hash shared: reaper, cleanup_stale and sweep_orphans enumerate
# it; separate tenant hashes would require a broader SCAN.
#
# runner_pool is deliberately not tenant-split: containers are still
# unbound and available to any caller; organization ownership appears
# only at bind time.
def session_field(org_id: str | None, conversation_id: str) -> str:
    return f"{org_id or '-'}:{conversation_id}"

# Stamped on every runner Docker container so a container can be recognised as
# ours from Docker alone, without Redis bookkeeping.
RUNNER_LABEL = "aurum-ai.component"
RUNNER_LABEL_VALUE = "conversation-runner"

# Both creation paths name their containers "runner-<conversation_id>" /
# "runner-pool-<hex>", so containers created before RUNNER_LABEL existed are
# still identifiable and therefore still sweepable.
RUNNER_NAME_PREFIX = "runner-"

# Docker reports these as "the container is not doing anything any more".
# Deliberately excludes "created": that is the state of a container between
# containers.create() and .start(), i.e. one its own creator is still working on.
_DEAD_STATES = frozenset({"exited", "dead", "removing"})

# The Kubernetes Redis lease must outlive the complete reconciliation path,
# not just Pod readiness. The outer budget also bounds Redis lookup/bookkeeping
# and runtime get/delete calls; the lease margin leaves room for cancellation-
# safe deletion of a Pod created just before that deadline.
_KUBERNETES_RECONCILIATION_BUDGET_SECONDS = 50.0
_KUBERNETES_API_CALL_TIMEOUT_MAX_SECONDS = 3.0
_KUBERNETES_CLEANUP_SCHEDULING_SLACK_SECONDS = 5.0

_T = TypeVar("_T")
WaitGuard = Callable[[str | None, str], Awaitable[bool]]


async def _redis(call: Awaitable[_T] | _T) -> _T:
    """redis-py types hash/list commands as `Awaitable[T] | T` because the
    same command mixin backs both the sync and async clients — mypy can't
    know redis_client.client is always the async one. The cast is a runtime
    no-op; it just tells mypy the awaitable branch is the real one."""
    return await cast(Awaitable[_T], call)


def _kubernetes_cleanup_lease_margin() -> float:
    """Upper bound for a cancellation-safe delete under configured retries."""
    ready_timeout = float(settings.KUBERNETES_RUNNER_READY_TIMEOUT_SECONDS)
    attempts = max(1, int(settings.KUBERNETES_API_RETRY_ATTEMPTS))
    api_call_timeout = min(
        ready_timeout, _KUBERNETES_API_CALL_TIMEOUT_MAX_SECONDS
    )
    sleeps = attempts - 1
    retry_backoff: float
    if sleeps <= 3:
        retry_backoff = float(0.25 * (2**sleeps - 1))
    else:
        retry_backoff = 1.75 + 2.0 * (sleeps - 3)
    return (
        attempts * api_call_timeout
        + retry_backoff
        + min(ready_timeout, 10.0)
        + _KUBERNETES_CLEANUP_SCHEDULING_SLACK_SECONDS
    )


@dataclass
class ContainerRef:
    container_id: str
    conversation_id: str
    ip_address: str | None


@dataclass
class PoolEntry:
    resource_id: str
    resource_name: str
    resource_uid: str
    created_at: float


class SessionManager:
    """Manages runner resources while keeping business state runtime-neutral."""

    def __init__(
        self,
        runtime: RunnerRuntime | None = None,
        *,
        docker: Any | None = None,
        wait_guard: WaitGuard | None = None,
    ) -> None:
        if runtime is not None and docker is not None:
            raise ValueError("Pass either runtime or deprecated docker, not both")
        # `docker=` remains a test-only compatibility shim until the existing
        # Docker-focused tests have been migrated to the runtime contract.
        self.runtime = runtime or cast(RunnerRuntime, DockerRunnerRuntime(docker))
        self._wait_guard = wait_guard

    @property
    def docker(self) -> Any:
        """Deprecated test-only access to the underlying Docker client."""
        if not isinstance(self.runtime, DockerRunnerRuntime):
            raise AttributeError("The configured runner runtime has no Docker client")
        return self.runtime.docker

    async def close(self) -> None:
        """Close resources owned by the configured runtime."""
        await self.runtime.close()

    async def ready(self) -> None:
        """Verify that the configured runner runtime can provision runners."""
        await self.runtime.ready()

    def _static_env(self) -> dict[str, str]:
        """Env vars that are the same for every runner container regardless
        of which conversation (if any) it ends up bound to — safe to bake
        into pool containers ahead of time."""
        return {
            "TEMPORAL_HOST": settings.TEMPORAL_HOST,
            "TEMPORAL_NAMESPACE": settings.TEMPORAL_NAMESPACE,
            "REDIS_URL": settings.REDIS_URL,
            # The runner always talks to the backend (session-token authenticated
            # /chat/completions + /conversations proxy), never to the real
            # LLM_GATEWAY_URL directly — see RUNNER_BACKEND_URL docstring.
            "LLM_GATEWAY_URL": settings.RUNNER_BACKEND_URL,
            "OTEL_EXPORTER_OTLP_ENDPOINT": settings.OTEL_EXPORTER_OTLP_ENDPOINT,
            "LANGFUSE_ENABLED": str(settings.LANGFUSE_ENABLED),
            "LANGFUSE_PUBLIC_KEY": settings.LANGFUSE_PUBLIC_KEY,
            "LANGFUSE_SECRET_KEY": settings.LANGFUSE_SECRET_KEY,
            "LANGFUSE_HOST": settings.LANGFUSE_HOST,
            "LANGFUSE_CAPTURE_FULL_CONTENT": str(settings.LANGFUSE_CAPTURE_FULL_CONTENT),
            "EVALUATOR_ENABLED": str(settings.EVALUATOR_ENABLED),
            "EVALUATOR_MODEL": settings.EVALUATOR_MODEL,
            "CODE_SKILL_MAX_ROUNDS": str(settings.CODE_SKILL_MAX_ROUNDS),
            "CODE_SKILL_ROUND_TIMEOUT_SECONDS": str(settings.CODE_SKILL_ROUND_TIMEOUT_SECONDS),
        }

    async def get_or_create(
        self,
        conversation_id: str,
        user_id: str,
        org_id: str | None,
        generation: int = 1,
    ) -> ContainerRef:
        """Return a running container for the conversation, creating one if
        needed — serialized per conversation_id. Without this, two
        concurrent first messages both pass the "no existing container"
        check: the cold-start path's second `create` hits a Docker 409 on
        the shared fixed name (after the first message was already saved to
        the DB, surfacing as a 500), and the pool-claim path has both
        `/bind` a container to the same conversation, leaking the loser as
        an untracked zombie no reaper ever finds. `timeout` is a safety net
        (auto-releases if this process crashes mid-creation) well above the
        slowest real path (cold-start container boot, ~30s worst case);
        `blocking_timeout` bounds how long a second request for the same
        conversation waits its turn before failing loudly instead of
        hanging the request forever.
        """
        lock_timeout = 45.0
        blocking_timeout = 40.0
        operation_timeout: float | None = None
        if self.runtime.kind == "kubernetes":
            operation_timeout = (
                float(settings.KUBERNETES_RUNNER_READY_TIMEOUT_SECONDS)
                + _KUBERNETES_RECONCILIATION_BUDGET_SECONDS
            )
            # Keep the Redis lease beyond the strict whole-manager deadline so
            # cancellation-safe Pod cleanup finishes before another replica can
            # acquire the same conversation lock.
            lock_timeout = operation_timeout + _kubernetes_cleanup_lease_margin()
            blocking_timeout = lock_timeout + 5.0
        lock = redis_client.client.lock(
            f"lock:get_or_create:{conversation_id}",
            timeout=lock_timeout,
            blocking_timeout=blocking_timeout,
        )
        async with lock:
            if operation_timeout is None:
                return await self._get_or_create_locked(
                    conversation_id, user_id, org_id, generation
                )
            try:
                async with asyncio.timeout(operation_timeout):
                    return await self._get_or_create_locked(
                        conversation_id, user_id, org_id, generation
                    )
            except TimeoutError:
                raise RunnerRuntimeError(
                    "timed out reconciling Kubernetes runner"
                ) from None

    async def _get_or_create_locked(
        self,
        conversation_id: str,
        user_id: str,
        org_id: str | None,
        generation: int = 1,
    ) -> ContainerRef:
        field = session_field(org_id, conversation_id)
        existing = await _redis(redis_client.client.hget("session_containers", field))
        if existing:
            data = self._decode_record(existing)

            if (
                data is not None
                and data.get("conversation_id") == conversation_id
                and self._record_matches_runtime(data)
            ):
                resource = await self.runtime.get(self._resource_id(data))
                reused = await self._refresh_owned_record(
                    field, data, resource, conversation_id, user_id, org_id, generation
                )
                if reused is not None:
                    return reused
                if (
                    resource
                    and self._resource_matches_record(resource, data, conversation_id)
                    and not (
                        self.runtime.kind == "kubernetes"
                        and resource.state == "running"
                        and data.get("generation", 1) == generation
                    )
                ):
                    # Found but not running (exited/dead/created) — the
                    # cold-start path below creates a container with the same
                    # fixed name (f"runner-{conversation_id}"), so leaving
                    # this one behind means every future attempt for this
                    # conversation gets a Docker 409 Conflict forever.
                    await self.runtime.delete(
                        resource.resource_id, self._record_uid(data)
                    )

            await _redis(redis_client.client.hdel("session_containers", field))

        if self.runtime.supports_warm_pool and settings.RUNNER_POOL_SIZE > 0:
            claimed = await self.claim_from_pool(conversation_id, user_id, org_id, generation)
            if claimed is not None:
                return claimed

        token = session_token_service.issue(conversation_id, user_id, org_id, generation=generation)
        token_issued_at = time.time()
        task_queue = f"session-{conversation_id}"

        resource = await self.runtime.create_bound(
            f"runner-{conversation_id}",
            self._static_env(),
            RunnerBinding(conversation_id, user_id, org_id, token, task_queue),
        )
        await _redis(redis_client.client.hset(
            "session_containers",
            session_field(org_id, conversation_id),
            self._encode_record(
                self._record(resource, conversation_id, token_issued_at, generation)
            ),
        ))
        return ContainerRef(resource.resource_id, conversation_id, resource.ip_address)

    async def _refresh_owned_record(
        self,
        field: str,
        data: dict[str, object],
        resource: RunnerResource | None,
        conversation_id: str,
        user_id: str,
        org_id: str | None,
        generation: int = 1,
    ) -> ContainerRef | None:
        if (
            resource is None
            or data.get("generation", 1) != generation
            or data.get("conversation_id") != conversation_id
            or not self._record_matches_runtime(data)
            or not self._resource_matches_record(resource, data, conversation_id)
            or resource.state != "running"
            or not resource.ready
        ):
            return None

        refreshed_at = time.time()
        data["last_active"] = refreshed_at
        token_age = refreshed_at - float(
            cast(float | int | str, data.get("token_issued_at", 0))
        )
        if token_age > settings.RUNNER_TOKEN_REFRESH_AFTER_SECONDS:
            token = session_token_service.issue(conversation_id, user_id, org_id, generation=generation)
            refresh_resource = RunnerResource(
                resource.resource_id,
                str(
                    data.get("resource_name")
                    or data.get("container_name")
                    or f"runner-{conversation_id}"
                ),
                resource.uid,
                resource.ip_address,
                resource.state,
                resource.ready,
                resource.created_at,
                resource.labels,
            )
            if await self.runtime.refresh_token(refresh_resource, token):
                data["token_issued_at"] = time.time()
        await _redis(
            redis_client.client.hset(
                "session_containers", field, self._encode_record(data)
            )
        )
        return ContainerRef(
            container_id=resource.resource_id,
            conversation_id=conversation_id,
            ip_address=(
                resource.ip_address
                if data.get("version") is not None
                else cast(str | None, data.get("ip_address"))
            ),
        )

    async def refresh_owned_waiting_runtime(
        self,
        conversation_id: str,
        user_id: str,
        org_id: str | None,
    ) -> bool:
        """Refresh an existing exact-owned runtime without provisioning one."""
        field = session_field(org_id, conversation_id)
        lock = redis_client.client.lock(
            f"lock:get_or_create:{conversation_id}",
            timeout=45.0,
            blocking_timeout=40.0,
        )
        async with lock:
            existing = await _redis(
                redis_client.client.hget("session_containers", field)
            )
            if not existing:
                return False
            data = self._decode_record(existing)
            if (
                data is None
                or data.get("conversation_id") != conversation_id
                or not self._record_matches_runtime(data)
            ):
                return False
            resource = await self.runtime.get(self._resource_id(data))
            return (
                await self._refresh_owned_record(
                    field, data, resource, conversation_id, user_id, org_id, int(cast(int, data.get("generation", 1)))
                )
                is not None
            )

    def _resource_id(self, data: Mapping[str, object]) -> str:
        return str(data.get("resource_id") or data["container_id"])

    def _decode_record(self, raw: object) -> dict[str, object] | None:
        try:
            decoded = dict(raw) if isinstance(raw, Mapping) else json.loads(raw)  # type: ignore[arg-type]
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        return cast(dict[str, object], decoded) if isinstance(decoded, dict) else None

    def _encode_record(self, data: Mapping[str, object]) -> str:
        return json.dumps(dict(data))

    def _record_uid(self, data: Mapping[str, object]) -> str:
        return str(data.get("resource_uid") or self._resource_id(data))

    def _record_matches_runtime(self, data: Mapping[str, object]) -> bool:
        """Legacy records are Docker-only; v2 records name their runtime."""
        runtime = data.get("runtime")
        if runtime is None:
            return self.runtime.kind == "docker"
        return data.get("version") == 2 and runtime == self.runtime.kind

    def _resource_matches_record(
        self, resource: RunnerResource, data: Mapping[str, object], conversation_id: str
    ) -> bool:
        if resource.uid != self._record_uid(data):
            return False
        if self.runtime.kind != "kubernetes":
            return True
        return (
            resource.name == f"runner-{conversation_id}"
            and resource.labels.get("aurum-ai.conversation") == conversation_id
        )

    def _resource_from_data(self, data: Mapping[str, object]) -> RunnerResource:
        resource_id = self._resource_id(data)
        return RunnerResource(
            resource_id=resource_id,
            name=str(data.get("resource_name") or data.get("container_name") or resource_id),
            uid=str(data.get("resource_uid") or resource_id),
            ip_address=cast(str | None, data.get("ip_address")),
            state="running",
            ready=True,
            created_at=0,
            labels={},
        )

    def _record(
        self, resource: RunnerResource, conversation_id: str, token_issued_at: float, generation: int = 1
    ) -> dict[str, object]:
        record: dict[str, object] = {
            "version": 2,
            "generation": generation,
            "runtime": self.runtime.kind,
            "resource_id": resource.resource_id,
            "resource_name": resource.name,
            "resource_uid": resource.uid,
            "conversation_id": conversation_id,
            "ip_address": resource.ip_address,
            "last_active": time.time(),
            "token_issued_at": token_issued_at,
        }
        if self.runtime.kind == "docker":
            record["container_id"] = resource.resource_id
        return record

    # ---------------------------------------------------------------
    # Warm pool: unbound runner containers, kept ready for get_or_create()
    # to hand off to a brand-new conversation instead of paying the ~3-4s
    # container-create + Python/Temporal-client boot tax on every first
    # message. Each pool container boots without CONVERSATION_ID and waits
    # on its own POST /bind (see conversation-runner's src/health.py) —
    # everything after that point (Temporal worker, event publishing, ...)
    # behaves exactly like a directly-created container, including idle
    # reaping via the normal session_containers bookkeeping.
    # ---------------------------------------------------------------

    async def _create_pool_container(self) -> PoolEntry:
        name = f"runner-pool-{uuid.uuid4().hex[:12]}"
        resource = await self.runtime.create_unbound(
            name, self._static_env()
        )
        return PoolEntry(resource.resource_id, resource.name, resource.uid, resource.created_at)

    async def claim_from_pool(
        self,
        conversation_id: str,
        user_id: str,
        org_id: str | None,
        generation: int = 1,
    ) -> ContainerRef | None:
        """Pop a warm container from the pool and bind it to `conversation_id`.
        Returns None (never raises) on any failure so callers fall back to
        the normal cold-start path — a broken pool must never break chat."""
        if not self.runtime.supports_warm_pool:
            return None
        entry_json = await _redis(redis_client.client.lpop(POOL_REDIS_KEY))
        if not entry_json:
            return None
        try:
            # lpop's stub type covers the count= variant (returns a list) even
            # though we never pass count — our own rpush always stores a
            # single JSON string.
            data = self._decode_record(entry_json)
            if data is None:
                return None
            entry = self._pool_entry(data)
        except (KeyError, TypeError, ValueError):
            return None

        token = session_token_service.issue(conversation_id, user_id, org_id, generation=generation)
        token_issued_at = time.time()
        try:
            resource = await self.runtime.bind(
                RunnerResource(
                    entry.resource_id,
                    entry.resource_name,
                    entry.resource_uid,
                    None,
                    "running",
                    True,
                    entry.created_at,
                    {},
                ),
                f"runner-{conversation_id}",
                RunnerBinding(
                    conversation_id,
                    user_id,
                    org_id,
                    token,
                    f"session-{conversation_id}",
                ),
            )
        except Exception:
            logger.warning(
                "Failed to bind pool runner %s to conversation %s; discarding it",
                entry.resource_name,
                conversation_id,
                exc_info=True,
            )
            await self.runtime.delete(entry.resource_id, entry.resource_uid)
            return None

        await _redis(redis_client.client.hset(
            "session_containers",
            session_field(org_id, conversation_id),
            self._encode_record(self._record(resource, conversation_id, token_issued_at, generation)),
        ))
        logger.info(
            "Claimed pool runner %s (renamed to %s) for new conversation %s",
            entry.resource_name,
            resource.name,
            conversation_id,
        )
        return ContainerRef(resource.resource_id, conversation_id, resource.ip_address)

    def _pool_entry(self, data: Mapping[str, object]) -> PoolEntry:
        resource_id = str(data.get("resource_id") or data["container_id"])
        return PoolEntry(
            resource_id,
            str(data.get("resource_name") or data.get("container_name") or resource_id),
            str(data.get("resource_uid") or resource_id),
            float(cast(float | int | str, data.get("created_at", 0))),
        )

    async def _pool_size(self) -> int:
        return await _redis(redis_client.client.llen(POOL_REDIS_KEY))

    async def top_up_pool(self) -> int:
        """Reconciles the pool to RUNNER_POOL_SIZE: creates containers if
        under target, terminates the oldest excess ones if over (e.g. the
        target was lowered at runtime, or entries piled up across restarts
        since the pool list lives in Redis, not in this process). Returns
        the number of containers created (negative if containers were
        instead removed)."""
        target = settings.RUNNER_POOL_SIZE
        if not self.runtime.supports_warm_pool or target <= 0:
            return 0
        current = await self._pool_size()

        if current > target:
            removed = 0
            for _ in range(current - target):
                entry_json = await _redis(redis_client.client.lpop(POOL_REDIS_KEY))
                if not entry_json:
                    break
                try:
                    data = self._decode_record(entry_json)
                    if data is None:
                        raise ValueError("invalid pool record")
                    entry = self._pool_entry(data)
                    await self.runtime.delete(entry.resource_id, entry.resource_uid)
                except Exception:
                    logger.warning("Failed to trim excess pool container", exc_info=True)
                removed += 1
            if removed:
                logger.info("Pool trimmed: removed %d excess runner container(s)", removed)
            return -removed

        created = 0
        for _ in range(target - current):
            try:
                entry = await self._create_pool_container()
            except Exception:
                logger.warning("Failed to create pool container", exc_info=True)
                break
            await _redis(redis_client.client.rpush(
                POOL_REDIS_KEY,
                self._encode_record(
                    {
                        "resource_id": entry.resource_id,
                        "resource_name": entry.resource_name,
                        "resource_uid": entry.resource_uid,
                        # Keep aliases for a rolling rollback to pre-contract
                        # Docker-only code that still consumes pool records.
                        "container_id": entry.resource_id,
                        "container_name": entry.resource_name,
                        "created_at": entry.created_at,
                    }
                ),
            ))
            created += 1
        if created:
            logger.info("Pool topped up: created %d runner container(s)", created)
        return created

    async def cleanup_stale_pool(self) -> int:
        """Drop pool entries whose container no longer exists/isn't running,
        or that got bound behind our back (shouldn't happen, but a claim
        that crashed after LPOP-ing but before this process recorded it in
        session_containers would otherwise leak a bound container back into
        the pool's Redis list on the next reconciliation — pool-status
        catches that too)."""
        if not self.runtime.supports_warm_pool:
            return 0
        raw_entries = await _redis(redis_client.client.lrange(POOL_REDIS_KEY, 0, -1))
        removed = 0
        for raw in raw_entries:
            keep = False
            try:
                data = self._decode_record(raw)
                if data is None:
                    raise ValueError("invalid pool record")
                entry = self._pool_entry(data)
                resource = await self.runtime.get(entry.resource_id)
                if resource and resource.state == "running" and resource.ready:
                    keep = await self.runtime.pool_is_unbound(resource)
            except Exception:
                keep = False
            if not keep:
                await _redis(redis_client.client.lrem(POOL_REDIS_KEY, 1, raw))
                removed += 1
        if removed:
            logger.info("cleanup_stale_pool removed %d stale pool entries", removed)
        return removed

    async def drain_pool(self) -> int:
        """Terminate every container currently sitting in the pool. Used when
        RUNNER_POOL_SIZE is (or becomes) 0 — without this, containers created
        while pooling was previously enabled would sit around forever, since
        reap_idle() only ever looks at session_containers, not the pool."""
        if not self.runtime.supports_warm_pool:
            return 0
        removed = 0
        while True:
            entry_json = await _redis(redis_client.client.lpop(POOL_REDIS_KEY))
            if not entry_json:
                break
            try:
                data = self._decode_record(entry_json)
                if data is None:
                    raise ValueError("invalid pool record")
                entry = self._pool_entry(data)
                await self.runtime.delete(entry.resource_id, entry.resource_uid)
            except Exception:
                logger.warning("Failed to drain pool container", exc_info=True)
            removed += 1
        if removed:
            logger.info("drain_pool terminated %d runner container(s)", removed)
        return removed

    async def maintain_pool(self) -> None:
        """Background loop: reconciles and tops up the warm pool. Started in
        the FastAPI lifespan alongside run_reaper(). If pooling is disabled
        (RUNNER_POOL_SIZE == 0) it instead drains any containers left over
        from when it was previously enabled."""
        interval = settings.RUNNER_POOL_MAINTAIN_INTERVAL_SECONDS
        if self.runtime.supports_warm_pool and settings.RUNNER_POOL_SIZE > 0:
            logger.info(
                "runner pool maintainer started (size=%d, interval=%ss)",
                settings.RUNNER_POOL_SIZE, interval,
            )
        while True:
            try:
                if settings.RUNNER_POOL_SIZE > 0:
                    await self.cleanup_stale_pool()
                    await self.top_up_pool()
                else:
                    await self.drain_pool()
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("pool maintainer loop iteration failed", exc_info=True)
                await asyncio.sleep(interval)

    async def cleanup_stale(self) -> int:
        """Remove Redis entries for containers that are no longer running.

        Called at startup to recover from unclean shutdowns. Returns the number
        of stale entries removed.
        """
        _log = logger
        try:
            all_entries = await _redis(redis_client.client.hgetall("session_containers"))
        except Exception as exc:
            _log.warning("cleanup_stale: could not read from Redis: %s", exc)
            return 0

        removed = 0
        # The hash key is session_field ("{org_id}:{conversation_id}"), not a
        # bare conversation_id; the variable name reflects that distinction.
        for field, data_str in all_entries.items():
            data = self._decode_record(data_str)
            if data is None:
                await _redis(redis_client.client.hdel("session_containers", field))
                removed += 1
                continue
            try:
                if not self._record_matches_runtime(data):
                    await _redis(redis_client.client.hdel("session_containers", field))
                    removed += 1
                    continue
                resource = await self.runtime.get(self._resource_id(data))
                owned = resource is not None and self._resource_matches_record(
                    resource, data, str(data.get("conversation_id", ""))
                )
                if not owned:
                    await _redis(redis_client.client.hdel("session_containers", field))
                    removed += 1
                    continue
                if resource is not None and resource.state not in (
                    "running",
                    "created",
                    "paused",
                ):
                    await self.runtime.delete(resource.resource_id, self._record_uid(data))
                    await _redis(redis_client.client.hdel("session_containers", field))
                    removed += 1
            except Exception as exc:
                _log.warning(
                    "cleanup_stale: error checking runner resource for %s", field, exc_info=exc
                )

        return removed

    async def _tracked_container_ids(self) -> set[tuple[str, str]]:
        """Every container id the backend currently believes it owns — the
        per-conversation hash plus the warm pool list."""
        tracked: set[tuple[str, str]] = set()
        entries = await _redis(redis_client.client.hgetall("session_containers"))
        raw_pool = await _redis(redis_client.client.lrange(POOL_REDIS_KEY, 0, -1))
        for raw in list(entries.values()) + list(raw_pool):
            try:
                data = self._decode_record(raw)
                if data is not None and self._record_matches_runtime(data):
                    tracked.add((self._resource_id(data), self._record_uid(data)))
            except (KeyError, TypeError):
                continue
        return tracked

    async def sweep_orphans(self) -> int:
        """Remove runner containers Docker still has but Redis no longer knows
        about. Returns the number of containers removed.

        Everything else that cleans up after runner containers (cleanup_stale,
        cleanup_stale_pool, reap_idle, terminate) starts from a Redis record
        and looks up the container it names — so a container whose record is
        gone is invisible to all of them and stays on the host forever. That
        is the normal state after a host reboot: the `redis` service has no
        volume (see docker-compose.yml), so every session_containers entry and
        the whole pool list are gone, while Docker still has the containers
        themselves sitting there as Exited. cleanup_stale_pool() also drops
        entries without deleting the container, leaking one each time a pool
        container dies on its own.

        Hence this sweep goes the other way round: ask Docker what runner
        containers exist, and remove the ones Redis can't account for. Dead
        ones go immediately; a *running* orphan is only removed once it is
        older than RUNNER_ORPHAN_GRACE_SECONDS, because both creation paths
        register their container in Redis only after it reports ready, so a
        young untracked container may well be one that is being created right
        now. Past that age it is genuinely unreachable — get_or_create() finds
        conversations by Redis record, so a container no record points at can
        never serve a chat again.
        """
        try:
            tracked = await self._tracked_container_ids()
        except Exception:
            # Without the Redis side there is no way to tell an orphan from a
            # live session, and deleting a live session's container is far
            # worse than leaving an orphan for the next pass.
            logger.warning("sweep_orphans: could not read Redis bookkeeping", exc_info=True)
            return 0

        try:
            resources = await self.runtime.list()
        except Exception:
            logger.warning("sweep_orphans: could not list containers", exc_info=True)
            return 0

        now = time.time()
        removed = 0
        for resource in resources:
            if (
                (resource.resource_id, resource.uid) in tracked
                or not self._is_runner_resource(resource)
            ):
                continue
            state = resource.state.lower()
            if state not in _DEAD_STATES:
                age = now - resource.created_at
                if age < settings.RUNNER_ORPHAN_GRACE_SECONDS:
                    continue
                conversation_id = self._resource_conversation_id(resource)
                if self._wait_guard is not None:
                    if conversation_id is None:
                        try:
                            safely_unbound = (
                                self.runtime.supports_warm_pool
                                and await self.runtime.pool_is_unbound(resource)
                            )
                        except Exception:
                            safely_unbound = False
                        if not safely_unbound:
                            logger.warning(
                                "sweep_orphans: runtime binding is unconfirmed for %s; retaining",
                                resource.resource_id,
                            )
                            continue
                    else:
                        try:
                            if await self._wait_guard(None, conversation_id):
                                continue
                        except Exception:
                            logger.warning(
                                "sweep_orphans: durable wait state unavailable for %s; retaining",
                                resource.resource_id,
                                exc_info=True,
                            )
                            continue
            try:
                await self.runtime.delete(resource.resource_id, resource.uid)
            except Exception:
                logger.warning(
                    "sweep_orphans: failed to remove %s", resource.resource_id, exc_info=True
                )
                continue
            removed += 1

        if removed:
            logger.info("sweep_orphans removed %d untracked runner container(s)", removed)
        return removed

    def _is_runner_resource(self, resource: RunnerResource) -> bool:
        labels = resource.labels
        if labels.get(RUNNER_LABEL) == RUNNER_LABEL_VALUE:
            return True
        return resource.name.startswith(RUNNER_NAME_PREFIX)

    @staticmethod
    def _resource_conversation_id(resource: RunnerResource) -> str | None:
        labelled = resource.labels.get("aurum-ai.conversation")
        if labelled:
            return labelled
        if resource.name.startswith("runner-") and not resource.name.startswith(
            "runner-pool-"
        ):
            return resource.name.removeprefix("runner-")
        return None

    @staticmethod
    def _field_org_id(field: str) -> str | None:
        org_id, separator, _ = field.partition(":")
        if not separator:
            return None
        return None if org_id == "-" else org_id

    async def reap_idle(self, idle_seconds: int | None = None) -> int:
        """Terminate runner containers with no chat activity for `idle_seconds`.

        Runner containers are created per conversation and stay up until reaped.
        The idle timestamp is refreshed on every `get_or_create` (i.e. each chat
        turn), so this only removes genuinely inactive sessions. Returns the
        number of containers terminated.
        """
        if idle_seconds is None:
            idle_seconds = settings.RUNNER_IDLE_REAP_SECONDS
        now = time.time()
        try:
            all_entries = await _redis(redis_client.client.hgetall("session_containers"))
        except Exception as exc:
            logger.warning("reap_idle: could not read from Redis: %s", exc)
            return 0

        reaped = 0
        for field, data_str in all_entries.items():
            data = self._decode_record(data_str)
            if data is None:
                await _redis(redis_client.client.hdel("session_containers", field))
                continue

            last_active = data.get("last_active")
            if last_active is None:
                # Legacy entry created before idle tracking — backfill a grace
                # period so it gets a fair TTL instead of being reaped instantly.
                data["last_active"] = now
                await _redis(redis_client.client.hset(
                    "session_containers", field, self._encode_record(data)
                ))
                continue

            if now - float(cast(float | int | str, last_active)) >= idle_seconds:
                try:
                    conversation_id = str(data.get("conversation_id", ""))
                    if self._wait_guard is not None and conversation_id:
                        try:
                            if await self._wait_guard(
                                self._field_org_id(field), conversation_id
                            ):
                                continue
                        except Exception:
                            logger.warning(
                                "reap_idle: durable wait state unavailable for %s; retaining",
                                field,
                                exc_info=True,
                            )
                            continue
                    await self.terminate(field)
                    reaped += 1
                except Exception as exc:
                    logger.warning(
                        "reap_idle: failed to terminate %s: %s", field, exc
                    )

        if reaped:
            logger.info("reap_idle terminated %d idle runner containers", reaped)
        return reaped

    async def run_reaper(self) -> None:
        """Background loop that periodically reaps idle runner containers.

        Started in the FastAPI lifespan and cancelled on shutdown.
        """
        interval = settings.RUNNER_REAP_INTERVAL_SECONDS
        logger.info(
            "runner reaper started (interval=%ss, idle_ttl=%ss)",
            interval,
            settings.RUNNER_IDLE_REAP_SECONDS,
        )
        while True:
            try:
                await asyncio.sleep(interval)
                await self.reap_idle()
                await self.sweep_orphans()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("reaper loop iteration failed", exc_info=True)

    async def terminate(self, field: str) -> None:
        """Stop/remove the container identified by a session_containers field
        ("{org_id}:{conversation_id}", see session_field)."""
        existing = await _redis(redis_client.client.hget("session_containers", field))
        if existing:
            data = self._decode_record(existing)

            if data is not None and self._record_matches_runtime(data):
                try:
                    resource_id = self._resource_id(data)
                    if self.runtime.kind == "kubernetes":
                        resource = await self.runtime.get(resource_id)
                        if resource is not None and self._resource_matches_record(
                            resource,
                            data,
                            str(data.get("conversation_id", "")),
                        ):
                            await self.runtime.delete(resource_id, self._record_uid(data))
                    else:
                        await self.runtime.delete(resource_id, self._record_uid(data))
                except Exception:
                    pass
            await _redis(redis_client.client.hdel("session_containers", field))


async def _durable_wait_guard(org_id: str | None, conversation_id: str) -> bool:
    # Lazy import keeps the runtime adapter independent from database service
    # construction while still giving production reapers a durable fail-closed
    # check. Unit-created managers opt in explicitly through wait_guard.
    from src.services.interaction_maintenance import durable_wait_protected

    return await durable_wait_protected(org_id, conversation_id)


session_manager = SessionManager(
    runtime=create_runner_runtime(settings), wait_guard=_durable_wait_guard
)
