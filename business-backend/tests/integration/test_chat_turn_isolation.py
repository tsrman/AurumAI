import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


from src.routers.chat import chat_stream
from src.schemas.chat import ChatRequest

pytestmark = pytest.mark.usefixtures("mock_chat_lifecycle")

USER_ID = "11111111-1111-1111-1111-111111111111"
ORG_ID = "22222222-2222-2222-2222-222222222222"
CONVERSATION_ID = "33333333-3333-3333-3333-333333333333"


class _FakeUser:
    user_id = USER_ID
    org_id = ORG_ID
    role = "member"


class _AllowQuota:
    async def preflight(self, org_id: uuid.UUID) -> None:
        return None


@pytest.mark.asyncio
async def test_chat_stream_filters_events_by_turn_id() -> None:
    """Each /chat/stream subscriber should only receive events tagged with its turn_id."""
    own_turn_id = uuid.uuid4()
    other_turn_id = uuid.uuid4()

    async def fake_listen(pubsub: Any, channel: str, **kwargs: Any) -> AsyncGenerator[dict, None]:
        assert channel == f"{ORG_ID}:session:{CONVERSATION_ID}:events"
        for event in [
            {"type": "message-start", "turn_id": str(own_turn_id), "data": {"role": "assistant"}},
            {"type": "content-delta", "turn_id": str(other_turn_id), "data": {"delta": "wrong"}},
            {"type": "content-delta", "turn_id": str(own_turn_id), "data": {"delta": "right"}},
            {"type": "done", "turn_id": str(own_turn_id), "data": {}},
            {"type": "done", "turn_id": str(other_turn_id), "data": {}},
        ]:
            yield event

    fake_event_bus = MagicMock()
    fake_event_bus.subscribe_channel = AsyncMock(return_value=MagicMock())
    fake_event_bus.listen = fake_listen

    fake_conv = MagicMock()
    fake_conv.user_id = USER_ID
    fake_conv.org_id = ORG_ID

    fake_session = MagicMock()
    fake_session.scalar = AsyncMock(return_value=None)
    fake_session.commit = AsyncMock()

    request = ChatRequest(
        conversation_id=CONVERSATION_ID,
        messages=[{"role": "user", "content": "hello"}],
        model="gpt-4o-mini",
    )

    with patch("src.routers.chat.conversation_repository.get", new_callable=AsyncMock, return_value=fake_conv), patch(
        "src.routers.chat.interaction_service.begin_turn", new_callable=AsyncMock
    ):
        with patch(
            "src.routers.chat.workflow_definition_repository.list_accessible",
            new_callable=AsyncMock,
            return_value=[],
        ):
            with patch("src.routers.chat.message_repository.create", new_callable=AsyncMock):
                with patch("src.routers.chat.session_manager.get_or_create", new_callable=AsyncMock):
                    with patch("src.routers.chat.get_temporal_client", return_value=AsyncMock()):
                        with patch("src.routers.chat._signal_or_start_conversation_workflow", new_callable=AsyncMock, return_value="00000000-0000-0000-0000-000000000001"):
                            with patch("src.routers.chat.event_bus", fake_event_bus):
                                with patch("src.routers.chat.ensure_model_allowed", new_callable=AsyncMock):
                                    with patch("src.routers.chat.uuid.uuid4", return_value=own_turn_id):
                                        response = await chat_stream(
                                            request=request,
                                            user=_FakeUser(),
                                            session=fake_session,
                                            quota=_AllowQuota(),
                                        )

                                        chunks = []
                                        async for chunk in response.body_iterator:
                                            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)

    text = "".join(chunks)
    events = []
    current_type = None
    for line in text.splitlines():
        if line.startswith("event: "):
            current_type = line.removeprefix("event: ")
        elif line.startswith("data: ") and current_type is not None:
            data = json.loads(line.removeprefix("data: "))
            events.append({"type": current_type, **data})
            current_type = None

    types = [e["type"] for e in events]
    assert types == ["message-start", "content-delta", "done"]
    assert any(e.get("delta") == "right" for e in events)
    assert not any(e.get("delta") == "wrong" for e in events)
