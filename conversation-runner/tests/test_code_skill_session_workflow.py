import asyncio
import json
from contextlib import AsyncExitStack

import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.workflows.code_skill_session import CodeSkillSessionInput, CodeSkillSessionWorkflow


def _make_llm_call(responses: list[str]):
    calls: list[dict] = []

    @activity.defn(name="llm_call")
    async def llm_call(request: dict, turn_id: str | None = None, publish_events: bool = True) -> str:
        calls.append({"request": request, "publish_events": publish_events})
        return responses[len(calls) - 1]

    return llm_call, calls


def _make_publish_event():
    events: list[tuple[str, dict]] = []

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    return publish_event, events


def _make_open_skill_session(session: dict | None = None):
    calls: list[tuple] = []

    @activity.defn(name="open_skill_session")
    async def open_skill_session(
        skill_id: str, conversation_id: str | None = None, session_token: str | None = None
    ) -> dict:
        calls.append((skill_id, conversation_id, session_token))
        return session or {"session_id": "sess-1", "skill_md": "instructions"}

    return open_skill_session, calls


def _make_exec_in_skill_session(result: dict | None = None):
    calls: list[tuple] = []

    @activity.defn(name="exec_in_skill_session")
    async def exec_in_skill_session(session_id: str, command: str, timeout: float | None = None) -> dict:
        calls.append((session_id, command, timeout))
        return result or {"stdout": "", "stderr": "", "exit_code": 0, "timed_out": False}

    return exec_in_skill_session, calls


def _make_close_skill_session():
    calls: list[str] = []

    @activity.defn(name="close_skill_session")
    async def close_skill_session(session_id: str) -> None:
        calls.append(session_id)

    return close_skill_session, calls


async def _run_code_skill_workflow(input: CodeSkillSessionInput, activities: list, tool_activities: list):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(
                Worker(
                    env.client,
                    task_queue="test-tq",
                    workflows=[CodeSkillSessionWorkflow],
                    activities=activities,
                )
            )
            await stack.enter_async_context(
                Worker(env.client, task_queue="tools", activities=tool_activities)
            )
            handle = await env.client.start_workflow(
                CodeSkillSessionWorkflow.run,
                input,
                id="code-skill-session-test",
                task_queue="test-tq",
            )
            return await handle.result()


@pytest.mark.asyncio
async def test_returns_final_answer_when_no_commands_requested():
    llm_call, llm_calls = _make_llm_call(["Nothing to run, the task is trivially done."])
    publish_event, events = _make_publish_event()
    open_session, open_calls = _make_open_skill_session({"session_id": "sess-1", "skill_md": "Do the thing."})
    exec_session, exec_calls = _make_exec_in_skill_session()
    close_session, close_calls = _make_close_skill_session()

    result = await _run_code_skill_workflow(
        CodeSkillSessionInput(
            skill_id="skill-1", task="do the thing", conversation_id="conv-1"
        ),
        [llm_call, publish_event],
        [open_session, exec_session, close_session],
    )

    assert result == "Nothing to run, the task is trivially done."
    assert open_calls == [("skill-1", "conv-1", None)]
    assert exec_calls == []
    assert close_calls == ["sess-1"]
    # The system prompt must carry the skill's own instructions.
    system_message = llm_calls[0]["request"]["messages"][0]
    assert system_message["role"] == "system"
    assert "Do the thing." in system_message["content"]
    assert llm_calls[0]["publish_events"] is False


@pytest.mark.asyncio
async def test_runs_multiple_rounds_then_returns_final_answer():
    round1 = json.dumps({"command": "ls"})
    round2 = json.dumps({"command": "cat out.txt"})
    llm_call, llm_calls = _make_llm_call(
        [
            f"<run_command>{round1}</run_command>",
            f"<run_command>{round2}</run_command>",
            "Done — the file contained 'hello'.",
        ]
    )
    publish_event, events = _make_publish_event()
    open_session, _ = _make_open_skill_session({"session_id": "sess-1", "skill_md": "instructions"})
    exec_session, exec_calls = _make_exec_in_skill_session(
        {"stdout": "hello\n", "stderr": "", "exit_code": 0, "timed_out": False}
    )
    close_session, close_calls = _make_close_skill_session()

    result = await _run_code_skill_workflow(
        CodeSkillSessionInput(
            skill_id="skill-1", task="read out.txt", conversation_id="conv-1", max_rounds=8
        ),
        [llm_call, publish_event],
        [open_session, exec_session, close_session],
    )

    assert result == "Done — the file contained 'hello'."
    assert len(llm_calls) == 3
    assert exec_calls == [
        ("sess-1", "ls", 60.0),
        ("sess-1", "cat out.txt", 60.0),
    ]
    assert close_calls == ["sess-1"]

    run_command_events = [e for e in events if e[0] == "run-command"]
    assert [e[1]["command"] for e in run_command_events] == ["ls", "cat out.txt"]
    run_result_events = [e for e in events if e[0] == "run-result"]
    assert len(run_result_events) == 2

    # The third llm_call must see the previous command's output as context.
    third_request = llm_calls[2]["request"]
    assert any("hello" in m.get("content", "") for m in third_request["messages"])


@pytest.mark.asyncio
async def test_closes_session_even_when_a_round_fails():
    llm_call, llm_calls = _make_llm_call([f'<run_command>{json.dumps({"command": "boom"})}</run_command>'])
    publish_event, events = _make_publish_event()
    open_session, _ = _make_open_skill_session()
    close_session, close_calls = _make_close_skill_session()

    @activity.defn(name="exec_in_skill_session")
    async def failing_exec(session_id: str, command: str, timeout: float | None = None) -> dict:
        # Non-retryable so the test fails fast instead of exercising
        # Temporal's default indefinite-retry-until-timeout policy.
        raise ApplicationError("skill-executor unreachable", non_retryable=True)

    result = await _run_code_skill_workflow(
        CodeSkillSessionInput(skill_id="skill-1", task="break it", conversation_id="conv-1"),
        [llm_call, publish_event],
        [open_session, failing_exec, close_session],
    )

    # A dead sandbox degrades into an error answer for the parent loop to
    # relay — the workflow itself must not fail (that would hang the turn).
    assert "Code skill execution failed" in result
    assert "skill-executor unreachable" in result
    assert close_calls == ["sess-1"]


@pytest.mark.asyncio
async def test_close_skill_session_runs_when_a_round_activity_is_cancelled():
    """close_skill_session in the finally block must run
    even when the thing that sent this workflow into `finally` was a
    cancellation, not a normal ActivityError — asyncio.shield protects that
    specific call from also being torn down if a workflow-level cancellation
    lands while it's in flight. This test exercises the closely related,
    reliably reproducible path (a round's own activity gets cancelled,
    caught by `except ActivityError`, finally still runs close_skill_session)
    — genuinely timing a cancellation to land inside the shielded await
    itself isn't reproducible deterministically against the real Temporal
    Python SDK's cancellation delivery in a fast unit test."""
    llm_call, _ = _make_llm_call(
        [f'<run_command>{json.dumps({"command": "sleep 100"})}</run_command>']
    )
    publish_event, _ = _make_publish_event()
    open_session, _ = _make_open_skill_session()
    close_session, close_calls = _make_close_skill_session()

    round_started = asyncio.Event()

    @activity.defn(name="exec_in_skill_session")
    async def cancelled_exec(session_id: str, command: str, timeout: float | None = None) -> dict:
        round_started.set()
        await activity.wait_for_cancelled()
        raise asyncio.CancelledError()

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(
                Worker(
                    env.client,
                    task_queue="test-tq",
                    workflows=[CodeSkillSessionWorkflow],
                    activities=[llm_call, publish_event],
                )
            )
            await stack.enter_async_context(
                Worker(
                    env.client,
                    task_queue="tools",
                    activities=[open_session, cancelled_exec, close_session],
                )
            )
            handle = await env.client.start_workflow(
                CodeSkillSessionWorkflow.run,
                CodeSkillSessionInput(
                    skill_id="skill-1", task="do it", conversation_id="conv-1"
                ),
                id="code-skill-session-cancel-test",
                task_queue="test-tq",
            )
            await asyncio.wait_for(round_started.wait(), timeout=10)
            await handle.cancel()

            result = await handle.result()

    assert "Code skill execution failed" in result
    assert close_calls == ["sess-1"]


@pytest.mark.asyncio
async def test_exhausts_round_budget_and_summarizes():
    command = json.dumps({"command": "poll"})
    run_command_text = f"<run_command>{command}</run_command>"
    llm_call, llm_calls = _make_llm_call(
        [run_command_text, run_command_text, "Summarized after hitting the round limit."]
    )
    publish_event, events = _make_publish_event()
    open_session, _ = _make_open_skill_session()
    exec_session, exec_calls = _make_exec_in_skill_session()
    close_session, close_calls = _make_close_skill_session()

    result = await _run_code_skill_workflow(
        CodeSkillSessionInput(
            skill_id="skill-1", task="poll forever", conversation_id="conv-1", max_rounds=2
        ),
        [llm_call, publish_event],
        [open_session, exec_session, close_session],
    )

    assert result == "Summarized after hitting the round limit."
    assert len(llm_calls) == 3
    assert len(exec_calls) == 2
    final_request = llm_calls[-1]["request"]
    assert final_request["messages"][-1]["content"] == (
        "You've reached the round limit. Provide a concise final answer based on the work above."
    )
    assert close_calls == ["sess-1"]
