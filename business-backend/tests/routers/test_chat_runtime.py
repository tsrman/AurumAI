from unittest.mock import AsyncMock, MagicMock, patch
from datetime import date

import pytest

from fastapi import HTTPException, status

from src.core.auth import UserInfo
from src.core.exceptions import PlatformException
from src.schemas.chat import ChatRequest
from src.routers import chat
from src.services.quota import QuotaExceededError
from src.services.runner_runtime import RunnerReadinessError

pytestmark = pytest.mark.usefixtures("mock_chat_lifecycle")


class AllowQuota:
    async def preflight(self, org_id) -> None:
        return None


@pytest.mark.asyncio
async def test_chat_stream_maps_runner_runtime_error_to_generic_503() -> None:
    """When runner provisioning fails before the SSE response starts, the
    API should fail safely without exposing the runtime's internal message."""

    conversation_id = "22222222-2222-2222-2222-222222222222"
    user_id = "33333333-3333-3333-3333-333333333333"
    org_id = "44444444-4444-4444-4444-444444444444"
    conv = MagicMock()
    conv.id = conversation_id
    conv.user_id = user_id
    conv.org_id = org_id
    conv.status = "active"
    conv.last_message_at = None

    fake_session = MagicMock()
    fake_session.scalar = AsyncMock(return_value=None)
    fake_session.commit = AsyncMock()

    user = UserInfo(
        user_id=user_id,
        org_id=org_id,
        role="member",
    )

    fake_pubsub = MagicMock()
    fake_pubsub.unsubscribe = AsyncMock()
    fake_pubsub.aclose = AsyncMock()
    fake_event_bus = MagicMock()
    fake_event_bus.subscribe_channel = AsyncMock(return_value=fake_pubsub)

    secret_message = "secret URL https://token:supersecret@kubernetes.default/api"
    with patch("src.routers.chat.event_bus", fake_event_bus), patch.object(
        chat.interaction_service, "begin_turn", AsyncMock()
    ), patch.object(chat.interaction_service, "finish_turn", AsyncMock()):
        with patch(
            "src.routers.chat.conversation_repository.get",
            new_callable=AsyncMock,
            return_value=conv,
        ):
            with patch(
                "src.routers.chat.workflow_definition_repository.list_accessible",
                new_callable=AsyncMock,
                return_value=[],
            ):
                with patch("src.routers.chat.message_repository.create", new_callable=AsyncMock):
                    with patch("src.routers.chat.ensure_model_allowed", new_callable=AsyncMock):
                        with patch(
                            "src.routers.chat.session_manager.get_or_create",
                            new_callable=AsyncMock,
                            side_effect=RunnerReadinessError(secret_message),
                        ):
                            with pytest.raises(HTTPException) as exc_info:
                                await chat.chat_stream(
                                    request=ChatRequest(
                                        conversation_id=conversation_id,
                                        messages=[{"role": "user", "content": "hi"}],
                                        model="gpt-4o-mini",
                                    ),
                                    user=user,
                                    session=fake_session,
                                    quota=AllowQuota(),
                                )

    assert exc_info.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert exc_info.value.detail == "Runner runtime unavailable"
    assert "secret URL" not in str(exc_info.value.detail)
    assert "supersecret" not in str(exc_info.value.detail)
    fake_pubsub.unsubscribe.assert_awaited_once_with(f"{org_id}:session:{conversation_id}:events")
    fake_pubsub.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_stream_rejects_quota_before_persisting_or_starting_runtime() -> None:
    conversation_id = "22222222-2222-2222-2222-222222222222"
    user_id = "33333333-3333-3333-3333-333333333333"
    org_id = "44444444-4444-4444-4444-444444444444"
    conv = MagicMock(
        id=conversation_id,
        user_id=user_id,
        org_id=org_id,
        status="active",
        last_message_at=None,
    )
    user = UserInfo(
        user_id=user_id,
        org_id=org_id,
        role="member",
    )
    fake_session = MagicMock()
    fake_session.scalar = AsyncMock(return_value=None)
    fake_session.commit = AsyncMock()
    order: list[str] = []

    class RejectQuota:
        async def preflight(self, actual_org_id) -> None:
            order.append("preflight")
            raise QuotaExceededError(
                "tokens", used=12, limit=10, period_end=date(2026, 9, 1)
            )

    async def get_conversation(*args, **kwargs):
        order.append("conversation")
        return conv

    async def allow_model(*args, **kwargs):
        order.append("model")

    with patch(
        "src.routers.chat.conversation_repository.get", side_effect=get_conversation
    ):
        with patch(
            "src.routers.chat.workflow_definition_repository.list_accessible",
            new_callable=AsyncMock,
            return_value=[],
        ):
            with patch("src.routers.chat.ensure_model_allowed", side_effect=allow_model):
                with patch(
                    "src.routers.chat.message_repository.create", new_callable=AsyncMock
                ) as create_message:
                    with patch(
                        "src.routers.chat.event_bus.subscribe_channel", new_callable=AsyncMock
                    ) as subscribe:
                        with patch(
                            "src.routers.chat.session_manager.get_or_create",
                            new_callable=AsyncMock,
                        ) as get_runner:
                            with pytest.raises(PlatformException) as exc_info:
                                await chat.chat_stream(
                                    request=ChatRequest(
                                        conversation_id=conversation_id,
                                        messages=[{"role": "user", "content": "hi"}],
                                        model="gpt-test",
                                    ),
                                    user=user,
                                    session=fake_session,
                                    quota=RejectQuota(),
                                )

    assert order == ["conversation", "model", "preflight"]
    assert exc_info.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    assert exc_info.value.code == "quota_exceeded"
    assert exc_info.value.details == {
        "resource": "tokens",
        "used": 12,
        "limit": 10,
        "periodEnd": "2026-09-01",
    }
    create_message.assert_not_awaited()
    fake_session.commit.assert_not_awaited()
    subscribe.assert_not_awaited()
    get_runner.assert_not_awaited()


@pytest.mark.asyncio
async def test_conversation_start_failure_is_logged_before_generic_503(caplog) -> None:
    """The Temporal start path maps any failure to a generic 503; the cause
    must still reach the server log, or the failure cannot be diagnosed."""
    conversation_id = "22222222-2222-2222-2222-222222222222"
    user_id = "33333333-3333-3333-3333-333333333333"
    org_id = "44444444-4444-4444-4444-444444444444"
    conv = MagicMock(id=conversation_id, user_id=user_id, org_id=org_id, status="active", last_message_at=None)
    fake_session = MagicMock()
    fake_session.scalar = AsyncMock(return_value=None)
    fake_session.commit = AsyncMock()
    fake_pubsub = MagicMock(unsubscribe=AsyncMock(), aclose=AsyncMock())
    fake_event_bus = MagicMock(subscribe_channel=AsyncMock(return_value=fake_pubsub))

    with (
        patch("src.routers.chat.event_bus", fake_event_bus),
        patch.object(chat.interaction_service, "begin_turn", AsyncMock()),
        patch.object(chat.interaction_service, "finish_turn", AsyncMock()),
        patch("src.routers.chat.conversation_repository.get", new_callable=AsyncMock, return_value=conv),
        patch("src.routers.chat.workflow_definition_repository.list_accessible", new_callable=AsyncMock, return_value=[]),
        patch("src.routers.chat.message_repository.create", new_callable=AsyncMock),
        patch("src.routers.chat.ensure_model_allowed", new_callable=AsyncMock),
        patch("src.routers.chat.session_manager.get_or_create", new_callable=AsyncMock),
        patch("src.routers.chat.get_temporal_client", side_effect=RuntimeError("temporal down")),
        caplog.at_level("WARNING", logger=chat.__name__),
        pytest.raises(HTTPException) as exc_info,
    ):
        await chat.chat_stream(
            request=ChatRequest(
                conversation_id=conversation_id,
                messages=[{"role": "user", "content": "hi"}],
                model="gpt-4o-mini",
            ),
            user=UserInfo(user_id=user_id, org_id=org_id, role="member"),
            session=fake_session,
            quota=AllowQuota(),
        )

    assert exc_info.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert exc_info.value.detail == "Runner runtime unavailable"
    records = [r for r in caplog.records if r.getMessage() == "conversation_start_unavailable"]
    assert len(records) == 1
    assert records[0].conversation_id == conversation_id
    assert isinstance(records[0].exc_info[1], RuntimeError)
