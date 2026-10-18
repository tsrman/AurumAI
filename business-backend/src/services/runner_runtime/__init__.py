from src.services.runner_runtime.base import (
    RunnerBinding,
    RunnerPermanentError,
    RunnerReadinessError,
    RunnerResource,
    RunnerRuntime,
    RunnerRuntimeError,
)
from src.services.runner_runtime.docker import DockerRunnerRuntime
from src.services.runner_runtime.kubernetes import KubernetesRunnerRuntime
from typing import cast


def create_runner_runtime(runtime_settings: object) -> RunnerRuntime:
    """Build the configured runtime without making callers depend on a driver."""
    if getattr(runtime_settings, "RUNNER_RUNTIME") == "kubernetes":
        return cast(RunnerRuntime, KubernetesRunnerRuntime())
    return cast(RunnerRuntime, DockerRunnerRuntime())

__all__ = [
    "DockerRunnerRuntime",
    "KubernetesRunnerRuntime",
    "create_runner_runtime",
    "RunnerBinding",
    "RunnerPermanentError",
    "RunnerReadinessError",
    "RunnerResource",
    "RunnerRuntime",
    "RunnerRuntimeError",
]
