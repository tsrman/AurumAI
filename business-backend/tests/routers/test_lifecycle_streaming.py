"""Real request dependency lifetime must not serialize inference and cancel."""

# ruff: noqa: F811
import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core import dependencies
from src.core.auth import UserInfo, get_current_user, get_current_user_or_session
from src.core.config import settings
from src.core.database import _AppSession
from src.core.dependencies import get_org_db_session
from src.models.interaction import InteractionTurn
from src.routers import chat
from src.services.quota import get_quota_service
from tests.routers.test_chat_completions import RecordingQuotaService
from tests.services.test_interaction_outbox import outbox_fixture  # noqa: F401


@pytest.mark.asyncio
async def test_live_llm_stream_allows_parallel_internal_request_and_persisted_cancel(
    outbox_fixture, monkeypatch
):
    fixture = outbox_fixture
    engine = create_async_engine(
        settings.DATABASE_URL,
        poolclass=NullPool,
        connect_args={"server_settings": {"role": "aurum_ai_app"}},
    )
    sessions = async_sessionmaker(engine, sync_session_class=_AppSession, expire_on_commit=False)
    monkeypatch.setattr(dependencies, "async_session", sessions)
    user = UserInfo(
        user_id=str(fixture.user_id),
        org_id=str(fixture.org_id),
        role="member",
        raw_claims={
            "type": "session",
            "conversation_id": str(fixture.conversation_id),
            "generation": 1,
        },
    )

    async def browser_session():
        async with sessions() as session:
            session.info["org_id"] = str(fixture.org_id)
            yield session

    app = FastAPI()
    app.include_router(chat.router, prefix="/v1")
    app.dependency_overrides[get_current_user_or_session] = lambda: user
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_org_db_session] = browser_session
    app.dependency_overrides[get_quota_service] = RecordingQuotaService
    monkeypatch.setattr(chat.authorization_service, "has_capability", AsyncMock(return_value=True))
    monkeypatch.setattr(chat, "ensure_model_allowed", AsyncMock())
    monkeypatch.setattr(chat.interaction_service, "signal_turn_cancel", AsyncMock())
    monkeypatch.setattr(chat, "get_langfuse", lambda: None)
    monkeypatch.setattr(chat.llm_client, "api_key", "isolated-test-credential")
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    class ProviderStream(httpx.AsyncByteStream):
        def __init__(self, first):
            self.first = first

        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"started"}}]}\n\n'
            if self.first:
                entered.set()
                await release.wait()
            yield b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\ndata: [DONE]\n\n'

    async def provider(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=ProviderStream(calls == 1))

    client_class = httpx.AsyncClient

    def client_factory(**kwargs):
        return client_class(**kwargs, transport=httpx.MockTransport(provider))

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    tasks = []
    try:
        async with client_class(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            payload = {
                "model": "test",
                "messages": [{"role": "user", "content": "test"}],
                "stream": True,
            }
            first = asyncio.create_task(client.post("/v1/chat/completions", json=payload))
            tasks.append(first)
            await asyncio.wait_for(entered.wait(), 5)
            second = asyncio.create_task(client.post("/v1/chat/completions", json=payload))
            cancel = asyncio.create_task(client.post(f"/v1/chat/{fixture.conversation_id}/cancel"))
            tasks.extend((second, cancel))
            assert (await asyncio.wait_for(asyncio.shield(second), 3)).status_code == 200
            assert (await asyncio.wait_for(asyncio.shield(cancel), 3)).status_code == 204
            async with fixture.sessions() as observer:
                assert (
                    await observer.scalar(
                        select(InteractionTurn.status).where(InteractionTurn.id == fixture.turn_id)
                    )
                    == "cancelled"
                )
            assert not first.done() and not release.is_set()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
        await engine.dispose()
