import os

import httpx
import pytest


BASE_URL = os.getenv("BUSINESS_BACKEND_URL", "http://localhost:8000")
KEYCLOAK_URL = os.getenv("KEYCLOAK_URL", "http://localhost:8080")
REALM = os.getenv("KEYCLOAK_REALM", "aurum-ai")
CLIENT_ID = os.getenv("KEYCLOAK_CLIENT_ID", "business-ui")
KEYCLOAK_USERNAME = os.getenv("KEYCLOAK_USERNAME", "user")
KEYCLOAK_PASSWORD = os.getenv("KEYCLOAK_PASSWORD", "user")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_chat_stream_endpoint() -> None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Health check Keycloak before attempting authentication.
        try:
            await client.get(f"{KEYCLOAK_URL}/realms/{REALM}")
        except httpx.NetworkError as exc:
            pytest.skip(f"Keycloak not reachable: {exc}")

        token_response = await client.post(
            f"{KEYCLOAK_URL}/realms/{REALM}/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": CLIENT_ID,
                "username": KEYCLOAK_USERNAME,
                "password": KEYCLOAK_PASSWORD,
            },
        )
        if token_response.status_code != 200:
            pytest.skip(
                f"Unable to obtain access token: {token_response.status_code} {token_response.text}"
            )
        access_token = token_response.json().get("access_token")
        if not access_token:
            pytest.skip("Access token missing from Keycloak response")

        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            conv_response = await client.post(
                f"{BASE_URL}/v1/conversations",
                headers=headers,
                json={"title": "integration test chat", "model": "gpt-4o-mini"},
            )
        except httpx.NetworkError as exc:
            pytest.skip(f"Business backend not reachable: {exc}")

        if conv_response.status_code != 200:
            pytest.skip(
                f"Unable to create conversation: {conv_response.status_code} {conv_response.text}"
            )
        conversation_id = conv_response.json().get("data", {}).get("id")
        if not conversation_id:
            pytest.skip("Conversation id missing from response")

        stream_response = await client.post(
            f"{BASE_URL}/v1/chat/stream",
            headers=headers,
            json={
                "conversation_id": conversation_id,
                "messages": [{"role": "user", "content": "hello"}],
                "model": "gpt-4o-mini",
            },
            timeout=60.0,
        )
        assert stream_response.status_code == 200

        body = stream_response.text
        assert any(marker in body for marker in ("content-delta", "message-end", "done")), (
            f"SSE stream did not contain expected marker. Body snippet: {body[:500]!r}"
        )
