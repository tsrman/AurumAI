from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.core.auth import UserInfo
from src.routers.models import list_models


@pytest.fixture
def user() -> UserInfo:
    return UserInfo(
        user_id="22222222-2222-2222-2222-222222222222",
        email="user@example.com",
        full_name="User",
        org_id="11111111-1111-1111-1111-111111111111",
        org_name="Org",
        role="org_admin",
    )


@pytest.mark.asyncio
async def test_list_models_returns_gateway_models(user: UserInfo, monkeypatch, db) -> None:
    monkeypatch.setattr("src.routers.models.settings.DEBUG", False)
    monkeypatch.setattr("src.routers.models.settings.LLM_GATEWAY_URL", "http://gateway")
    monkeypatch.setattr("src.routers.models.settings.GATEWAY_API_KEY", "secret")

    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value={
        "data": [
            {"id": "model-a", "object": "model"},
            {"id": "model-b", "object": "model"},
        ]
    })

    async_client = AsyncMock()
    async_client.__aenter__.return_value = async_client
    async_client.__aexit__ = AsyncMock(return_value=False)
    async_client.get = AsyncMock(return_value=response)

    with patch("httpx.AsyncClient", return_value=async_client) as mock_client:
        result = await list_models(user, db)

    assert result.data == [
        {"id": "model-a", "object": "model", "vision": False},
        {"id": "model-b", "object": "model", "vision": False},
    ]
    assert mock_client.call_args.kwargs.get("verify") is True


@pytest.mark.asyncio
async def test_list_models_uses_custom_ca_cert(user: UserInfo, monkeypatch, db) -> None:
    monkeypatch.setattr("src.routers.models.settings.DEBUG", False)
    monkeypatch.setattr("src.routers.models.settings.LLM_GATEWAY_URL", "https://gateway")
    monkeypatch.setattr("src.routers.models.settings.GATEWAY_API_KEY", "secret")
    monkeypatch.setattr("src.routers.models.settings.GATEWAY_CA_CERT", "/certs/ca.pem")

    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value={"data": []})

    async_client = AsyncMock()
    async_client.__aenter__.return_value = async_client
    async_client.__aexit__ = AsyncMock(return_value=False)
    async_client.get = AsyncMock(return_value=response)

    with patch("httpx.AsyncClient", return_value=async_client) as mock_client:
        await list_models(user, db)

    assert mock_client.call_args.kwargs.get("verify") == "/certs/ca.pem"


@pytest.mark.asyncio
async def test_list_models_returns_mock_when_debug_and_no_key(
    user: UserInfo, monkeypatch, db
) -> None:
    monkeypatch.setattr("src.routers.models.settings.DEBUG", True)
    monkeypatch.setattr("src.routers.models.settings.GATEWAY_API_KEY", "")

    result = await list_models(user, db)

    assert any(m["id"] == "gpt-4o-mini" for m in result.data)


@pytest.mark.asyncio
async def test_list_models_returns_mock_on_gateway_error(user: UserInfo, monkeypatch, db) -> None:
    monkeypatch.setattr("src.routers.models.settings.DEBUG", False)
    monkeypatch.setattr("src.routers.models.settings.LLM_GATEWAY_URL", "http://gateway")
    monkeypatch.setattr("src.routers.models.settings.GATEWAY_API_KEY", "secret")

    async_client = AsyncMock()
    async_client.__aenter__.return_value = async_client
    async_client.__aexit__ = AsyncMock(return_value=False)
    async_client.get = AsyncMock(side_effect=httpx.ConnectError("unreachable"))

    with patch("httpx.AsyncClient", return_value=async_client):
        result = await list_models(user, db)

    assert isinstance(result.data, list)
