import asyncio
import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select, text

from src.core.auth import UserInfo, get_current_user_or_session
from src.core.database import async_session
from src.core.dependencies import get_service_db_session
from src.models.conversation import Conversation
from src.models.message import Message
from src.models.organization import Organization
from src.models.user import User
from src.routers import chat


@pytest_asyncio.fixture
async def test_org(db):
    org = Organization(name="Test Org", slug=f"test-org-{uuid.uuid4().hex[:8]}")
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return org


@pytest_asyncio.fixture
async def test_user(db, test_org):
    user = User(
        keycloak_id=uuid.uuid4(),
        email=f"test-{uuid.uuid4().hex[:8]}@example.com",
        org_id=test_org.id,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest_asyncio.fixture
async def test_conversation(db, test_org, test_user):
    conv = Conversation(
        org_id=test_org.id,
        user_id=test_user.id,
        title="Test Conversation",
        model="gpt-4o-mini",
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


@pytest_asyncio.fixture
async def client(db, test_org, test_user, test_conversation):
    async def override_get_service_db_session():
        await db.execute(
            text("SELECT set_config('app.current_org_id', :org_id, false)"),
            {"org_id": str(test_org.id)},
        )
        yield db
        await db.execute(text("SELECT set_config('app.current_org_id', '', false)"))

    async def override_get_current_user_or_session():
        return UserInfo(
            user_id=str(test_user.id),
            org_id=str(test_org.id),
            raw_claims={
                "type": "session",
                "conversation_id": str(test_conversation.id),
            },
        )

    test_app = FastAPI()
    test_app.include_router(chat.router, prefix="/v1")
    test_app.dependency_overrides[get_service_db_session] = override_get_service_db_session
    test_app.dependency_overrides[
        get_current_user_or_session
    ] = override_get_current_user_or_session

    async with httpx.AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://test"
    ) as client:
        yield client


@pytest_asyncio.fixture
async def concurrent_client():
    """Client that creates a fresh DB session per request to exercise real concurrency."""
    org = user = conv = None
    async with async_session() as setup_session:
        org = Organization(name="Test Org", slug=f"test-org-{uuid.uuid4().hex[:8]}")
        setup_session.add(org)
        await setup_session.flush()
        await setup_session.execute(
            text("SELECT set_config('app.current_org_id', :org_id, false)"),
            {"org_id": str(org.id)},
        )
        user = User(
            keycloak_id=uuid.uuid4(),
            email=f"test-{uuid.uuid4().hex[:8]}@example.com",
            org_id=org.id,
        )
        setup_session.add(user)
        await setup_session.flush()
        conv = Conversation(
            org_id=org.id,
            user_id=user.id,
            title="Test Conversation",
            model="gpt-4o-mini",
        )
        setup_session.add(conv)
        await setup_session.commit()

    async def override_get_service_db_session():
        async with async_session() as session:
            await session.execute(
                text("SELECT set_config('app.current_org_id', :org_id, false)"),
                {"org_id": str(org.id)},
            )
            yield session
            await session.execute(
                text("SELECT set_config('app.current_org_id', '', false)")
            )

    def override_get_current_user_or_session():
        return UserInfo(
            user_id=str(user.id),
            org_id=str(org.id),
            raw_claims={
                "type": "session",
                "conversation_id": str(conv.id),
            },
        )

    test_app = FastAPI()
    test_app.include_router(chat.router, prefix="/v1")
    test_app.dependency_overrides[get_service_db_session] = override_get_service_db_session
    test_app.dependency_overrides[
        get_current_user_or_session
    ] = override_get_current_user_or_session

    async with httpx.AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://test"
    ) as client:
        yield client

    async with async_session() as cleanup_session:
        await cleanup_session.execute(
            text("SELECT set_config('app.current_org_id', :org_id, false)"),
            {"org_id": str(org.id)},
        )
        if conv is not None:
            await cleanup_session.execute(
                text("DELETE FROM conversations WHERE id = :conv_id"),
                {"conv_id": conv.id},
            )
        if user is not None:
            await cleanup_session.execute(
                text("DELETE FROM users WHERE id = :user_id"),
                {"user_id": user.id},
            )
        if org is not None:
            await cleanup_session.execute(
                text("DELETE FROM organizations WHERE id = :org_id"),
                {"org_id": org.id},
            )
        await cleanup_session.commit()


@pytest.mark.asyncio
async def test_persist_assistant_message_is_idempotent(client, test_conversation, db):
    turn_id = str(uuid.uuid4())
    url = f"/v1/conversations/{test_conversation.id}/messages"

    response1 = await client.post(
        url,
        json={
            "role": "assistant",
            "content": "first assistant response",
            "model": "gpt-4o-mini",
            "turn_id": turn_id,
        },
    )
    assert response1.status_code == 201

    response2 = await client.post(
        url,
        json={
            "role": "assistant",
            "content": "second assistant response",
            "model": "gpt-4o-mini",
            "turn_id": turn_id,
        },
    )
    assert response2.status_code == 201

    result = await db.execute(
        select(Message).where(
            Message.conversation_id == test_conversation.id,
            Message.role == "assistant",
        )
    )
    assistant_messages = result.scalars().all()
    assert len(assistant_messages) == 1
    assert assistant_messages[0].content == "second assistant response"
    assert str(assistant_messages[0].turn_id) == turn_id


@pytest.mark.asyncio
async def test_persist_assistant_message_returns_tool_calls_and_results(client, test_conversation):
    turn_id = str(uuid.uuid4())
    url = f"/v1/conversations/{test_conversation.id}/messages"
    tool_calls = [{"call_id": "call-1", "skill_id": "search", "args": {"q": "docs"}}]
    tool_results = {"call-1": {"skill_id": "search", "output": {"found": "yes"}}}

    response = await client.post(
        url,
        json={
            "role": "assistant",
            "content": "here's what I found",
            "model": "gpt-4o-mini",
            "turn_id": turn_id,
            "tool_calls": tool_calls,
            "tool_results": tool_results,
        },
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["toolCalls"] == tool_calls
    assert data["toolResults"] == tool_results


@pytest.mark.asyncio
async def test_persist_assistant_message_is_idempotent_under_concurrency(concurrent_client):
    turn_id = str(uuid.uuid4())
    user_info = concurrent_client._transport.app.dependency_overrides[get_current_user_or_session]()
    conversation_id = user_info.raw_claims["conversation_id"]
    url = f"/v1/conversations/{conversation_id}/messages"

    async def post():
        return await concurrent_client.post(
            url,
            json={
                "role": "assistant",
                "content": "concurrent assistant response",
                "model": "gpt-4o-mini",
                "turn_id": turn_id,
            },
        )

    responses = await asyncio.gather(*[post() for _ in range(5)])
    assert all(r.status_code == 201 for r in responses)

    async with async_session() as session:
        await session.execute(
            text("SELECT set_config('app.current_org_id', :org_id, false)"),
            {"org_id": str(concurrent_client._transport.app.dependency_overrides[get_current_user_or_session]().org_id)},
        )
        result = await session.execute(
            select(Message).where(
                Message.conversation_id == conversation_id,
                Message.role == "assistant",
            )
        )
        assistant_messages = result.scalars().all()
        assert len(assistant_messages) == 1
        assert assistant_messages[0].content == "concurrent assistant response"
        assert str(assistant_messages[0].turn_id) == turn_id
