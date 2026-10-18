from dataclasses import dataclass
from typing import Literal, Mapping, Protocol


class RunnerRuntimeError(RuntimeError):
    """A safe runtime error suitable for returning from orchestration code.

    Runtime adapters must only pass diagnostic messages that cannot contain
    credentials, tokens, or credential-bearing URLs to this exception.
    """


class RunnerPermanentError(RunnerRuntimeError):
    """An error which cannot be resolved by retrying the runtime operation."""


class RunnerReadinessError(RunnerRuntimeError):
    """A runner resource was created but did not become ready."""


@dataclass(frozen=True)
class RunnerBinding:
    conversation_id: str
    user_id: str
    org_id: str | None
    session_token: str
    task_queue: str


@dataclass(frozen=True)
class RunnerResource:
    resource_id: str
    name: str
    uid: str
    ip_address: str | None
    state: str
    ready: bool
    created_at: float
    labels: dict[str, str]


class RunnerRuntime(Protocol):
    kind: Literal["docker", "kubernetes"]
    supports_warm_pool: bool

    async def create_bound(
        self, name: str, static_env: Mapping[str, str], binding: RunnerBinding
    ) -> RunnerResource: ...

    async def create_unbound(
        self, name: str, static_env: Mapping[str, str]
    ) -> RunnerResource: ...

    async def bind(
        self, resource: RunnerResource, new_name: str, binding: RunnerBinding
    ) -> RunnerResource: ...

    async def refresh_token(self, resource: RunnerResource, token: str) -> bool: ...

    async def pool_is_unbound(self, resource: RunnerResource) -> bool: ...

    async def get(self, resource_id: str) -> RunnerResource | None: ...

    async def list(self) -> list[RunnerResource]: ...

    async def delete(self, resource_id: str, uid: str | None = None) -> None: ...

    async def close(self) -> None: ...

    async def ready(self) -> None: ...
