import datetime
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core import dependencies
from src.core.auth import UserInfo, get_current_user, get_current_user_or_session
from src.core.dependencies import get_org_db_session, get_service_db_session
from src.models.conversation import Conversation
from src.models.organization import Organization
from src.models.user import User
from src.repositories.message_repository import message_repository
from src.routers import chat
from src.services.organization_model_settings import organization_model_settings_service


@pytest_asyncio.fixture
async def client(db, test_org: Organization, test_user: User):
    async def override_get_org_db_session():
        await db.execute(
            text("SELECT set_config('app.current_org_id', :org_id, false)"),
            {"org_id": str(test_org.id)},
        )
        yield db
        await db.execute(text("SELECT set_config('app.current_org_id', '', false)"))

    def override_get_current_user():
        return UserInfo(user_id=str(test_user.id), org_id=str(test_org.id), role="member")

    test_app = FastAPI()
    test_app.include_router(chat.router, prefix="/v1")
    test_app.dependency_overrides[get_org_db_session] = override_get_org_db_session
    test_app.dependency_overrides[get_service_db_session] = override_get_org_db_session
    test_app.dependency_overrides[get_current_user] = override_get_current_user
    test_app.dependency_overrides[get_current_user_or_session] = override_get_current_user

    async with httpx.AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://test"
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_create_conversation_persists_owner_fields(client, db, test_user, test_org):
    response = await client.post("/v1/conversations", json={"model": "gpt-4o-mini"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["model"] == "gpt-4o-mini"
    assert data["status"] == "active"

    from src.repositories.conversation_repository import conversation_repository
    conv = await conversation_repository.get(db, data["id"])
    assert str(conv.user_id) == str(test_user.id)
    assert str(conv.org_id) == str(test_org.id)


@pytest.mark.asyncio
async def test_list_conversations_only_returns_own(client, db, test_user, test_org):
    mine = Conversation(
        org_id=test_org.id, user_id=test_user.id, title="Mine", model="gpt-4o-mini"
    )
    other_user = User(
        keycloak_id=uuid.uuid4(),
        email=f"other-{uuid.uuid4().hex[:8]}@example.com",
        org_id=test_org.id,
    )
    db.add_all([mine, other_user])
    await db.flush()
    theirs = Conversation(
        org_id=test_org.id, user_id=other_user.id, title="Theirs", model="gpt-4o-mini"
    )
    db.add(theirs)
    await db.commit()

    response = await client.get("/v1/conversations")
    assert response.status_code == 200
    titles = [c["title"] for c in response.json()["data"]]
    assert "Mine" in titles
    assert "Theirs" not in titles


@pytest.mark.asyncio
async def test_get_conversation_returns_messages(client, db, test_user, test_org):
    conv = Conversation(
        org_id=test_org.id, user_id=test_user.id, title="With messages", model="gpt-4o-mini"
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)

    await message_repository.create(
        db,
        conversation_id=conv.id,
        org_id=test_org.id,
        user_id=test_user.id,
        role="user",
        content="hello",
        model="gpt-4o-mini",
        turn_id=uuid.uuid4(),
    )

    response = await client.get(f"/v1/conversations/{conv.id}")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["messages"]["data"][0]["content"] == "hello"


@pytest.mark.asyncio
async def test_get_conversation_resolves_org_default_temperature_for_a_real_session_token(
    monkeypatch, db: AsyncSession, test_org: Organization, test_user: User
):
    """conversation-runner reaches this route through a session token, not a
    Keycloak JWT — get_current_user_or_session + get_service_db_session
    (CLAUDE.md). The `client` fixture above overrides get_service_db_session
    directly with a manually set_config'd session, which never exercises the
    real _set_org_and_sync/RLS path a session token actually goes through in
    production. Drive the real dependency chain here so a session-token
    identity that can't see OrganizationSettings under RLS would show up as a
    silent fallback to the 0.7 platform default instead of the real 1.4 org
    default, rather than passing every other test unnoticed."""
    await organization_model_settings_service.set_default_temperature(db, test_org.id, 1.4)
    await db.commit()

    conv = Conversation(
        org_id=test_org.id, user_id=test_user.id, title="Session", model="gpt-4o-mini"
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)

    identity = UserInfo(
        user_id=str(test_user.id),
        org_id=str(test_org.id),
        role="member",
        raw_claims={"type": "session", "conversation_id": str(conv.id)},
    )

    @asynccontextmanager
    async def same_session():
        yield db

    monkeypatch.setattr(dependencies, "async_session", same_session)

    app = FastAPI()
    app.include_router(chat.router, prefix="/v1")
    app.dependency_overrides[get_current_user_or_session] = lambda: identity

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as session_client:
        response = await session_client.get(f"/v1/conversations/{conv.id}")

    assert response.status_code == 200
    assert response.json()["data"]["temperature"] == 1.4


@pytest.mark.asyncio
async def test_get_conversation_404_for_nonexistent_id(client):
    response = await client.get(f"/v1/conversations/{uuid.uuid4()}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_conversation_422_for_invalid_id(client):
    response = await client.get("/v1/conversations/not-a-uuid")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_conversation_403_for_other_users_conversation(client, db, test_org):
    other_user = User(
        keycloak_id=uuid.uuid4(),
        email=f"other-{uuid.uuid4().hex[:8]}@example.com",
        org_id=test_org.id,
    )
    db.add(other_user)
    await db.flush()
    conv = Conversation(
        org_id=test_org.id, user_id=other_user.id, title="Not yours", model="gpt-4o-mini"
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)

    response = await client.get(f"/v1/conversations/{conv.id}")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_patch_conversation_updates_title(client, db, test_user, test_org):
    conv = Conversation(
        org_id=test_org.id, user_id=test_user.id, title="Old title", model="gpt-4o-mini"
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)

    response = await client.patch(f"/v1/conversations/{conv.id}", json={"title": "New title"})
    assert response.status_code == 200
    assert response.json()["data"]["title"] == "New title"


@pytest.mark.asyncio
async def test_patch_conversation_metadata_is_actually_persisted(client, db, test_user, test_org):
    """Regression test: ConversationUpdate.metadata
    has no matching Conversation column (it's `extra`) — repository.update's
    plain setattr() silently created a non-mapped instance attribute that
    was never flushed, so the value just vanished behind a 200 response."""
    conv = Conversation(
        org_id=test_org.id, user_id=test_user.id, title="Has metadata", model="gpt-4o-mini"
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)

    response = await client.patch(
        f"/v1/conversations/{conv.id}", json={"metadata": {"source": "widget"}}
    )
    assert response.status_code == 200

    await db.refresh(conv)
    assert conv.extra == {"source": "widget"}


@pytest.mark.asyncio
async def test_delete_conversation_sets_status_deleted(
    client, db, test_user, test_org, monkeypatch
):
    conv = Conversation(
        org_id=test_org.id, user_id=test_user.id, title="To delete", model="gpt-4o-mini"
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)

    cancel = AsyncMock(return_value=(uuid.uuid4(), []))
    signal = AsyncMock()
    monkeypatch.setattr(chat.interaction_service, "cancel_active_turn", cancel)
    monkeypatch.setattr(chat.interaction_service, "signal_turn_cancel", signal)

    response = await client.delete(f"/v1/conversations/{conv.id}")
    assert response.status_code == 204

    await db.refresh(conv)
    assert conv.status == "deleted"
    cancel.assert_awaited_once()
    assert cancel.await_args.args[2] == conv.id
    cancelled_turn_id, targets = cancel.return_value
    signal.assert_awaited_once_with(targets, cancelled_turn_id, conv.id)


@pytest.mark.asyncio
async def test_deleting_twice_is_idempotent(client, db, test_user, test_org):
    conv = Conversation(
        org_id=test_org.id, user_id=test_user.id, title="Delete twice", model="gpt-4o-mini"
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)

    first = await client.delete(f"/v1/conversations/{conv.id}")
    second = await client.delete(f"/v1/conversations/{conv.id}")
    assert first.status_code == 204
    assert second.status_code == 204


@pytest.mark.asyncio
async def test_deleted_conversation_returns_410_on_get_and_patch(client, db, test_user, test_org):
    """Regression test: a soft-deleted conversation
    (status="deleted") used to keep working exactly as if nothing happened
    on GET/PATCH/cancel/chat/stream — only the list endpoint filtered it
    out."""
    conv = Conversation(
        org_id=test_org.id, user_id=test_user.id, title="Deleted", model="gpt-4o-mini"
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)

    delete_response = await client.delete(f"/v1/conversations/{conv.id}")
    assert delete_response.status_code == 204

    get_response = await client.get(f"/v1/conversations/{conv.id}")
    assert get_response.status_code == 410

    patch_response = await client.patch(
        f"/v1/conversations/{conv.id}", json={"title": "renamed"}
    )
    assert patch_response.status_code == 410

    cancel_response = await client.post(f"/v1/chat/{conv.id}/cancel")
    assert cancel_response.status_code == 410


@pytest.mark.asyncio
async def test_deleted_conversation_is_excluded_from_list(client, db, test_user, test_org):
    conv = Conversation(
        org_id=test_org.id, user_id=test_user.id, title="Will be deleted", model="gpt-4o-mini"
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)

    delete_response = await client.delete(f"/v1/conversations/{conv.id}")
    assert delete_response.status_code == 204

    list_response = await client.get("/v1/conversations")
    titles = [c["title"] for c in list_response.json()["data"]]
    assert "Will be deleted" not in titles


@pytest.mark.asyncio
async def test_list_conversations_paginates_via_cursor(client, db, test_user, test_org):
    now = datetime.datetime.now(datetime.timezone.utc)
    for i in range(5):
        conv = Conversation(
            org_id=test_org.id,
            user_id=test_user.id,
            title=f"Conv {i}",
            model="gpt-4o-mini",
            last_message_at=now - datetime.timedelta(minutes=i),
        )
        db.add(conv)
    await db.commit()

    first_page = await client.get("/v1/conversations", params={"limit": 2})
    assert first_page.status_code == 200
    first_titles = [c["title"] for c in first_page.json()["data"]]
    assert first_titles == ["Conv 0", "Conv 1"]
    pagination = first_page.json()["pagination"]
    assert pagination["hasMore"] is True
    cursor = pagination["cursor"]
    assert cursor is not None

    second_page = await client.get(
        "/v1/conversations", params={"limit": 2, "cursor": cursor}
    )
    second_titles = [c["title"] for c in second_page.json()["data"]]
    assert second_titles == ["Conv 2", "Conv 3"]
    assert second_page.json()["pagination"]["hasMore"] is True

    third_page = await client.get(
        "/v1/conversations",
        params={"limit": 2, "cursor": second_page.json()["pagination"]["cursor"]},
    )
    third_titles = [c["title"] for c in third_page.json()["data"]]
    assert third_titles == ["Conv 4"]
    assert third_page.json()["pagination"]["hasMore"] is False
    assert third_page.json()["pagination"]["cursor"] is None


@pytest.mark.parametrize("page_size", [1, 7, 50])
async def test_message_history_paginates_all_52_rows_without_gaps(
    client, db, test_org, test_user, page_size
):
    from src.models.message import Message

    conv = Conversation(org_id=test_org.id, user_id=test_user.id, title="Long", model="test")
    db.add(conv)
    await db.flush()
    expected = []
    # Tied turn timestamps and inverted per-role times must not split/reorder turns.
    timestamp = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    for index in range(26):
        turn_id = uuid.UUID(int=index + 1)
        pair = []
        for role in ("user", "assistant"):
            message = Message(id=uuid.uuid4(), conversation_id=conv.id, org_id=test_org.id,
                              role=role, content=f"{role}-{index}", turn_id=turn_id,
                              created_at=timestamp + datetime.timedelta(
                                  milliseconds=175 if role == "user" else 0))
            pair.append(message)
            expected.append(str(message.id))
        db.add_all(reversed(pair))
    await db.commit()

    params = {"limit": page_size}
    actual = []
    for _ in range(53):
        response = await client.get(f"/v1/conversations/{conv.id}", params=params)
        assert response.status_code == 200
        page = response.json()["data"]["messages"]
        ids = [message["id"] for message in page["data"]]
        assert 0 < len(ids) <= page_size
        actual.extend(ids)
        assert page["pagination"]["hasMore"] == (len(actual) < 52)
        if not page["pagination"]["hasMore"]:
            assert page["pagination"].get("cursor") is None
            break
        assert page["pagination"]["cursor"] == ids[-1]
        params["cursor"] = page["pagination"]["cursor"]
    assert actual == expected


async def test_message_history_rejects_cursor_from_another_conversation(
    client, db, test_org, test_user
):
    from src.models.message import Message

    first = Conversation(org_id=test_org.id, user_id=test_user.id, title="First", model="test")
    second = Conversation(org_id=test_org.id, user_id=test_user.id, title="Second", model="test")
    db.add_all([first, second])
    await db.flush()
    message = Message(conversation_id=second.id, org_id=test_org.id, role="user", content="other")
    db.add(message)
    await db.commit()
    response = await client.get(f"/v1/conversations/{first.id}", params={"cursor": str(message.id)})
    assert response.status_code == 400


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"cursor": "invalid"}])
async def test_message_history_validates_pagination(client, db, test_org, test_user, params):
    conv = Conversation(org_id=test_org.id, user_id=test_user.id, title="Limits", model="test")
    db.add(conv)
    await db.commit()
    response = await client.get(f"/v1/conversations/{conv.id}", params=params)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_conversations_with_a_hermes_session_name_their_agent(client, db, test_user, test_org):
    from src.models.hermes_session import HermesSession

    hermes = Conversation(org_id=test_org.id, user_id=test_user.id, title="Hermes", model="gpt-4o-mini")
    plain = Conversation(org_id=test_org.id, user_id=test_user.id, title="Plain", model="gpt-4o-mini")
    db.add_all([hermes, plain])
    await db.flush()
    # Closed sessions count too: the composer keeps Hermes for the conversation.
    db.add(
        HermesSession(
            id=uuid.uuid4(), org_id=test_org.id, user_id=test_user.id, conversation_id=hermes.id,
            expires_at=datetime.datetime.now(datetime.timezone.utc), state="closed",
            closed_at=datetime.datetime.now(datetime.timezone.utc),
        )
    )
    await db.flush()

    listed = {item["title"]: item["agent"] for item in (await client.get("/v1/conversations")).json()["data"]}
    detail = (await client.get(f"/v1/conversations/{hermes.id}")).json()["data"]
    renamed = (await client.patch(f"/v1/conversations/{plain.id}", json={"title": "Renamed"})).json()["data"]

    assert listed["Hermes"] == "hermes" and listed["Plain"] is None
    assert detail["agent"] == "hermes"
    assert renamed["agent"] is None


@pytest.mark.asyncio
async def test_a_failed_turns_reason_comes_back_marked_failed(client, db, test_user, test_org):
    from src.repositories.message_repository import message_repository as messages

    conv = Conversation(org_id=test_org.id, user_id=test_user.id, title="Failed", model="gpt-4o-mini")
    db.add(conv)
    await db.flush()
    await messages.upsert_assistant_by_turn(
        db, conversation_id=str(conv.id), turn_id=uuid.uuid4(), role="assistant",
        content="Could not get a model response. Reason: x", org_id=str(test_org.id), extra={"failed": True},
    )
    await messages.upsert_assistant_by_turn(
        db, conversation_id=str(conv.id), turn_id=uuid.uuid4(), role="assistant",
        content="A real answer", org_id=str(test_org.id),
    )
    await db.flush()

    data = (await client.get(f"/v1/conversations/{conv.id}")).json()["data"]["messages"]["data"]

    assert {item["content"]: item["failed"] for item in data} == {
        "Could not get a model response. Reason: x": True,
        "A real answer": False,
    }
