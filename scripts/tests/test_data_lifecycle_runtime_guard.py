"""The disposable backend must never reap a runner belonging to another stack."""

import importlib.util
from pathlib import Path

import pytest


def load_guard():
    path = Path(__file__).parents[1] / "check-data-lifecycle-runtime.py"
    spec = importlib.util.spec_from_file_location("lifecycle_runtime_guard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name,labels", [
    ("/runner-pool-owned", {}),
    ("/runner-1234", {}),
    ("/custom-name", {"aurum-ai.component": "conversation-runner"}),
])
def test_foreign_runner_prevents_test_backend_start(name, labels):
    guard = load_guard()
    with pytest.raises(ValueError, match="foreign_runner"):
        guard.validate_runner({"Name": name, "Config": {"Labels": labels},
                               "NetworkSettings": {"Networks": {"aurum-ai_runner-net": {}}}})


def test_only_exact_owned_network_is_accepted():
    guard = load_guard()
    runner = {"Name": "/runner-owned", "Config": {"Labels": {}},
              "NetworkSettings": {"Networks": {"phaseb-lifecycle-test_runner": {}}}}
    guard.validate_runner(runner)
    runner["NetworkSettings"]["Networks"]["foreign"] = {}
    with pytest.raises(ValueError, match="foreign_runner"):
        guard.validate_runner(runner)


def test_unrelated_infrastructure_is_not_a_runner():
    load_guard().validate_runner({"Name": "/postgres", "Config": {"Labels": {}},
                                  "NetworkSettings": {"Networks": {"other": {}}}})
