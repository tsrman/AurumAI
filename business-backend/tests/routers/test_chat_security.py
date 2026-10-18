import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, status

from src.core.auth import UserInfo
from src.models.conversation import Conversation
from src.routers.chat import _get_owned_conversation, _parse_conversation_id


@pytest.mark.asyncio
async def test_parse_conversation_id_valid():
    conversation_id = str(uuid.uuid4())
    parsed = _parse_conversation_id(conversation_id)
    assert str(parsed) == conversation_id


def test_parse_conversation_id_invalid_raises_422():
    with pytest.raises(HTTPException) as exc_info:
        _parse_conversation_id("not-a-uuid")
    assert exc_info.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.asyncio
async def test_get_owned_conversation_not_found_raises_404():
    user = UserInfo(
        user_id="user-1",
        org_id="org-1",
        role="member",
    )
    with patch(
        "src.routers.chat.conversation_repository.get", new_callable=AsyncMock
    ) as mock_get:
        mock_get.return_value = None
        with pytest.raises(HTTPException) as exc_info:
            await _get_owned_conversation(None, str(uuid.uuid4()), user)
    assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_get_owned_conversation_foreign_user_raises_403():
    user = UserInfo(
        user_id="user-1",
        org_id="org-1",
        role="member",
    )
    conv = Conversation(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        title="Foreign",
        model="gpt-4o-mini",
    )
    with patch(
        "src.routers.chat.conversation_repository.get", new_callable=AsyncMock
    ) as mock_get:
        mock_get.return_value = conv
        with pytest.raises(HTTPException) as exc_info:
            await _get_owned_conversation(None, str(conv.id), user)
    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.asyncio
async def test_get_owned_conversation_same_org_different_user_raises_403():
    org_id = uuid.uuid4()
    user = UserInfo(
        user_id="user-1",
        org_id=str(org_id),
        role="member",
    )
    conv = Conversation(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        org_id=org_id,
        title="Same org",
        model="gpt-4o-mini",
    )
    with patch(
        "src.routers.chat.conversation_repository.get", new_callable=AsyncMock
    ) as mock_get:
        mock_get.return_value = conv
        with pytest.raises(HTTPException) as exc_info:
            await _get_owned_conversation(None, str(conv.id), user)
    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.asyncio
async def test_get_owned_conversation_owner_returns_conversation():
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    user = UserInfo(
        user_id=str(user_id),
        org_id=str(org_id),
        role="member",
    )
    conv = Conversation(
        id=uuid.uuid4(),
        user_id=user_id,
        org_id=org_id,
        title="Mine",
        model="gpt-4o-mini",
    )
    with patch(
        "src.routers.chat.conversation_repository.get", new_callable=AsyncMock
    ) as mock_get:
        mock_get.return_value = conv
        result = await _get_owned_conversation(None, str(conv.id), user)
    assert result == conv
