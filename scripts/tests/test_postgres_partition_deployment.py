"""Partition migrations must have the same extension in Compose and CI."""

from pathlib import Path

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
