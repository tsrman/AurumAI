import asyncio
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from src.container_manager import (
    BundleExtractionError,
    NetworkPolicyError,
    SessionNotFoundError,
    SandboxSession,
)
from src.object_storage import ObjectStorageError
from src.main import app


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


BUNDLE_BYTES = b"fake-zip-bytes"
CHECKSUM = hashlib.sha256(BUNDLE_BYTES).hexdigest()


@pytest.mark.asyncio
async def test_open_session_success(client):
    session = SandboxSession(
        session_id="sess-1",
        resource_id="c1",
        resource_uid="c1",
        resource_name="skill-sandbox-sess-1",
        skill_md="instructions",
        created_at=1000.0,
        last_active=1000.0,
    )
    with patch(
        "src.main.object_storage.get_bytes", AsyncMock(return_value=BUNDLE_BYTES)
    ), patch(
        "src.main.extract_skill_md_body", MagicMock(return_value="instructions")
    ), patch(
        "src.main.sandbox_runtime.create_session", AsyncMock(return_value=session)
    ):
        response = await client.post(
            "/sessions",
            json={
                "skill_id": "skill-1",
                "bundle_storage_key": "skills/org/abc.zip",
                "bundle_checksum": CHECKSUM,
                "network_policy": "none",
            },
        )
    assert response.status_code == 201
    data = response.json()
    assert data["session_id"] == "sess-1"
    assert data["skill_md"] == "instructions"


@pytest.mark.asyncio
async def test_open_session_rejects_checksum_mismatch(client):
    with patch("src.main.object_storage.get_bytes", AsyncMock(return_value=BUNDLE_BYTES)):
        response = await client.post(
            "/sessions",
            json={
                "skill_id": "skill-1",
                "bundle_storage_key": "skills/org/abc.zip",
                "bundle_checksum": "deadbeef" * 8,
                "network_policy": "none",
            },
        )
    assert response.status_code == 502
    assert "checksum mismatch" in response.json()["detail"]


@pytest.mark.asyncio
async def test_open_session_502_when_storage_unreachable(client):
    with patch(
        "src.main.object_storage.get_bytes",
        AsyncMock(side_effect=ObjectStorageError("connection refused")),
    ):
        response = await client.post(
            "/sessions",
            json={
                "skill_id": "skill-1",
                "bundle_storage_key": "skills/org/abc.zip",
                "bundle_checksum": CHECKSUM,
            },
        )
    assert response.status_code == 502


@pytest.mark.asyncio
async def test_open_session_502_on_bad_bundle(client):
    with patch(
        "src.main.object_storage.get_bytes", AsyncMock(return_value=BUNDLE_BYTES)
    ), patch(
        "src.main.extract_skill_md_body", MagicMock(return_value="instructions")
    ), patch(
        "src.main.sandbox_runtime.create_session",
        AsyncMock(side_effect=BundleExtractionError("bad zip")),
    ):
        response = await client.post(
            "/sessions",
            json={
                "skill_id": "skill-1",
                "bundle_storage_key": "skills/org/abc.zip",
                "bundle_checksum": CHECKSUM,
            },
        )
    assert response.status_code == 502


@pytest.mark.asyncio
async def test_open_session_422_when_egress_network_not_configured(client):
    with patch(
        "src.main.object_storage.get_bytes", AsyncMock(return_value=BUNDLE_BYTES)
    ), patch(
        "src.main.extract_skill_md_body", MagicMock(return_value="instructions")
    ), patch(
        "src.main.sandbox_runtime.create_session",
        AsyncMock(side_effect=NetworkPolicyError("egress not configured")),
    ):
        response = await client.post(
            "/sessions",
            json={
                "skill_id": "skill-1",
                "bundle_storage_key": "skills/org/abc.zip",
                "bundle_checksum": CHECKSUM,
                "network_policy": "egress",
            },
        )
    assert response.status_code == 422
    assert "egress not configured" in response.json()["detail"]


@pytest.mark.asyncio
async def test_exec_in_session_success(client):
    result = {"stdout": "hi", "stderr": "", "exit_code": 0, "timed_out": False}
    with patch("src.main.sandbox_runtime.exec_command", AsyncMock(return_value=result)):
        response = await client.post("/sessions/sess-1/exec", json={"command": "echo hi"})
    assert response.status_code == 200
    assert response.json() == result


@pytest.mark.asyncio
async def test_exec_in_session_404_for_unknown_session(client):
    with patch(
        "src.main.sandbox_runtime.exec_command",
        AsyncMock(side_effect=SessionNotFoundError("nope")),
    ):
        response = await client.post("/sessions/nope/exec", json={"command": "echo hi"})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_close_session_always_204(client):
    with patch("src.main.sandbox_runtime.close_session", AsyncMock()) as mock_close:
        response = await client.delete("/sessions/sess-1")
    assert response.status_code == 204
    mock_close.assert_awaited_once_with("sess-1")


@pytest.mark.asyncio
async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_kubernetes_ready_returns_200(client, monkeypatch):
    monkeypatch.setattr("src.main.settings.runtime", "kubernetes")
    with patch("src.main.sandbox_runtime.ready", AsyncMock()) as check:
        response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"sandbox_runtime": "ok"},
    }
    check.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "error_name"),
    [
        (PermissionError("https://token:secret@cluster forbidden"), "PermissionError"),
        (FileNotFoundError("namespace secret-name missing"), "FileNotFoundError"),
    ],
)
async def test_kubernetes_ready_returns_sanitized_503(
    client, monkeypatch, failure: Exception, error_name: str
):
    monkeypatch.setattr("src.main.settings.runtime", "kubernetes")
    with patch("src.main.sandbox_runtime.ready", AsyncMock(side_effect=failure)):
        response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not ready",
        "checks": {"sandbox_runtime": "fail"},
        "errors": {"sandbox_runtime": error_name},
    }
    assert str(failure) not in response.text
    assert "secret" not in response.text


@pytest.mark.asyncio
async def test_kubernetes_ready_times_out_with_sanitized_503(client, monkeypatch):
    monkeypatch.setattr("src.main.settings.runtime", "kubernetes")
    monkeypatch.setattr("src.main._READY_TIMEOUT_SECONDS", 0.001, raising=False)

    async def hang() -> None:
        await asyncio.sleep(60)

    with patch("src.main.sandbox_runtime.ready", side_effect=hang):
        response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json()["errors"] == {"sandbox_runtime": "TimeoutError"}
    assert "0.001" not in response.text
