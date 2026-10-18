from src.config import Settings
from src.sandbox_runtime.base import (
    BundleExtractionError,
    NetworkPolicyError,
    SandboxExecError,
    SandboxRuntime,
    SandboxSession,
    SessionNotFoundError,
    _bundle_to_tar,
    _wrap_single_file_tar,
)
from src.sandbox_runtime.docker import DockerSandboxRuntime
from src.sandbox_runtime.kubernetes import KubernetesSandboxRuntime


def create_sandbox_runtime(runtime_settings: Settings) -> SandboxRuntime:
    if runtime_settings.runtime == "docker":
        return DockerSandboxRuntime(runtime_settings=runtime_settings)
    return KubernetesSandboxRuntime(runtime_settings=runtime_settings)


__all__ = [
    "BundleExtractionError",
    "DockerSandboxRuntime",
    "KubernetesSandboxRuntime",
    "NetworkPolicyError",
    "SandboxExecError",
    "SandboxRuntime",
    "SandboxSession",
    "SessionNotFoundError",
    "_bundle_to_tar",
    "_wrap_single_file_tar",
    "create_sandbox_runtime",
]
