"""Tests for the gateway model list — specifically, keeping models that
cannot serve a chat completion out of it.

Background: the gateway reports embedding and speech models next to chat
models, with no field distinguishing them (`id`/`created`/`owned_by` only).
They were offered in the UI's model picker, which defaults to the first
entry — and an embedding model could sort ahead of every chat model, so it
became the default for every user who had not picked one by hand. Their first message came back blank.
"""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from src.core.auth import UserInfo, get_current_user
from src.core.authorization_types import AccessPermission, GrantSource, PrincipalType
from src.core.dependencies import get_org_db_session
from src.models.access_grant import AccessGrant
from src.models.organization import Organization
from src.models.user import User
from src.routers import models as models_router
from src.routers.models import is_chat_model, list_gateway_models


@pytest.mark.parametrize(
    "model_id",
    [
        # Embedding, speech and rerank models reject /v1/chat/completions.
        "openai/text-embedding-3-large",
        "openai/text-embedding-3-small",
        "provider/speech-realtime-preview",
        "openai/whisper-1",
        "openai/tts-1",
        "cohere/rerank-v3.5",
    ],
)
def test_is_chat_model_rejects_non_chat_models(model_id: str) -> None:
    assert not is_chat_model(model_id)


@pytest.mark.parametrize(
    "model_id",
    [
        "openai/gpt-4o-mini",
        "deepseek/deepseek-v4-flash",
        # "Superseded" is not "not a chat model": deprecated and
        # release-candidate variants still serve chat completions, so
        # filtering them would hide working models from the whole org.
        "provider/chat-model/deprecated",
        "provider/chat-model/rc",
        "gpt-4o-mini",
        "anthropic/claude-3-5-sonnet",
        # Guards against over-broad matching: "tts-" must not swallow a chat
        # model whose name merely contains those letters.
        "meta-llama/llama-3.1-8b-instruct",
    ],
)
def test_is_chat_model_keeps_chat_models(model_id: str) -> None:
    assert is_chat_model(model_id)


def _gateway_listing(models: list[dict[str, Any]]) -> Any:
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": models})

    def make_client(**kwargs: Any) -> Any:
        kwargs.pop("verify", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    return make_client


@pytest.mark.asyncio
async def test_list_gateway_models_drops_non_chat_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filtering lives in list_gateway_models, not in the route, so the group
    permission catalog (routers/groups.py) sees the same list — a
    A legacy model permission would gate something nobody can
    select anyway."""
    monkeypatch.setattr("src.routers.models.settings.DEBUG", False)
    monkeypatch.setattr("src.routers.models.settings.GATEWAY_API_KEY", "key")
    monkeypatch.setattr(
        "httpx.AsyncClient",
        _gateway_listing(
            [
                {"id": "openai/text-embedding-3-large"},
                {"id": "openai/gpt-4o-mini"},
                {"id": "provider/speech-realtime-preview"},
                {"id": "deepseek/deepseek-v4-flash"},
            ]
        ),
    )

    models = await list_gateway_models()

    assert [m["id"] for m in models] == [
        "openai/gpt-4o-mini",
        "deepseek/deepseek-v4-flash",
    ]


@pytest.mark.asyncio
async def test_list_gateway_models_falls_back_to_mock_models_on_gateway_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The existing fallback must survive the filtering step — every mock id
    is a chat model, so none of them may be filtered out."""
    monkeypatch.setattr("src.routers.models.settings.DEBUG", False)
    monkeypatch.setattr("src.routers.models.settings.GATEWAY_API_KEY", "key")

    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def make_client(**kwargs: Any) -> Any:
        kwargs.pop("verify", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("httpx.AsyncClient", make_client)

    models = await list_gateway_models()

    assert [m["id"] for m in models] == ["gpt-4o-mini", "gpt-4o", "claude-3-5-sonnet"]


@pytest.mark.asyncio
async def test_models_route_returns_only_live_v2_grants_without_private_metadata(
    db,
    test_org: Organization,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    granted_id = "provider/granted:model-v1"
    private_id = "provider/private:model-v1"
    db.add(
        AccessGrant(
            org_id=test_org.id,
            resource_type="model",
            resource_id=granted_id,
            principal_type=PrincipalType.USER.value,
            principal_id=test_user.id,
            permission=AccessPermission.READ.value,
            created_via=GrantSource.PROVISIONING.value,
        )
    )
    await db.commit()

    async def gateway_models() -> list[Any]:
        return [
            {"id": granted_id, "object": "model", "owned_by": "visible-provider"},
            {
                "id": private_id,
                "object": "model",
                "owned_by": "private-provider-metadata",
            },
            "raw-private-model",
        ]

    async def override_session():
        yield db

    app = FastAPI()
    app.include_router(models_router.router, prefix="/v1")
    app.dependency_overrides[get_current_user] = lambda: UserInfo(
        user_id=str(test_user.id),
        org_id=str(test_org.id),
        role="member",
    )
    app.dependency_overrides[get_org_db_session] = override_session
    monkeypatch.setattr(models_router, "list_gateway_models", gateway_models)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/models")

    assert response.status_code == 200
    assert response.json()["data"] == [
        {"id": granted_id, "object": "model", "owned_by": "visible-provider"}
    ]
    assert private_id not in response.text
    assert "private-provider-metadata" not in response.text
    assert "raw-private-model" not in response.text
