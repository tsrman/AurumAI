import json
from contextlib import AsyncExitStack

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.workflows.agent import AgentExecutionSnapshot, AgentInput, AgentWorkflow


def _snapshot(
    *, allowed_skill_ids: tuple[str, ...] = ("search",)
) -> AgentExecutionSnapshot:
    return AgentExecutionSnapshot(
        agent_definition_id="agent-1",
        name="Researcher",
        model="provider/model-v1",
        system_prompt="Use primary evidence.",
        temperature=0.2,
        allowed_skill_ids=allowed_skill_ids,
        knowledge_bases=({"id": "kb-1", "name": "Policies"},),
    )


def _input(goal: str, **kwargs) -> AgentInput:  # noqa: ANN003
    return AgentInput(agent=_snapshot(), goal=goal, **kwargs)


def _make_llm_call(responses: list[str]):
    calls: list[dict] = []
    publish_events_seen: list[bool] = []

    @activity.defn(name="llm_call")
    async def llm_call(
        request: dict, turn_id: str | None = None, publish_events: bool = True
    ) -> str:
        calls.append(request)
        publish_events_seen.append(publish_events)
        return responses[len(calls) - 1]

    return llm_call, calls, publish_events_seen


def _make_publish_event():
    events: list[tuple[str, dict]] = []

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    return publish_event, events


def _make_execute_skill(result: dict | None = None):
    calls: list[tuple[str, dict, str, str]] = []

    @activity.defn(name="execute_skill")
    async def execute_skill(
        skill_id: str, args: dict, conversation_id: str, session_token: str | None = None
    ) -> dict:
        calls.append((skill_id, args, conversation_id, session_token))
        return result if result is not None else {"ok": True}

    return execute_skill, calls


def _make_get_conversation_id(conversation_id: str = "test-conversation"):
    @activity.defn(name="get_conversation_id")
    async def get_conversation_id() -> str | None:
        return conversation_id

    return get_conversation_id


def _make_get_session_token(session_token: str | None = "test-token"):
    @activity.defn(name="get_session_token")
    async def get_session_token() -> str | None:
        return session_token

    return get_session_token


def _make_list_available_skills(skills: list[dict] | None = None):
    @activity.defn(name="list_available_skills")
    async def list_available_skills() -> list[dict]:
        return skills or [
            {
                "id": "search",
                "name": "Search",
                "description": "Search evidence",
                "inputSchema": {"type": "object"},
            },
            {
                "id": "hidden",
                "name": "Hidden",
                "description": "Must not appear",
                "inputSchema": {},
            },
        ]

    return list_available_skills


async def _run_agent_workflow(
    input: AgentInput,
    activities: list,
    tool_activities: list | None = None,
    available_skills: list[dict] | None = None,
):
    activities = [
        *activities,
        _make_get_conversation_id(),
        _make_get_session_token(),
        _make_list_available_skills(available_skills),
    ]
    async with await WorkflowEnvironment.start_time_skipping() as env:
        workers = [
            Worker(
                env.client,
                task_queue="test-tq",
                workflows=[AgentWorkflow],
                activities=activities,
            )
        ]
        if tool_activities:
            workers.append(
                Worker(env.client, task_queue="tools", activities=tool_activities)
            )
        async with AsyncExitStack() as stack:
            for w in workers:
                await stack.enter_async_context(w)
            handle = await env.client.start_workflow(
                AgentWorkflow.run,
                input,
                id="agent-test",
                task_queue="test-tq",
            )
            return await handle.result()


@pytest.mark.asyncio
async def test_agent_returns_final_answer_without_tool_calls():
    llm_call, llm_calls, publish_events_seen = _make_llm_call(["The answer is 42."])
    publish_event, events = _make_publish_event()

    result = await _run_agent_workflow(
        _input("answer the question"),
        [llm_call, publish_event],
    )

    assert result == "The answer is 42."
    assert len(llm_calls) == 1
    # the planning llm_call must never publish live —
    # this is an internal loop, not the user-facing chat turn.
    assert publish_events_seen == [False]
    complete_events = [e for e in events if e[0] == "agent-complete"]
    assert complete_events == [
        ("agent-complete", {"answer": "The answer is 42.", "iterations": 1})
    ]


@pytest.mark.asyncio
async def test_agent_calls_tool_then_returns_final_answer():
    tool_call_response = json.dumps({"skill_id": "search", "args": {"q": "docs"}})
    llm_call, llm_calls, publish_events_seen = _make_llm_call(
        [f"<tool_call>{tool_call_response}</tool_call>", "Final answer after tool use."]
    )
    publish_event, events = _make_publish_event()
    execute_skill, skill_calls = _make_execute_skill({"found": "yes"})

    result = await _run_agent_workflow(
        _input("find something"),
        [llm_call, publish_event],
        [execute_skill],
    )

    assert result == "Final answer after tool use."
    assert len(llm_calls) == 2
    assert publish_events_seen == [False, False]
    # execute_skill must receive the real session token,
    # not fall back to tool-worker's dummy smoke-test token.
    assert skill_calls == [("search", {"q": "docs"}, "test-conversation", "test-token")]
    tool_call_events = [e for e in events if e[0] == "agent-tool-call"]
    assert tool_call_events == [
        ("agent-tool-call", {"skill_id": "search", "args": {"q": "docs"}})
    ]
    # Second llm_call request should include the tool result as context.
    second_request = llm_calls[1]
    tool_result_messages = [
        m for m in second_request["messages"] if "Tool result for search" in m.get("content", "")
    ]
    assert len(tool_result_messages) == 1


@pytest.mark.asyncio
async def test_agent_exhausts_iteration_budget_and_summarizes():
    tool_call_response = json.dumps({"skill_id": "search", "args": {}})
    tool_call_text = f"<tool_call>{tool_call_response}</tool_call>"
    llm_call, llm_calls, publish_events_seen = _make_llm_call(
        [tool_call_text, tool_call_text, tool_call_text, "Summarized final answer."]
    )
    publish_event, events = _make_publish_event()
    execute_skill, skill_calls = _make_execute_skill({"ok": True})

    result = await _run_agent_workflow(
        _input("loop forever"),
        [llm_call, publish_event],
        [execute_skill],
    )

    assert result == "Summarized final answer."
    assert len(llm_calls) == 4
    assert publish_events_seen == [False, False, False, False]
    assert len(skill_calls) == 3
    # Final llm_call is a summarization request, not another tool round.
    final_request = llm_calls[-1]
    assert final_request["messages"][-1]["content"] == (
        "Provide a concise final answer based on the work above."
    )
    complete_events = [e for e in events if e[0] == "agent-complete"]
    assert complete_events == [
        ("agent-complete", {"answer": "Summarized final answer.", "iterations": 3})
    ]


@pytest.mark.asyncio
async def test_agent_output_schema_valid_json_returns_cleaned_json():
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    raw = '```json\n{"answer": "42"}\n```'
    llm_call, llm_calls, publish_events_seen = _make_llm_call([raw])
    publish_event, events = _make_publish_event()

    result = await _run_agent_workflow(
        _input(
            "answer as json",
            output_schema=schema,
        ),
        [llm_call, publish_event],
    )

    assert result == '{"answer": "42"}'
    assert len(llm_calls) == 1


@pytest.mark.asyncio
async def test_agent_output_schema_invalid_json_retries_then_succeeds():
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    llm_call, llm_calls, publish_events_seen = _make_llm_call(
        ["not valid json at all", '{"answer": "ok"}']
    )
    publish_event, events = _make_publish_event()

    result = await _run_agent_workflow(
        _input(
            "answer as json",
            output_schema=schema,
        ),
        [llm_call, publish_event],
    )

    assert result == '{"answer": "ok"}'
    assert len(llm_calls) == 2
    retry_request = llm_calls[1]
    assert "Invalid JSON error" in retry_request["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_agent_output_schema_validation_error_retries_then_succeeds():
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    # First response is valid JSON but missing the required "answer" field.
    llm_call, llm_calls, publish_events_seen = _make_llm_call(
        ['{"wrong_field": "x"}', '{"answer": "ok"}']
    )
    publish_event, events = _make_publish_event()

    result = await _run_agent_workflow(
        _input(
            "answer as json",
            output_schema=schema,
        ),
        [llm_call, publish_event],
    )

    assert result == '{"answer": "ok"}'
    assert len(llm_calls) == 2
    retry_request = llm_calls[1]
    assert "Validation error" in retry_request["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_agent_output_schema_retries_remain_bounded():
    schema = {"type": "object", "required": ["answer"]}
    llm_call, llm_calls, _ = _make_llm_call(
        ["invalid one", "invalid two", "invalid three", "bounded fallback"]
    )
    publish_event, _ = _make_publish_event()

    result = await _run_agent_workflow(
        _input("answer as json", output_schema=schema),
        [llm_call, publish_event],
    )

    assert result == "bounded fallback"
    assert len(llm_calls) == 4


@pytest.mark.asyncio
async def test_agent_fetches_conversation_id_and_session_token_via_activities():
    """workflow code must never read settings.conversation_id/
    session_token directly (invisible to pool-mode's runtime POST /bind) — it
    must go through activities. Using distinct, deliberately-not-hardcoded
    values here proves the workflow actually threads the activity result
    through to execute_skill, not some hardcoded/settings-imported constant.
    """
    tool_call_response = json.dumps({"skill_id": "search", "args": {}})
    llm_call, llm_calls, _ = _make_llm_call(
        [f"<tool_call>{tool_call_response}</tool_call>", "Done."]
    )
    publish_event, _events = _make_publish_event()
    execute_skill, skill_calls = _make_execute_skill({"ok": True})

    activities = [
        llm_call,
        publish_event,
        _make_get_conversation_id("conv-xyz"),
        _make_get_session_token("secret-session-token"),
        _make_list_available_skills(),
    ]

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(
                Worker(
                    env.client,
                    task_queue="test-tq",
                    workflows=[AgentWorkflow],
                    activities=activities,
                )
            )
            await stack.enter_async_context(
                Worker(env.client, task_queue="tools", activities=[execute_skill])
            )
            handle = await env.client.start_workflow(
                AgentWorkflow.run,
                _input("find something"),
                id="agent-test-2",
                task_queue="test-tq",
            )
            result = await handle.result()

    assert result == "Done."
    assert skill_calls == [("search", {}, "conv-xyz", "secret-session-token")]


@pytest.mark.asyncio
async def test_snapshot_drives_prompt_model_correlation_and_internal_visibility():
    llm_call, llm_calls, publish_events_seen = _make_llm_call(["Evidence found."])
    publish_event, events = _make_publish_event()

    result = await _run_agent_workflow(
        AgentInput(
            agent=_snapshot(),
            goal="Find evidence",
            event_visibility="internal",
            workflow_run_id="run-1",
            workflow_phase="node",
            workflow_node_key="research",
        ),
        [llm_call, publish_event],
    )

    assert result == "Evidence found."
    assert publish_events_seen == [False]
    assert events == []
    request = llm_calls[0]
    assert request["model"] == "provider/model-v1"
    assert request["temperature"] == 0.2
    assert request["stream"] is True
    assert "response_format" not in request
    assert request["workflow_run_id"] == "run-1"
    assert request["workflow_phase"] == "node"
    assert request["workflow_node_key"] == "research"
    system_prompt = request["messages"][0]["content"]
    assert "Use primary evidence." in system_prompt
    assert "Search evidence" in system_prompt
    assert "Must not appear" not in system_prompt
    assert "kb-1" in system_prompt
    assert "Policies" in system_prompt


@pytest.mark.asyncio
async def test_agent_never_executes_tool_outside_filtered_snapshot_allowlist():
    forbidden = '<tool_call>{"skill_id":"hidden","args":{"q":"secret"}}</tool_call>'
    llm_call, llm_calls, _ = _make_llm_call([forbidden, "Safe final answer."])
    publish_event, _ = _make_publish_event()
    execute_skill, skill_calls = _make_execute_skill()

    result = await _run_agent_workflow(
        _input("Use only configured tools"),
        [llm_call, publish_event],
        [execute_skill],
    )

    assert result == "Safe final answer."
    assert skill_calls == []
    assert "skill_not_allowed:hidden" in llm_calls[1]["messages"][-1]["content"]
