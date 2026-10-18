from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiodocker.exceptions import DockerError

from src.services.runner_runtime import DockerRunnerRuntime, RunnerBinding, RunnerRuntimeError


class ListedContainer:
    def __init__(self, created: object) -> None:
        self.id = "runner-1"
        self._payload = {
            "Names": ["/runner-conv-1"],
            "State": "running",
            "Created": created,
            "Labels": {"aurum-ai.component": "conversation-runner"},
        }

    def __getitem__(self, key: str) -> Any:
        return self._payload[key]


class RuntimeDocker:
    def __init__(self) -> None:
        self.containers = type("Containers", (), {})()
        self.containers.list = AsyncMock()
        self.containers.get = AsyncMock()
        self.containers.create = AsyncMock()


class BoundContainer:
    def __init__(self) -> None:
        self.id = "runner-bound"
        self.start = AsyncMock()
        self.show = AsyncMock(
            return_value={"State": {"Status": "running", "Running": True}}
        )


def binding() -> RunnerBinding:
    return RunnerBinding("conv-1", "user-1", "org-1", "session-token", "session-conv-1")


@pytest.mark.asyncio
async def test_list_parses_rfc3339_docker_created_timestamp() -> None:
    docker = RuntimeDocker()
    docker.containers.list.return_value = [ListedContainer("2026-08-30T12:34:56.123456789Z")]

    resources = await DockerRunnerRuntime(docker).list()  # type: ignore[arg-type]

    assert resources[0].created_at == pytest.approx(1788093296.123456)


@pytest.mark.asyncio
async def test_get_returns_none_only_for_docker_not_found() -> None:
    docker = RuntimeDocker()
    docker.containers.get.side_effect = DockerError(404, "missing")
    runtime = DockerRunnerRuntime(docker)  # type: ignore[arg-type]

    assert await runtime.get("runner-404") is None

    docker.containers.get.side_effect = DockerError(500, "token=top-secret")
    with pytest.raises(RunnerRuntimeError) as exc_info:
        await runtime.get("runner-500")

    assert "top-secret" not in str(exc_info.value)
    assert str(exc_info.value) == "Docker runner inspect failed"


@pytest.mark.asyncio
async def test_list_translates_transport_error_without_leaking_details() -> None:
    docker = RuntimeDocker()
    docker.containers.list.side_effect = ConnectionError("http://user:secret@example.test")

    with pytest.raises(RunnerRuntimeError) as exc_info:
        await DockerRunnerRuntime(docker).list()  # type: ignore[arg-type]

    assert "secret" not in str(exc_info.value)
    assert str(exc_info.value) == "Docker runner list failed"


@pytest.mark.asyncio
async def test_create_bound_translates_docker_create_error_without_leaking_details() -> None:
    docker = RuntimeDocker()
    docker.containers.create.side_effect = DockerError(500, "token=top-secret")

    with pytest.raises(RunnerRuntimeError) as exc_info:
        await DockerRunnerRuntime(docker).create_bound("runner-conv-1", {}, binding())  # type: ignore[arg-type]

    assert str(exc_info.value) == "Docker runner create failed"
    assert "top-secret" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_create_bound_translates_start_transport_error() -> None:
    docker = RuntimeDocker()
    container = BoundContainer()
    container.start.side_effect = ConnectionError("http://user:secret@example.test")
    docker.containers.create.return_value = container

    with pytest.raises(RunnerRuntimeError) as exc_info:
        await DockerRunnerRuntime(docker).create_bound("runner-conv-1", {}, binding())  # type: ignore[arg-type]

    assert str(exc_info.value) == "Docker runner start failed"
    assert "secret" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_translates_show_transport_error() -> None:
    docker = RuntimeDocker()
    container = BoundContainer()
    container.show.side_effect = ConnectionError("http://user:secret@example.test")
    docker.containers.get.return_value = container

    with pytest.raises(RunnerRuntimeError) as exc_info:
        await DockerRunnerRuntime(docker).get("runner-1")  # type: ignore[arg-type]

    assert str(exc_info.value) == "Docker runner inspect failed"
    assert "secret" not in str(exc_info.value)
