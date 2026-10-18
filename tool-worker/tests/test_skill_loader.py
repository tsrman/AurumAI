from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.config import settings
from src.skill_loader import fetch_skill


@pytest.fixture(autouse=True)
def _configure_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "backend_api_url", "http://backend:8000")
    monkeypatch.setattr(settings, "session_token", "tok-1")


def _make_client(response: MagicMock) -> MagicMock:
    client = MagicMock()
    client.__aenter__.return_value = client
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=response)
    return client


@pytest.mark.asyncio
async def test_fetch_skill_returns_data_on_success():
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"data": {"id": "skill-1", "type": "mcp"}}
    client = _make_client(response)

    with patch("httpx.AsyncClient", return_value=client):
        result = await fetch_skill("skill-1")

    assert result == {"id": "skill-1", "type": "mcp"}
    client.get.assert_awaited_once_with(
        "http://backend:8000/v1/skills/skill-1",
        headers={"Authorization": "Bearer tok-1"},
    )


@pytest.mark.asyncio
async def test_fetch_skill_normalizes_camel_case_keys_to_snake_case():
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "data": {
            "id": "skill-1",
            "type": "mcp",
            "mcpTransport": "sse",
            "mcpEndpoint": "http://mcp-everything:3001/sse",
            "mcpAuthToken": "secret-token",
            "activityName": "echo",
            "inputSchema": {"type": "object"},
        }
    }
    client = _make_client(response)

    with patch("httpx.AsyncClient", return_value=client):
        result = await fetch_skill("skill-1")

    assert result == {
        "id": "skill-1",
        "type": "mcp",
        "mcp_transport": "sse",
        "mcp_endpoint": "http://mcp-everything:3001/sse",
        "mcp_auth_token": "secret-token",
        "activity_name": "echo",
        "input_schema": {"type": "object"},
    }


@pytest.mark.asyncio
async def test_fetch_skill_uses_provided_session_token_over_settings_default():
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"data": {"id": "skill-1", "type": "mcp"}}
    client = _make_client(response)

    with patch("httpx.AsyncClient", return_value=client):
        await fetch_skill("skill-1", session_token="conv-session-token")

    client.get.assert_awaited_once_with(
        "http://backend:8000/v1/skills/skill-1",
        headers={"Authorization": "Bearer conv-session-token"},
    )


@pytest.mark.asyncio
async def test_fetch_skill_maps_404_to_value_error():
    response = MagicMock()
    response.status_code = 404
    response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("not found", request=None, response=response)
    )
    client = _make_client(response)

    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(ValueError, match="Skill skill-missing not found"):
            await fetch_skill("skill-missing")


@pytest.mark.asyncio
async def test_fetch_skill_rechecks_backend_after_live_grant_revocation():
    allowed = MagicMock()
    allowed.raise_for_status = MagicMock()
    allowed.json.return_value = {"data": {"id": "skill-live", "type": "mcp"}}
    revoked = MagicMock()
    revoked.status_code = 404
    revoked.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("not found", request=None, response=revoked)
    )
    client = _make_client(allowed)
    client.get = AsyncMock(side_effect=[allowed, revoked])

    with patch("httpx.AsyncClient", return_value=client):
        first = await fetch_skill("skill-live", session_token="same-runner-token")
        with pytest.raises(ValueError, match="Skill skill-live not found"):
            await fetch_skill("skill-live", session_token="same-runner-token")

    assert first["id"] == "skill-live"
    assert client.get.await_count == 2
    assert all(
        call.kwargs["headers"]["Authorization"] == "Bearer same-runner-token"
        for call in client.get.await_args_list
    )


@pytest.mark.asyncio
async def test_fetch_skill_reraises_non_404_http_errors():
    response = MagicMock()
    response.status_code = 500
    error = httpx.HTTPStatusError("server error", request=None, response=response)
    response.raise_for_status = MagicMock(side_effect=error)
    client = _make_client(response)

    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_skill("skill-1")


@pytest.mark.asyncio
async def test_fetch_skill_raises_value_error_on_missing_data_key():
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"meta": {}}
    client = _make_client(response)

    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(ValueError, match="missing or invalid 'data'"):
            await fetch_skill("skill-1")


@pytest.mark.asyncio
async def test_fetch_skill_raises_value_error_when_data_is_not_a_dict():
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"data": "unexpected-string"}
    client = _make_client(response)

    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(ValueError, match="missing or invalid 'data'"):
            await fetch_skill("skill-1")
