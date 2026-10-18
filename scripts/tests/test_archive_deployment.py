"""Privileged lifecycle credentials must never enter normal API workloads."""

from pathlib import Path
import json
import os
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_data_template_change_selects_backend_and_deployment_harness(tmp_path):
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    picker = next(step["run"] for step in workflow["jobs"]["changes"]["steps"] if step.get("id") == "pick")
    # Exercise the actual CI picker with a synthetic PR diff, without changing git.
    stub = tmp_path / "git"
    stub.write_text("#!/bin/sh\nprintf '%s\\n' charts/aurum-ai/templates/data-worker-deployment.yaml\n")
    stub.chmod(0o700)
    output = tmp_path / "output"
    result = subprocess.run(["bash", "-c", picker], capture_output=True, text=True,
                            env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
                                 "EVENT": "pull_request", "BASE_SHA": "base", "HEAD_SHA": "head",
                                 "GITHUB_OUTPUT": str(output)})
    assert result.returncode == 0, result.stderr
    selected = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert "business-backend" in selected["python"]
    assert "scripts" in selected["harness"]


def test_standalone_admin_health_uses_its_listening_port():
    services = yaml.safe_load((ROOT / "scripts/fixtures/data-lifecycle-compose.yml").read_text())["services"]
    admin = services["backend-admin"]
    port = admin["entrypoint"][admin["entrypoint"].index("--port") + 1]
    assert f"127.0.0.1:{port}/health" in admin.get("healthcheck", {}).get("test", [""])[-1]


def render(*options):
    return subprocess.run(
        ["helm", "template", "archive-test", str(ROOT / "charts/aurum-ai"), *options],
        capture_output=True, text=True,
    )


def enabled(*options):
    result = render("--set", "dataLifecycle.enabled=true", "--set", "dataLifecycle.existingSecret=operator-only",
                    "--set", "dataLifecycle.archiveS3.endpoint=http://archive-minio:9000",
                    "--set", "dataLifecycle.destinations[0].cidr=192.0.2.1/32",
                    "--set", "dataLifecycle.destinations[0].ports[0].protocol=TCP",
                    "--set", "dataLifecycle.destinations[0].ports[0].port=5432", *options)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def test_worker_runs_during_retention_rollback_with_separate_secret_and_no_api_exposure():
    docs = enabled()
    deployments = {doc["metadata"]["name"]: doc for doc in docs if doc["kind"] == "Deployment"}
    pod = deployments["archive-test-data-worker"]["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    container = pod["containers"][0]
    assert container["command"] == ["python", "-m", "src.maintenance_worker", "data"]
    env = {entry["name"]: entry for entry in container["env"]}
    assert env["APP_ARCHIVE_ENABLED"]["value"] == "false"
    assert env["APP_ARCHIVE_RETENTION_DAYS"]["value"] == "180"
    assert env["APP_DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"] == "operator-only"
    assert env["APP_ARCHIVE_S3_SECRET_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "operator-only"
    assert not pod.get("volumes")
    assert not container.get("ports")
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert container["readinessProbe"]["exec"]["command"][-1] == "data-health"
    for component in ("backend", "backend-admin", "tool-worker"):
        runtime = deployments[f"archive-test-{component}"]["spec"]["template"]["spec"]["containers"][0]
        assert "operator-only" not in str(runtime)
        assert "APP_ARCHIVE_S3_SECRET_KEY" not in str(runtime)
    policy = next(doc for doc in docs if doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"] == "archive-test-data-worker")
    assert policy["spec"]["ingress"] == []
    assert policy["spec"]["egress"][1]["to"] == [{"ipBlock": {"cidr": "192.0.2.1/32"}}]


def test_explicit_archive_enable_and_custom_retention():
    docs = enabled("--set", "dataLifecycle.archiveEnabled=true", "--set", "dataLifecycle.retentionDays=90")
    worker = next(doc for doc in docs if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "archive-test-data-worker")
    env = {entry["name"]: entry for entry in worker["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["APP_ARCHIVE_ENABLED"]["value"] == "true"
    assert env["APP_ARCHIVE_RETENTION_DAYS"]["value"] == "90"


def test_default_has_no_privileged_worker_or_secret():
    result = render()
    assert result.returncode == 0
    assert "archive-test-data-worker" not in result.stdout
    assert "APP_ARCHIVE_S3_SECRET_KEY" not in result.stdout


def test_enable_requires_explicit_operator_secret_and_network_destinations():
    for options in (
        ("--set", "dataLifecycle.enabled=true"),
        ("--set", "dataLifecycle.enabled=true", "--set", "dataLifecycle.existingSecret=operator-only"),
        ("--set", "dataLifecycle.archiveEnabled=true"),
    ):
        assert render(*options).returncode != 0


def test_privileged_worker_cannot_disable_network_isolation():
    result = render("--set", "dataLifecycle.enabled=true", "--set", "dataLifecycle.existingSecret=operator-only",
                    "--set", "dataLifecycle.archiveS3.endpoint=http://archive-minio:9000",
                    "--set", "dataLifecycle.destinations[0].cidr=192.0.2.1/32",
                    "--set", "dataLifecycle.destinations[0].ports[0].port=9000",
                    "--set", "networkPolicies.enabled=false")
    assert result.returncode != 0


def test_compose_keeps_migrations_and_archive_credentials_outside_normal_services():
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    worker = services["data-worker"]
    assert worker["entrypoint"] == ["python", "-m", "src.maintenance_worker", "data"]
    assert worker["network_mode"] == "service:data-egress"
    assert worker["user"] == "10001:10001"
    assert not worker.get("ports") and not worker.get("volumes")
    assert worker["environment"]["APP_DATABASE_URL"] == "${APP_ARCHIVE_DATABASE_URL:-}"
    assert worker["environment"]["APP_ARCHIVE_ENABLED"] == "${APP_ARCHIVE_ENABLED:-false}"
    assert worker["environment"]["APP_ARCHIVE_RETENTION_DAYS"] == "${APP_ARCHIVE_RETENTION_DAYS:-180}"
    assert worker["depends_on"]["data-egress"]["condition"] == "service_healthy"
    assert services["rag-migrate"]["entrypoint"] == ["python", "-m", "alembic", "upgrade", "head"]
    assert services["rag-api"]["depends_on"]["rag-migrate"]["condition"] == "service_completed_successfully"
    private = services["rag-archive"]
    assert private["entrypoint"] == ["python", "-m", "uvicorn", "src.main_archive:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8111"]
    assert not private.get("ports") and not private.get("volumes")
    assert set(private["networks"]) == {"rag-control-net", "rag-net"}
    assert "RAG_API_DATABASE_URL" not in private["environment"]
    assert private["environment"]["RAG_API_S3_BUCKET"] == services["backend"]["environment"]["APP_S3_BUCKET"]
    assert services["rag-worker"]["environment"]["RAG_WORKER_S3_BUCKET"] == private["environment"]["RAG_API_S3_BUCKET"]
    for name in ("backend", "backend-admin", "rag-api", "rag-worker", "tool-worker", "identity-worker"):
        env = services[name].get("environment", {})
        assert not any("ARCHIVE_" in key for key in env)
        assert "RAG_API_MIGRATION_DATABASE_URL" not in env


def test_archive_profile_has_all_dependencies_without_enabling_normal_rag_workers():
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    enabled_services = {name for name, service in services.items()
                        if not service.get("profiles") or "archive" in service["profiles"]}
    for name in enabled_services:
        assert set(services[name].get("depends_on", {})) <= enabled_services
    assert "rag-worker" not in enabled_services


def test_selective_compose_rag_worker_includes_successful_migration():
    # Render the real targeted Compose model with an empty dotenv. Do not start
    # anything or load the operator's root .env into captured config output.
    result = subprocess.run(
        ['docker', 'compose', '--env-file', '/dev/null', '-f', str(ROOT / 'docker-compose.yml'),
         'config', '--format', 'json', 'rag-worker'],
        capture_output=True, text=True, env={'PATH': os.environ['PATH'], 'HOME': os.environ['HOME'], 'APP_SECRET_KEY': 'synthetic-compose-render-only', 'KEYCLOAK_ADMIN_PASSWORD': 'synthetic-compose-render-only'},
    )
    assert result.returncode == 0
    services = json.loads(result.stdout)['services']
    assert 'rag-migrate' in services, 'selective worker start must include schema migration'
    assert services['rag-worker']['depends_on']['rag-migrate']['condition'] == 'service_completed_successfully'
    assert services['rag-migrate']['depends_on']['postgres']['condition'] == 'service_healthy'
