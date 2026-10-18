"""Execute real Bash exit/cleanup logic with external infrastructure boundaries stubbed."""

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


def run_cleanup(tmp_path, *, primary=0, cleanup=0, verify=0, missing=False):
    # Source only definitions: never run the wrapper's live main/traps.
    definitions = (ROOT / "smoke-test-k8s.sh").read_text().split("trap on_exit EXIT", 1)[0]
    fixture_python = tmp_path / "business-backend/.venv/bin/python"
    fixture_python.parent.mkdir(parents=True)
    fixture_python.write_text(
        '#!/bin/bash\n'
        'printf "%s\\n" "$2" >> "$EVENTS"\n'
        'case "$2" in cleanup) exit "$CLEANUP_STATUS";; '
        'verify-clean) exit "$VERIFY_STATUS";; *) exit 99;; esac\n'
    )
    fixture_python.chmod(0o700)
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir()
    journal = journal_dir / "state.json"
    if not missing:
        journal.write_text("{}")
    work = tmp_path / "work"
    work.mkdir()
    (work / "kubeconfig").write_text("test kubeconfig")
    events = tmp_path / "events"
    script = definitions + r'''
REPO_ROOT="$CASE_DIR"
WORK_DIR="$CASE_DIR/work"
KUBECONFIG="$WORK_DIR/kubeconfig"
USER_QUESTIONS_STATE="$CASE_DIR/journal/state.json"
USER_QUESTIONS_RECOVERY_DIR="$CASE_DIR/journal"
USER_QUESTIONS_MARKER=user-questions-e2e-0123456789abcdef
CREATED_RELEASE=1
CREATED_CLUSTER=1
CREATED_TEMPORAL_NAMESPACE=1
CREATED_GATEWAY=1
CREATED_RELEASE_NAMESPACE=1
CREATED_OWNER_CONFIGMAP=1
CREATED_FETCH_FIXTURE=1
CREATED_NETWORK_PROBE=1
CREATED_NETWORK=1
COMPOSE_CREATED_SERVICES=(test-service)
KEEP_RESOURCES=0
docker() { printf 'docker %s\n' "$*" >> "$EVENTS"; printf 'owned-container\n'; }
kubectl() { printf 'kubectl %s\n' "$*" >> "$EVENTS"; }
helm() { printf 'helm %s\n' "$*" >> "$EVENTS"; }
k3d() { printf 'k3d %s\n' "$*" >> "$EVENTS"; }
dump_failure_diagnostics() { :; }
trap on_exit EXIT
exit "$PRIMARY_STATUS"
'''
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(script)
    result = subprocess.run(
        ["bash", str(wrapper)],
        env={**os.environ, "CASE_DIR": str(tmp_path), "EVENTS": str(events),
             "CLEANUP_STATUS": str(cleanup), "VERIFY_STATUS": str(verify),
             "PRIMARY_STATUS": str(primary)},
        capture_output=True, text=True, timeout=10,
    )
    calls = events.read_text().splitlines() if events.exists() else []
    return result, calls, journal, work


@pytest.mark.parametrize("cleanup,verify,want", [(7, 0, 7), (0, 9, 9), (7, 9, 7)])
@pytest.mark.parametrize("primary", [0, 23])
def test_recovery_failure_preserves_runtime_journal_and_primary_exit(
    tmp_path, cleanup, verify, want, primary
):
    result, calls, journal, work = run_cleanup(
        tmp_path, primary=primary, cleanup=cleanup, verify=verify
    )
    assert journal.exists()
    assert (work / "kubeconfig").exists(), "failed recovery must retain kubeconfig"
    assert not any(call.startswith(("docker ", "kubectl ", "helm ", "k3d ")) for call in calls)
    assert calls == (["cleanup"] if cleanup else ["cleanup", "verify-clean"])
    assert result.returncode == (primary or want)


def test_missing_established_journal_blocks_teardown(tmp_path):
    result, calls, _, work = run_cleanup(tmp_path, missing=True)
    assert result.returncode != 0
    assert (work / "kubeconfig").exists()
    assert not calls


@pytest.mark.parametrize("primary", [0, 23])
def test_verified_recovery_allows_owned_teardown(tmp_path, primary):
    result, calls, journal, work = run_cleanup(tmp_path, primary=primary)
    assert calls[:2] == ["cleanup", "verify-clean"]
    assert any(call.startswith("helm uninstall ") for call in calls)
    assert any(call.startswith("k3d cluster delete ") for call in calls)
    assert any(
        "temporal-admin-tools" in call and "operator namespace delete" in call
        for call in calls
    )
    assert not journal.exists()
    assert not work.exists()
    assert result.returncode == primary
