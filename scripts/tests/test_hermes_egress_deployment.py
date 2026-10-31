"""Compose deployment of hermes-egress (docker-compose.hermes.yml)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
OVERLAY = ROOT / "docker-compose.hermes.yml"
ACCEPTANCE = ROOT / "smoke-test-hermes-egress.sh"
pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker compose is required")


def _render(**env: str) -> dict[str, Any]:
    environment = os.environ.copy()
    environment.update(
        {
            "APP_SECRET_KEY": "deployment-contract-secret-not-for-runtime",
            "KEYCLOAK_ADMIN_PASSWORD": "deployment-contract-keycloak-not-for-runtime",
            "HERMES_EGRESS_SERVICE_SECRET": "hermes-egress-contract-secret",
            **env,
        }
    )
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "-f", str(OVERLAY), "--profile", "hermes",
         "--profile", "skills-exec", "--profile", "build", "config", "--format", "json"],
        cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_sandbox_networks_are_per_sandbox_and_never_declared_here() -> None:
    config = _render()
    services, networks = config["services"], config["networks"]
    guard = services["hermes-egress-guard"]

    assert "hermes-sandbox" not in networks
    assert networks["hermes-backend"]["internal"] is True
    assert guard["container_name"] == "aurum-ai-hermes-egress-guard"
    assert set(guard["networks"]) == {"hermes-backend", "hermes-public"}
    assert guard["environment"]["HERMES_EGRESS_SANDBOX_CIDRS"] == "172.31.0.0/16"
    assert set(services["backend"]["networks"]) >= {"hermes-backend"}


def test_skill_executor_creates_sandboxes_from_the_guards_pool() -> None:
    services = _render(HERMES_ENABLED="true", HERMES_SANDBOX_POOL="172.30.0.0/16")["services"]
    executor = services["skill-executor"]["environment"]

    assert executor["SKILL_EXECUTOR_HERMES_ENABLED"] == "true"
    assert executor["SKILL_EXECUTOR_HERMES_GUARD_CONTAINER"] == "aurum-ai-hermes-egress-guard"
    assert executor["SKILL_EXECUTOR_HERMES_SUBNET_POOL"] == "172.30.0.0/16"
    assert services["hermes-egress-guard"]["environment"]["HERMES_EGRESS_SANDBOX_CIDRS"] == "172.30.0.0/16"
    assert services["hermes-sandbox"]["profiles"] == ["build"]


def test_guard_holds_net_admin_and_the_proxy_holds_nothing() -> None:
    services = _render()["services"]
    guard, proxy = services["hermes-egress-guard"], services["hermes-egress"]

    assert guard["cap_drop"] == ["ALL"] and guard["cap_add"] == ["NET_ADMIN"]
    assert proxy["cap_drop"] == ["ALL"] and "cap_add" not in proxy
    assert proxy["network_mode"] == "service:hermes-egress-guard"
    assert proxy["user"] == "65534:65534"
    assert guard["read_only"] is True and proxy["read_only"] is True
    assert proxy["entrypoint"][:5] == ["python3", "/usr/local/bin/web-egress-readiness", "--wait", "--port", "8192"]
    assert proxy["depends_on"]["hermes-egress-guard"]["condition"] == "service_healthy"
    assert guard["depends_on"]["backend"]["restart"] is True


@pytest.mark.parametrize("mode", ["none", "packages", "public"])
def test_one_variable_sets_the_maximum_for_backend_guard_and_proxy(mode: str) -> None:
    services = _render(HERMES_EGRESS_MAX_MODE=mode)["services"]

    assert services["backend"]["environment"]["APP_HERMES_EGRESS_MAX_MODE"] == mode
    assert services["backend-admin"]["environment"]["APP_HERMES_EGRESS_MAX_MODE"] == mode
    assert services["hermes-egress-guard"]["environment"]["HERMES_EGRESS_MAX_MODE"] == mode
    assert services["hermes-egress"]["environment"]["HERMES_EGRESS_MAX_MODE"] == mode


def test_defaults_are_off_and_packages() -> None:
    services = _render()["services"]

    assert services["backend"]["environment"]["APP_HERMES_ENABLED"] == "false"
    assert services["hermes-egress"]["environment"]["HERMES_EGRESS_MAX_MODE"] == "packages"
    assert services["hermes-egress"]["environment"]["HERMES_EGRESS_PACKAGE_HOSTS"] == "pypi.org,files.pythonhosted.org,registry.npmjs.org"


def test_the_service_secret_is_required() -> None:
    environment = {key: value for key, value in os.environ.items() if key != "HERMES_EGRESS_SERVICE_SECRET"}
    environment["APP_SECRET_KEY"] = "deployment-contract-secret-not-for-runtime"
    environment["KEYCLOAK_ADMIN_PASSWORD"] = "deployment-contract-keycloak-not-for-runtime"
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "-f", str(OVERLAY), "--profile", "hermes", "config"],
        cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
    )

    assert result.returncode != 0
    assert "HERMES_EGRESS_SERVICE_SECRET must be set" in result.stderr


def test_acceptance_attaches_probes_like_a_sandbox_and_is_disposable() -> None:
    script = ACCEPTANCE.read_text()
    base = yaml.safe_load((ROOT / "scripts" / "fixtures" / "hermes-egress-base.yml").read_text())

    assert "PROJECT=hermes-egress-test" in script
    assert '--network "$SANDBOX_NET"' in script
    assert "gateway_mode_ipv4=isolated" in script
    assert 'down -v --remove-orphans' in script
    assert "docker-compose.hermes.yml" in script
    assert all("hermes-sandbox" not in service.get("networks", []) for service in base["services"].values())


def test_ci_runs_the_harness_when_the_overlay_changes() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "grep -qx 'docker-compose.hermes.yml'" in workflow
    assert "scripts/.hermes-egress-trigger" in workflow


@pytest.mark.parametrize("service", ["backend", "backend-admin"])
def test_enabled_hermes_loads_in_both_api_planes(service: str) -> None:
    rendered = _render(HERMES_ENABLED="true")["services"][service]["environment"]
    check = (
        "from src.core.config import settings\n"
        "from src.services.hermes_egress import require_egress_secret\n"
        "assert settings.HERMES_ENABLED\n"
        + ("require_egress_secret()\n" if service == "backend" else "")
    )
    result = subprocess.run(
        ["python", "-c", check],
        cwd=ROOT / "business-backend",
        env={"PATH": os.environ["PATH"], **{key: str(value) for key, value in rendered.items()}},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr[-2000:]


def _render_previews(**env: str) -> dict[str, Any]:
    environment = os.environ.copy()
    environment.update(
        {
            "APP_SECRET_KEY": "deployment-contract-secret-not-for-runtime",
            "KEYCLOAK_ADMIN_PASSWORD": "deployment-contract-keycloak-not-for-runtime",
            "HERMES_EGRESS_SERVICE_SECRET": "hermes-egress-contract-secret",
            **env,
        }
    )
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "-f", str(OVERLAY), "--profile", "hermes",
         "--profile", "hermes-preview", "--profile", "skills-exec", "config", "--format", "json"],
        cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_previews_are_off_by_default_and_their_service_holds_nothing() -> None:
    services = _render_previews(
        HERMES_PREVIEW_ORIGIN="http://{preview}.preview.localhost:8095",
        HERMES_PREVIEW_SERVICE_SECRET="preview-contract-secret",
        HERMES_PREVIEW_EXECUTOR_SECRET="executor-contract-secret",
    )["services"]
    preview = services["hermes-preview"]

    assert services["backend"]["environment"]["APP_HERMES_PREVIEW_ENABLED"] == "false"
    assert services["skill-executor"]["environment"]["SKILL_EXECUTOR_HERMES_PREVIEW_SECRET"] == "executor-contract-secret"
    assert preview["user"] == "10001:10001" and preview["read_only"] is True
    assert preview["cap_drop"] == ["ALL"] and "no-new-privileges:true" in preview["security_opt"]
    assert preview["environment"]["HERMES_PREVIEW_BACKEND_URL"] == "http://hermes-backend:8000"
    assert preview["environment"]["HERMES_PREVIEW_EXECUTOR_URL"] == "ws://skill-executor:8090"
    # It never joins a sandbox network: sandboxes are reached only through skill-executor.
    assert set(preview["networks"]) == {"hermes-backend", "skills-net", "hermes-public"}
    assert "volumes" not in preview


def test_the_overlay_without_previews_needs_none_of_their_settings() -> None:
    services = _render()["services"]

    assert "hermes-preview" not in services
