import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Literal, Mapping, NoReturn

import aiodocker
import httpx
from aiodocker.exceptions import DockerError

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


def _field(container: Any, key: str, default: Any = None) -> Any:
    try:
        return container[key]
    except KeyError:
        return default


def _created_at(value: object) -> float:
    """Normalize Docker's numeric or RFC3339 `Created` values to epoch seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return 0.0


def _is_not_found(exc: DockerError) -> bool:
    return getattr(exc, "status", None) == 404


def _raise_runtime_error(operation: str, exc: Exception) -> NoReturn:
    """Translate adapter failures without exposing daemon-provided detail."""
    logger.warning("Docker runner %s failed", operation, exc_info=exc)
    status = getattr(exc, "status", None)
    error_type = (
        RunnerPermanentError
        if isinstance(status, int) and 400 <= status < 500
        else RunnerRuntimeError
    )
    raise error_type(f"Docker runner {operation} failed") from None


class DockerRunnerRuntime:
    """Docker Engine implementation of the conversation runner contract."""

    kind: Literal["docker"] = "docker"
    supports_warm_pool = True

    def __init__(self, docker: aiodocker.Docker | None = None) -> None:
        self._docker = docker
        self._owns_docker = docker is None

    @property
    def docker(self) -> aiodocker.Docker:
        if self._docker is None:
            self._docker = aiodocker.Docker()
            self._owns_docker = True
        return self._docker

    async def close(self) -> None:
        if self._owns_docker and self._docker is not None:
            await self._docker.close()
            self._docker = None
            self._owns_docker = False

    def _container_config(self, env: Mapping[str, str]) -> dict[str, Any]:
        return {
            "Image": settings.RUNNER_IMAGE,
            "Env": [f"{key}={value}" for key, value in env.items()],
            "Labels": {RUNNER_LABEL: RUNNER_LABEL_VALUE},
            "HostConfig": {
                "NetworkMode": settings.DOCKER_NETWORK,
                "Memory": settings.RUNNER_MEMORY_LIMIT,
                "CpuQuota": settings.RUNNER_CPU_QUOTA,
            },
        }

    async def create_bound(
        self, name: str, static_env: Mapping[str, str], binding: RunnerBinding
    ) -> RunnerResource:
        env = dict(static_env)
        env.update(
            {
                "CONVERSATION_ID": binding.conversation_id,
                "USER_ID": binding.user_id,
                "ORG_ID": binding.org_id or "",
                "SESSION_TOKEN": binding.session_token,
                "TASK_QUEUE": binding.task_queue,
            }
        )
        try:
            container = await self.docker.containers.create(
                config=self._container_config(env), name=name
            )
        except Exception as exc:
            _raise_runtime_error("create", exc)
        try:
            await container.start()
            return await self._wait_for_ready(container, name)
        except RunnerRuntimeError:
            await self.delete(container.id)
            raise
        except Exception as exc:
            await self.delete(container.id)
            _raise_runtime_error("start", exc)

    async def create_unbound(
        self, name: str, static_env: Mapping[str, str]
    ) -> RunnerResource:
        try:
            container = await self.docker.containers.create(
                config=self._container_config(static_env), name=name
            )
        except Exception as exc:
            _raise_runtime_error("create", exc)
        try:
            await container.start()
            await self._wait_for_pool_ready(name)
        except RunnerRuntimeError:
            raise
        except Exception as exc:
            _raise_runtime_error("start", exc)
        return RunnerResource(
            resource_id=container.id,
            name=name,
            uid=container.id,
            ip_address=None,
            state="running",
            ready=True,
            created_at=time.time(),
            labels={RUNNER_LABEL: RUNNER_LABEL_VALUE},
        )

    async def bind(
        self, resource: RunnerResource, new_name: str, binding: RunnerBinding
    ) -> RunnerResource:
        bind_url = f"http://{resource.name}:{settings.RUNNER_POOL_PORT}/bind"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    bind_url,
                    json={
                        "conversation_id": binding.conversation_id,
                        "user_id": binding.user_id,
                        "org_id": binding.org_id,
                        "session_token": binding.session_token,
                        "task_queue": binding.task_queue,
                    },
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning(
                "Failed to bind pool runner %s to conversation %s",
                resource.name,
                binding.conversation_id,
                exc_info=exc,
            )
            raise RunnerRuntimeError("runner pool bind request failed") from None

        name = new_name
        try:
            container = await self.docker.containers.get(resource.resource_id)
            await container.rename(new_name)
        except Exception:
            logger.warning(
                "Failed to rename claimed pool runner %s", resource.name, exc_info=True
            )
            name = resource.name
        return RunnerResource(
            resource_id=resource.resource_id,
            name=name,
            uid=resource.uid,
            ip_address=resource.ip_address,
            state="running",
            ready=True,
            created_at=resource.created_at,
            labels=resource.labels,
        )

    async def refresh_token(self, resource: RunnerResource, token: str) -> bool:
        url = f"http://{resource.name}:{settings.RUNNER_POOL_PORT}/refresh-token"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(url, json={"session_token": token})
                response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning(
                "Failed to refresh session token for runner %s", resource.name, exc_info=exc
            )
            return False
        return True

    async def pool_is_unbound(self, resource: RunnerResource) -> bool:
        url = f"http://{resource.name}:{settings.RUNNER_POOL_PORT}/pool-status"
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(url)
                return response.status_code == 200 and not response.json().get("bound")
        except httpx.HTTPError:
            return False

    async def get(self, resource_id: str) -> RunnerResource | None:
        try:
            container = await self.docker.containers.get(resource_id)
        except DockerError as exc:
            if _is_not_found(exc):
                return None
            _raise_runtime_error("inspect", exc)
        except Exception as exc:
            _raise_runtime_error("inspect", exc)
        try:
            info = await container.show()
        except DockerError as exc:
            if _is_not_found(exc):
                return None
            _raise_runtime_error("inspect", exc)
        except Exception as exc:
            _raise_runtime_error("inspect", exc)
        return self._resource_from_info(container, info)

    async def list(self) -> list[RunnerResource]:
        try:
            containers = await self.docker.containers.list(all=True)
            resources: list[RunnerResource] = []
            for container in containers:
                resource_id = str(getattr(container, "id", _field(container, "Id", "")))
                names = _field(container, "Names", []) or []
                name = str(names[0]).lstrip("/") if names else resource_id
                state = str(_field(container, "State", "")).lower()
                labels = dict(_field(container, "Labels", {}) or {})
                resources.append(
                    RunnerResource(
                        resource_id=resource_id,
                        name=name,
                        uid=resource_id,
                        ip_address=None,
                        state=state,
                        ready=state == "running",
                        created_at=_created_at(_field(container, "Created", 0)),
                        labels=labels,
                    )
                )
            return resources
        except Exception as exc:
            _raise_runtime_error("list", exc)

    async def delete(self, resource_id: str, uid: str | None = None) -> None:
        del uid  # Docker IDs are immutable; there is no separate UID precondition.
        try:
            container = await self.docker.containers.get(resource_id)
        except DockerError as exc:
            if not _is_not_found(exc):
                logger.warning("Docker runner delete lookup failed", exc_info=exc)
            return
        except Exception as exc:
            logger.warning("Docker runner delete lookup failed", exc_info=exc)
            return
        try:
            await container.kill()
        except Exception as exc:
            logger.warning("Docker runner delete kill failed", exc_info=exc)
        try:
            await container.delete(force=True)
        except Exception as exc:
            logger.warning("Docker runner delete failed", exc_info=exc)

    async def ready(self) -> None:
        try:
            await self.docker.version()
        except Exception as exc:
            logger.warning("Docker runner runtime is unavailable", exc_info=exc)
            raise RunnerRuntimeError("Docker runner runtime is unavailable") from None

    async def _wait_for_ready(
        self, container: aiodocker.containers.DockerContainer, name: str
    ) -> RunnerResource:
        for _ in range(30):
            try:
                info = await container.show()
            except Exception as exc:
                _raise_runtime_error("inspect", exc)
            state = info.get("State", {})
            health = state.get("Health")
            if health is not None:
                if health.get("Status") == "healthy":
                    return self._resource_from_info(container, info, name=name, ready=True)
                if health.get("Status") == "unhealthy":
                    raise RunnerReadinessError("runner container became unhealthy")
            elif state.get("Running") and state.get("Status") == "running":
                return self._resource_from_info(container, info, name=name, ready=True)
            elif state.get("Status") in ("exited", "dead"):
                raise RunnerReadinessError("runner container exited before becoming ready")
            await asyncio.sleep(1)
        raise RunnerReadinessError("timed out waiting for runner container readiness")

    async def _wait_for_pool_ready(self, name: str) -> None:
        url = f"http://{name}:{settings.RUNNER_POOL_PORT}/health"
        async with httpx.AsyncClient(timeout=2.0) as client:
            for _ in range(30):
                try:
                    response = await client.get(url)
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
        raise RunnerReadinessError("timed out waiting for pool runner readiness")

    def _resource_from_info(
        self,
        container: Any,
        info: dict[str, Any],
        *,
        name: str | None = None,
        ready: bool | None = None,
    ) -> RunnerResource:
        state = info.get("State", {})
        status = str(state.get("Status", ""))
        health = state.get("Health")
        is_ready = (
            ready
            if ready is not None
            else health.get("Status") == "healthy"
            if health is not None
            else bool(state.get("Running") and status == "running")
        )
        network = info.get("NetworkSettings", {}).get("Networks", {}).get(
            settings.DOCKER_NETWORK, {}
        )
        resource_id = str(getattr(container, "id", info.get("Id", "")))
        return RunnerResource(
            resource_id=resource_id,
            name=name or str(info.get("Name", resource_id)).lstrip("/"),
            uid=resource_id,
            ip_address=network.get("IPAddress") or None,
            state=status,
            ready=is_ready,
            created_at=_created_at(info.get("Created", time.time())),
            labels=dict(info.get("Config", {}).get("Labels", {}) or {}),
        )
