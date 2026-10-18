import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from unittest.mock import AsyncMock, patch

from src.core.config import settings
from src.routers import health as health_router
from src.services.runner_runtime import RunnerReadinessError
from src.services.session_manager import session_manager


@pytest.fixture
def client():
    test_app = FastAPI()
    test_app.include_router(health_router.router)
    return httpx.AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test")


@pytest.fixture
def all_checks_pass(monkeypatch):
    """All three required dependencies respond."""

    async def ok() -> None:
        return None

    monkeypatch.setattr(health_router, "_check_database", ok)
    monkeypatch.setattr(health_router, "_check_redis", ok)
    monkeypatch.setattr(health_router, "_check_keycloak", ok)


def _configure_runner_runtime_probe(monkeypatch, runtime: str) -> None:
    monkeypatch.setattr(health_router, "settings", settings, raising=False)
    monkeypatch.setattr(health_router.settings, "RUNNER_RUNTIME", runtime)
    monkeypatch.setattr(health_router, "session_manager", session_manager, raising=False)


@pytest.mark.asyncio
async def test_health_returns_ok(client):
    async with client as c:
        response = await c.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_health_does_not_touch_dependencies(client, monkeypatch):
    """/health checks process liveness. An unavailable DB must not cause
    an endless restart loop for an otherwise responsive container."""

    async def boom() -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(health_router, "_check_database", boom)
    monkeypatch.setattr(health_router, "_check_redis", boom)
    monkeypatch.setattr(health_router, "_check_keycloak", boom)

    async with client as c:
        response = await c.get("/health")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_ready_returns_ready_when_all_dependencies_answer(client, all_checks_pass):
    async with client as c:
        response = await c.get("/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": "ok", "redis": "ok", "keycloak": "ok"},
    }


@pytest.mark.asyncio
async def test_ready_checks_runner_runtime_when_kubernetes(client, all_checks_pass, monkeypatch):
    """If the backend is configured to launch runners in Kubernetes, /ready
    must verify that API path before the load balancer sends chat traffic."""

    _configure_runner_runtime_probe(monkeypatch, "kubernetes")

    with patch(
        "src.routers.health.session_manager.ready", new_callable=AsyncMock, create=True
    ) as check:
        async with client as c:
            response = await c.get("/ready")

    assert response.status_code == 200
    assert response.json()["checks"]["runner_runtime"] == "ok"
    check.assert_awaited_once()


@pytest.mark.asyncio
async def test_ready_omits_runner_runtime_when_using_docker(client, all_checks_pass, monkeypatch):
    """Docker runner readiness is intentionally not part of /ready; Docker
    deployments have long-lived backend instances where temporary runner
    churn must not remove the whole API process from service."""

    _configure_runner_runtime_probe(monkeypatch, "docker")

    with patch(
        "src.routers.health.session_manager.ready", new_callable=AsyncMock, create=True
    ) as check:
        async with client as c:
            response = await c.get("/ready")

    assert response.status_code == 200
    assert "runner_runtime" not in response.json()["checks"]
    check.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_runner_runtime_failure_reports_type_without_secret(
    client, all_checks_pass, monkeypatch
):
    """A Kubernetes API readiness failure may carry URLs or credentials in
    the exception message, so the response names only the exception type."""

    _configure_runner_runtime_probe(monkeypatch, "kubernetes")
    leaked_message = "https://token:supersecret@kubernetes.default/api failed"

    async def boom() -> None:
        raise RunnerReadinessError(leaked_message)

    with patch("src.routers.health.session_manager.ready", side_effect=boom, create=True):
        async with client as c:
            response = await c.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not ready"
    assert body["checks"]["runner_runtime"] == "fail"
    assert body["errors"]["runner_runtime"] == "RunnerReadinessError"
    assert leaked_message not in response.text
    assert "supersecret" not in response.text


@pytest.mark.asyncio
async def test_ready_runner_runtime_timeout_reports_type_without_details(
    client, all_checks_pass, monkeypatch
):
    """A hung Kubernetes API probe should fail closed without leaking timing
    internals or hanging the readiness endpoint."""

    import asyncio

    _configure_runner_runtime_probe(monkeypatch, "kubernetes")
    monkeypatch.setattr(health_router, "_CHECK_TIMEOUT_SECONDS", 0.01)

    async def hang() -> None:
        await asyncio.sleep(60)

    with patch("src.routers.health.session_manager.ready", side_effect=hang, create=True):
        async with client as c:
            response = await c.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["runner_runtime"] == "fail"
    assert body["errors"]["runner_runtime"] == "TimeoutError"
    assert "0.01" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["_check_database", "_check_redis", "_check_keycloak"])
async def test_ready_returns_503_when_a_dependency_is_down(client, all_checks_pass, monkeypatch, broken):
    async def boom() -> None:
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(health_router, broken, boom)

    async with client as c:
        response = await c.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not ready"
    assert body["checks"][broken.removeprefix("_check_")] == "fail"


@pytest.mark.asyncio
async def test_ready_does_not_leak_exception_text(client, all_checks_pass, monkeypatch):
    """Exception text can contain credentials; expose only the safe failure status."""

    async def boom() -> None:
        raise RuntimeError("postgresql://user:sup3rsecret@postgres:5432/db unavailable")

    monkeypatch.setattr(health_router, "_check_database", boom)

    async with client as c:
        response = await c.get("/ready")

    assert response.status_code == 503
    assert "sup3rsecret" not in response.text


@pytest.mark.asyncio
async def test_ready_times_out_instead_of_hanging(client, all_checks_pass, monkeypatch):
    """A stalled dependency must not stall the readiness probe itself."""
    import asyncio

    async def hang() -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(health_router, "_check_redis", hang)
    monkeypatch.setattr(health_router, "_CHECK_TIMEOUT_SECONDS", 0.05)

    async with client as c:
        response = await c.get("/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["redis"] == "fail"
