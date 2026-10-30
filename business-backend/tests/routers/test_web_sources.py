from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport

from src.core.auth import UserInfo, get_current_user
from src.core.dependencies import get_org_db_session
from src.routers import chat
from src.schemas.web_tools import WebSourceResponse
from src.repositories.web_source_repository import WebSourceRepositoryError


class _Service:
    async def list_sources(
        self,
        session: object,
        subject: object,
        conversation_id: UUID,
        turn_id: UUID,
    ) -> list[WebSourceResponse]:
        del session, subject, conversation_id, turn_id
        return [
            WebSourceResponse(
                source_id=uuid4(),
                kind="read",
                requested_url="https://example.org/report",
                final_url="https://cdn.example.org/report",
                title="Report",
                content_type="text/html",
                snippet="Normalized content",
                published_at=None,
                retrieved_at=datetime(2026, 9, 7, tzinfo=UTC),
                truncated=False,
                page_count=None,
            )
        ]


async def test_owner_source_route_returns_server_provenance(monkeypatch) -> None:
    from src.routers import web_sources

    identity = UserInfo(
        user_id=str(uuid4()),
        org_id=str(uuid4()),
        role="member",
        raw_claims={},
    )

    async def identity_override() -> UserInfo:
        return identity

    async def db_override():  # noqa: ANN202
        yield object()

    app = FastAPI()
    app.include_router(web_sources.router, prefix="/v1")
    app.dependency_overrides[get_current_user] = identity_override
    app.dependency_overrides[get_org_db_session] = db_override
    monkeypatch.setattr(web_sources, "web_tools_service", _Service())
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/v1/conversations/{uuid4()}/turns/{uuid4()}/sources"
        )

    assert response.status_code == 200
    source = response.json()["data"][0]
    assert source["kind"] == "read"
    assert source["finalUrl"] == "https://cdn.example.org/report"
    assert source["snippet"] == "Normalized content"
    assert "content" not in source


async def test_runner_citations_are_rechecked_for_exact_turn_before_persistence() -> None:
    org_id = uuid4()
    user_id = uuid4()
    conversation_id = uuid4()
    turn_id = uuid4()
    accepted_id = uuid4()
    wrong_turn_id = uuid4()
    claimed_unsupported_id = uuid4()
    conv = SimpleNamespace(
        id=conversation_id,
        org_id=org_id,
        user_id=user_id,
        status="active",
        last_message_at=None,
    )
    stored = SimpleNamespace(
        id=uuid4(),
        turn_id=turn_id,
        role="assistant",
        content=f"Fact [source:{accepted_id}]",
        model="test",
        tokens_in=0,
        tokens_out=0,
        cost_usd=0,
        latency_ms=None,
        tool_calls=[],
        tool_results={"safe-call": {"status": "completed"}},
        extra={
            "web_source_ids": [str(accepted_id)],
            "unsupported_web_source_ids": [
                str(wrong_turn_id),
                str(claimed_unsupported_id),
            ],
        },
        created_at=datetime.now(UTC),
    )
    session = MagicMock()
    session.commit = AsyncMock()
    repository = MagicMock()
    repository.owned_source_ids = AsyncMock(return_value={str(accepted_id)})
    identity = UserInfo(
        user_id=str(user_id),
        org_id=str(org_id),
        role="member",
        raw_claims={"type": "session", "conversation_id": str(conversation_id)},
    )

    with (
        patch.object(chat.conversation_repository, "get", AsyncMock(return_value=conv)),
        patch.object(
            chat.message_repository,
            "upsert_assistant_by_turn",
            AsyncMock(return_value=stored),
        ) as upsert,
        patch.object(chat.interaction_service, "finish_turn", AsyncMock()),
        patch.object(chat, "_answer_media", AsyncMock(side_effect=lambda *args: args[-1])),
        patch.object(chat, "WebSourceRepository", return_value=repository),
    ):
        response = await chat.persist_conversation_message(
            str(conversation_id),
            chat.PersistConversationMessageRequest(
                role="assistant",
                content=f"Fact [source:{accepted_id}]",
                model="test",
                turn_id=turn_id,
                tool_results={
                    "safe-call": {"status": "completed"},
                    "__web_sources__": {
                        "sourceIds": [str(accepted_id), str(wrong_turn_id)],
                        "unsupportedSourceIds": [str(claimed_unsupported_id)],
                    },
                },
            ),
            identity,
            session,
        )

    repository.owned_source_ids.assert_awaited_once_with(
        session,
        conversation_id,
        user_id,
        turn_id,
        (str(accepted_id), str(wrong_turn_id)),
    )
    assert upsert.await_args.kwargs["tool_results"] == {
        "safe-call": {"status": "completed"}
    }
    assert upsert.await_args.kwargs["extra"] == stored.extra
    assert response.data.sourceIds == [str(accepted_id)]
    assert response.data.turnId == str(turn_id)
    assert response.data.unsupportedSourceIds == [
        str(wrong_turn_id),
        str(claimed_unsupported_id),
    ]


async def test_runner_session_cannot_persist_into_another_conversation() -> None:
    identity = UserInfo(
        user_id=str(uuid4()),
        org_id=str(uuid4()),
        role="member",
        raw_claims={"type": "session", "conversation_id": str(uuid4())},
    )
    session = MagicMock()

    with pytest.raises(HTTPException) as caught:
        await chat.persist_conversation_message(
            str(uuid4()),
            chat.PersistConversationMessageRequest(
                role="assistant",
                content="cross conversation",
                turn_id=uuid4(),
            ),
            identity,
            session,
        )

    assert caught.value.status_code == 403


async def test_source_claims_require_turn_bound_to_same_conversation_and_owner() -> None:
    org_id, user_id, conversation_id, turn_id = (uuid4() for _ in range(4))
    conv = SimpleNamespace(
        id=conversation_id,
        org_id=org_id,
        user_id=user_id,
        status="active",
        last_message_at=None,
    )
    session = MagicMock()
    repository = MagicMock()
    repository.owned_source_ids = AsyncMock(
        side_effect=WebSourceRepositoryError("interaction_not_found")
    )
    identity = UserInfo(
        user_id=str(user_id),
        org_id=str(org_id),
        role="member",
        raw_claims={"type": "session", "conversation_id": str(conversation_id)},
    )

    with (
        patch.object(chat.conversation_repository, "get", AsyncMock(return_value=conv)),
        patch.object(chat, "WebSourceRepository", return_value=repository),
        patch.object(chat.message_repository, "upsert_assistant_by_turn") as upsert,
        pytest.raises(HTTPException) as caught,
    ):
        await chat.persist_conversation_message(
            str(conversation_id),
            chat.PersistConversationMessageRequest(
                role="assistant",
                content="wrong turn",
                turn_id=turn_id,
                tool_results={
                    "__web_sources__": {
                        "sourceIds": [str(uuid4())],
                        "unsupportedSourceIds": [],
                    }
                },
            ),
            identity,
            session,
        )

    assert caught.value.status_code == 404
    upsert.assert_not_called()
