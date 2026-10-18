import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from fastapi import HTTPException

from src.core.auth import UserInfo
from src.models.workflow_run import WorkflowRunStatus
from src.routers import chat
from src.schemas.chat import ChatRequest
from src.services.workflow_authorization import WorkflowDefinitionNotReadable
from src.services.runner_runtime import RunnerRuntimeError

pytestmark = pytest.mark.usefixtures("mock_chat_lifecycle")


class AllowQuota:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def preflight(self, org_id) -> None:  # noqa: ANN001
        self.order.append("quota")


def _fixture():
    conversation_id, user_id, org_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conv = SimpleNamespace(
        id=conversation_id,
        user_id=user_id,
        org_id=org_id,
        status="active",
        last_message_at=None,
    )
    user = UserInfo(user_id=str(user_id), org_id=str(org_id), role="member")
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    pubsub = MagicMock()
    pubsub.unsubscribe = AsyncMock()
    pubsub.aclose = AsyncMock()
    return conv, user, session, pubsub


@pytest.mark.asyncio
async def test_workflow_heartbeats_keep_stream_open_past_original_deadline(monkeypatch):
    conv, user, session, pubsub = _fixture()
    run = SimpleNamespace(id=uuid.uuid4(), execution_snapshot={"supervisor": {"model": "test"}})
    monkeypatch.setattr(chat.settings, "EVENT_SUBSCRIBE_TIMEOUT", 0.2)

    async def messages():
        for _ in range(8):
            await asyncio.sleep(0.04)
            yield {
                "type": "message",
                "data": json.dumps(
                    {
                        "type": "workflow-node-status",
                        "data": {"status": "waiting_human"},
                    }
                ),
            }
        yield {"type": "message", "data": json.dumps({"type": "done", "data": {}})}

    pubsub.listen = messages
    with (
        patch.object(chat.conversation_repository, "get", AsyncMock(return_value=conv)),
        patch.object(
            chat.workflow_run_service, "create_authorized_run", AsyncMock(return_value=run)
        ),
        patch.object(chat.interaction_service, "begin_turn", AsyncMock()),
        patch.object(chat.event_bus, "subscribe_channel", AsyncMock(return_value=pubsub)),
        patch.object(chat.session_manager, "get_or_create", AsyncMock()),
        patch.object(chat, "get_temporal_client", return_value=MagicMock()),
        patch.object(chat, "_signal_or_start_conversation_workflow", AsyncMock(return_value="00000000-0000-0000-0000-000000000001")),
    ):
        response = await chat.chat_stream(
            ChatRequest(
                conversation_id=str(conv.id),
                model="test",
                workflow_id=uuid.uuid4(),
                messages=[{"role": "user", "content": "hi"}],
            ),
            user,
            session,
            AllowQuota([]),
        )
        chunks = [chunk async for chunk in response.body_iterator]
    assert len(chunks) == 9
    assert "event: done" in chunks[-1]


@pytest.mark.asyncio
async def test_workflow_chat_ignores_composer_model_and_signals_exact_run_payload() -> None:
    conv, user, session, pubsub = _fixture()
    workflow_id, run_id = uuid.uuid4(), uuid.uuid4()
    order: list[str] = []
    run = SimpleNamespace(
        id=run_id,
        execution_snapshot={"supervisor": {"model": "provider/supervisor"}},
    )

    async def create_run(*args, **kwargs):  # noqa: ANN002, ANN003
        order.append("snapshot")
        return run

    async def signal(*args, **kwargs):  # noqa: ANN002, ANN003
        order.append("signal")
        return "00000000-0000-0000-0000-000000000001"

    async def begin_turn(*args, **kwargs):  # noqa: ANN002, ANN003
        order.append("turn")

    async def commit() -> None:
        order.append("commit")

    session.commit.side_effect = commit
    with (
        patch(
            "src.routers.chat.conversation_repository.get",
            new_callable=AsyncMock,
            return_value=conv,
        ),
        patch("src.routers.chat.ensure_model_allowed", new_callable=AsyncMock) as ensure_model,
        patch.object(
            chat.workflow_run_service,
            "create_authorized_run",
            side_effect=create_run,
        ) as create_authorized_run,
        patch(
            "src.routers.chat.event_bus.subscribe_channel",
            new_callable=AsyncMock,
            return_value=pubsub,
        ),
        patch("src.routers.chat.session_manager.get_or_create", new_callable=AsyncMock),
        patch(
            "src.routers.chat._signal_or_start_conversation_workflow",
            side_effect=signal,
        ) as signal_workflow,
        patch("src.routers.chat.get_temporal_client", return_value=MagicMock()),
        patch.object(
            chat.interaction_service, "begin_turn", side_effect=begin_turn
        ) as reserve_turn,
    ):
        await chat.chat_stream(
            request=ChatRequest(
                conversation_id=str(conv.id),
                messages=[{"role": "user", "content": "compare reports"}],
                model="composer/ignored",
                workflow_id=workflow_id,
            ),
            user=user,
            session=session,
            quota=AllowQuota(order),
        )

    ensure_model.assert_not_awaited()
    create_authorized_run.assert_awaited_once()
    stored_message = session.add.call_args.args[0]
    assert stored_message.model == "provider/supervisor"
    assert create_authorized_run.call_args.args[-1] == stored_message.id
    reserve_turn.assert_awaited_once_with(
        session,
        chat.authorization_subject(user),
        conv.id,
        stored_message.turn_id,
        run_id,
    )
    payload = signal_workflow.call_args.args[2]
    assert payload == {
        "role": "user",
        "lifecycle_operation_id": ANY,
        "lifecycle_generation": 1,
        "content": "compare reports",
        "turn_id": str(stored_message.turn_id),
        "workflow_run_id": str(run_id),
        "user_questions_enabled": False,
        "web_tools_enabled": False,
    }
    assert order == ["snapshot", "quota", "turn", "commit", "commit", "signal", "commit"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operator_enabled", "has_capability", "expected"),
    [(False, True, False), (True, False, False), (True, True, True)],
)
async def test_web_tools_are_advertised_only_with_operator_and_live_capability(
    monkeypatch,
    operator_enabled: bool,
    has_capability: bool,
    expected: bool,
) -> None:
    conv, user, session, pubsub = _fixture()
    stored_message = SimpleNamespace(turn_id=uuid.uuid4())
    signal = AsyncMock(return_value="00000000-0000-0000-0000-000000000001")
    monkeypatch.setattr(chat.settings, "WEB_ENABLED", operator_enabled)
    with (
        patch.object(chat.conversation_repository, "get", AsyncMock(return_value=conv)),
        patch.object(chat, "_maybe_classify_workflow", AsyncMock(return_value=None)),
        patch.object(chat, "ensure_model_allowed", AsyncMock()),
        patch.object(chat.message_repository, "create", AsyncMock(return_value=stored_message)),
        patch.object(chat.interaction_service, "begin_turn", AsyncMock()),
        patch.object(chat.event_bus, "subscribe_channel", AsyncMock(return_value=pubsub)),
        patch.object(chat.session_manager, "get_or_create", AsyncMock()),
        patch.object(chat, "_signal_or_start_conversation_workflow", signal),
        patch.object(chat, "get_temporal_client", return_value=MagicMock()),
        patch.object(
            chat.authorization_service,
            "has_capability",
            AsyncMock(return_value=has_capability),
        ) as capability_check,
    ):
        await chat.chat_stream(
            request=ChatRequest(
                conversation_id=str(conv.id),
                messages=[{"role": "user", "content": "research"}],
                model="test",
            ),
            user=user,
            session=session,
            quota=AllowQuota([]),
        )

    assert signal.call_args.args[2]["web_tools_enabled"] is expected
    if operator_enabled:
        capability_check.assert_awaited_once()
    else:
        capability_check.assert_not_awaited()


@pytest.mark.asyncio
async def test_workflow_admission_denial_rolls_back_without_starting_runner() -> None:
    conv, user, session, pubsub = _fixture()
    workflow_id = uuid.uuid4()
    with (
        patch(
            "src.routers.chat.conversation_repository.get",
            new_callable=AsyncMock,
            return_value=conv,
        ),
        patch.object(
            chat.workflow_run_service,
            "create_authorized_run",
            new_callable=AsyncMock,
            side_effect=WorkflowDefinitionNotReadable(workflow_id),
        ),
        patch("src.routers.chat.event_bus.subscribe_channel", new_callable=AsyncMock) as subscribe,
        patch(
            "src.routers.chat.session_manager.get_or_create", new_callable=AsyncMock
        ) as get_runner,
    ):
        with pytest.raises(HTTPException) as caught:
            await chat.chat_stream(
                request=ChatRequest(
                    conversation_id=str(conv.id),
                    messages=[{"role": "user", "content": "compare"}],
                    model="ignored",
                    workflow_id=workflow_id,
                ),
                user=user,
                session=session,
                quota=AllowQuota([]),
            )

    assert caught.value.status_code == 404
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
    subscribe.assert_not_awaited()
    get_runner.assert_not_awaited()


@pytest.mark.asyncio
async def test_workflow_runner_failure_marks_exact_run_failed() -> None:
    conv, user, session, pubsub = _fixture()
    workflow_id, run_id = uuid.uuid4(), uuid.uuid4()
    run = SimpleNamespace(
        id=run_id,
        execution_snapshot={"supervisor": {"model": "provider/supervisor"}},
    )
    with (
        patch(
            "src.routers.chat.conversation_repository.get",
            new_callable=AsyncMock,
            return_value=conv,
        ),
        patch.object(
            chat.workflow_run_service,
            "create_authorized_run",
            new_callable=AsyncMock,
            return_value=run,
        ),
        patch.object(
            chat.workflow_run_service,
            "transition",
            new_callable=AsyncMock,
        ) as transition,
        patch.object(chat.interaction_service, "begin_turn", AsyncMock()),
        patch(
            "src.routers.chat.event_bus.subscribe_channel",
            new_callable=AsyncMock,
            return_value=pubsub,
        ),
        patch(
            "src.routers.chat.session_manager.get_or_create",
            new_callable=AsyncMock,
            side_effect=RunnerRuntimeError("runner failed"),
        ),
    ):
        with pytest.raises(HTTPException) as caught:
            await chat.chat_stream(
                request=ChatRequest(
                    conversation_id=str(conv.id),
                    messages=[{"role": "user", "content": "compare"}],
                    model="ignored",
                    workflow_id=workflow_id,
                ),
                user=user,
                session=session,
                quota=AllowQuota([]),
            )

    assert caught.value.status_code == 503
    transition.assert_awaited_once_with(
        session,
        run_id,
        WorkflowRunStatus.FAILED,
        "runner_unavailable",
    )
    assert session.commit.await_count == 3
    pubsub.unsubscribe.assert_awaited_once()
    pubsub.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_ordinary_runner_failure_marks_exact_interaction_turn_failed() -> None:
    conv, user, session, pubsub = _fixture()
    stored_message = SimpleNamespace(turn_id=uuid.uuid4())
    with (
        patch.object(chat.conversation_repository, "get", AsyncMock(return_value=conv)),
        patch.object(chat, "_maybe_classify_workflow", AsyncMock(return_value=None)),
        patch.object(chat, "ensure_model_allowed", AsyncMock()),
        patch.object(chat.message_repository, "create", AsyncMock(return_value=stored_message)),
        patch.object(chat.interaction_service, "begin_turn", AsyncMock()) as begin_turn,
        patch.object(chat.interaction_service, "finish_turn", AsyncMock()) as finish_turn,
        patch.object(chat.event_bus, "subscribe_channel", AsyncMock(return_value=pubsub)),
        patch.object(
            chat.session_manager,
            "get_or_create",
            AsyncMock(side_effect=RunnerRuntimeError("runner failed")),
        ),
    ):
        with pytest.raises(HTTPException) as caught:
            await chat.chat_stream(
                request=ChatRequest(
                    conversation_id=str(conv.id),
                    messages=[{"role": "user", "content": "hello"}],
                    model="provider/model",
                ),
                user=user,
                session=session,
                quota=AllowQuota([]),
            )

    assert caught.value.status_code == 503
    turn_id = begin_turn.await_args.args[3]
    finish_turn.assert_awaited_once_with(
        session,
        conv.org_id,
        conv.user_id,
        conv.id,
        turn_id,
        status="failed",
    )
    assert session.commit.await_count == 3
    pubsub.unsubscribe.assert_awaited_once()
    pubsub.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_assistant_persistence_finishes_exact_interaction_turn_before_commit() -> None:
    conv, _, session, _ = _fixture()
    turn_id = uuid.uuid4()
    message = SimpleNamespace(
        id=uuid.uuid4(),
        role="assistant",
        content="done",
        model="test",
        tokens_in=0,
        tokens_out=0,
        cost_usd=0,
        latency_ms=None,
        tool_calls=None,
        tool_results=None,
        extra={},
        created_at=chat.datetime.now(chat.timezone.utc),
    )
    order: list[str] = []

    async def finish(*args, **kwargs):  # noqa: ANN002, ANN003
        order.append("finish")

    async def commit() -> None:
        order.append("commit")

    session.commit.side_effect = commit
    identity = UserInfo(
        user_id=str(conv.user_id),
        org_id=str(conv.org_id),
        role="member",
        raw_claims={"type": "session", "conversation_id": str(conv.id)},
    )
    with (
        patch.object(chat.conversation_repository, "get", AsyncMock(return_value=conv)),
        patch.object(
            chat.message_repository, "upsert_assistant_by_turn", AsyncMock(return_value=message)
        ),
        patch.object(chat.interaction_service, "finish_turn", side_effect=finish) as finish_turn,
    ):
        response = await chat.persist_conversation_message(
            str(conv.id),
            chat.PersistConversationMessageRequest(
                role="assistant", content="done", model="test", turn_id=turn_id
            ),
            identity,
            session,
        )

    assert response.data.content == "done"
    finish_turn.assert_awaited_once_with(
        session, conv.org_id, conv.user_id, conv.id, turn_id, status="completed"
    )
    assert order == ["finish", "commit"]
