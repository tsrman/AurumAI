import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


from src.core.auth import UserInfo
from src.routers import chat
from src.routers.chat import _extract_workflow_call
from src.schemas.chat import ChatRequest
from src.services.workflow_graph import WorkflowGraphError

pytestmark = pytest.mark.usefixtures("mock_chat_lifecycle")


@pytest.fixture(autouse=True)
def _interaction_turn_boundary():
    with patch.object(chat.interaction_service, "begin_turn", AsyncMock()):
        yield


def test_extract_workflow_call_handles_missing_closing_tag_and_code_fence() -> None:
    """Live verification against a real small model showed it correctly
    naming a workflow but wrapping the reply in a markdown code fence and
    never emitting the closing tag — the parser must recover that instead of
    silently discarding a correct classification."""
    workflow_id = uuid.uuid4()
    text = f'```\n<workflow_call>{{"workflow_id": "{workflow_id}"}}\n```'
    assert _extract_workflow_call(text) == workflow_id


def test_extract_workflow_call_prefers_well_formed_closing_tag() -> None:
    workflow_id = uuid.uuid4()
    text = (
        f'<workflow_call>{{"workflow_id": "{workflow_id}"}}</workflow_call>'
        " (trailing commentary the model appended)"
    )
    assert _extract_workflow_call(text) == workflow_id


def test_extract_workflow_call_returns_none_without_a_start_tag() -> None:
    assert _extract_workflow_call("no match here") is None


class RoutingQuota:
    """Records the reserve/accept/complete/cancel calls a classification
    attempt makes, distinct from the endpoint's own `session`: quota's real
    reserve/accept/complete run through their own session factory, not the
    request-scoped one."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def preflight(self, org_id) -> None:  # noqa: ANN001
        self.calls.append("preflight")

    async def reserve(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.calls.append("reserve")

    async def accept(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.calls.append("accept")

    async def complete(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.calls.append("complete")

    async def cancel(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.calls.append("cancel")


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_classifier_cancellation_refunds_only_unaccepted_calls(accepted, cleanup_fails):
    conv, user, session, _ = _fixture()
    quota = RoutingQuota()
    entered = asyncio.Event()

    async def stream(request, *, on_accepted=None):
        if accepted:
            await on_accepted()
        entered.set()
        await asyncio.Event().wait()
        yield ""

    async def cancel(*args):
        quota.calls.append("cancel")
        if cleanup_fails:
            raise RuntimeError("test quota store unavailable")

    with (
        patch.object(chat.workflow_definition_repository, "list_accessible", AsyncMock(return_value=[SimpleNamespace(id=uuid.uuid4(), name="test", description="")])),
        patch.object(chat, "ensure_model_allowed", AsyncMock()),
        patch.object(chat.llm_client, "stream_chat", stream),
        patch.object(quota, "cancel", cancel),
    ):
        task = asyncio.create_task(chat._maybe_classify_workflow(session, quota, user, ChatRequest(
            conversation_id=str(conv.id), model="test-model", messages=[{"role": "user", "content": "hi"}],
        )))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert quota.calls.count("reserve") == 1
    assert quota.calls.count("cancel") == (0 if accepted else 1)


def _fixture():
    conversation_id, user_id, org_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conv = SimpleNamespace(
        id=conversation_id,
        user_id=user_id,
        org_id=org_id,
        status="active",
        last_message_at=None,
        temperature=None,
        agent_definition_id=None,
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


def _fake_stream_chat(content: str):
    async def stream(request, *, on_accepted=None):  # noqa: ANN001
        if on_accepted is not None:
            await on_accepted()
        yield json.dumps({"choices": [{"delta": {"content": content}}]})
        yield json.dumps(
            {
                "choices": [{"delta": {}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        )

    return stream


@pytest.mark.asyncio
async def test_no_auto_routable_candidates_never_invokes_classifier() -> None:
    conv, user, session, pubsub = _fixture()
    with patch(
        "src.routers.chat.conversation_repository.get",
        new_callable=AsyncMock,
        return_value=conv,
    ), patch(
        "src.routers.chat.workflow_definition_repository.list_accessible",
        new_callable=AsyncMock,
        return_value=[],
    ) as list_accessible, patch(
        "src.routers.chat.llm_client.stream_chat"
    ) as stream_chat, patch(
        "src.routers.chat.ensure_model_allowed", new_callable=AsyncMock
    ), patch(
        "src.routers.chat.message_repository.create", new_callable=AsyncMock
    ) as create_message, patch(
        "src.routers.chat.event_bus.subscribe_channel",
        new_callable=AsyncMock,
        return_value=pubsub,
    ), patch(
        "src.routers.chat.session_manager.get_or_create", new_callable=AsyncMock
    ), patch(
        "src.routers.chat._signal_or_start_conversation_workflow", new_callable=AsyncMock, return_value="00000000-0000-0000-0000-000000000001"
    ), patch(
        "src.routers.chat.get_temporal_client", return_value=MagicMock()
    ):
        await chat.chat_stream(
            request=ChatRequest(
                conversation_id=str(conv.id),
                messages=[{"role": "user", "content": "hello"}],
                model="gpt-test",
            ),
            user=user,
            session=session,
            quota=RoutingQuota(),
        )

    list_accessible.assert_awaited_once()
    stream_chat.assert_not_called()
    create_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_classifier_match_routes_exactly_like_an_explicit_pick() -> None:
    conv, user, session, pubsub = _fixture()
    workflow_id, run_id = uuid.uuid4(), uuid.uuid4()
    candidate = SimpleNamespace(id=workflow_id, name="Vendor onboarding", description="...")
    run = SimpleNamespace(
        id=run_id,
        execution_snapshot={"supervisor": {"model": "provider/supervisor"}},
    )
    quota = RoutingQuota()

    with patch(
        "src.routers.chat.conversation_repository.get",
        new_callable=AsyncMock,
        return_value=conv,
    ), patch(
        "src.routers.chat.workflow_definition_repository.list_accessible",
        new_callable=AsyncMock,
        return_value=[candidate],
    ), patch(
        "src.routers.chat.ensure_model_allowed", new_callable=AsyncMock
    ), patch(
        "src.routers.chat.llm_client.stream_chat",
        side_effect=_fake_stream_chat(
            f'<workflow_call>{{"workflow_id": "{workflow_id}"}}</workflow_call>'
        ),
    ), patch.object(
        chat.workflow_run_service,
        "create_authorized_run",
        new_callable=AsyncMock,
        return_value=run,
    ) as create_authorized_run, patch(
        "src.routers.chat.event_bus.subscribe_channel",
        new_callable=AsyncMock,
        return_value=pubsub,
    ), patch(
        "src.routers.chat.session_manager.get_or_create", new_callable=AsyncMock
    ), patch(
        "src.routers.chat._signal_or_start_conversation_workflow", new_callable=AsyncMock, return_value="00000000-0000-0000-0000-000000000001"
    ) as signal_workflow, patch(
        "src.routers.chat.get_temporal_client", return_value=MagicMock()
    ):
        await chat.chat_stream(
            request=ChatRequest(
                conversation_id=str(conv.id),
                messages=[{"role": "user", "content": "onboard Acme as a vendor"}],
                model="gpt-test",
            ),
            user=user,
            session=session,
            quota=quota,
        )

    create_authorized_run.assert_awaited_once()
    assert create_authorized_run.call_args.args[2] == workflow_id
    payload = signal_workflow.call_args.args[2]
    assert payload["workflow_run_id"] == str(run_id)
    assert payload["user_questions_enabled"] is False
    assert payload["web_tools_enabled"] is False
    assert "accept" in quota.calls and "complete" in quota.calls
    assert "cancel" not in quota.calls


@pytest.mark.asyncio
async def test_classifier_id_outside_candidate_set_falls_back_to_ordinary_chat() -> None:
    conv, user, session, pubsub = _fixture()
    workflow_id, hallucinated_id = uuid.uuid4(), uuid.uuid4()
    candidate = SimpleNamespace(id=workflow_id, name="Vendor onboarding", description="...")

    with patch(
        "src.routers.chat.conversation_repository.get",
        new_callable=AsyncMock,
        return_value=conv,
    ), patch(
        "src.routers.chat.workflow_definition_repository.list_accessible",
        new_callable=AsyncMock,
        return_value=[candidate],
    ), patch(
        "src.routers.chat.ensure_model_allowed", new_callable=AsyncMock
    ), patch(
        "src.routers.chat.llm_client.stream_chat",
        side_effect=_fake_stream_chat(
            f'<workflow_call>{{"workflow_id": "{hallucinated_id}"}}</workflow_call>'
        ),
    ), patch.object(
        chat.workflow_run_service, "create_authorized_run", new_callable=AsyncMock
    ) as create_authorized_run, patch(
        "src.routers.chat.message_repository.create", new_callable=AsyncMock
    ) as create_message, patch(
        "src.routers.chat.event_bus.subscribe_channel",
        new_callable=AsyncMock,
        return_value=pubsub,
    ), patch(
        "src.routers.chat.session_manager.get_or_create", new_callable=AsyncMock
    ), patch(
        "src.routers.chat._signal_or_start_conversation_workflow", new_callable=AsyncMock, return_value="00000000-0000-0000-0000-000000000001"
    ), patch(
        "src.routers.chat.get_temporal_client", return_value=MagicMock()
    ):
        await chat.chat_stream(
            request=ChatRequest(
                conversation_id=str(conv.id),
                messages=[{"role": "user", "content": "hello"}],
                model="gpt-test",
            ),
            user=user,
            session=session,
            quota=RoutingQuota(),
        )

    create_authorized_run.assert_not_awaited()
    create_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_routed_run_creation_failure_falls_back_instead_of_404() -> None:
    conv, user, session, pubsub = _fixture()
    workflow_id = uuid.uuid4()
    candidate = SimpleNamespace(id=workflow_id, name="Vendor onboarding", description="...")

    with patch(
        "src.routers.chat.conversation_repository.get",
        new_callable=AsyncMock,
        return_value=conv,
    ), patch(
        "src.routers.chat.workflow_definition_repository.list_accessible",
        new_callable=AsyncMock,
        return_value=[candidate],
    ), patch(
        "src.routers.chat.ensure_model_allowed", new_callable=AsyncMock
    ), patch(
        "src.routers.chat.llm_client.stream_chat",
        side_effect=_fake_stream_chat(
            f'<workflow_call>{{"workflow_id": "{workflow_id}"}}</workflow_call>'
        ),
    ), patch.object(
        chat.workflow_run_service,
        "create_authorized_run",
        new_callable=AsyncMock,
        side_effect=WorkflowGraphError("invalid_snapshot"),
    ), patch(
        "src.routers.chat.message_repository.create", new_callable=AsyncMock
    ) as create_message, patch(
        "src.routers.chat.event_bus.subscribe_channel",
        new_callable=AsyncMock,
        return_value=pubsub,
    ), patch(
        "src.routers.chat.session_manager.get_or_create", new_callable=AsyncMock
    ), patch(
        "src.routers.chat._signal_or_start_conversation_workflow", new_callable=AsyncMock, return_value="00000000-0000-0000-0000-000000000001"
    ), patch(
        "src.routers.chat.get_temporal_client", return_value=MagicMock()
    ):
        # No HTTPException — an auto-routed failure degrades to ordinary chat.
        await chat.chat_stream(
            request=ChatRequest(
                conversation_id=str(conv.id),
                messages=[{"role": "user", "content": "hello"}],
                model="gpt-test",
            ),
            user=user,
            session=session,
            quota=RoutingQuota(),
        )

    session.rollback.assert_awaited_once()
    create_message.assert_awaited_once()
