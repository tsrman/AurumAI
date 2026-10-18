import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from temporalio.exceptions import ApplicationError

from src.workflows.activities import (
    _LLMGatewayError,
    _raise_workflow_api_error,
    authorize_workflow_step,
    llm_call,
    load_conversation_state,
    list_available_skills,
    load_workflow_run_snapshot,
    get_session_token,
    persist_assistant_message,
    persist_workflow_node_result,
    publish_event,
    record_langfuse_score,
    refresh_runner_activity,
    transition_workflow_run,
)


def _workflow_api_client(data: object) -> tuple[MagicMock, MagicMock]:
    response = MagicMock()
    response.is_error = False
    response.content = json.dumps({"data": data}).encode()
    response.json.return_value = {"data": data}
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.request = AsyncMock(return_value=response)
    return client, response


def _empty_workflow_api_client() -> tuple[MagicMock, MagicMock]:
    # A real 204 response (touch-activity) has an empty body — httpx raises
    # json.JSONDecodeError from response.json() in that case, so .json is
    # deliberately left unconfigured: the test asserts it's never called.
    response = MagicMock()
    response.is_error = False
    response.content = b""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.request = AsyncMock(return_value=response)
    return client, response


@pytest.mark.asyncio
async def test_load_workflow_run_snapshot_uses_internal_session_endpoint(monkeypatch):
    monkeypatch.setattr("src.workflows.activities.settings.llm_gateway_url", "http://backend:8000")
    monkeypatch.setattr("src.workflows.activities.settings.session_token", "session-token")
    client, _ = _workflow_api_client({"runId": "run-1"})

    with patch("httpx.AsyncClient", return_value=client):
        result = await load_workflow_run_snapshot("run-1")

    assert result == {"runId": "run-1"}
    client.request.assert_awaited_once_with(
        "GET",
        "http://backend:8000/v1/internal/workflow-runs/run-1/snapshot",
        headers={"Authorization": "Bearer session-token"},
        json=None,
    )


@pytest.mark.asyncio
async def test_refresh_runner_activity_hits_the_touch_activity_endpoint(monkeypatch):
    # touch-activity is a real 204 No Content endpoint (see
    # workflow_runs_internal.py) — regression coverage for a bug live
    # verification caught: _workflow_api_request used to call
    # response.json() unconditionally, which raises on an empty body and
    # failed every confirmation-gated node before it ever reached the
    # human wait.
    monkeypatch.setattr("src.workflows.activities.settings.llm_gateway_url", "http://backend:8000")
    monkeypatch.setattr("src.workflows.activities.settings.session_token", "session-token")
    client, response = _empty_workflow_api_client()

    with patch("httpx.AsyncClient", return_value=client):
        await refresh_runner_activity("run-1")

    client.request.assert_awaited_once_with(
        "POST",
        "http://backend:8000/v1/internal/workflow-runs/run-1/touch-activity",
        headers={"Authorization": "Bearer session-token"},
        json=None,
    )
    response.json.assert_not_called()


@pytest.mark.asyncio
async def test_workflow_mutation_activities_send_camel_case_contract(monkeypatch):
    monkeypatch.setattr("src.workflows.activities.settings.llm_gateway_url", "http://backend:8000")
    client, _ = _workflow_api_client({"status": "ok"})

    with patch("httpx.AsyncClient", return_value=client):
        await authorize_workflow_step("run-1", "node", "research")
        await transition_workflow_run("run-1", "failed", "access_revoked")
        await persist_workflow_node_result(
            "run-1", "research", "failed", None, "access_revoked"
        )

    assert client.request.await_args_list[0].kwargs["json"] == {
        "phase": "node",
        "nodeKey": "research",
    }
    assert client.request.await_args_list[1].kwargs["json"] == {
        "status": "failed",
        "errorCode": "access_revoked",
    }
    assert client.request.await_args_list[2].kwargs["json"] == {
        "status": "failed",
        "output": None,
        "errorCode": "access_revoked",
    }


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (404, "access_revoked"),
        (413, "output_limit_exceeded"),
        (429, "quota_exhausted"),
        (409, "workflow_unavailable"),
    ],
)
def test_workflow_api_errors_are_stable_non_retryable(
    status_code: int, error_type: str
) -> None:
    response = httpx.Response(
        status_code, request=httpx.Request("POST", "http://backend/internal")
    )

    with pytest.raises(ApplicationError) as exc_info:
        _raise_workflow_api_error(response)

    assert exc_info.value.type == error_type
    assert exc_info.value.non_retryable is True


@pytest.fixture(autouse=True)
def configure_settings(monkeypatch):
    monkeypatch.setattr("src.workflows.activities.settings.conversation_id", "conv-1")


@pytest.fixture(autouse=True)
def mock_langfuse(monkeypatch):
    monkeypatch.setattr("src.workflows.activities.get_langfuse", MagicMock(return_value=None))


@pytest.mark.asyncio
async def test_llm_call_aggregates_deltas_and_publishes_events():
    deltas = ["Hello, ", "world", "!"]
    chunks = [
        json.dumps({"choices": [{"delta": {"content": delta}}]})
        for delta in deltas
    ]

    async def _stream(request):
        for chunk in chunks:
            yield chunk

    mock_llm_client = MagicMock()
    mock_llm_client.stream_chat = _stream
    mock_publisher = AsyncMock()

    with patch("src.workflows.activities.llm_client", mock_llm_client), patch(
        "src.workflows.activities.event_publisher.publish", mock_publisher
    ):
        result = await llm_call({"model": "gpt-4", "messages": []}, turn_id="turn-1")

    assert result == "Hello, world!"
    assert mock_publisher.await_count == 3
    for delta in deltas:
        mock_publisher.assert_any_await(
            "content-delta", {"delta": delta, "turn_id": "turn-1"}
        )


@pytest.mark.asyncio
async def test_llm_call_ignores_done_and_malformed_chunks():
    chunks = [
        "",
        "[DONE]",
        "not-json",
        json.dumps({"choices": [{"delta": {"content": "ok"}}]}),
        json.dumps({"choices": [{}]}),
        json.dumps({"missing": "data"}),
    ]

    async def _stream(request):
        for chunk in chunks:
            yield chunk

    mock_llm_client = MagicMock()
    mock_llm_client.stream_chat = _stream
    mock_publisher = AsyncMock()

    with patch("src.workflows.activities.llm_client", mock_llm_client), patch(
        "src.workflows.activities.event_publisher.publish", mock_publisher
    ):
        result = await llm_call({"model": "gpt-4", "messages": []}, turn_id="turn-2")

    assert result == "ok"
    mock_publisher.assert_awaited_once_with(
        "content-delta", {"delta": "ok", "turn_id": "turn-2"}
    )


@pytest.mark.asyncio
async def test_llm_call_raises_on_gateway_error_chunk_instead_of_treating_it_as_empty():
    """Regression test: business-backend's
    /v1/chat/completions yields {"error": {...}} when the real gateway
    fails mid-stream — the old bare "no choices -> continue" silently
    treated this as an empty completion instead of a real failure the
    activity's retry_policy (H7) could act on."""
    chunks = [
        json.dumps({"choices": [{"delta": {"content": "partial"}}]}),
        json.dumps({"error": {"message": "gateway returned 502"}}),
    ]

    async def _stream(request):
        for chunk in chunks:
            yield chunk

    mock_llm_client = MagicMock()
    mock_llm_client.stream_chat = _stream
    mock_publisher = AsyncMock()

    with patch("src.workflows.activities.llm_client", mock_llm_client), patch(
        "src.workflows.activities.event_publisher.publish", mock_publisher
    ):
        with pytest.raises(_LLMGatewayError, match="gateway returned 502"):
            await llm_call({"model": "gpt-4", "messages": []}, turn_id="turn-3")


async def _run_llm_call(chunks: list[str], **kwargs) -> tuple[str, list]:
    async def _stream(request):
        for chunk in chunks:
            yield chunk

    mock_llm_client = MagicMock()
    mock_llm_client.stream_chat = _stream
    calls: list[tuple[str, dict]] = []

    async def _record_publish(event_type: str, data: dict) -> None:
        calls.append((event_type, data))

    with patch("src.workflows.activities.llm_client", mock_llm_client), patch(
        "src.workflows.activities.event_publisher.publish", _record_publish
    ):
        result = await llm_call({"model": "gpt-4", "messages": []}, **kwargs)
    return result, calls


@pytest.mark.asyncio
async def test_llm_call_publish_events_false_suppresses_everything():
    # publish_events=False is for internal, non-chat-facing calls
    # (EvaluationWorkflow's judge call, CodeSkillSessionWorkflow's
    # sub-loop) — neither reasoning nor content should ever reach the
    # conversation's SSE stream, regardless of hide_tool_call_tag.
    chunks = [
        json.dumps({"choices": [{"delta": {"reasoning": "thinking...", "content": ""}}]}),
        json.dumps({"choices": [{"delta": {"content": "answer"}}]}),
    ]

    result, calls = await _run_llm_call(chunks, turn_id=None, publish_events=False)

    assert result == "answer"
    assert calls == []


@pytest.mark.asyncio
async def test_llm_call_adds_workflow_correlation_only_to_langfuse_metadata():
    async def _stream(request):
        yield json.dumps({"choices": [{"delta": {"content": "answer"}}]})

    mock_llm_client = MagicMock()
    mock_llm_client.stream_chat = _stream
    generation = MagicMock()
    langfuse = MagicMock()
    langfuse.generation.return_value = generation
    request = {
        "model": "provider/model",
        "messages": [{"role": "user", "content": "secret"}],
        "stream": True,
        "workflow_run_id": "run-1",
        "workflow_phase": "node",
        "workflow_node_key": "research",
    }

    with patch("src.workflows.activities.llm_client", mock_llm_client), patch(
        "src.workflows.activities.get_langfuse", return_value=langfuse
    ):
        await llm_call(request, publish_events=False)

    metadata = langfuse.generation.call_args.kwargs["metadata"]
    assert metadata == {
        "workflow_run_id": "run-1",
        "workflow_phase": "node",
        "workflow_node_key": "research",
    }


@pytest.mark.asyncio
async def test_llm_call_hide_tool_call_tag_still_streams_reasoning():
    # hide_tool_call_tag is the skills planning call — it hides the raw
    # <tool_call>{...}</tool_call> content from the user mid-generation, but
    # reasoning tokens are a separate phase that never contains that syntax,
    # so they should still stream live.
    chunks = [
        json.dumps({"choices": [{"delta": {"reasoning": "thinking...", "content": ""}}]}),
    ]

    result, calls = await _run_llm_call(
        chunks, turn_id=None, publish_events=True, hide_tool_call_tag=True
    )

    assert result == ""
    assert calls == [("thinking-delta", {"delta": "thinking...", "turn_id": None})]


@pytest.mark.asyncio
async def test_llm_call_hide_tool_call_tag_streams_plain_content_live():
    # No <tool_call> tag ever appears — the answer streams incrementally
    # (not buffered until the end), same as hide_tool_call_tag=False would.
    deltas = ["Hello, ", "world", "!"]
    chunks = [json.dumps({"choices": [{"delta": {"content": d}}]}) for d in deltas]

    result, calls = await _run_llm_call(
        chunks, turn_id="t1", publish_events=True, hide_tool_call_tag=True
    )

    assert result == "Hello, world!"
    content_calls = [c for c in calls if c[0] == "content-delta"]
    assert content_calls == [
        ("content-delta", {"delta": "Hello, ", "turn_id": "t1"}),
        ("content-delta", {"delta": "world", "turn_id": "t1"}),
        ("content-delta", {"delta": "!", "turn_id": "t1"}),
    ]


@pytest.mark.asyncio
async def test_llm_call_hide_tool_call_tag_holds_back_and_suppresses_the_tag():
    # Prose before the tag streams live; the tag itself (and anything after)
    # never reaches the user — the outer workflow replaces it with a tool
    # synthesis response instead.
    deltas = ["Let me check. ", "<tool_call>", '{"skill_id": "s1"}', "</tool_call>"]
    chunks = [json.dumps({"choices": [{"delta": {"content": d}}]}) for d in deltas]

    result, calls = await _run_llm_call(
        chunks, turn_id="t1", publish_events=True, hide_tool_call_tag=True
    )

    assert result == 'Let me check. <tool_call>{"skill_id": "s1"}</tool_call>'
    content_calls = [c for c in calls if c[0] == "content-delta"]
    assert content_calls == [("content-delta", {"delta": "Let me check. ", "turn_id": "t1"})]


@pytest.mark.asyncio
async def test_llm_call_hide_tool_call_tag_never_flashes_a_partial_tag():
    # The tag arrives split across chunk boundaries — the holdback buffer
    # must never publish a partial "<tool_c" fragment while waiting to see
    # whether it completes into the real tag.
    deltas = ["safe text", "<tool", "_call>", "hidden"]
    chunks = [json.dumps({"choices": [{"delta": {"content": d}}]}) for d in deltas]

    result, calls = await _run_llm_call(
        chunks, turn_id="t1", publish_events=True, hide_tool_call_tag=True
    )

    assert result == "safe text<tool_call>hidden"
    content_calls = [c for c in calls if c[0] == "content-delta"]
    assert content_calls == [("content-delta", {"delta": "safe text", "turn_id": "t1"})]


@pytest.mark.asyncio
async def test_llm_call_hide_tool_call_tag_flushes_a_holdback_that_never_became_a_tag():
    # A trailing "<tool_c" that turns out to be a false alarm (never
    # completes into the real tag) must still reach the user once the
    # stream ends.
    deltas = ["hi <tool_ca", "sual conversation"]
    chunks = [json.dumps({"choices": [{"delta": {"content": d}}]}) for d in deltas]

    result, calls = await _run_llm_call(
        chunks, turn_id="t1", publish_events=True, hide_tool_call_tag=True
    )

    assert result == "hi <tool_casual conversation"
    content_calls = [c for c in calls if c[0] == "content-delta"]
    assert content_calls == [
        ("content-delta", {"delta": "hi ", "turn_id": "t1"}),
        ("content-delta", {"delta": "<tool_casual conversation", "turn_id": "t1"}),
    ]


@pytest.mark.asyncio
async def test_load_conversation_state_returns_messages_after_load():
    expected_messages = [{"role": "user", "content": "hi"}]
    mock_state_manager = MagicMock()
    mock_state_manager.load = AsyncMock()
    mock_state_manager.messages = expected_messages

    with patch("src.workflows.activities.state_manager", mock_state_manager):
        result = await load_conversation_state()

    mock_state_manager.load.assert_awaited_once()
    assert result == expected_messages


@pytest.mark.asyncio
async def test_get_session_token_returns_current_settings_value(monkeypatch):
    # This activity exists specifically so workflow code (which can't reliably
    # see settings mutated by POST /bind at runtime) can fetch the live token.
    monkeypatch.setattr("src.workflows.activities.settings.session_token", "live-token")

    assert await get_session_token() == "live-token"


@pytest.mark.asyncio
async def test_list_available_skills_returns_data_from_backend(monkeypatch):
    monkeypatch.setattr("src.workflows.activities.settings.llm_gateway_url", "http://backend:8000")
    monkeypatch.setattr("src.workflows.activities.settings.session_token", "session-token")
    monkeypatch.setattr("src.workflows.activities.settings.conversation_id", "conv-1")

    expected_skills = [{"id": "skill-1", "name": "echo", "type": "mcp"}]
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value={"data": expected_skills})

    client = MagicMock()
    client.__aenter__.return_value = client
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=response)

    with patch("httpx.AsyncClient", return_value=client):
        result = await list_available_skills()

    client.get.assert_awaited_once_with(
        "http://backend:8000/v1/skills/",
        headers={"Authorization": "Bearer session-token"},
        params={"conversation_id": "conv-1"},
    )
    assert result == expected_skills


@pytest.mark.asyncio
async def test_list_available_skills_returns_empty_list_when_no_data(monkeypatch):
    monkeypatch.setattr("src.workflows.activities.settings.llm_gateway_url", "http://backend:8000")
    monkeypatch.setattr("src.workflows.activities.settings.session_token", "session-token")
    monkeypatch.setattr("src.workflows.activities.settings.conversation_id", "conv-1")

    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value={})

    client = MagicMock()
    client.__aenter__.return_value = client
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=response)

    with patch("httpx.AsyncClient", return_value=client):
        result = await list_available_skills()

    assert result == []


@pytest.mark.asyncio
async def test_publish_event_calls_event_publisher_with_correct_args():
    mock_publisher = AsyncMock()

    with patch("src.workflows.activities.event_publisher.publish", mock_publisher):
        await publish_event("agent-message", {"text": "hello"})

    mock_publisher.assert_awaited_once_with("agent-message", {"text": "hello"})


@pytest.mark.asyncio
async def test_persist_assistant_message_posts_to_backend(monkeypatch):
    monkeypatch.setattr("src.workflows.activities.settings.conversation_id", "conv-1")
    monkeypatch.setattr("src.workflows.activities.settings.llm_gateway_url", "http://backend:8000")
    monkeypatch.setattr("src.workflows.activities.settings.session_token", "session-token")

    response = MagicMock()
    response.raise_for_status = MagicMock()

    client = MagicMock()
    client.__aenter__.return_value = client
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=response)

    with patch("httpx.AsyncClient", return_value=client):
        await persist_assistant_message("hello", "gpt-4o-mini", turn_id="turn-3")

    client.post.assert_awaited_once_with(
        "http://backend:8000/v1/conversations/conv-1/messages",
        headers={"Authorization": "Bearer session-token"},
        json={
            "role": "assistant",
            "content": "hello",
            "model": "gpt-4o-mini",
            "turn_id": "turn-3",
            "tool_calls": None,
            "tool_results": None,
        },
    )
    response.raise_for_status.assert_called_once()


@pytest.mark.asyncio
async def test_persist_assistant_message_posts_tool_calls_and_results(monkeypatch):
    monkeypatch.setattr("src.workflows.activities.settings.conversation_id", "conv-1")
    monkeypatch.setattr("src.workflows.activities.settings.llm_gateway_url", "http://backend:8000")
    monkeypatch.setattr("src.workflows.activities.settings.session_token", "session-token")

    response = MagicMock()
    response.raise_for_status = MagicMock()

    client = MagicMock()
    client.__aenter__.return_value = client
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=response)

    tool_calls = [{"call_id": "call-1", "skill_id": "search", "args": {"q": "docs"}}]
    tool_results = {"call-1": {"skill_id": "search", "output": {"found": "yes"}}}

    with patch("httpx.AsyncClient", return_value=client):
        await persist_assistant_message(
            "hello",
            "gpt-4o-mini",
            turn_id="turn-3",
            tool_calls=tool_calls,
            tool_results=tool_results,
        )

    client.post.assert_awaited_once_with(
        "http://backend:8000/v1/conversations/conv-1/messages",
        headers={"Authorization": "Bearer session-token"},
        json={
            "role": "assistant",
            "content": "hello",
            "model": "gpt-4o-mini",
            "turn_id": "turn-3",
            "tool_calls": tool_calls,
            "tool_results": tool_results,
        },
    )
    response.raise_for_status.assert_called_once()


@pytest.mark.asyncio
async def test_record_langfuse_score_is_noop_when_langfuse_disabled():
    # The autouse mock_langfuse fixture makes get_langfuse() return None.
    await record_langfuse_score("conv-1", "faithfulness", 1.0, "looks good")


@pytest.mark.asyncio
async def test_record_langfuse_score_calls_score_and_flush():
    mock_lf = MagicMock()

    with patch("src.workflows.activities.get_langfuse", MagicMock(return_value=mock_lf)):
        await record_langfuse_score("conv-1", "faithfulness", 1.0, "looks good")

    mock_lf.score.assert_called_once_with(
        trace_id="conv-1", name="faithfulness", value=1.0, comment="looks good"
    )
    mock_lf.flush.assert_called_once()


@pytest.mark.asyncio
async def test_record_langfuse_score_swallows_langfuse_failure():
    mock_lf = MagicMock()
    mock_lf.score.side_effect = RuntimeError("langfuse is down")

    with patch("src.workflows.activities.get_langfuse", MagicMock(return_value=mock_lf)):
        await record_langfuse_score("conv-1", "faithfulness", 0.0, "bad")  # must not raise

    mock_lf.flush.assert_not_called()
