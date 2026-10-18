"""Fresh Temporal startup dependencies and owned Compose setup containers."""

import os
from pathlib import Path
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("consumer", ["backend", "tool-worker", "rag-api", "rag-worker", "temporal-ui"])
def test_temporal_consumers_wait_for_namespace(consumer):
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    assert services[consumer]["depends_on"].get("temporal-create-namespace") == {
        "condition": "service_completed_successfully"
    }
    assert services["temporal-create-namespace"]["depends_on"]["temporal"] == {
        "condition": "service_healthy"
    }


@pytest.mark.parametrize("original_state", ["missing", "stopped", "running"])
def test_k8s_harness_starts_and_restores_setup_services(tmp_path, original_state):
    source = (ROOT / "smoke-test-k8s.sh").read_text()
    definitions = source.split("trap on_exit EXIT", 1)[0]
    definitions += "\ncompose_service_state() {" + source.split("compose_service_state() {", 1)[1].split("wait_for_temporal() {", 1)[0]
    script = definitions + r'''
docker() { printf '%s\n' "$*" >> "$EVENTS"; }
compose_service_state() { printf '%s\n' "$ORIGINAL_STATE"; }
start_compose_data_plane
cleanup
'''
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(script)
    events = tmp_path / "events"
    result = subprocess.run(
        ["bash", str(wrapper)], capture_output=True, text=True, timeout=10,
        env={**os.environ, "EVENTS": str(events), "ORIGINAL_STATE": original_state,
             "KEEP_RESOURCES": "0"},
    )
    assert result.returncode == 0, result.stderr
    calls = events.read_text().splitlines()
    up = next(call.split() for call in calls if " up " in call)
    for service in ("temporal-admin-tools", "temporal-create-namespace"):
        assert service in up
        stopped = any(call.endswith(f"stop {service}") for call in calls)
        removed = any(call.endswith(f"rm -f {service}") for call in calls)
        assert stopped == (original_state != "running")
        assert removed == (original_state == "missing")
