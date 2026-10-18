import asyncio
import threading
from datetime import timedelta

import pytest
from pydantic import ValidationError
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.config import Settings, settings
from src.workflows.conversation import ConversationInput, ConversationWorkflow


@activity.defn(name="load_conversation_state")
async def mock_load_conversation_state() -> list[dict]:
    return []


@activity.defn(name="list_available_skills")
async def mock_list_available_skills() -> list[dict]:
    return []


@activity.defn(name="get_session_token")
async def mock_get_session_token() -> str | None:
    return None


@activity.defn(name="get_conversation_temperature")
async def mock_get_conversation_temperature() -> float:
    return 0.7


@activity.defn(name="llm_call")
async def mock_llm_call(
    request: dict,
    turn_id: str | None = None,
    publish_events: bool = True,
    hide_tool_call_tag: bool = False,
) -> str:
    return ""


@activity.defn(name="publish_event")
async def mock_publish_event(event_type: str, data: dict) -> None:
    return None


@activity.defn(name="persist_assistant_message")
async def mock_persist_assistant_message(
    content: str,
    model: str | None = None,
    turn_id: str | None = None,
    tool_calls: list | None = None,
    tool_results: dict | None = None,
) -> None:
    return None


@pytest.mark.asyncio
async def test_workflow_exits_on_idle_timeout():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-tq",
            workflows=[ConversationWorkflow],
            activities=[
                mock_load_conversation_state,
                mock_list_available_skills,
                mock_get_session_token,
                mock_get_conversation_temperature,
                mock_llm_call,
                mock_publish_event,
                mock_persist_assistant_message,
            ],
        ):
            handle = await env.client.start_workflow(
                ConversationWorkflow.run,
                ConversationInput(conversation_id="conv-idle"),
                id="conversation-conv-idle",
                task_queue="test-tq",
            )
            await env.sleep(timedelta(seconds=settings.idle_ttl_seconds + 10))
            assert await handle.result() == ""


@pytest.mark.asyncio
async def test_turn_request_carries_the_resolved_conversation_temperature(monkeypatch):
    # A completed ordinary turn otherwise starts a real EvaluationWorkflow
    # child, which isn't registered on this single-workflow Worker — same
    # workaround as test_cancel_generation_only_aborts_current_turn_not_the_whole_workflow.
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_enabled", False)
    # get_conversation_temperature is re-fetched every turn (not cached at
    # workflow start) specifically so a conversation-level override set
    # mid-conversation applies to the very next turn — this pins that the
    # activity's result actually reaches the LLM request, not just that the
    # activity gets called.
    captured_requests: list[dict] = []

    @activity.defn(name="get_conversation_temperature")
    async def recording_temperature() -> float:
        return 0.42

    @activity.defn(name="llm_call")
    async def recording_llm_call(
        request: dict,
        turn_id: str | None = None,
        publish_events: bool = True,
        hide_tool_call_tag: bool = False,
    ) -> str:
        captured_requests.append(request)
        return "response"

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-turn-temperature",
            workflows=[ConversationWorkflow],
            activities=[
                mock_load_conversation_state,
                mock_list_available_skills,
                mock_get_session_token,
                recording_temperature,
                recording_llm_call,
                mock_publish_event,
                mock_persist_assistant_message,
            ],
        ):
            handle = await env.client.start_workflow(
                ConversationWorkflow.run,
                ConversationInput(conversation_id="conv-temperature"),
                id="conversation-conv-temperature",
                task_queue="test-turn-temperature",
            )
            await handle.signal(
                ConversationWorkflow.user_message,
                {
                    "role": "user",
                    "content": "hello",
                    "turn_id": "turn-temperature",
                    "model": "gpt-4o-mini",
                },
            )
            await env.sleep(timedelta(seconds=settings.idle_ttl_seconds + 10))
            await handle.result()

    assert len(captured_requests) == 1
    assert captured_requests[0]["temperature"] == 0.42


@pytest.mark.parametrize("invalid_ttl", [-1, -100])
def test_invalid_idle_ttl_seconds_raises(invalid_ttl):
    with pytest.raises(ValidationError):
        Settings(
            conversation_id="conv",
            user_id="user",
            session_token="token",
            idle_ttl_seconds=invalid_ttl,
        )


def test_idle_ttl_zero_is_valid():
    s = Settings(
        conversation_id="conv",
        user_id="user",
        session_token="token",
        idle_ttl_seconds=0,
    )
    assert s.idle_ttl_seconds == 0


@pytest.mark.asyncio
async def test_cancel_queued_workflow_run_does_not_cancel_active_ordinary_turn(
    monkeypatch,
):
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_enabled", False)
    llm_started = threading.Event()
    llm_release = threading.Event()
    persisted: list[str] = []

    @activity.defn(name="llm_call")
    async def slow_llm_call(
        request: dict,
        turn_id: str | None = None,
        publish_events: bool = True,
        hide_tool_call_tag: bool = False,
    ) -> str:
        llm_started.set()
        await asyncio.to_thread(llm_release.wait)
        return "ordinary-response"

    @activity.defn(name="persist_assistant_message")
    async def recording_persist(
        content: str,
        model: str | None = None,
        turn_id: str | None = None,
        tool_calls: list | None = None,
        tool_results: dict | None = None,
    ) -> None:
        persisted.append(content)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-exact-run-cancel",
            workflows=[ConversationWorkflow],
            activities=[
                mock_load_conversation_state,
                mock_list_available_skills,
                mock_get_session_token,
                mock_get_conversation_temperature,
                slow_llm_call,
                mock_publish_event,
                recording_persist,
            ],
        ):
            handle = await env.client.start_workflow(
                ConversationWorkflow.run,
                ConversationInput(conversation_id="conv-exact-run-cancel"),
                id="conversation-conv-exact-run-cancel",
                task_queue="test-exact-run-cancel",
            )
            await handle.signal(
                ConversationWorkflow.user_message,
                {
                    "role": "user",
                    "content": "ordinary turn",
                    "turn_id": "ordinary-turn",
                    "model": "gpt-4o-mini",
                },
            )
            await asyncio.wait_for(asyncio.to_thread(llm_started.wait), timeout=10)
            await handle.signal(ConversationWorkflow.cancel_workflow_run, "queued-run")
            llm_release.set()
            await env.sleep(timedelta(seconds=settings.idle_ttl_seconds + 10))
            result = await handle.result()

    assert persisted == ["ordinary-response"]
    assert result == "ordinary-response"


@pytest.mark.asyncio
async def test_cancel_generation_only_aborts_current_turn_not_the_whole_workflow(monkeypatch):
    # The second turn runs to completion, which would otherwise try to start
    # a real EvaluationWorkflow child — not registered on this test's
    # single-workflow Worker, matching the pattern used elsewhere in
    # test_conversation_tool_loop.py.
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_enabled", False)
    """Regression test: cancel_generation used to
    set a flag that also gated the workflow's own outer while loop, so any
    cancel — even mid-turn — permanently ended the workflow, and a message
    already queued (or arriving right after) was silently dropped with no
    persist/no SSE event ever emitted for it (backend had already saved it
    and opened an SSE stream that then hung until timeout). Cancel must
    only abort the in-flight turn; the workflow keeps waiting for more
    messages and still processes the next one normally."""
    calls: list[str] = []
    llm_started = threading.Event()
    llm_release = threading.Event()
    call_count = 0

    @activity.defn(name="llm_call")
    async def slow_then_fast_llm_call(
        request: dict,
        turn_id: str | None = None,
        publish_events: bool = True,
        hide_tool_call_tag: bool = False,
    ) -> str:
        nonlocal call_count
        call_count += 1
        calls.append("llm_call")
        if call_count == 1:
            llm_started.set()
            await asyncio.to_thread(llm_release.wait)
            return "should-not-be-persisted"
        return "second-turn-response"

    @activity.defn(name="publish_event")
    async def recording_publish_event(event_type: str, data: dict) -> None:
        calls.append(f"publish_event:{event_type}")

    @activity.defn(name="persist_assistant_message")
    async def recording_persist_assistant_message(
        content: str,
        model: str | None = None,
        turn_id: str | None = None,
        tool_calls: list | None = None,
        tool_results: dict | None = None,
    ) -> None:
        calls.append(f"persist_assistant_message:{content}")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-tq",
            workflows=[ConversationWorkflow],
            activities=[
                mock_load_conversation_state,
                mock_list_available_skills,
                mock_get_session_token,
                mock_get_conversation_temperature,
                slow_then_fast_llm_call,
                recording_publish_event,
                recording_persist_assistant_message,
            ],
        ):
            handle = await env.client.start_workflow(
                ConversationWorkflow.run,
                ConversationInput(conversation_id="conv-cancel"),
                id="conversation-conv-cancel",
                task_queue="test-tq",
            )
            await handle.signal(
                ConversationWorkflow.user_message,
                {
                    "role": "user",
                    "content": "hello",
                    "turn_id": "t1",
                    "model": "gpt-4o-mini",
                },
            )
            await asyncio.wait_for(asyncio.to_thread(llm_started.wait), timeout=10)
            await handle.signal(ConversationWorkflow.cancel_generation)
            llm_release.set()

            # Queued right after cancel — this is exactly the message that
            # used to get dropped: the workflow used to see _cancelled and
            # exit its outer loop before ever popping this one.
            await handle.signal(
                ConversationWorkflow.user_message,
                {
                    "role": "user",
                    "content": "still there?",
                    "turn_id": "t2",
                    "model": "gpt-4o-mini",
                },
            )
            await env.sleep(timedelta(seconds=settings.idle_ttl_seconds + 10))
            result = await handle.result()

    assert "llm_call" in calls
    assert "publish_event:message-start" in calls
    # First turn aborted by cancel — never persisted, never emitted done.
    assert "persist_assistant_message:should-not-be-persisted" not in calls
    # Second turn, queued right after cancel, still ran to completion —
    # the workflow was still alive to process it.
    assert "persist_assistant_message:second-turn-response" in calls
    assert result == "second-turn-response"


@pytest.mark.asyncio
async def test_failed_turn_still_emits_done_and_keeps_the_workflow_alive(monkeypatch):
    """Regression test: a turn that raised took the whole workflow down and
    never published "done".

    The backend's SSE generator (routers/chat.py) only stops on "done", so
    the browser held an open connection showing a spinner until
    EVENT_SUBSCRIBE_TIMEOUT (120s) — with no indication of what went wrong.
    Concretely: selecting an embedding model made the gateway answer 404,
    llm_call raised, and the user got a permanent spinner.

    Two things are asserted because either alone is insufficient: the
    failure has to reach the user (message-end carrying the reason, then
    done to close the stream), and the workflow has to survive it — it is
    long-lived, so dying on one bad turn would break every later message in
    the conversation too.
    """
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_enabled", False)
    calls: list[str] = []
    published: list[tuple[str, dict]] = []
    failed_turns: list[str] = []
    call_count = 0

    @activity.defn(name="llm_call")
    async def failing_then_working_llm_call(
        request: dict,
        turn_id: str | None = None,
        publish_events: bool = True,
        hide_tool_call_tag: bool = False,
    ) -> str:
        nonlocal call_count
        call_count += 1
        if turn_id == "t1":
            raise RuntimeError("LLM gateway error: model_not_found")
        return "second-turn-response"

    @activity.defn(name="publish_event")
    async def recording_publish_event(event_type: str, data: dict) -> None:
        calls.append(f"publish_event:{event_type}")
        published.append((event_type, data))

    @activity.defn(name="persist_assistant_message")
    async def recording_persist_assistant_message(
        content: str,
        model: str | None = None,
        turn_id: str | None = None,
        tool_calls: list | None = None,
        tool_results: dict | None = None,
    ) -> None:
        calls.append(f"persist_assistant_message:{content}")

    @activity.defn(name="fail_interaction_turn")
    async def recording_fail_interaction_turn(turn_id: str) -> None:
        failed_turns.append(turn_id)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-tq",
            workflows=[ConversationWorkflow],
            activities=[
                mock_load_conversation_state,
                mock_list_available_skills,
                mock_get_session_token,
                mock_get_conversation_temperature,
                failing_then_working_llm_call,
                recording_publish_event,
                recording_persist_assistant_message,
                recording_fail_interaction_turn,
            ],
        ):
            handle = await env.client.start_workflow(
                ConversationWorkflow.run,
                ConversationInput(conversation_id="conv-fail"),
                id="conversation-conv-fail",
                task_queue="test-tq",
            )
            await handle.signal(
                ConversationWorkflow.user_message,
                {
                    "role": "user",
                    "content": "hello",
                    "turn_id": "t1",
                    "model": "openai/text-embedding-3-small",
                },
            )
            await handle.signal(
                ConversationWorkflow.user_message,
                {
                    "role": "user",
                    "content": "still there?",
                    "turn_id": "t2",
                    "model": "gpt-4o-mini",
                },
            )
            await env.sleep(timedelta(seconds=settings.idle_ttl_seconds + 10))
            result = await handle.result()

    failed_turn = [(e, d) for e, d in published if d.get("turn_id") == "t1"]
    assert ("done", {"turn_id": "t1", "evaluation_pending": False}) in failed_turn
    # The reason has to travel with it — a blank message-end would close the
    # stream but still leave the user guessing.
    message_ends = [d for e, d in failed_turn if e == "message-end"]
    assert message_ends and "model_not_found" in message_ends[0]["content"]
    # Nothing was persisted for the failed turn: an error notice belongs on
    # screen, not in the stored conversation history.
    assert not any(c.startswith("persist_assistant_message") and "model_not_found" in c for c in calls)
    assert failed_turns == ["t1"]
    # The workflow outlived the failure and handled the next message.
    assert result == "second-turn-response"
