from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from src.core.auth import UserInfo
from src.repositories.image_repository import ImageRepositoryError
from src.routers import chat


def _fixture():  # noqa: ANN202
    org_id, user_id, conversation_id, turn_id = uuid4(), uuid4(), uuid4(), uuid4()
    conv = SimpleNamespace(
        id=conversation_id, org_id=org_id, user_id=user_id, status="active", last_message_at=None
    )
    identity = UserInfo(
        user_id=str(user_id),
        org_id=str(org_id),
        role="member",
        raw_claims={"type": "session", "conversation_id": str(conversation_id)},
    )
    session = MagicMock()
    session.commit = AsyncMock()
    return conv, identity, session, turn_id


def _stored(turn_id, extra):  # noqa: ANN001, ANN202
    return SimpleNamespace(
        id=uuid4(),
        turn_id=turn_id,
        role="assistant",
        content="Here it is",
        model="test",
        tokens_in=0,
        tokens_out=0,
        cost_usd=0,
        latency_ms=None,
        tool_calls=[],
        tool_results={},
        extra=extra,
        created_at=datetime.now(UTC),
    )


async def test_only_images_of_this_turn_are_kept_on_the_message() -> None:
    conv, identity, session, turn_id = _fixture()
    own, foreign = uuid4(), uuid4()
    repository = MagicMock()
    repository.owned_image_ids = AsyncMock(return_value={str(own)})
    repository.turn_image_ids = AsyncMock(return_value=[str(own)])
    stored = _stored(turn_id, {"image_ids": [str(own)]})
    with (
        patch.object(chat.conversation_repository, "get", AsyncMock(return_value=conv)),
        patch.object(
            chat.message_repository, "upsert_assistant_by_turn", AsyncMock(return_value=stored)
        ) as upsert,
        patch.object(chat.interaction_service, "finish_turn", AsyncMock()),
        patch.object(chat, "ImageRepository", return_value=repository),
        patch.object(chat, "turn_files", AsyncMock(return_value=[])),
        patch.object(chat, "turn_previews", AsyncMock(return_value=[])),
    ):
        response = await chat.persist_conversation_message(
            str(conv.id),
            chat.PersistConversationMessageRequest(
                role="assistant",
                content="Here it is",
                turn_id=turn_id,
                tool_results={"__images__": {"imageIds": [str(own), str(foreign), str(own)]}},
            ),
            identity,
            session,
        )

    repository.owned_image_ids.assert_awaited_once_with(
        session, conv.id, conv.user_id, turn_id, (str(own), str(foreign))
    )
    assert upsert.await_args.kwargs["tool_results"] is None
    assert upsert.await_args.kwargs["extra"] == {"image_ids": [str(own)]}
    assert response.data.imageIds == [str(own)]


@pytest.mark.parametrize(
    "claims",
    [{"imageIds": ["not-a-uuid"]}, {"imageIds": [], "urls": ["https://a"]}],
)
async def test_malformed_image_claims_are_rejected(claims) -> None:  # noqa: ANN001
    conv, identity, session, turn_id = _fixture()
    with (
        patch.object(chat.conversation_repository, "get", AsyncMock(return_value=conv)),
        patch.object(chat.message_repository, "upsert_assistant_by_turn", AsyncMock()) as upsert,
    ):
        with pytest.raises(HTTPException) as caught:
            await chat.persist_conversation_message(
                str(conv.id),
                chat.PersistConversationMessageRequest(
                    role="assistant", content="x", turn_id=turn_id, tool_results={"__images__": claims}
                ),
                identity,
                session,
            )
    assert caught.value.status_code == 422
    upsert.assert_not_called()


async def test_claims_for_a_foreign_turn_are_not_found() -> None:
    conv, identity, session, turn_id = _fixture()
    repository = MagicMock()
    repository.owned_image_ids = AsyncMock(side_effect=ImageRepositoryError("interaction_not_found"))
    with (
        patch.object(chat.conversation_repository, "get", AsyncMock(return_value=conv)),
        patch.object(chat.message_repository, "upsert_assistant_by_turn", AsyncMock()) as upsert,
        patch.object(chat, "ImageRepository", return_value=repository),
    ):
        with pytest.raises(HTTPException) as caught:
            await chat.persist_conversation_message(
                str(conv.id),
                chat.PersistConversationMessageRequest(
                    role="assistant",
                    content="x",
                    turn_id=turn_id,
                    tool_results={"__images__": {"imageIds": [str(uuid4())]}},
                ),
                identity,
                session,
            )
    assert caught.value.status_code == 404
    upsert.assert_not_called()


async def test_the_answer_shows_the_images_and_files_the_runner_never_saw() -> None:
    """Hermes's tool results never reach the runner: the turn's other images
    follow the claimed ones, and its files come with the answer."""
    conv, identity, session, turn_id = _fixture()
    claimed, hermes_image = str(uuid4()), str(uuid4())
    attached = [{"fileId": str(uuid4()), "name": "report.csv", "contentType": "text/csv", "sizeBytes": 12}]
    repository = MagicMock()
    repository.owned_image_ids = AsyncMock(return_value={claimed})
    repository.turn_image_ids = AsyncMock(return_value=[hermes_image, claimed])
    files = AsyncMock(return_value=attached)
    expected = {"image_ids": [claimed, hermes_image], "files": attached}
    with (
        patch.object(chat.conversation_repository, "get", AsyncMock(return_value=conv)),
        patch.object(
            chat.message_repository, "upsert_assistant_by_turn", AsyncMock(return_value=_stored(turn_id, expected))
        ) as upsert,
        patch.object(chat.interaction_service, "finish_turn", AsyncMock()),
        patch.object(chat, "ImageRepository", return_value=repository),
        patch.object(chat, "turn_files", files),
        patch.object(chat, "turn_previews", AsyncMock(return_value=[])),
    ):
        response = await chat.persist_conversation_message(
            str(conv.id),
            chat.PersistConversationMessageRequest(
                role="assistant",
                content="Here it is",
                turn_id=turn_id,
                tool_results={"__images__": {"imageIds": [claimed]}},
            ),
            identity,
            session,
        )

    repository.turn_image_ids.assert_awaited_once_with(session, conv.id, conv.user_id, turn_id)
    files.assert_awaited_once_with(session, conv.org_id, conv.id, conv.user_id, turn_id)
    assert upsert.await_args.kwargs["extra"] == expected
    assert response.data.imageIds == [claimed, hermes_image]
    assert [item.model_dump() for item in response.data.files] == attached
