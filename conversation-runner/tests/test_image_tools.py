import asyncio
import json
from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.config import settings
from src.workflows.builtin_tools import (
    ExecutionContext,
    builtin_instructions,
    enabled_builtin_tools,
    image_tool_context,
    image_tool_error_context,
    parse_builtin,
    prepare_builtin_invocation,
)
from src.workflows.conversation import ConversationInput, ConversationWorkflow

GENERATED = "55555555-5555-4555-8555-555555555555"
EDITED = "66666666-6666-4666-8666-666666666666"


def test_image_tools_stay_unknown_unless_enabled() -> None:
    call = {"tool": "generate_image", "args": {"prompt": "a lighthouse"}}
    with pytest.raises(ValueError, match="unknown built-in tool"):
        parse_builtin(call)
    assert parse_builtin(call, image_tools=frozenset({"generate_image"})) == (
        "generate_image",
        {"prompt": "a lighthouse", "size": "auto"},
    )
    with pytest.raises(ValueError, match="unknown built-in tool"):
        prepare_builtin_invocation(call, lambda: "id", enabled_tools=frozenset({"web_search"}))
    invocation = prepare_builtin_invocation(
        call, lambda: "id", enabled_tools=enabled_builtin_tools(False, False, image_generation_enabled=True)
    )
    assert invocation is not None and invocation.name == "generate_image"


@pytest.mark.parametrize(
    "args",
    [
        {"prompt": ""},
        {"prompt": "x", "size": "4096x4096"},
        {"prompt": "x", "url": "https://example.org/a.png"},
    ],
)
def test_generate_arguments_are_strict(args: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse_builtin({"tool": "generate_image", "args": args}, image_tools=frozenset({"generate_image"}))


def test_edit_takes_only_an_image_id() -> None:
    tools = frozenset({"edit_image"})
    assert parse_builtin(
        {"tool": "edit_image", "args": {"image_id": GENERATED, "prompt": "blue sky"}}, image_tools=tools
    ) == ("edit_image", {"prompt": "blue sky", "size": "auto", "image_id": GENERATED})
    with pytest.raises(ValidationError):
        parse_builtin({"tool": "edit_image", "args": {"image_id": "/etc/passwd", "prompt": "x"}}, image_tools=tools)


def test_instructions_for_chat_and_node_agents_per_capability() -> None:
    ordinary = builtin_instructions(
        "ordinary", user_questions_enabled=False, image_generation_enabled=True
    )
    assert "generate_image" in ordinary and "edit_image" not in ordinary
    both = builtin_instructions(
        "ordinary",
        user_questions_enabled=False,
        image_generation_enabled=True,
        image_editing_enabled=True,
    )
    assert "edit_image" in both
    # Node agents run in the "agent" scope; their images go to the final answer.
    agent = builtin_instructions(
        "agent", user_questions_enabled=False, image_generation_enabled=True
    )
    assert "generate_image" in agent and "workflow's final answer" in agent
    assert "below your answer" not in agent
    assert "image" not in builtin_instructions("agent", user_questions_enabled=False)
    for scope in ("supervisor_prepare", "supervisor_synthesize", "evaluator"):
        assert "image" not in builtin_instructions(
            scope,  # type: ignore[arg-type]
            user_questions_enabled=False,
            image_generation_enabled=True,
            image_editing_enabled=True,
        )


def test_tool_context_carries_only_the_validated_id_and_size() -> None:
    context = image_tool_context(
        "generate_image",
        {"imageId": GENERATED, "contentType": "image/png", "width": 1024, "height": 1024},
    )
    assert context["image_ids"] == [GENERATED]
    assert json.loads(context["text"]) == {
        "imageId": GENERATED,
        "contentType": "image/png",
        "width": 1024,
        "height": 1024,
    }
    with pytest.raises(ValidationError):
        image_tool_context(
            "generate_image",
            {"imageId": GENERATED, "contentType": "image/png", "url": "https://cdn/a.png"},
        )
    assert image_tool_error_context("edit_image", "image_limit") == {
        "kind": "image_result",
        "tool": "edit_image",
        "image_ids": [],
        "text": '{"error":"image_limit"}',
    }


def _activities(
    responses: list[str],
    llm_requests: list[dict[str, Any]],
    image_calls: list[tuple[Any, ...]],
    events: list[tuple[str, dict[str, Any]]],
    persisted: list[tuple[Any, ...]],
    persisted_event: asyncio.Event,
    image_results: list[dict[str, Any] | str],
) -> list[Any]:
    @activity.defn(name="load_conversation_state")
    async def load_conversation_state() -> list[dict[str, Any]]:
        return []

    @activity.defn(name="list_available_skills")
    async def list_available_skills() -> list[dict[str, Any]]:
        return []

    @activity.defn(name="get_session_token")
    async def get_session_token() -> str:
        return "session-token"

    @activity.defn(name="get_conversation_temperature")
    async def get_conversation_temperature() -> float:
        return 0.7

    @activity.defn(name="llm_call")
    async def llm_call(
        request: dict[str, Any],
        turn_id: str | None = None,
        publish_events: bool = True,
        hide_tool_call_tag: bool = False,
    ) -> str:
        del turn_id, publish_events, hide_tool_call_tag
        llm_requests.append(request)
        return responses[len(llm_requests) - 1]

    @activity.defn(name="register_interaction_execution")
    async def register_interaction_execution(*args: Any) -> ExecutionContext:
        return ExecutionContext(
            turn_id=str(args[0]),
            execution_id="33333333-3333-4333-8333-333333333333",
            phase="ordinary",
            node_key=None,
        )

    @activity.defn(name="execute_image_tool")
    async def execute_image_tool(*args: Any) -> dict[str, Any]:
        image_calls.append(args)
        outcome = image_results[len(image_calls) - 1]
        if isinstance(outcome, str):
            raise ApplicationError(outcome, type=outcome, non_retryable=True)
        return outcome

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict[str, Any]) -> None:
        events.append((event_type, data))

    @activity.defn(name="persist_assistant_message")
    async def persist_assistant_message(*args: Any) -> None:
        persisted.append(args)
        persisted_event.set()

    return [
        load_conversation_state,
        list_available_skills,
        get_session_token,
        get_conversation_temperature,
        llm_call,
        register_interaction_execution,
        execute_image_tool,
        publish_event,
        persist_assistant_message,
    ]


async def _run_turn(message: dict[str, Any], **lists: Any) -> Any:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="image-tools",
            workflows=[ConversationWorkflow],
            activities=_activities(**lists),
        ):
            handle = await env.client.start_workflow(
                ConversationWorkflow.run,
                ConversationInput(conversation_id="conversation-images"),
                id="conversation-images",
                task_queue="image-tools",
            )
            await handle.signal(ConversationWorkflow.user_message, message)
            await asyncio.wait_for(lists["persisted_event"].wait(), timeout=10)
            await env.sleep(timedelta(seconds=settings.idle_ttl_seconds + 1))
            return await handle.result()


def _lists(responses: list[str], image_results: list[dict[str, Any] | str]) -> dict[str, Any]:
    return {
        "responses": responses,
        "llm_requests": [],
        "image_calls": [],
        "events": [],
        "persisted": [],
        "persisted_event": asyncio.Event(),
        "image_results": image_results,
    }


@pytest.mark.asyncio
async def test_generate_then_edit_persists_both_images(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_enabled", False)
    lists = _lists(
        [
            '<tool_call>{"tool":"generate_image","args":{"prompt":"a red lighthouse at dusk"}}</tool_call>',
            f'<tool_call>{{"tool":"edit_image","args":{{"image_id":"{GENERATED}","prompt":"add snow"}}}}</tool_call>',
            "Here is the lighthouse, now covered in snow.",
        ],
        [
            {"kind": "image_result", "tool": "generate_image", "image_ids": [GENERATED], "text": "{}"},
            {"kind": "image_result", "tool": "edit_image", "image_ids": [EDITED], "text": "{}"},
        ],
    )
    result = await _run_turn(
        {
            "role": "user",
            "content": "Draw a lighthouse, then add snow",
            "turn_id": "44444444-4444-4444-8444-444444444444",
            "model": "chat-model",
            "image_generation_enabled": True,
            "image_editing_enabled": True,
        },
        **lists,
    )

    assert result == "Here is the lighthouse, now covered in snow."
    system_prompt = lists["llm_requests"][0]["messages"][0]["content"]
    assert "generate_image" in system_prompt and "edit_image" in system_prompt
    assert [call[2] for call in lists["image_calls"]] == ["generate_image", "edit_image"]
    assert lists["image_calls"][1][3] == {"prompt": "add snow", "size": "auto", "image_id": GENERATED}
    assert lists["persisted"][0][4]["__images__"] == {"imageIds": [GENERATED, EDITED]}
    progress = json.dumps(
        [data for kind, data in lists["events"] if kind in {"tool-calls", "tool-result"}]
    )
    assert "Generating image" in progress and "Editing image" in progress
    assert "lighthouse" not in progress, "prompts never reach the progress stream"


@pytest.mark.asyncio
async def test_failed_generation_is_reported_to_the_model_not_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_enabled", False)
    lists = _lists(
        [
            '<tool_call>{"tool":"generate_image","args":{"prompt":"x"}}</tool_call>',
            "I could not create the image.",
        ],
        ["image_rejected"],
    )
    await _run_turn(
        {
            "role": "user",
            "content": "draw",
            "turn_id": "44444444-4444-4444-8444-444444444444",
            "model": "chat-model",
            "image_generation_enabled": True,
        },
        **lists,
    )
    assert "image_rejected" in lists["llm_requests"][1]["messages"][-1]["content"]
    tool_results = lists["persisted"][0][4] or {}
    assert "__images__" not in tool_results


@pytest.mark.asyncio
async def test_disabled_images_never_dispatch_a_hallucinated_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without image tools a generate_image call stays an unknown built-in, as
    before image tools existed: the turn fails and nothing is dispatched."""
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_enabled", False)
    hallucination = '<tool_call>{"tool":"generate_image","args":{"prompt":"x"}}</tool_call>'
    lists = _lists([hallucination], [])
    failed = asyncio.Event()

    @activity.defn(name="fail_interaction_turn")
    async def fail_interaction_turn(turn_id: str, reason: str | None = None) -> None:
        failed.set()

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="image-tools-disabled",
            workflows=[ConversationWorkflow],
            activities=[*_activities(**lists), fail_interaction_turn],
        ):
            handle = await env.client.start_workflow(
                ConversationWorkflow.run,
                ConversationInput(conversation_id="conversation-images-off"),
                id="conversation-images-off",
                task_queue="image-tools-disabled",
            )
            await handle.signal(
                ConversationWorkflow.user_message,
                {
                    "role": "user",
                    "content": "draw",
                    "turn_id": "44444444-4444-4444-8444-444444444444",
                    "model": "chat-model",
                    "user_questions_enabled": True,
                },
            )
            await asyncio.wait_for(failed.wait(), timeout=10)

    assert lists["image_calls"] == [] and lists["persisted"] == []
    assert "generate_image" not in lists["llm_requests"][0]["messages"][0]["content"]


def _http_client(response: Any) -> Any:
    from unittest.mock import AsyncMock, MagicMock

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.request = AsyncMock(return_value=response)
    return client


@pytest.mark.asyncio
async def test_image_activity_posts_to_the_runner_only_route(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock, patch

    from src.workflows.activities import execute_image_tool

    monkeypatch.setattr("src.workflows.activities.settings.llm_gateway_url", "http://backend:8000")
    monkeypatch.setattr("src.workflows.activities.settings.session_token", "session-token")
    response = MagicMock(is_error=False)
    response.json.return_value = {
        "imageId": GENERATED,
        "contentType": "image/webp",
        "width": 1024,
        "height": 768,
    }
    client = _http_client(response)
    context = ExecutionContext(turn_id="turn-1", execution_id="execution-1", phase="ordinary", node_key=None)
    with patch("httpx.AsyncClient", return_value=client):
        result = await execute_image_tool(
            context, "00000000-0000-0000-0000-000000000002", "generate_image", {"prompt": "x", "size": "auto"}
        )

    assert result["image_ids"] == [GENERATED]
    client.request.assert_awaited_once_with(
        "POST",
        "http://backend:8000/v1/internal/turns/turn-1/executions/"
        "execution-1/image-operations/00000000-0000-0000-0000-000000000002",
        headers={"Authorization": "Bearer session-token"},
        json={"tool": "generate_image", "args": {"prompt": "x", "size": "auto"}},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"detail": {"code": "image_limit"}, "internal": "secret"}, "image_limit"),
        ({"detail": {"code": "image_quota_exceeded"}}, "image_quota_exceeded"),
        ({"detail": {"code": "something_else"}}, "image_unavailable"),
        ({"detail": "Interaction not found"}, "image_unavailable"),
        # business-backend's own error body around the same detail.
        ({"error": {"code": "FORBIDDEN", "message": {"code": "image_limit"}, "details": {}}}, "image_limit"),
        ({"error": {"code": "FORBIDDEN", "message": {"code": "something_else"}}}, "image_unavailable"),
        ({"error": {"code": "FORBIDDEN", "message": "Interaction not found"}}, "image_unavailable"),
    ],
)
async def test_image_activity_errors_are_stable_and_non_retryable(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], expected: str
) -> None:
    from unittest.mock import MagicMock, patch

    from src.workflows.activities import execute_image_tool

    monkeypatch.setattr("src.workflows.activities.settings.llm_gateway_url", "http://backend:8000")
    response = MagicMock(is_error=True)
    response.json.return_value = payload
    context = ExecutionContext(turn_id="t", execution_id="e", phase="ordinary", node_key=None)
    with patch("httpx.AsyncClient", return_value=_http_client(response)):
        with pytest.raises(ApplicationError) as caught:
            await execute_image_tool(context, "i", "generate_image", {"prompt": "x", "size": "auto"})
    assert caught.value.type == expected and caught.value.non_retryable
    assert "secret" not in str(caught.value)


def test_recent_images_are_listed_only_for_editing_and_sanitized() -> None:
    from src.workflows.builtin_tools import recent_image_list

    raw = [
        {"id": GENERATED, "prompt": "a red lighthouse" + "!" * 300},
        {"id": "not-a-uuid", "prompt": "dropped"},
        "garbage",
        *({"id": EDITED, "prompt": "p"} for _ in range(10)),
    ]
    listed = recent_image_list(raw)
    assert listed[0] == {"image_id": GENERATED, "prompt": ("a red lighthouse" + "!" * 300)[:200]}
    assert all(item["image_id"] != "not-a-uuid" for item in listed) and len(listed) <= 8
    uploaded = recent_image_list([{"id": GENERATED, "prompt": None, "origin": "uploaded"}])
    assert uploaded == [{"image_id": GENERATED, "prompt": "", "source": "attached by the user"}]
    with_list = builtin_instructions(
        "ordinary", user_questions_enabled=False, image_editing_enabled=True, recent_images=raw
    )
    assert GENERATED in with_list
    generation_only = builtin_instructions(
        "ordinary", user_questions_enabled=False, image_generation_enabled=True, recent_images=raw
    )
    assert GENERATED not in generation_only


@pytest.mark.asyncio
async def test_a_later_turn_edits_an_image_from_the_conversation_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_enabled", False)
    lists = _lists(
        [
            f'<tool_call>{{"tool":"edit_image","args":{{"image_id":"{GENERATED}","prompt":"make it blue"}}}}</tool_call>',
            "Done, it is blue now.",
        ],
        [{"kind": "image_result", "tool": "edit_image", "image_ids": [EDITED], "text": "{}"}],
    )
    await _run_turn(
        {
            "role": "user",
            "content": "Now make it blue",
            "turn_id": "44444444-4444-4444-8444-444444444444",
            "model": "chat-model",
            "image_generation_enabled": True,
            "image_editing_enabled": True,
            "recent_images": [{"id": GENERATED, "prompt": "a red lighthouse"}],
        },
        **lists,
    )
    system_prompt = lists["llm_requests"][0]["messages"][0]["content"]
    assert GENERATED in system_prompt and "a red lighthouse" in system_prompt
    assert lists["image_calls"][0][3]["image_id"] == GENERATED
    assert lists["persisted"][0][4]["__images__"] == {"imageIds": [EDITED]}
