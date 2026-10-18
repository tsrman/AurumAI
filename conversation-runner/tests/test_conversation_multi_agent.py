import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.config import settings
from src.workflows.conversation import ConversationInput, ConversationWorkflow
from src.workflows.evaluator import EvaluationInput
from src.workflows.multi_agent import MultiAgentInput, MultiAgentResult


@workflow.defn(name="MultiAgentWorkflow")
class StubMultiAgentWorkflow:
    @workflow.run
    async def run(self, input: MultiAgentInput) -> MultiAgentResult:
        if input.run_id == "run-active":
            await workflow.execute_activity(
                "record_multi_agent_started",
                start_to_close_timeout=timedelta(seconds=10),
            )
            await workflow.wait_condition(lambda: False)
        if input.run_id == "run-failed":
            return MultiAgentResult(status="failed", error_code="access_revoked")
        if input.run_id == "run-web-race":
            return MultiAgentResult(
                status="completed",
                answer="cancelled sourced answer",
                model="provider/supervisor",
                source_ids=("11111111-1111-4111-8111-111111111111",),
            )
        return MultiAgentResult(
            status="completed",
            answer=f"answer:{input.run_id}",
            model="provider/supervisor",
            context_used=({"node_key": "research", "output": "evidence"},),
        )


@workflow.defn(name="EvaluationWorkflow")
class StubEvaluationWorkflow:
    @workflow.run
    async def run(self, input: EvaluationInput) -> None:
        await workflow.execute_activity(
            "record_evaluation",
            input,
            start_to_close_timeout=timedelta(seconds=10),
        )


async def _run_conversation(
    messages: list[dict],
    *,
    cancel_run_ids: tuple[str, ...] = (),
    active_cancel_run_id: str | None = None,
    evaluations: list[EvaluationInput] | None = None,
    cancel_after_completed_status: str | None = None,
    cancel_during_persist_turn: str | None = None,
    llm_requests: list[dict] | None = None,
    history_path: Path | None = None,
) -> tuple[str, list[tuple[str, dict]], list[tuple]]:
    events: list[tuple[str, dict]] = []
    persisted: list[tuple] = []
    multi_agent_started = asyncio.Event()
    completed_status_started = asyncio.Event()
    release_completed_status = asyncio.Event()
    persist_started = asyncio.Event()
    release_persist = asyncio.Event()

    @activity.defn(name="register_conversation_execution")
    async def register_conversation_execution(workflow_id: str, run_id: str) -> None:
        assert workflow_id and run_id

    @activity.defn(name="record_multi_agent_started")
    async def record_multi_agent_started() -> None:
        multi_agent_started.set()

    @activity.defn(name="record_evaluation")
    async def record_evaluation(input: EvaluationInput) -> None:
        if evaluations is not None:
            evaluations.append(input)

    @activity.defn(name="load_conversation_state")
    async def load_conversation_state() -> list[dict]:
        return []

    @activity.defn(name="list_available_skills")
    async def list_available_skills() -> list[dict]:
        return []

    @activity.defn(name="get_session_token")
    async def get_session_token() -> str | None:
        return "session-token"

    @activity.defn(name="get_conversation_temperature")
    async def get_conversation_temperature() -> float:
        return 0.7

    @activity.defn(name="get_evaluator_config")
    async def get_evaluator_config() -> dict:
        return {"enabled": settings.evaluator_enabled, "model": settings.evaluator_model}

    @activity.defn(name="llm_call")
    async def llm_call(
        request: dict,
        turn_id: str | None = None,
        publish_events: bool = True,
        hide_tool_call_tag: bool = False,
    ) -> str:
        if llm_requests is not None:
            llm_requests.append(request)
        return "ordinary answer"

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict) -> None:
        events.append((event_type, data))
        if (
            event_type == "workflow-run-status"
            and data.get("status") == "completed"
            and data.get("workflowRunId") == cancel_after_completed_status
        ):
            completed_status_started.set()
            await release_completed_status.wait()

    @activity.defn(name="persist_assistant_message")
    async def persist_assistant_message(
        content: str,
        model: str | None = None,
        turn_id: str | None = None,
        tool_calls: list | None = None,
        tool_results: dict | None = None,
    ) -> None:
        persisted.append((content, model, turn_id, tool_calls, tool_results))
        if turn_id is not None and turn_id == cancel_during_persist_turn:
            persist_started.set()
            await release_persist.wait()

    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = Worker(
            env.client,
            task_queue="conversation-multi-agent",
            workflows=[
                ConversationWorkflow,
                StubMultiAgentWorkflow,
                StubEvaluationWorkflow,
            ],
            activities=[
                load_conversation_state,
                list_available_skills,
                get_session_token,
                get_conversation_temperature,
                get_evaluator_config,
                register_conversation_execution,
                llm_call,
                publish_event,
                persist_assistant_message,
                record_multi_agent_started,
                record_evaluation,
            ],
        )
        async with worker:
            handle = await env.client.start_workflow(
                ConversationWorkflow.run,
                ConversationInput(conversation_id="conv-workflows"),
                id="conversation-multi-agent",
                task_queue="conversation-multi-agent",
            )
            for run_id in cancel_run_ids:
                await handle.signal(ConversationWorkflow.cancel_workflow_run, run_id)
            for message in messages:
                await handle.signal(ConversationWorkflow.user_message, message)
            if active_cancel_run_id is not None:
                await asyncio.wait_for(multi_agent_started.wait(), timeout=10)
                await handle.signal(ConversationWorkflow.cancel_workflow_run, active_cancel_run_id)
            if cancel_after_completed_status is not None:
                await asyncio.wait_for(completed_status_started.wait(), timeout=10)
                await handle.signal(
                    ConversationWorkflow.cancel_workflow_run,
                    cancel_after_completed_status,
                )
                release_completed_status.set()
            if cancel_during_persist_turn is not None:
                await asyncio.wait_for(persist_started.wait(), timeout=10)
                await handle.signal(
                    ConversationWorkflow.cancel_turn, cancel_during_persist_turn
                )
                release_persist.set()
            await env.sleep(timedelta(seconds=settings.idle_ttl_seconds + 10))
            result = await handle.result()
            if history_path is not None:
                history_path.write_text((await handle.fetch_history()).to_json())
    return result, events, persisted


@pytest.mark.asyncio
async def test_selected_turn_persists_only_final_supervisor_answer(monkeypatch) -> None:
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_enabled", False)
    result, events, persisted = await _run_conversation(
        [
            {
                "role": "user",
                "content": "research this",
                "turn_id": "turn-success",
                "workflow_run_id": "run-success",
            }
        ]
    )

    assert result == "answer:run-success"
    assert persisted == [
        (
            "answer:run-success",
            "provider/supervisor",
            "turn-success",
            None,
            None,
        )
    ]
    assert events == [
        (
            "workflow-run-status",
            {
                "turnId": "turn-success",
                "workflowRunId": "run-success",
                "status": "completed",
            },
        ),
        (
            "message-end",
            {
                "role": "assistant",
                "content": "answer:run-success",
                "turn_id": "turn-success",
            },
        ),
        ("done", {"turn_id": "turn-success", "evaluation_pending": False}),
    ]


@pytest.mark.asyncio
async def test_failed_workflow_leaks_no_child_content_and_next_normal_turn_runs(
    monkeypatch,
) -> None:
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_enabled", False)
    result, events, persisted = await _run_conversation(
        [
            {
                "role": "user",
                "content": "restricted",
                "turn_id": "turn-failed",
                "workflow_run_id": "run-failed",
            },
            {
                "role": "user",
                "content": "ordinary",
                "turn_id": "turn-normal",
                "model": "provider/ordinary",
            },
        ]
    )

    assert result == "ordinary answer"
    assert persisted == [("ordinary answer", "provider/ordinary", "turn-normal", None, None)]
    failed_events = [
        item
        for item in events
        if item[1].get("turnId") == "turn-failed" or item[1].get("turn_id") == "turn-failed"
    ]
    assert failed_events == [
        (
            "workflow-run-status",
            {
                "turnId": "turn-failed",
                "workflowRunId": "run-failed",
                "status": "failed",
                "errorCode": "access_revoked",
            },
        ),
        ("done", {"turn_id": "turn-failed", "evaluation_pending": False}),
    ]
    assert not any(
        event_type in {"content-delta", "agent-thought", "agent-tool-call", "tool-result"}
        for event_type, _ in failed_events
    )


@pytest.mark.asyncio
async def test_queued_exact_cancel_skips_only_matching_run(monkeypatch) -> None:
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_enabled", False)
    result, events, persisted = await _run_conversation(
        [
            {
                "role": "user",
                "content": "first",
                "turn_id": "turn-first",
                "workflow_run_id": "run-first",
            },
            {
                "role": "user",
                "content": "second",
                "turn_id": "turn-second",
                "workflow_run_id": "run-second",
            },
        ],
        cancel_run_ids=("run-first",),
    )

    assert result == "answer:run-second"
    assert persisted == [("answer:run-second", "provider/supervisor", "turn-second", None, None)]
    statuses = [item for item in events if item[0] == "workflow-run-status"]
    assert statuses == [
        (
            "workflow-run-status",
            {
                "turnId": "turn-first",
                "workflowRunId": "run-first",
                "status": "cancelled",
            },
        ),
        (
            "workflow-run-status",
            {
                "turnId": "turn-second",
                "workflowRunId": "run-second",
                "status": "completed",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_active_exact_cancel_returns_cancelled_without_persistence(monkeypatch) -> None:
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_enabled", False)
    result, events, persisted = await _run_conversation(
        [
            {
                "role": "user",
                "content": "cancel this",
                "turn_id": "turn-active",
                "workflow_run_id": "run-active",
            }
        ],
        active_cancel_run_id="run-active",
    )

    assert result == ""
    assert persisted == []
    assert events == [
        (
            "workflow-run-status",
            {
                "turnId": "turn-active",
                "workflowRunId": "run-active",
                "status": "cancelled",
            },
        ),
        ("done", {"turn_id": "turn-active", "evaluation_pending": False}),
    ]


@pytest.mark.asyncio
async def test_cancel_after_child_completion_never_persists_sources_or_pollutes_next_turn(
    monkeypatch,
) -> None:
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_enabled", False)
    llm_requests: list[dict] = []
    result, events, persisted = await _run_conversation(
        [
            {
                "role": "user",
                "content": "research",
                "turn_id": "turn-web-race",
                "workflow_run_id": "run-web-race",
                "web_tools_enabled": True,
            },
            {
                "role": "user",
                "content": "next",
                "turn_id": "turn-after-race",
                "model": "provider/ordinary",
            },
        ],
        cancel_after_completed_status="run-web-race",
        llm_requests=llm_requests,
    )

    assert result == "ordinary answer"
    assert persisted == [
        ("ordinary answer", "provider/ordinary", "turn-after-race", None, None)
    ]
    assert not any(
        "cancelled sourced answer" in str(message)
        for message in llm_requests[0]["messages"]
    )
    race_statuses = [
        data
        for event, data in events
        if event == "workflow-run-status" and data["workflowRunId"] == "run-web-race"
    ]
    assert [item["status"] for item in race_statuses] == ["completed", "cancelled"]


@pytest.mark.asyncio
async def test_successful_workflow_preserves_internal_evaluation_context(
    monkeypatch,
) -> None:
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_enabled", True)
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_model", "provider/test-evaluator")
    evaluations: list[EvaluationInput] = []

    _, events, _ = await _run_conversation(
        [
            {
                "role": "user",
                "content": "research this",
                "turn_id": "turn-eval",
                "workflow_run_id": "run-eval",
            }
        ],
        evaluations=evaluations,
    )

    assert len(evaluations) == 1
    assert evaluations[0].query == "research this"
    assert evaluations[0].agent_answer == "answer:run-eval"
    assert "research" in evaluations[0].context_used
    assert "evidence" in evaluations[0].context_used
    # That context is the raw node output, so the judge's justification must not
    # reach the user-visible message score for this turn.
    assert evaluations[0].reveal_reason is False
    assert events[-1] == (
        "done",
        {"turn_id": "turn-eval", "evaluation_pending": True},
    )


@pytest.mark.asyncio
async def test_enabled_evaluator_without_a_model_does_not_start_a_child(monkeypatch) -> None:
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_enabled", True)
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_model", "")
    evaluations: list[EvaluationInput] = []
    _, events, _ = await _run_conversation(
        [{"role": "user", "content": "research", "turn_id": "no-eval", "workflow_run_id": "run-eval"}],
        evaluations=evaluations,
    )
    assert evaluations == []
    assert events[-1] == ("done", {"turn_id": "no-eval", "evaluation_pending": False})


async def test_cancel_generation_records_the_run_before_the_child_handle_exists() -> None:
    """_run_multi_agent_turn sets _active_workflow_run_id before awaiting
    start_child_workflow and assigns the handle only after it returns.  A
    cancel delivered on that await must still be recorded, or the run finishes
    and persists an answer for a turn the user cancelled."""
    conversation = ConversationWorkflow()
    conversation._active_workflow_run_id = "run-1"
    conversation._active_workflow_handle = None

    await conversation.cancel_generation()

    assert conversation._cancelled is True
    assert "run-1" in conversation._cancelled_workflow_run_ids


async def test_exact_turn_cancel_ignores_a_delayed_signal_for_another_turn() -> None:
    conversation = ConversationWorkflow()
    conversation._active_turn_id = "new-turn"
    conversation._active_workflow_run_id = None

    await conversation.cancel_turn("old-turn")
    assert conversation._cancelled is False

    await conversation.cancel_turn("new-turn")
    assert conversation._cancelled is True


@pytest.mark.asyncio
async def test_cancel_during_persist_still_closes_the_stream(monkeypatch) -> None:
    """A cancel landing inside the persist activity must still publish `done`.

    /v1/chat/stream terminates only on `done`, and no cancel signal handler
    publishes one, so returning straight after persist left the browser
    spinning until EVENT_SUBSCRIBE_TIMEOUT (120s).
    """
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_enabled", False)
    _, events, persisted = await _run_conversation(
        [
            {
                "role": "user",
                "content": "research",
                "turn_id": "turn-persist-cancel",
                "workflow_run_id": "run-persist-cancel",
            }
        ],
        cancel_during_persist_turn="turn-persist-cancel",
    )

    assert [item[2] for item in persisted] == ["turn-persist-cancel"]
    # The cancelled turn never streams its answer...
    assert not any(event == "message-end" for event, _ in events)
    # ...but the stream is still closed exactly once.
    done = [data for event, data in events if event == "done"]
    assert done == [{"turn_id": "turn-persist-cancel", "evaluation_pending": False}]
