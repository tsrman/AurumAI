import pytest
from pydantic import ValidationError

from src.core.config import Settings


def test_kubernetes_runtime_requires_disabled_warm_pool() -> None:
    with pytest.raises(ValidationError, match="RUNNER_POOL_SIZE must be 0"):
        Settings(_env_file=None, DEBUG=True, RUNNER_RUNTIME="kubernetes", RUNNER_POOL_SIZE=1)


def test_kubernetes_runtime_defaults_are_safe() -> None:
    value = Settings(_env_file=None, DEBUG=True, RUNNER_RUNTIME="kubernetes", RUNNER_POOL_SIZE=0)

    assert value.KUBERNETES_RUNNER_NAMESPACE == "aurum-ai-runners"
    assert value.KUBERNETES_RUNNER_READY_TIMEOUT_SECONDS == 60
    assert value.KUBERNETES_RUNNER_IMAGE_PULL_POLICY == "IfNotPresent"
