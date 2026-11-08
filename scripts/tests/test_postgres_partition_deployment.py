"""Partition migrations must have the same extension in Compose and CI."""

from pathlib import Path
import json
import os
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_compose_builds_partition_capable_postgres():
    service = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["postgres"]
    assert service["build"] == "./infra/postgres"
    assert service["image"] == "aurum-ai-postgres:latest"


def test_ci_installs_extension_before_backend_migrations():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["python"]["steps"]
    extension = next(
        i for i, step in enumerate(steps) if step.get("name") == "Install partition extension"
    )
    migration = next(i for i, step in enumerate(steps) if step.get("name") == "Run migrations")
    assert extension < migration
    assert steps[extension]["if"] == "matrix.service == 'business-backend'"
    assert "infra/postgres/install-partman.sh" in steps[extension]["run"]
    dockerfile = (ROOT / "infra/postgres/Dockerfile").read_text()
    assert "install-partman.sh" in dockerfile
    assert "COPY partman.sql /docker-entrypoint-initdb.d/03-partman.sql" in dockerfile
    assert "infra/postgres/partman.sql" in steps[extension]["run"]
    service = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["postgres"]
    assert not any(mount.endswith(":/docker-entrypoint-initdb.d") for mount in service["volumes"])


def test_partition_image_changes_exercise_backend_and_image():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    picker = next(s["run"] for s in workflow["jobs"]["changes"]["steps"] if s.get("id") == "pick")
    assert "infra/postgres" in next(
        line for line in picker.splitlines() if line.strip().startswith("images=")
    )
    assert "business-backend/.partition-infra-trigger" in picker


@pytest.mark.parametrize("overrides, expected", [
    ({}, ("20", "40", "30")),
    ({"APP_DB_POOL_SIZE": "7", "APP_DB_MAX_OVERFLOW": "0", "APP_DB_POOL_TIMEOUT": "1.5"}, ("7", "0", "1.5")),
])
def test_both_compose_api_planes_receive_database_pool_settings(overrides, expected):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("APP_DB_")}
    environment.update({
        "APP_SECRET_KEY": "pool-deployment-contract-secret",
        "KEYCLOAK_ADMIN_PASSWORD": "pool-deployment-contract-keycloak",
        **overrides,
    })
    result = subprocess.run(
        ["docker", "compose", "--env-file", os.devnull, "-f", "docker-compose.yml", "config", "--format", "json"],
        cwd=ROOT, env=environment, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    keys = ("APP_DB_POOL_SIZE", "APP_DB_MAX_OVERFLOW", "APP_DB_POOL_TIMEOUT")
    for service in ("backend", "backend-admin"):
        assert tuple(services[service]["environment"].get(key) for key in keys) == expected


@pytest.mark.parametrize("overrides, expected", [
    ([], {"backend": ("20", "40", "30"), "backend-admin": ("20", "40", "30")}),
    (["backend.databasePool.size=7", "backend.databasePool.maxOverflow=0", "backend.databasePool.timeout=1.5",
      "backendAdmin.databasePool.size=3", "backendAdmin.databasePool.maxOverflow=5", "backendAdmin.databasePool.timeout=9.25"],
     {"backend": ("7", "0", "1.5"), "backend-admin": ("3", "5", "9.25")}),
])
def test_both_helm_api_planes_receive_independent_database_pool_settings(overrides, expected):
    command = ["helm", "template", "pool", str(ROOT / "charts" / "aurum-ai")]
    for setting in overrides:
        command.extend(("--set", setting))
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    deployments = {item["metadata"]["name"]: item for item in yaml.safe_load_all(result.stdout)
                   if item and item["kind"] == "Deployment"}
    keys = ("APP_DB_POOL_SIZE", "APP_DB_MAX_OVERFLOW", "APP_DB_POOL_TIMEOUT")
    for service, values in expected.items():
        container = deployments[f"pool-{service}"]["spec"]["template"]["spec"]["containers"][0]
        env = {item["name"]: item.get("value") for item in container["env"]}
        assert tuple(env.get(key) for key in keys) == values
