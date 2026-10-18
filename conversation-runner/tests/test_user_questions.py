import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.workflows.agent import AgentExecutionSnapshot, AgentInput, AgentWorkflow
from src.workflows.builtin_tools import ExecutionContext
from src.workflows.conversation import ConversationInput, ConversationWorkflow
from src.workflows.multi_agent import MultiAgentInput, MultiAgentWorkflow
from src.workflows.multi_agent import _stable_error_code
from src.workflows.user_questions import QuestionWaitState, wait_for_answer


def test_duplicate_notification_is_not_second_answer() -> None:
    state = QuestionWaitState()
    state.notify("q", 2)
    state.notify("q", 1)
    state.notify("q", 2)
    state.notify("previous-turn-question", 99)
    assert state.version("q") == 2
    assert state.version("unknown") == 0


@pytest.mark.parametrize(
    "code", ["user_input_timeout", "user_input_cancelled", "user_input_limit"]
)
def test_question_failures_keep_stable_multi_agent_edge_codes(code: str) -> None:
    assert _stable_error_code(
        ApplicationError(code, type=code, non_retryable=True)
    ) == code


@workflow.defn(name="QuestionWaitHarness")
class QuestionWaitHarness:
    def __init__(self) -> None:
        self.state = QuestionWaitState()
        self.cancelled = False

    @workflow.run
    async def run(self, question_id: str, deadline: str) -> dict[str, Any]:
        try:
            return await wait_for_answer(
                self.state,
                question_id,
                datetime.fromisoformat(deadline),
                lambda: self.cancelled,
            )
        except ApplicationError as exc:
            return {"error": exc.type}

    @workflow.signal
    def cancel(self) -> None:
        self.cancelled = True

    @workflow.signal(name="question_available")
    def question_available(self, question_id: str, version: int) -> None:
        self.state.notify(question_id, version)


@pytest.mark.asyncio
async def test_cancel_before_consume_never_reads_the_answer() -> None:
    read_started = asyncio.Event()
    consume_count = 0

    @activity.defn(name="read_user_question")
    async def read_user_question(question_id: str) -> dict[str, Any]:
        read_started.set()
        return {"questionId": question_id, "status": "pending", "version": 1}

    @activity.defn(name="consume_user_question")
    async def consume_user_question(question_id: str) -> dict[str, Any]:
        nonlocal consume_count
        consume_count += 1
        return {"text": "must not be consumed", "optionId": None}

    @activity.defn(name="expire_user_question")
    async def expire_user_question(question_id: str) -> dict[str, Any]:
        return {"questionId": question_id, "status": "expired", "version": 2}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="question-cancel-queue",
            workflows=[QuestionWaitHarness],
            activities=[
                read_user_question,
                consume_user_question,
                expire_user_question,
            ],
        ):
            handle = await env.client.start_workflow(
                QuestionWaitHarness.run,
                args=[
                    "question-current",
                    (datetime.now(UTC) + timedelta(days=30)).isoformat(),
                ],
                id="question-cancel-test",
                task_queue="question-cancel-queue",
            )
            await asyncio.wait_for(read_started.wait(), timeout=5)
            await handle.signal(QuestionWaitHarness.cancel)
            result = await handle.result()

    assert result == {"error": "user_input_cancelled"}
    assert consume_count == 0


@dataclass
class QuestionBackend:
    created: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    answered: set[str] = field(default_factory=set)
    invocation_ids: list[str] = field(default_factory=list)
    register_calls: list[tuple[Any, ...]] = field(default_factory=list)
    consume_count: int = 0
    deadline_seconds: int = 30 * 24 * 60 * 60
    deadline: str | None = None
    reject_create_with_code: str | None = None


def _question_activities(backend: QuestionBackend) -> list[Any]:
    @activity.defn(name="register_interaction_execution")
    async def register_interaction_execution(*args: Any) -> ExecutionContext:
        backend.register_calls.append(args)
        return ExecutionContext(
            turn_id=str(args[0]),
            execution_id="execution-1",
            phase=str(args[4]),  # type: ignore[arg-type]
            node_key=args[5],
        )

    @activity.defn(name="create_user_question")
    async def create_user_question(
        context: ExecutionContext, invocation_id: str, question: dict[str, Any]
    ) -> dict[str, Any]:
        assert context.execution_id == "execution-1"
        assert question["text"].startswith("Which")
        backend.invocation_ids.append(invocation_id)
        if backend.reject_create_with_code is not None:
            raise ApplicationError(
                backend.reject_create_with_code,
                type=backend.reject_create_with_code,
                non_retryable=True,
            )
        question_id = f"question-{len(backend.invocation_ids)}"
        await backend.created.put(question_id)
        if backend.deadline is None:
            backend.deadline = (
                datetime.now(UTC) + timedelta(seconds=backend.deadline_seconds)
            ).isoformat()
        return {
            "questionId": question_id,
            "status": "pending",
            "version": 1,
            "expiresAt": backend.deadline,
        }

    @activity.defn(name="read_user_question")
    async def read_user_question(question_id: str) -> dict[str, Any]:
        return {
            "questionId": question_id,
            "status": "answered" if question_id in backend.answered else "pending",
            "version": 2 if question_id in backend.answered else 1,
            "expiresAt": backend.deadline,
        }

    @activity.defn(name="consume_user_question")
    async def consume_user_question(question_id: str) -> dict[str, Any]:
        backend.consume_count += 1
        return {"text": f"answer for {question_id}", "optionId": None}

    @activity.defn(name="expire_user_question")
    async def expire_user_question(question_id: str) -> dict[str, Any]:
        return {
            "questionId": question_id,
            "status": "expired",
            "version": 2,
            "expiresAt": backend.deadline,
        }

    return [
        register_interaction_execution,
        create_user_question,
        read_user_question,
        consume_user_question,
        expire_user_question,
    ]


@pytest.mark.asyncio
async def test_agent_ask_answer_then_final_uses_exact_runtime_and_empty_skills() -> None:
    backend = QuestionBackend()
    llm_calls: list[dict[str, Any]] = []

    @activity.defn(name="llm_call")
    async def llm_call(
        request: dict[str, Any], turn_id: str | None = None, publish_events: bool = True
    ) -> str:
        llm_calls.append(request)
        if len(llm_calls) == 1:
            return (
                '<tool_call>{"tool":"ask_user","call_id":"forged",'
                '"args":{"text":"Which city?"}}</tool_call>'
            )
        if len(llm_calls) == 2:
            return '<tool_call>{"tool":"ask_user","args":{"text":"Which district?"}}</tool_call>'
        return "Final answer using both answers."

    @activity.defn(name="list_available_skills")
    async def list_available_skills() -> list[dict[str, Any]]:
        return []

    @activity.defn(name="get_conversation_id")
    async def get_conversation_id() -> str:
        return "conversation-1"

    @activity.defn(name="get_session_token")
    async def get_session_token() -> str:
        return "session-token"

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict[str, Any]) -> None:
        return None

    snapshot = AgentExecutionSnapshot(
        agent_definition_id="00000000-0000-0000-0000-000000000011",
        name="Researcher",
        model="provider/model",
        system_prompt="Research carefully.",
        temperature=0.2,
    )
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="question-test",
            workflows=[AgentWorkflow],
            activities=[
                llm_call,
                list_available_skills,
                get_conversation_id,
                get_session_token,
                publish_event,
                *_question_activities(backend),
            ],
        ):
            handle = await env.client.start_workflow(
                AgentWorkflow.run,
                AgentInput(
                    agent=snapshot,
                    goal="Find a place",
                    event_visibility="internal",
                    turn_id="turn-1",
                    workflow_phase="node",
                    workflow_node_key="research",
                    user_questions_enabled=True,
                ),
                id="agent-question-test",
                task_queue="question-test",
            )
            first_question = await asyncio.wait_for(backend.created.get(), timeout=5)
            backend.answered.add(first_question)
            await handle.signal("question_available", args=[first_question, 2])
            second_question = await asyncio.wait_for(backend.created.get(), timeout=5)
            backend.answered.add(second_question)
            await handle.signal("question_available", args=[second_question, 2])
            result = await handle.result()

    assert result == "Final answer using both answers."
    assert backend.consume_count == 2
    assert len(backend.invocation_ids) == 2
    assert backend.invocation_ids[0] != backend.invocation_ids[1]
    assert backend.invocation_ids[0] != "forged"
    assert backend.register_calls[0][0] == "turn-1"
    assert backend.register_calls[0][1] == "agent-question-test"
    assert backend.register_calls[0][2]
    assert backend.register_calls[0][3] == "question-test"
    assert backend.register_calls[0][4:] == (
        "node",
        "research",
        "00000000-0000-0000-0000-000000000011",
        1,
    )
    assert '"tool":"ask_user"' in llm_calls[0]["messages"][0]["content"]
    assert "answer for question-1" in llm_calls[1]["messages"][-1]["content"]
    assert "answer for question-2" in llm_calls[2]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_agent_reloads_rotated_session_token_after_question_wait() -> None:
    backend = QuestionBackend()
    llm_calls = 0
    token_reads = 0
    skill_tokens: list[str | None] = []

    @activity.defn(name="llm_call")
    async def llm_call(
        request: dict[str, Any], turn_id: str | None = None, publish_events: bool = True
    ) -> str:
        nonlocal llm_calls
        llm_calls += 1
        if llm_calls == 1:
            return '<tool_call>{"tool":"ask_user","args":{"text":"Which city?"}}</tool_call>'
        if llm_calls == 2:
            return '<tool_call>{"skill_id":"search","args":{"q":"city"}}</tool_call>'
        return "Final answer with refreshed credentials."

    @activity.defn(name="list_available_skills")
    async def list_available_skills() -> list[dict[str, Any]]:
        return [
            {
                "id": "search",
                "name": "Search",
                "description": "Search evidence",
                "inputSchema": {"type": "object"},
            }
        ]

    @activity.defn(name="get_conversation_id")
    async def get_conversation_id() -> str:
        return "conversation-refresh"

    @activity.defn(name="get_session_token")
    async def get_session_token() -> str:
        nonlocal token_reads
        token_reads += 1
        return "old-token" if token_reads == 1 else "fresh-token"

    @activity.defn(name="get_conversation_temperature")
    async def get_conversation_temperature() -> float:
        return 0.7

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict[str, Any]) -> None:
        return None

    @activity.defn(name="execute_skill")
    async def execute_skill(
        skill_id: str,
        args: dict[str, Any],
        conversation_id: str,
        session_token: str | None = None,
    ) -> dict[str, bool]:
        skill_tokens.append(session_token)
        return {"ok": True}

    snapshot = AgentExecutionSnapshot(
        agent_definition_id="00000000-0000-0000-0000-000000000012",
        name="Researcher",
        model="provider/model",
        system_prompt="Research carefully.",
        temperature=0.2,
        allowed_skill_ids=("search",),
    )
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with (
            Worker(
                env.client,
                task_queue="question-token-refresh",
                workflows=[AgentWorkflow],
                activities=[
                    llm_call,
                    list_available_skills,
                    get_conversation_id,
                    get_session_token,
                    publish_event,
                    *_question_activities(backend),
                ],
            ),
            Worker(env.client, task_queue="tools", activities=[execute_skill]),
        ):
            handle = await env.client.start_workflow(
                AgentWorkflow.run,
                AgentInput(
                    agent=snapshot,
                    goal="Research a city",
                    event_visibility="internal",
                    turn_id="turn-refresh",
                    workflow_phase="node",
                    workflow_node_key="research",
                    user_questions_enabled=True,
                ),
                id="agent-question-token-refresh",
                task_queue="question-token-refresh",
            )
            question_id = await asyncio.wait_for(backend.created.get(), timeout=5)
            backend.answered.add(question_id)
            await handle.signal("question_available", args=[question_id, 2])
            result = await handle.result()

    assert result == "Final answer with refreshed credentials."
    assert token_reads == 2
    assert skill_tokens == ["fresh-token"]


@pytest.mark.asyncio
async def test_ordinary_ask_answer_then_final_persists_no_private_tool_payload(
    monkeypatch,
) -> None:
    backend = QuestionBackend()
    llm_calls: list[dict[str, Any]] = []
    persisted: list[tuple[Any, ...]] = []
    token_reads = 0
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_enabled", False)
    monkeypatch.setattr("src.workflows.conversation.settings.idle_ttl_seconds", 1)

    @activity.defn(name="load_conversation_state")
    async def load_conversation_state() -> list[dict[str, Any]]:
        return []

    @activity.defn(name="llm_call")
    async def llm_call(
        request: dict[str, Any],
        turn_id: str | None = None,
        publish_events: bool = True,
        hide_tool_call_tag: bool = False,
    ) -> str:
        llm_calls.append(request)
        if len(llm_calls) == 1:
            return '<tool_call>{"tool":"ask_user","args":{"text":"Which city?"}}</tool_call>'
        if len(llm_calls) == 2:
            return '<tool_call>{"tool":"ask_user","args":{"text":"Which district?"}}</tool_call>'
        return "Ordinary final answer."

    @activity.defn(name="list_available_skills")
    async def list_available_skills() -> list[dict[str, Any]]:
        return []

    @activity.defn(name="get_session_token")
    async def get_session_token() -> str:
        nonlocal token_reads
        token_reads += 1
        return "old-token" if token_reads == 1 else "fresh-token"

    @activity.defn(name="get_conversation_temperature")
    async def get_conversation_temperature() -> float:
        return 0.7

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict[str, Any]) -> None:
        return None

    @activity.defn(name="persist_assistant_message")
    async def persist_assistant_message(*args: Any) -> None:
        persisted.append(args)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="ordinary-question-queue",
            workflows=[ConversationWorkflow],
            activities=[
                load_conversation_state,
                llm_call,
                list_available_skills,
                get_session_token,
                get_conversation_temperature,
                publish_event,
                persist_assistant_message,
                *_question_activities(backend),
            ],
        ):
            handle = await env.client.start_workflow(
                ConversationWorkflow.run,
                ConversationInput(conversation_id="conversation-ordinary"),
                id="ordinary-question-test",
                task_queue="ordinary-question-queue",
            )
            await handle.signal(
                ConversationWorkflow.user_message,
                {
                    "role": "user",
                    "content": "Help",
                    "turn_id": "turn-ordinary",
                    "model": "provider/model",
                    "user_questions_enabled": True,
                },
            )
            first_question = await asyncio.wait_for(backend.created.get(), timeout=5)
            backend.answered.add(first_question)
            await handle.signal("question_available", args=[first_question, 2])
            second_question = await asyncio.wait_for(backend.created.get(), timeout=5)
            backend.answered.add(second_question)
            await handle.signal("question_available", args=[second_question, 2])
            result = await handle.result()

    assert result == "Ordinary final answer."
    assert backend.consume_count == 2
    assert token_reads == 3
    assert backend.register_calls[0][0] == "turn-ordinary"
    assert backend.register_calls[0][1] == "ordinary-question-test"
    assert backend.register_calls[0][3:] == (
        "ordinary-question-queue",
        "ordinary",
        None,
        None,
        1,
    )
    assert persisted == [
        (
            "Ordinary final answer.",
            "provider/model",
            "turn-ordinary",
            None,
            None,
        )
    ]
    assert "answer for question-1" in llm_calls[1]["messages"][-1]["content"]
    assert "answer for question-2" in llm_calls[2]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_ordinary_question_timeout_publishes_no_false_assistant(
    monkeypatch,
) -> None:
    backend = QuestionBackend(deadline_seconds=1)
    events: list[tuple[str, dict[str, Any]]] = []
    persisted: list[tuple[Any, ...]] = []
    failed_turns: list[str] = []
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_enabled", False)
    monkeypatch.setattr("src.workflows.conversation.settings.idle_ttl_seconds", 1)

    @activity.defn(name="load_conversation_state")
    async def load_conversation_state() -> list[dict[str, Any]]:
        return []

    @activity.defn(name="llm_call")
    async def llm_call(
        request: dict[str, Any],
        turn_id: str | None = None,
        publish_events: bool = True,
        hide_tool_call_tag: bool = False,
    ) -> str:
        return '<tool_call>{"tool":"ask_user","args":{"text":"Which city?"}}</tool_call>'

    @activity.defn(name="list_available_skills")
    async def list_available_skills() -> list[dict[str, Any]]:
        return []

    @activity.defn(name="get_session_token")
    async def get_session_token() -> str:
        return "session-token"

    @activity.defn(name="get_conversation_temperature")
    async def get_conversation_temperature() -> float:
        return 0.7

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict[str, Any]) -> None:
        events.append((event_type, data))

    @activity.defn(name="persist_assistant_message")
    async def persist_assistant_message(*args: Any) -> None:
        persisted.append(args)

    @activity.defn(name="fail_interaction_turn")
    async def fail_interaction_turn(turn_id: str) -> None:
        failed_turns.append(turn_id)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="ordinary-timeout-queue",
            workflows=[ConversationWorkflow],
            activities=[
                load_conversation_state,
                llm_call,
                list_available_skills,
                get_session_token,
                get_conversation_temperature,
                publish_event,
                persist_assistant_message,
                fail_interaction_turn,
                *_question_activities(backend),
            ],
        ):
            handle = await env.client.start_workflow(
                ConversationWorkflow.run,
                ConversationInput(conversation_id="conversation-timeout"),
                id="ordinary-timeout-test",
                task_queue="ordinary-timeout-queue",
            )
            await handle.signal(
                ConversationWorkflow.user_message,
                {
                    "role": "user",
                    "content": "Help",
                    "turn_id": "turn-timeout",
                    "model": "provider/model",
                    "user_questions_enabled": True,
                },
            )
            result = await handle.result()

    assert result == ""
    assert backend.consume_count == 0
    assert persisted == []
    assert failed_turns == ["turn-timeout"]
    assert [event for event, _ in events if event == "message-end"] == []
    assert ("done", {"turn_id": "turn-timeout", "evaluation_pending": False}) in events


@pytest.mark.asyncio
async def test_multi_agent_sibling_completes_while_question_node_waits() -> None:
    backend = QuestionBackend()
    beta_completed = asyncio.Event()
    run_id = "11111111-1111-1111-1111-111111111111"
    turn_id = "22222222-2222-2222-2222-222222222222"

    def agent(agent_id: str, name: str) -> dict[str, Any]:
        return {
            "agentDefinitionId": agent_id,
            "name": name,
            "model": "provider/model",
            "systemPrompt": f"You are {name}.",
            "temperature": 0.2,
            "allowedSkillIds": [],
            "knowledgeBases": [],
        }

    snapshot = {
        "runId": run_id,
        "turnId": turn_id,
        "userRequest": "Compare two places.",
        "maxParallelism": 2,
        "supervisor": agent("33333333-3333-3333-3333-333333333333", "Supervisor"),
        "nodes": [
            {
                "nodeKey": "alpha",
                "displayName": "Alpha",
                "instruction": "Ask for a city.",
                "agent": agent("44444444-4444-4444-4444-444444444444", "Alpha"),
            },
            {
                "nodeKey": "beta",
                "displayName": "Beta",
                "instruction": "Work independently.",
                "agent": agent("55555555-5555-5555-5555-555555555555", "Beta"),
            },
        ],
        "edges": [],
    }

    @activity.defn(name="load_workflow_run_snapshot")
    async def load_workflow_run_snapshot(requested_run_id: str) -> dict[str, Any]:
        assert requested_run_id == run_id
        return snapshot

    @activity.defn(name="authorize_workflow_step")
    async def authorize_workflow_step(
        requested_run_id: str, phase: str, node_key: str | None
    ) -> None:
        return None

    @activity.defn(name="transition_workflow_run")
    async def transition_workflow_run(
        requested_run_id: str, status: str, error_code: str | None
    ) -> None:
        return None

    @activity.defn(name="persist_workflow_node_result")
    async def persist_workflow_node_result(
        requested_run_id: str,
        phase_key: str,
        status: str,
        output: str | None,
        error_code: str | None,
    ) -> None:
        if phase_key == "beta" and status == "completed":
            beta_completed.set()

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict[str, Any]) -> None:
        return None

    @activity.defn(name="list_available_skills")
    async def list_available_skills() -> list[dict[str, Any]]:
        return []

    @activity.defn(name="get_conversation_id")
    async def get_conversation_id() -> str:
        return "conversation-multi"

    @activity.defn(name="get_session_token")
    async def get_session_token() -> str:
        return "session-token"

    @activity.defn(name="llm_call")
    async def llm_call(
        request: dict[str, Any], turn_id: str | None = None, publish_events: bool = True
    ) -> str:
        phase = request.get("workflow_phase")
        node_key = request.get("workflow_node_key")
        if phase == "supervisor_prepare":
            return '{"alpha":"alpha brief","beta":"beta brief"}'
        if phase == "node" and node_key == "alpha":
            if any("answer for question-1" in item.get("content", "") for item in request["messages"]):
                return "alpha done"
            return '<tool_call>{"tool":"ask_user","args":{"text":"Which city?"}}</tool_call>'
        if phase == "node" and node_key == "beta":
            return "beta done"
        return "combined final"

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="multi-question-queue",
            workflows=[MultiAgentWorkflow, AgentWorkflow],
            activities=[
                load_workflow_run_snapshot,
                authorize_workflow_step,
                transition_workflow_run,
                persist_workflow_node_result,
                publish_event,
                list_available_skills,
                get_conversation_id,
                get_session_token,
                llm_call,
                *_question_activities(backend),
            ],
        ):
            handle = await env.client.start_workflow(
                MultiAgentWorkflow.run,
                MultiAgentInput(run_id=run_id, user_questions_enabled=True),
                id=f"{run_id}:orchestrator",
                task_queue="multi-question-queue",
            )
            question_id = await asyncio.wait_for(backend.created.get(), timeout=5)
            await asyncio.wait_for(beta_completed.wait(), timeout=5)
            assert question_id not in backend.answered
            backend.answered.add(question_id)
            register = backend.register_calls[0]
            alpha = env.client.get_workflow_handle(
                f"{run_id}:node:alpha", run_id=str(register[2])
            )
            await alpha.signal("question_available", args=[question_id, 2])
            result = await handle.result()

    assert result.status == "completed"
    assert result.answer == "combined final"
    assert backend.consume_count == 1
    assert backend.register_calls[0][0] == turn_id
    assert backend.register_calls[0][4:] == (
        "node",
        "alpha",
        "44444444-4444-4444-4444-444444444444",
        1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", ["user_input_limit", "user_input_disabled"])
async def test_question_creation_policy_is_a_tool_result_not_agent_failure(
    error_code: str,
) -> None:
    backend = QuestionBackend(reject_create_with_code=error_code)
    llm_calls: list[dict[str, Any]] = []

    @activity.defn(name="llm_call")
    async def llm_call(
        request: dict[str, Any], turn_id: str | None = None, publish_events: bool = True
    ) -> str:
        llm_calls.append(request)
        if len(llm_calls) == 1:
            return '<tool_call>{"tool":"ask_user","args":{"text":"Which city?"}}</tool_call>'
        return "Completed with available data."

    @activity.defn(name="list_available_skills")
    async def list_available_skills() -> list[dict[str, Any]]:
        return []

    @activity.defn(name="get_conversation_id")
    async def get_conversation_id() -> str:
        return "conversation-limit"

    @activity.defn(name="get_session_token")
    async def get_session_token() -> str:
        return "session-token"

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict[str, Any]) -> None:
        return None

    snapshot = AgentExecutionSnapshot(
        agent_definition_id="66666666-6666-6666-6666-666666666666",
        name="Limited",
        model="provider/model",
        system_prompt="Work with available data.",
        temperature=0.2,
    )
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="question-limit-queue",
            workflows=[AgentWorkflow],
            activities=[
                llm_call,
                list_available_skills,
                get_conversation_id,
                get_session_token,
                publish_event,
                *_question_activities(backend),
            ],
        ):
            result = await env.client.execute_workflow(
                AgentWorkflow.run,
                AgentInput(
                    agent=snapshot,
                    goal="Try to clarify",
                    event_visibility="internal",
                    turn_id="turn-limit",
                    workflow_phase="node",
                    workflow_node_key="limited",
                    user_questions_enabled=True,
                ),
                id="agent-question-limit",
                task_queue="question-limit-queue",
            )

    assert result == "Completed with available data."
    assert backend.consume_count == 0
    assert error_code in llm_calls[1]["messages"][-1]["content"]
