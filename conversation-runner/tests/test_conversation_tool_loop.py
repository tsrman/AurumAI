import asyncio
import json
import threading
from contextlib import AsyncExitStack
from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.config import settings
from src.workflows.code_skill_session import CodeSkillSessionWorkflow
from src.workflows.conversation import ConversationInput, ConversationWorkflow
from src.workflows.evaluator import EvaluationWorkflow


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


@activity.defn(name="persist_assistant_message")
async def mock_persist_assistant_message(
    content: str,
    model: str | None = None,
    turn_id: str | None = None,
    tool_calls: list | None = None,
    tool_results: dict | None = None,
) -> None:
    return None


def _make_persist_assistant_message():
    calls: list[dict] = []

    @activity.defn(name="persist_assistant_message")
    async def persist_assistant_message(
        content: str,
        model: str | None = None,
        turn_id: str | None = None,
        tool_calls: list | None = None,
        tool_results: dict | None = None,
    ) -> None:
        calls.append(
            {
                "content": content,
                "model": model,
                "turn_id": turn_id,
                "tool_calls": tool_calls,
                "tool_results": tool_results,
            }
        )

    return persist_assistant_message, calls


@activity.defn(name="record_langfuse_score")
async def mock_record_langfuse_score(
    trace_id: str, name: str, value: float, comment: str = ""
) -> None:
    return None


@activity.defn(name="record_message_score")
async def mock_record_message_score(
    turn_id: str, score: float | None, reason: str
) -> None:
    return None


def _make_llm_call(response: str):
    calls: list[dict] = []

    @activity.defn(name="llm_call")
    async def llm_call(
        request: dict,
        turn_id: str | None = None,
        publish_events: bool = True,
        hide_tool_call_tag: bool = False,
    ) -> str:
        calls.append(request)
        return response

    return llm_call, calls


def _make_llm_call_sequence(responses: list[str]):
    """Records (request, publish_events) per call, returning responses in order."""
    calls: list[dict] = []

    @activity.defn(name="llm_call")
    async def llm_call(
        request: dict,
        turn_id: str | None = None,
        publish_events: bool = True,
        hide_tool_call_tag: bool = False,
    ) -> str:
        calls.append(
            {
                "request": request,
                "publish_events": publish_events,
                "hide_tool_call_tag": hide_tool_call_tag,
            }
        )
        # Clamp so extra calls (e.g. from the background EvaluationWorkflow,
        # which shares this task queue) don't raise IndexError.
        return responses[min(len(calls) - 1, len(responses) - 1)]

    return llm_call, calls


def _make_list_available_skills(skills: list[dict]):
    @activity.defn(name="list_available_skills")
    async def list_available_skills() -> list[dict]:
        return skills

    return list_available_skills


def _make_get_session_token(token: str | None):
    @activity.defn(name="get_session_token")
    async def get_session_token() -> str | None:
        return token

    return get_session_token


def _make_publish_event():
    events: list[tuple[str, dict]] = []

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    return publish_event, events


def _make_execute_skill(result: dict | None = None):
    calls: list[tuple] = []

    @activity.defn(name="execute_skill")
    async def execute_skill(
        skill_id: str, args: dict, conversation_id: str, session_token: str | None = None
    ) -> dict:
        calls.append((skill_id, args, conversation_id, session_token))
        return result if result is not None else {"ok": True}

    return execute_skill, calls


async def _run_one_turn(activities: list, tool_activities: list, message: dict):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(
                Worker(
                    env.client,
                    task_queue="test-tq",
                    workflows=[ConversationWorkflow, CodeSkillSessionWorkflow, EvaluationWorkflow],
                    activities=activities + [mock_record_langfuse_score, mock_record_message_score],
                )
            )
            await stack.enter_async_context(
                Worker(env.client, task_queue="tools", activities=tool_activities)
            )
            handle = await env.client.start_workflow(
                ConversationWorkflow.run,
                ConversationInput(conversation_id="conv-tools"),
                id="conversation-conv-tools",
                task_queue="test-tq",
            )
            await handle.signal(ConversationWorkflow.user_message, message)
            await env.sleep(timedelta(seconds=settings.idle_ttl_seconds + 10))
            return await handle.result()


@pytest.mark.asyncio
async def test_conversation_executes_tool_call_and_publishes_events(monkeypatch):
    # Disable the background evaluator so it doesn't add a third llm_call on
    # the same task queue and make the call-count assertions below flaky.
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_enabled", False)
    tool_call_payload = json.dumps(
        {"call_id": "call-1", "skill_id": "search", "args": {"q": "docs"}}
    )
    planning_response = f"Let me check.<tool_call>{tool_call_payload}</tool_call>"
    final_answer = "Found it."
    llm_call, llm_calls = _make_llm_call_sequence([planning_response, final_answer])
    publish_event, events = _make_publish_event()
    execute_skill, skill_calls = _make_execute_skill({"found": "yes"})
    persist_assistant_message, persist_calls = _make_persist_assistant_message()
    # A skill must actually be offered for a <tool_call> in the model's raw
    # text to be a real selection rather than a hallucination — see
    # test_conversation_ignores_tool_call_when_no_skills_are_available
    #.
    skills = [{"id": "search", "name": "search", "description": "Search docs", "inputSchema": {}}]

    result = await _run_one_turn(
        [
            mock_load_conversation_state,
            _make_list_available_skills(skills),
            mock_get_session_token,
            mock_get_conversation_temperature,
            llm_call,
            publish_event,
            persist_assistant_message,
        ],
        [execute_skill],
        {"role": "user", "content": "search for docs", "turn_id": "t1", "model": "gpt-4o-mini"},
    )

    assert result == final_answer
    assert skill_calls == [("search", {"q": "docs"}, "conv-tools", None)]

    tool_calls_events = [e for e in events if e[0] == "tool-calls"]
    assert tool_calls_events == [
        (
            "tool-calls",
            {
                "calls": [
                    {"call_id": "call-1", "skill_id": "search", "args": {"q": "docs"}}
                ],
                "turn_id": "t1",
            },
        )
    ]

    tool_result_events = [e for e in events if e[0] == "tool-result"]
    assert tool_result_events == [
        (
            "tool-result",
            {
                "call_id": "call-1",
                "skill_id": "search",
                "result": {"found": "yes"},
                "turn_id": "t1",
            },
        )
    ]

    # Persisted alongside the synthesized final answer (not the raw planning
    # response, which still carries the <tool_call> tag) so history reloads
    # can render the same tool-call/tool-result rows the live SSE stream
    # showed.
    assert persist_calls == [
        {
            "content": final_answer,
            "model": "gpt-4o-mini",
            "turn_id": "t1",
            "tool_calls": [{"call_id": "call-1", "skill_id": "search", "args": {"q": "docs"}}],
            "tool_results": {"call-1": {"found": "yes"}},
        }
    ]


@pytest.mark.asyncio
async def test_conversation_ignores_tool_call_when_no_skills_are_available():
    """Regression test: when list_available_skills is
    empty, the system prompt never mentions <tool_call> at all — a
    <tool_call> tag appearing in the model's raw text anyway is a
    hallucination, not a real selection. This used to get parsed and
    actually executed anyway (privilege bypass for admin:read holders, who
    skip all resource-level skill gates), while the raw tag stayed embedded
    in the "final" answer since this path never ran the synthesis pass the
    with-skills path does."""
    tool_call_payload = json.dumps(
        {"call_id": "call-1", "skill_id": "search", "args": {"q": "docs"}}
    )
    llm_response = f"Let me check.<tool_call>{tool_call_payload}</tool_call>"
    llm_call, llm_calls = _make_llm_call(llm_response)
    publish_event, events = _make_publish_event()
    execute_skill, skill_calls = _make_execute_skill({"found": "yes"})
    persist_assistant_message, persist_calls = _make_persist_assistant_message()

    result = await _run_one_turn(
        [
            mock_load_conversation_state,
            mock_list_available_skills,
            mock_get_session_token,
            mock_get_conversation_temperature,
            llm_call,
            publish_event,
            persist_assistant_message,
        ],
        [execute_skill],
        {"role": "user", "content": "search for docs", "turn_id": "t1", "model": "gpt-4o-mini"},
    )

    # The raw tag stays in the final answer as-is — no synthesis pass runs
    # without skills — but crucially the tool itself was never invoked.
    assert result == llm_response
    assert skill_calls == []
    assert [e for e in events if e[0] in ("tool-calls", "tool-result")] == []
    assert persist_calls == [
        {
            "content": llm_response,
            "model": "gpt-4o-mini",
            "turn_id": "t1",
            "tool_calls": None,
            "tool_results": None,
        }
    ]


@pytest.mark.asyncio
async def test_conversation_cancel_during_tool_call_stops_before_persisting():
    tool_call_payload = json.dumps({"call_id": "call-1", "skill_id": "search", "args": {}})
    llm_response = f"<tool_call>{tool_call_payload}</tool_call>"
    llm_call, llm_calls = _make_llm_call(llm_response)
    publish_event, events = _make_publish_event()
    # A skill must actually be offered for the <tool_call> above to be a
    # real selection rather than a hallucination the workflow now ignores
    # outright — otherwise slow_execute_skill below is
    # never invoked and tool_started.wait() hangs forever.
    skills = [{"id": "search", "name": "search", "description": "Search docs", "inputSchema": {}}]

    tool_started = threading.Event()
    tool_release = threading.Event()
    skill_calls: list[tuple] = []

    @activity.defn(name="execute_skill")
    async def slow_execute_skill(
        skill_id: str, args: dict, conversation_id: str, session_token: str | None = None
    ) -> dict:
        skill_calls.append((skill_id, args, conversation_id))
        tool_started.set()
        await asyncio.to_thread(tool_release.wait)
        return {"ok": True}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(
                Worker(
                    env.client,
                    task_queue="test-tq",
                    workflows=[ConversationWorkflow],
                    activities=[
                        mock_load_conversation_state,
                        _make_list_available_skills(skills),
                        mock_get_session_token,
                        mock_get_conversation_temperature,
                        llm_call,
                        publish_event,
                        mock_persist_assistant_message,
                    ],
                )
            )
            await stack.enter_async_context(
                Worker(env.client, task_queue="tools", activities=[slow_execute_skill])
            )
            handle = await env.client.start_workflow(
                ConversationWorkflow.run,
                ConversationInput(conversation_id="conv-cancel-tool"),
                id="conversation-conv-cancel-tool",
                task_queue="test-tq",
            )
            await handle.signal(
                ConversationWorkflow.user_message,
                {"role": "user", "content": "search", "turn_id": "t1", "model": "gpt-4o-mini"},
            )
            await asyncio.wait_for(asyncio.to_thread(tool_started.wait), timeout=10)
            await handle.signal(ConversationWorkflow.cancel_generation)
            tool_release.set()
            result = await handle.result()

    assert result == ""
    persist_events = [e for e in events if e[0] == "tool-result"]
    assert persist_events == []


@pytest.mark.asyncio
async def test_conversation_with_skills_synthesizes_final_answer_after_tool_call(monkeypatch):
    # Disable the background evaluator so it doesn't add a third llm_call on
    # the same task queue and make the call-count assertions below flaky.
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_enabled", False)
    tool_call_payload = json.dumps({"call_id": "call-1", "skill_id": "skill-1", "args": {"a": 1, "b": 2}})
    planning_response = f"<tool_call>{tool_call_payload}</tool_call>"
    final_answer = "The sum is 3."
    llm_call, llm_calls = _make_llm_call_sequence([planning_response, final_answer])
    publish_event, events = _make_publish_event()
    # execute_skill (tool-worker) always wraps its result as {"skill_id", "output"}.
    execute_skill, skill_calls = _make_execute_skill({"skill_id": "skill-1", "output": {"result": 3}})
    skills = [
        {
            "id": "skill-1",
            "name": "get-sum",
            "description": "Returns the sum of two numbers",
            "inputSchema": {"type": "object", "properties": {"a": {}, "b": {}}},
        }
    ]

    result = await _run_one_turn(
        [
            mock_load_conversation_state,
            _make_list_available_skills(skills),
            _make_get_session_token("conv-session-token"),
            mock_get_conversation_temperature,
            llm_call,
            publish_event,
            mock_persist_assistant_message,
        ],
        [execute_skill],
        {"role": "user", "content": "what is 1+2?", "turn_id": "t1", "model": "gpt-4o-mini"},
    )

    assert result == final_answer
    assert skill_calls == [("skill-1", {"a": 1, "b": 2}, "conv-tools", "conv-session-token")]

    assert len(llm_calls) == 2
    # Planning call carries the skills system prompt and streams normally,
    # except the real llm_call activity holds back a <tool_call> tag from
    # the user (hide_tool_call_tag=True) — tested in isolation in
    # test_activities.py; this mock doesn't simulate that holdback itself.
    assert llm_calls[0]["publish_events"] is True
    assert llm_calls[0]["hide_tool_call_tag"] is True
    planning_system_message = llm_calls[0]["request"]["messages"][0]
    assert planning_system_message["role"] == "system"
    assert "skill-1" in planning_system_message["content"]
    assert "get-sum" in planning_system_message["content"]
    # Synthesis includes the tool result and still hides a possible built-in
    # protocol tag: ask_user may legitimately be selected after a skill.
    assert llm_calls[1]["publish_events"] is True
    assert llm_calls[1]["hide_tool_call_tag"] is True
    synthesis_messages = llm_calls[1]["request"]["messages"]
    assert {"role": "assistant", "content": planning_response} in synthesis_messages
    assert any(
        m["role"] == "user" and "skill-1" in m["content"] and "3" in m["content"]
        for m in synthesis_messages
    )

    content_delta_events = [e for e in events if e[0] == "content-delta"]
    assert content_delta_events == []


@pytest.mark.asyncio
async def test_conversation_with_skills_no_tool_call_does_not_double_publish(monkeypatch):
    # The real llm_call activity streams content live even with
    # hide_tool_call_tag=True, holding back only a <tool_call>-tag-shaped
    # tail (see test_activities.py for that behavior in isolation). This
    # test's llm_call is a simplified mock that doesn't simulate that
    # streaming, so it only guards conversation.py's own side: it must NOT
    # explicitly re-publish the full answer itself once llm_call returns —
    # that would double-publish content on top of what llm_call already
    # streamed.
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_enabled", False)
    plain_answer = "Just a normal answer, no tools needed."
    llm_call, llm_calls = _make_llm_call_sequence([plain_answer])
    publish_event, events = _make_publish_event()
    skills = [{"id": "skill-1", "name": "echo", "description": "Echoes input", "inputSchema": {}}]

    execute_skill, _ = _make_execute_skill()

    result = await _run_one_turn(
        [
            mock_load_conversation_state,
            _make_list_available_skills(skills),
            mock_get_session_token,
            mock_get_conversation_temperature,
            llm_call,
            publish_event,
            mock_persist_assistant_message,
        ],
        [execute_skill],
        {"role": "user", "content": "hi", "turn_id": "t1", "model": "gpt-4o-mini"},
    )

    assert result == plain_answer
    assert len(llm_calls) == 1
    assert llm_calls[0]["publish_events"] is True
    assert llm_calls[0]["hide_tool_call_tag"] is True

    content_delta_events = [e for e in events if e[0] == "content-delta"]
    assert content_delta_events == []


@pytest.mark.asyncio
async def test_conversation_routes_code_skill_to_child_workflow(monkeypatch):
    # A type="code" skill must go through CodeSkillSessionWorkflow, never the
    # plain execute_skill activity — that's the Stage-2 one-shot stopgap.
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_enabled", False)
    tool_call_payload = json.dumps(
        {"call_id": "call-1", "skill_id": "code-skill-1", "args": {"task": "greet the user"}}
    )
    planning_response = f"<tool_call>{tool_call_payload}</tool_call>"
    sub_loop_answer = "Printed: Hello, World!"
    final_answer = "The skill printed a greeting."
    llm_call, llm_calls = _make_llm_call_sequence(
        [planning_response, sub_loop_answer, final_answer]
    )
    publish_event, events = _make_publish_event()
    execute_skill, skill_calls = _make_execute_skill()

    open_calls: list[tuple] = []

    @activity.defn(name="open_skill_session")
    async def open_skill_session(
        skill_id: str, conversation_id: str | None = None, session_token: str | None = None
    ) -> dict:
        open_calls.append((skill_id, conversation_id, session_token))
        return {"session_id": "sandbox-sess-1", "skill_md": "Greet the user by name."}

    exec_calls: list[tuple] = []

    @activity.defn(name="exec_in_skill_session")
    async def exec_in_skill_session(
        session_id: str, command: str, timeout: float | None = None
    ) -> dict:
        exec_calls.append((session_id, command, timeout))
        return {"stdout": "Hello, World!\n", "stderr": "", "exit_code": 0, "timed_out": False}

    close_calls: list[str] = []

    @activity.defn(name="close_skill_session")
    async def close_skill_session(session_id: str) -> None:
        close_calls.append(session_id)

    skills = [
        {"id": "code-skill-1", "name": "greet", "type": "code", "description": "Greets the user"}
    ]

    result = await _run_one_turn(
        [
            mock_load_conversation_state,
            _make_list_available_skills(skills),
            _make_get_session_token("conv-session-token"),
            mock_get_conversation_temperature,
            llm_call,
            publish_event,
            mock_persist_assistant_message,
        ],
        [execute_skill, open_skill_session, exec_in_skill_session, close_skill_session],
        {"role": "user", "content": "greet me", "turn_id": "t1", "model": "gpt-4o-mini"},
    )

    assert result == final_answer
    assert skill_calls == []
    assert open_calls == [("code-skill-1", "conv-tools", "conv-session-token")]
    # The sub-loop's own final answer had no <run_command> tags, so it never
    # needed exec_in_skill_session — only open + close.
    assert exec_calls == []
    assert close_calls == ["sandbox-sess-1"]

    synthesis_messages = llm_calls[2]["request"]["messages"]
    assert any(
        m["role"] == "user" and "code-skill-1" in m["content"] and sub_loop_answer in m["content"]
        for m in synthesis_messages
    )
