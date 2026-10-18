from src.config import Settings


def test_kubernetes_sandbox_defaults() -> None:
    value = Settings(_env_file=None, runtime="kubernetes")

    assert value.kubernetes_namespace == "aurum-ai-sandboxes"
    assert value.kubernetes_ready_timeout_seconds == 60
    assert value.kubernetes_image_pull_policy == "IfNotPresent"
