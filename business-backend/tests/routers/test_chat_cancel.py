from unittest.mock import AsyncMock, patch

import pytest

from src.core.auth import UserInfo
from src.models.conversation import Conversation
import uuid
from types import SimpleNamespace

from src.core.authorization_types import AuthorizationSubject
from src.routers import workflows


@pytest.mark.asyncio
async def test_cancel_generation_sends_signal():
    user = UserInfo(
        user_id=str(uuid.uuid4()),
        org_id=str(uuid.uuid4()),
        role="member",
    )
    conv = Conversation(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        title="Test",
        model="gpt-4o-mini",
    )

    with patch(
        "src.routers.chat._get_owned_conversation", new_callable=AsyncMock
    ) as mock_get_conv, patch(
        "src.routers.chat.get_temporal_client"
    ) as mock_get_client, patch(
        "src.routers.chat.interaction_service.cancel_active_turn",
        new_callable=AsyncMock,
        return_value=None,
    ):
        mock_get_conv.return_value = conv
        handle = AsyncMock()
        mock_get_client.return_value.get_workflow_handle.return_value = handle

        # import inside patch context to avoid startup side effects
        from src.routers.chat import cancel_generation

        await cancel_generation(str(conv.id), user, None)

        mock_get_client.return_value.get_workflow_handle.assert_called_once_with(
            f"conversation-{conv.id}"
        )
        handle.signal.assert_awaited_once_with("cancel_generation")


@pytest.mark.asyncio
async def test_cancel_generation_commits_durable_turn_before_exact_signal():
    from src.routers import chat

    org_id, user_id, conversation_id, turn_id = (uuid.uuid4() for _ in range(4))
    user = UserInfo(user_id=str(user_id), org_id=str(org_id), role="member")
    session = AsyncMock()
    conv = SimpleNamespace(id=conversation_id)
    targets = []
    order = []

    async def cancel_active(*args):
        order.append("durable-cancel")
        return turn_id, targets

    async def commit():
        order.append("commit")

    async def signal(*args):
        order.append("exact-signal")

    session.commit.side_effect = commit
    with patch.object(chat, "_get_owned_conversation", AsyncMock(return_value=conv)), patch.object(
        chat.interaction_service, "cancel_active_turn", side_effect=cancel_active
    ) as cancel, patch.object(
        chat.interaction_service, "signal_turn_cancel", side_effect=signal
    ) as exact_signal, patch.object(chat, "get_temporal_client") as legacy:
        legacy.return_value.get_workflow_handle.return_value = AsyncMock()
        await chat.cancel_generation(str(conversation_id), user, session)

    cancel.assert_awaited_once_with(
        session, AuthorizationSubject(user_id=user_id, org_id=org_id, role="member"),
        conversation_id,
    )
    exact_signal.assert_awaited_once_with(targets, turn_id, conversation_id)
    legacy.assert_not_called()
    assert order == ["durable-cancel", "commit", "exact-signal"]


@pytest.mark.asyncio
async def test_cancel_generation_does_not_signal_if_durable_commit_fails():
    from src.routers import chat

    org_id, user_id, conversation_id, turn_id = (uuid.uuid4() for _ in range(4))
    user = UserInfo(user_id=str(user_id), org_id=str(org_id), role="member")
    session = AsyncMock()
    session.commit.side_effect = RuntimeError("commit failed")
    with patch.object(chat, "_get_owned_conversation", AsyncMock(return_value=SimpleNamespace(id=conversation_id))), patch.object(
        chat.interaction_service, "cancel_active_turn", AsyncMock(return_value=(turn_id, []))
    ), patch.object(chat.interaction_service, "signal_turn_cancel", new_callable=AsyncMock) as signal, patch.object(
        chat, "get_temporal_client"
    ) as legacy:
        legacy.return_value.get_workflow_handle.return_value = AsyncMock()
        with pytest.raises(RuntimeError, match="commit failed"):
            await chat.cancel_generation(str(conversation_id), user, session)
    signal.assert_not_awaited()
    legacy.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_workflow_run_commits_before_sending_exact_run_signal():
    org_id, user_id, conversation_id, run_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    user = UserInfo(user_id=str(user_id), org_id=str(org_id), role="member")
    session = AsyncMock()
    run = SimpleNamespace(id=run_id, conversation_id=conversation_id)
    order: list[str] = []

    async def request_cancel(*args):  # noqa: ANN002
        order.append("db-transition")
        return run

    async def signal(*args):  # noqa: ANN002
        order.append("signal")

    async def commit():
        order.append("commit")

    session.commit.side_effect = commit
    with patch.object(
        workflows.workflow_run_service,
        "request_cancel",
        side_effect=request_cancel,
    ) as cancel, patch(
        "src.routers.workflows.get_temporal_client"
    ) as temporal:
        handle = AsyncMock()
        handle.signal.side_effect = signal
        temporal.return_value.get_workflow_handle.return_value = handle

        response = await workflows.cancel_workflow_run(run_id, user, session)

    cancel.assert_awaited_once_with(
        session,
        AuthorizationSubject(user_id=user_id, org_id=org_id, role="member"),
        run_id,
    )
    temporal.return_value.get_workflow_handle.assert_called_once_with(
        f"conversation-{conversation_id}"
    )
    handle.signal.assert_awaited_once_with("cancel_workflow_run", str(run_id))
    assert response.data.run_id == run_id
    assert order == ["db-transition", "commit", "signal"]
