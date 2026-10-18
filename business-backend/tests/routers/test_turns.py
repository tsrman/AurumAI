"""Durable owner-only turn snapshots and status subscription."""

import asyncio
import json
from types import SimpleNamespace
from uuid import UUID, uuid4
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from httpx import ASGITransport

from src.core.auth import UserInfo, get_current_user
from src.core.dependencies import get_org_db_session
from src.schemas.chat import ChatRequest


class _Session:
    pass


class _PubSub:
    def __init__(self) -> None:
        self.closed = False

    async def unsubscribe(self, channel: str) -> None:
        del channel

    async def aclose(self) -> None:
        self.closed = True


def _state(
    turn_id: UUID,
    question_id: UUID,
    *,
    turn_status: str = "active",
    turn_version: int = 1,
    question_status: str = "pending",
    question_version: int = 1,
):
    from src.schemas.interaction import TurnQuestionState, TurnSnapshot

    return TurnSnapshot(
        turn_id=turn_id,
        status=turn_status,
        version=turn_version,
        questions=[
            TurnQuestionState(
                question_id=question_id,
                status=question_status,
                version=question_version,
            )
        ],
    )


def test_question_progress_has_no_private_payload() -> None:
    from src.routers.turns import question_event

    assert question_event("q", "t", "pending", 1) == {
        "questionId": "q",
        "turnId": "t",
        "status": "pending",
        "version": 1,
    }


async def test_events_subscribe_before_snapshot_and_never_relay_hint_payload(
    monkeypatch,
) -> None:
    from src.routers import turns

    org_id, user_id, conversation_id, turn_id, question_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    order: list[str] = []
    states = [
        _state(turn_id, question_id),
        _state(
            turn_id,
            question_id,
            turn_status="completed",
            turn_version=2,
            question_status="answered",
            question_version=2,
        ),
    ]

    class Service:
        async def get_turn_snapshot(self, *args, **kwargs):  # noqa: ANN002, ANN003
            del args, kwargs
            order.append("snapshot")
            return states.pop(0) if len(states) > 1 else states[0]

    class Bus:
        async def subscribe_channel(self, channel: str):
            order.append("subscribe")
            assert channel == f"{org_id}:session:{conversation_id}:events"
            return _PubSub()

        async def listen(self, pubsub, channel, timeout_seconds):  # noqa: ANN001
            del pubsub, channel, timeout_seconds
            yield {
                "turn_id": str(turn_id),
                "type": "tool-result",
                "data": {
                    "prompt": "private prompt",
                    "answer": "private answer",
                    "toolResults": {"secret": "do not relay"},
                },
            }

    async def db_override():
        yield _Session()

    app = FastAPI()
    app.include_router(turns.router, prefix="/v1")
    app.dependency_overrides[get_current_user] = lambda: UserInfo(
        user_id=str(user_id), org_id=str(org_id), role="member"
    )
    app.dependency_overrides[get_org_db_session] = db_override
    monkeypatch.setattr(turns, "interaction_service", Service())
    monkeypatch.setattr(turns, "event_bus", Bus())

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/v1/conversations/{conversation_id}/turns/{turn_id}/events"
        )

    assert response.status_code == 200
    assert order[:2] == ["subscribe", "snapshot"]
    assert "private prompt" not in response.text
    assert "private answer" not in response.text
    assert "toolResults" not in response.text
    chunks = [part for part in response.text.split("\n\n") if part]
    payloads = [json.loads(part.split("data: ", 1)[1]) for part in chunks]
    assert payloads == [
        {
            "turnId": str(turn_id),
            "status": "active",
            "version": 1,
        },
        {
            "questionId": str(question_id),
            "turnId": str(turn_id),
            "status": "pending",
            "version": 1,
        },
        {
            "turnId": str(turn_id),
            "status": "completed",
            "version": 2,
        },
        {
            "questionId": str(question_id),
            "turnId": str(turn_id),
            "status": "answered",
            "version": 2,
        },
    ]


async def test_periodic_reconciliation_recovers_without_redis_notification(
    monkeypatch,
) -> None:
    from src.routers import turns

    turn_id, question_id = uuid4(), uuid4()
    states = [
        _state(turn_id, question_id),
        _state(
            turn_id,
            question_id,
            turn_status="completed",
            turn_version=2,
            question_status="answered",
            question_version=2,
        ),
    ]

    async def load():
        return states.pop(0) if len(states) > 1 else states[0]

    async def silent_notifications():
        await asyncio.Event().wait()
        yield {}  # pragma: no cover - keeps this an async generator

    monkeypatch.setattr(turns, "_RECONCILE_INTERVAL_SECONDS", 0.01)
    chunks = [
        chunk
        async for chunk in turns.turn_event_stream(
            states[0], load, silent_notifications(), timeout_seconds=0.2
        )
    ]

    assert any('"status": "answered"' in chunk for chunk in chunks)
    assert any('"status": "completed"' in chunk for chunk in chunks)


async def test_terminal_initial_snapshot_closes_notification_source() -> None:
    from src.routers import turns

    turn_id, question_id = uuid4(), uuid4()
    terminal = _state(
        turn_id,
        question_id,
        turn_status="completed",
        turn_version=2,
        question_status="answered",
        question_version=2,
    )
    lifecycle: list[str] = []

    async def notifications():
        lifecycle.append("started")
        try:
            await asyncio.Event().wait()
            yield {}  # pragma: no cover - keeps this an async generator
        finally:
            lifecycle.append("closed")

    async def load():
        raise AssertionError("terminal initial snapshot must not reconcile")

    chunks = [
        chunk
        async for chunk in turns.turn_event_stream(
            terminal, load, notifications(), timeout_seconds=0.2
        )
    ]

    assert len(chunks) == 2
    assert lifecycle == ["started", "closed"]


async def test_snapshot_routes_return_active_and_exact_state(monkeypatch) -> None:
    from src.routers import turns

    org_id, user_id, conversation_id, turn_id, question_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    state = _state(turn_id, question_id)

    class Service:
        async def get_active_turn_snapshot(self, *args):  # noqa: ANN002
            return state

        async def get_turn_snapshot(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return state

    async def db_override():
        yield _Session()

    app = FastAPI()
    app.include_router(turns.router, prefix="/v1")
    app.dependency_overrides[get_current_user] = lambda: UserInfo(
        user_id=str(user_id), org_id=str(org_id), role="member"
    )
    app.dependency_overrides[get_org_db_session] = db_override
    monkeypatch.setattr(turns, "interaction_service", Service())
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        active = await client.get(f"/v1/conversations/{conversation_id}/turns/active")
        exact = await client.get(f"/v1/conversations/{conversation_id}/turns/{turn_id}")

    assert active.status_code == exact.status_code == 200
    assert active.json()["data"] == exact.json()["data"]
    assert active.json()["data"] == {
        "turnId": str(turn_id),
        "status": "active",
        "version": 1,
        "questions": [
            {
                "questionId": str(question_id),
                "status": "pending",
                "version": 1,
            }
        ],
    }


def test_turns_are_mounted_only_on_business_plane() -> None:
    from src.main_admin import app as admin_app
    from src.main_business import app as business_app

    business_paths = set(business_app.openapi()["paths"])
    admin_paths = set(admin_app.openapi()["paths"])
    path = "/v1/conversations/{conversation_id}/turns/{turn_id}/events"

    assert path in business_paths
    assert path not in admin_paths


async def test_server_rejects_new_turn_before_message_or_runtime_start() -> None:
    from src.routers import chat
    from src.services.interaction import InteractionConflict

    conversation_id, user_id, org_id = uuid4(), uuid4(), uuid4()
    conversation = SimpleNamespace(
        id=conversation_id,
        user_id=user_id,
        org_id=org_id,
        status="active",
        last_message_at=None,
    )
    user = UserInfo(user_id=str(user_id), org_id=str(org_id), role="member")
    session = MagicMock()
    session.rollback = AsyncMock()
    session.scalar = AsyncMock(return_value=None)

    class Quota:
        async def preflight(self, actual_org_id):  # noqa: ANN001
            assert actual_org_id == org_id

    with (
        patch.object(
            chat.conversation_repository, "get", AsyncMock(return_value=conversation)
        ),
        patch.object(
            chat.workflow_definition_repository,
            "list_accessible",
            AsyncMock(return_value=[]),
        ),
        patch.object(chat, "ensure_model_allowed", AsyncMock()),
        patch.object(
            chat.interaction_service,
            "begin_turn",
            AsyncMock(side_effect=InteractionConflict()),
        ),
        patch.object(chat.message_repository, "create", AsyncMock()) as create_message,
        patch.object(chat.event_bus, "subscribe_channel", AsyncMock()) as subscribe,
        patch.object(chat.session_manager, "get_or_create", AsyncMock()) as get_runtime,
    ):
        with pytest.raises(HTTPException) as caught:
            await chat.chat_stream(
                ChatRequest(
                    conversation_id=str(conversation_id),
                    model="provider/model",
                    messages=[{"role": "user", "content": "next"}],
                ),
                user,
                session,
                Quota(),
            )

    assert caught.value.status_code == 409
    session.rollback.assert_awaited_once()
    create_message.assert_not_awaited()
    subscribe.assert_not_awaited()
    get_runtime.assert_not_awaited()
