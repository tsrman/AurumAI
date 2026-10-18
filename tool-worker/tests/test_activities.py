from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from temporalio.exceptions import ApplicationError

from src import activities
from src.config import settings


@pytest.fixture(autouse=True)
def _set_session_token() -> None:
    """Ensure settings.session_token is set for tests that require auth headers."""
    original = settings.session_token
    settings.session_token = "test-session-token"
    yield
    settings.session_token = original


@pytest.fixture
def mock_trace() -> MagicMock:
    """Return a stand-in Langfuse trace that records spans and updates."""
    trace = MagicMock()
    span = MagicMock()
    span.end = MagicMock()
    trace.span.return_value = span
    trace.update = MagicMock()
    return trace


def _make_streamed_response(body: bytes) -> MagicMock:
    """A response whose .aiter_bytes() yields `body` in one chunk, matching
    the streaming client.stream(...) usage in the mcp_endpoint tool branch."""
    response = MagicMock()
    response.raise_for_status = MagicMock()

    async def _aiter_bytes():
        yield body

    response.aiter_bytes = _aiter_bytes
    return response


def _make_stream_client(response: MagicMock) -> MagicMock:
    """A client whose .stream(...) is an async context manager yielding `response`."""
    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=response)
    stream_cm.__aexit__ = AsyncMock(return_value=False)

    client = MagicMock()
    client.stream = MagicMock(return_value=stream_cm)
    client.__aenter__.return_value = client
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.fixture
def mock_httpx_client() -> MagicMock:
    """Return a ready-to-patch httpx.AsyncClient mock for the tool POST path."""
    response = _make_streamed_response(b'{"status": "ok"}')
    return _make_stream_client(response)


@pytest.mark.asyncio
async def test_execute_skill_mcp_calls_call_server(mock_trace: MagicMock) -> None:
    """execute_skill dispatches an mcp skill to MCPClient.call_server."""
    skill = {
        "id": "skill-mcp",
        "org_id": None,
        "type": "mcp",
        "mcp_transport": "stdio",
        "mcp_endpoint": "python tool.py",
        "activity_name": "search",
    }
    call_server_output = {"result": "found"}

    with (
        patch("src.activities.get_or_create_trace", return_value=mock_trace),
        patch("src.activities.fetch_skill", new_callable=AsyncMock, return_value=skill),
        patch(
            "src.activities.MCPClient.call_server",
            new_callable=AsyncMock,
            return_value=call_server_output,
        ) as mock_call_server,
    ):
        result = await activities.execute_skill("skill-mcp", {"q": "hello"}, "conv-1")

    assert result == {"skill_id": "skill-mcp", "output": call_server_output}
    mock_call_server.assert_awaited_once_with(
        transport="stdio",
        endpoint="python tool.py",
        tool_name="search",
        args={"q": "hello"},
        auth_token=None,
    )


@pytest.mark.asyncio
async def test_execute_skill_mcp_falls_back_to_stdio_when_transport_is_explicit_none(
    mock_trace: MagicMock,
) -> None:
    """business-backend's API response always includes
    mcp_transport as a key (possibly null) — skill.get("mcp_transport",
    "stdio") never applies its default for an explicit None, only a
    missing key, so a validly-created type=mcp skill with no transport set
    must still resolve to "stdio", not None."""
    skill = {
        "id": "skill-mcp",
        "org_id": None,
        "type": "mcp",
        "mcp_transport": None,
        "mcp_endpoint": "python tool.py",
        "activity_name": "search",
    }

    with (
        patch("src.activities.get_or_create_trace", return_value=mock_trace),
        patch("src.activities.fetch_skill", new_callable=AsyncMock, return_value=skill),
        patch(
            "src.activities.MCPClient.call_server",
            new_callable=AsyncMock,
            return_value={"result": "found"},
        ) as mock_call_server,
    ):
        await activities.execute_skill("skill-mcp", {"q": "hello"}, "conv-1")

    assert mock_call_server.call_args.kwargs["transport"] == "stdio"


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership", [{"org_id": "tenant"}, {}, {"org_id": ""}])
@pytest.mark.parametrize("transport", ["stdio", None, ""])
async def test_untrusted_stdio_never_dispatches(ownership, transport) -> None:
    skill = {
        "id": "tenant-stdio", "type": "mcp", "mcp_transport": transport,
        "mcp_endpoint": "python tenant.py", **ownership,
    }
    with (
        patch("src.activities.get_or_create_trace", return_value=None),
        patch("src.activities.fetch_skill", AsyncMock(return_value=skill)),
        patch("src.activities.MCPClient.call_server", AsyncMock(return_value={})) as dispatch,
    ):
        with pytest.raises(ApplicationError, match="operator") as raised:
            await activities.execute_skill("tenant-stdio", {})
    assert raised.value.non_retryable
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_skill_mcp_passes_auth_token_when_skill_has_one(mock_trace: MagicMock) -> None:
    skill = {
        "id": "skill-mcp",
        "type": "mcp",
        "mcp_transport": "sse",
        "mcp_endpoint": "http://mcp-everything:3001/sse",
        "activity_name": "get-sum",
        "mcp_auth_token": "secret-token",
    }

    with (
        patch("src.activities.get_or_create_trace", return_value=mock_trace),
        patch("src.activities.fetch_skill", new_callable=AsyncMock, return_value=skill),
        patch(
            "src.activities.MCPClient.call_server",
            new_callable=AsyncMock,
            return_value={"result": 42},
        ) as mock_call_server,
    ):
        await activities.execute_skill("skill-mcp", {"a": 1, "b": 2}, "conv-1")

    mock_call_server.assert_awaited_once_with(
        transport="sse",
        endpoint="http://mcp-everything:3001/sse",
        tool_name="get-sum",
        args={"a": 1, "b": 2},
        auth_token="secret-token",
    )


@pytest.mark.asyncio
async def test_execute_skill_passes_session_token_to_fetch_skill(mock_trace: MagicMock) -> None:
    """execute_skill forwards the calling conversation's session_token to fetch_skill."""
    skill = {
        "id": "skill-mcp",
        "type": "mcp",
        "mcp_transport": "sse",
        "mcp_endpoint": "http://mcp-everything:3001/sse",
        "activity_name": "echo",
    }

    with (
        patch("src.activities.get_or_create_trace", return_value=mock_trace),
        patch(
            "src.activities.fetch_skill", new_callable=AsyncMock, return_value=skill
        ) as mock_fetch_skill,
        patch(
            "src.activities.MCPClient.call_server",
            new_callable=AsyncMock,
            return_value={"result": "Echo: hi"},
        ),
    ):
        await activities.execute_skill(
            "skill-mcp", {"message": "hi"}, "conv-1", session_token="conv-session-token"
        )

    mock_fetch_skill.assert_awaited_once_with("skill-mcp", session_token="conv-session-token")


@pytest.mark.asyncio
async def test_execute_skill_tool_with_activity_name_returns_placeholder(
    mock_trace: MagicMock,
) -> None:
    """execute_skill returns a placeholder for tool skills with an activity_name."""
    skill = {
        "id": "skill-tool",
        "type": "tool",
        "activity_name": "do_thing",
    }

    with (
        patch("src.activities.get_or_create_trace", return_value=mock_trace),
        patch("src.activities.fetch_skill", new_callable=AsyncMock, return_value=skill),
    ):
        result = await activities.execute_skill("skill-tool", {"x": 1})

    assert result == {
        "skill_id": "skill-tool",
        "output": {
            "placeholder": True,
            "activity_name": "do_thing",
            "args": {"x": 1},
        },
    }


@pytest.mark.asyncio
async def test_execute_skill_hybrid_search_calls_rag_client(
    mock_trace: MagicMock,
) -> None:
    skill = {
        "id": "skill-hybrid",
        "type": "tool",
        "activity_name": "execute_hybrid_search",
    }

    with (
        patch("src.activities.get_or_create_trace", return_value=mock_trace),
        patch("src.activities.fetch_skill", new_callable=AsyncMock, return_value=skill),
        patch("src.activities.rag_client.search", new_callable=AsyncMock) as mock_search,
    ):
        mock_search.return_value = {"results": [], "warnings": []}
        result = await activities.execute_skill(
            "skill-hybrid",
            {"query": "test", "knowledge_base_id": "kb-1"},
            "conv-2",
            session_token="session-token",
        )

    assert result == {"skill_id": "skill-hybrid", "output": {"results": [], "warnings": []}}
    mock_search.assert_awaited_once_with(
        "test",
        5,
        knowledge_base_id="kb-1",
        session_token="session-token",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "session_token", "message"),
    [
        ({"query": "test"}, "token", "knowledge_base_id"),
        (
            {"query": "test", "knowledge_base_id": "kb-1"},
            None,
            "session token",
        ),
    ],
)
async def test_execute_skill_hybrid_search_requires_trusted_scope(
    mock_trace: MagicMock,
    args: dict[str, object],
    session_token: str | None,
    message: str,
) -> None:
    skill = {
        "id": "skill-hybrid",
        "type": "tool",
        "activity_name": "execute_hybrid_search",
    }

    with (
        patch("src.activities.get_or_create_trace", return_value=mock_trace),
        patch("src.activities.fetch_skill", new_callable=AsyncMock, return_value=skill),
        pytest.raises(ApplicationError, match=message),
    ):
        await activities.execute_skill(
            "skill-hybrid", args, "conv-2", session_token=session_token
        )

@pytest.mark.asyncio
async def test_execute_skill_tool_with_endpoint_posts_via_httpx(
    mock_trace: MagicMock,
    mock_httpx_client: MagicMock,
) -> None:
    """execute_skill POSTs to mcp_endpoint for tool skills without activity_name.

    No mcp_auth_token on the skill -> no Authorization header at all; in
    particular NOT the worker's own settings.session_token (the security review — that token must never be sent to an admin-configured URL)."""
    skill = {
        "id": "skill-tool-http",
        "type": "tool",
        "mcp_endpoint": "http://localhost:8000/run",
    }

    with (
        patch("src.activities.get_or_create_trace", return_value=mock_trace),
        patch("src.activities.fetch_skill", new_callable=AsyncMock, return_value=skill),
        patch("httpx.AsyncClient", return_value=mock_httpx_client),
    ):
        result = await activities.execute_skill("skill-tool-http", {"x": 1}, "conv-2")

    assert result == {"skill_id": "skill-tool-http", "output": {"status": "ok"}}
    mock_httpx_client.stream.assert_called_once_with(
        "POST",
        "http://localhost:8000/run",
        json={"tool": "default", "arguments": {"x": 1}},
        headers={},
    )


@pytest.mark.asyncio
async def test_execute_skill_tool_with_endpoint_uses_mcp_auth_token(
    mock_trace: MagicMock,
) -> None:
    """When the skill has its own mcp_auth_token, that (not the worker's
    internal session_token) is sent as the Authorization header."""
    skill = {
        "id": "skill-tool-http",
        "type": "tool",
        "mcp_endpoint": "http://localhost:8000/run",
        "mcp_auth_token": "skill-specific-token",
    }
    response = _make_streamed_response(b'{"status": "ok"}')
    client = _make_stream_client(response)

    with (
        patch("src.activities.get_or_create_trace", return_value=mock_trace),
        patch("src.activities.fetch_skill", new_callable=AsyncMock, return_value=skill),
        patch("httpx.AsyncClient", return_value=client),
    ):
        result = await activities.execute_skill("skill-tool-http", {"x": 1}, "conv-2")

    assert result == {"skill_id": "skill-tool-http", "output": {"status": "ok"}}
    client.stream.assert_called_once_with(
        "POST",
        "http://localhost:8000/run",
        json={"tool": "default", "arguments": {"x": 1}},
        headers={"Authorization": "Bearer skill-specific-token"},
    )


@pytest.mark.asyncio
async def test_execute_skill_tool_with_endpoint_rejects_oversized_response(
    mock_trace: MagicMock,
) -> None:
    """An mcp_endpoint response over the size cap fails fast (non-retryable)
    instead of the activity succeeding with a payload Temporal itself would
    later reject."""
    skill = {
        "id": "skill-tool-http",
        "type": "tool",
        "mcp_endpoint": "http://localhost:8000/run",
    }
    oversized = b"x" * (activities._MAX_MCP_HTTP_RESPONSE_BYTES + 1)
    response = _make_streamed_response(oversized)
    client = _make_stream_client(response)

    with (
        patch("src.activities.get_or_create_trace", return_value=mock_trace),
        patch("src.activities.fetch_skill", new_callable=AsyncMock, return_value=skill),
        patch("httpx.AsyncClient", return_value=client),
        pytest.raises(ApplicationError) as exc_info,
    ):
        await activities.execute_skill("skill-tool-http", {"x": 1}, "conv-2")

    assert "exceeded" in str(exc_info.value)
    assert exc_info.value.non_retryable is True


@pytest.mark.asyncio
async def test_execute_skill_unsupported_type_raises_application_error(
    mock_trace: MagicMock,
) -> None:
    """execute_skill raises a non-retryable ApplicationError for unsupported types."""
    skill = {"id": "skill-bad", "type": "unknown"}

    with (
        patch("src.activities.get_or_create_trace", return_value=mock_trace),
        patch("src.activities.fetch_skill", new_callable=AsyncMock, return_value=skill),
        pytest.raises(ApplicationError, match="Unsupported skill type: unknown"),
    ):
        await activities.execute_skill("skill-bad", {})

    mock_trace.update.assert_called_with(output={"error": "Unsupported skill type: unknown"})


@pytest.mark.asyncio
async def test_execute_skill_tool_without_activity_name_or_endpoint_raises_application_error(
    mock_trace: MagicMock,
) -> None:
    """A tool skill missing both activity_name and mcp_endpoint is a config error."""
    skill = {"id": "skill-tool-bad", "type": "tool"}

    with (
        patch("src.activities.get_or_create_trace", return_value=mock_trace),
        patch("src.activities.fetch_skill", new_callable=AsyncMock, return_value=skill),
        pytest.raises(
            ApplicationError,
            match="Tool skill skill-tool-bad has no activity_name or mcp_endpoint",
        ) as exc_info,
    ):
        await activities.execute_skill("skill-tool-bad", {})

    assert exc_info.value.non_retryable is True


@pytest.mark.asyncio
async def test_execute_skill_generic_exception_is_not_wrapped_and_stays_retryable(
    mock_trace: MagicMock,
) -> None:
    """
    Documents current behavior: unexpected (non-ValueError/KeyError) exceptions
    raised while executing a skill propagate as-is, not wrapped in a
    non-retryable ApplicationError. Temporal's default retry policy will keep
    retrying these indefinitely, unlike the config-error branch above.
    """
    skill = {
        "id": "skill-mcp-broken",
        "org_id": None,
        "type": "mcp",
        "mcp_transport": "stdio",
        "mcp_endpoint": "python tool.py",
        "activity_name": "search",
    }

    with (
        patch("src.activities.get_or_create_trace", return_value=mock_trace),
        patch("src.activities.fetch_skill", new_callable=AsyncMock, return_value=skill),
        patch(
            "src.activities.MCPClient.call_server",
            new_callable=AsyncMock,
            side_effect=RuntimeError("mcp subprocess crashed"),
        ),
        pytest.raises(RuntimeError, match="mcp subprocess crashed"),
    ):
        await activities.execute_skill("skill-mcp-broken", {})

    call_kwargs = mock_trace.update.call_args.kwargs
    assert "RuntimeError" in call_kwargs["output"]["error"]


@pytest.mark.asyncio
async def test_execute_skill_fetch_failure_updates_trace_and_reraises(
    mock_trace: MagicMock,
) -> None:
    """execute_skill records fetch errors on the trace and re-raises the exception."""
    with (
        patch("src.activities.get_or_create_trace", return_value=mock_trace),
        patch(
            "src.activities.fetch_skill",
            new_callable=AsyncMock,
            side_effect=RuntimeError("network down"),
        ),
        pytest.raises(RuntimeError, match="network down"),
    ):
        await activities.execute_skill("skill-mcp", {"q": "hello"}, "conv-3")

    mock_trace.update.assert_called_once()
    call_kwargs = mock_trace.update.call_args.kwargs
    assert "fetch_skill failed" in call_kwargs["output"]["error"]


@pytest.mark.asyncio
async def test_execute_skill_code_opens_execs_and_closes_session(mock_trace: MagicMock) -> None:
    """execute_skill dispatches a code skill through skill_executor_client:
    open a session, run the single command, always close the session."""
    skill = {
        "id": "skill-code",
        "type": "code",
        "bundle_storage_key": "skills/org/abc.zip",
        "bundle_checksum": "abc123",
    }
    exec_result = {"stdout": "Hello, World!\n", "stderr": "", "exit_code": 0, "timed_out": False}

    with (
        patch("src.activities.get_or_create_trace", return_value=mock_trace),
        patch("src.activities.fetch_skill", new_callable=AsyncMock, return_value=skill),
        patch(
            "src.activities.skill_executor_client.open_session",
            new_callable=AsyncMock,
            return_value={"session_id": "sess-1", "skill_md": "instructions"},
        ) as mock_open,
        patch(
            "src.activities.skill_executor_client.exec_command",
            new_callable=AsyncMock,
            return_value=exec_result,
        ) as mock_exec,
        patch(
            "src.activities.skill_executor_client.close_session",
            new_callable=AsyncMock,
        ) as mock_close,
    ):
        result = await activities.execute_skill(
            "skill-code", {"command": 'python scripts/greet.py "World"'}, "conv-1"
        )

    assert result == {"skill_id": "skill-code", "output": exec_result}
    mock_open.assert_awaited_once_with(skill, conversation_id="conv-1")
    mock_exec.assert_awaited_once_with("sess-1", 'python scripts/greet.py "World"')
    mock_close.assert_awaited_once_with("sess-1")


@pytest.mark.asyncio
async def test_execute_skill_code_closes_session_even_if_exec_fails(
    mock_trace: MagicMock,
) -> None:
    """The sandbox session must be torn down even when the command itself
    raises — a failed exec must never leak a live sandbox container."""
    skill = {
        "id": "skill-code",
        "type": "code",
        "bundle_storage_key": "skills/org/abc.zip",
        "bundle_checksum": "abc123",
    }

    with (
        patch("src.activities.get_or_create_trace", return_value=mock_trace),
        patch("src.activities.fetch_skill", new_callable=AsyncMock, return_value=skill),
        patch(
            "src.activities.skill_executor_client.open_session",
            new_callable=AsyncMock,
            return_value={"session_id": "sess-1", "skill_md": "instructions"},
        ),
        patch(
            "src.activities.skill_executor_client.exec_command",
            new_callable=AsyncMock,
            side_effect=RuntimeError("skill-executor unreachable"),
        ),
        patch(
            "src.activities.skill_executor_client.close_session",
            new_callable=AsyncMock,
        ) as mock_close,
        pytest.raises(RuntimeError, match="skill-executor unreachable"),
    ):
        await activities.execute_skill("skill-code", {"command": "echo hi"}, "conv-1")

    mock_close.assert_awaited_once_with("sess-1")


@pytest.mark.asyncio
async def test_execute_skill_code_missing_command_raises_application_error(
    mock_trace: MagicMock,
) -> None:
    skill = {
        "id": "skill-code",
        "type": "code",
        "bundle_storage_key": "skills/org/abc.zip",
        "bundle_checksum": "abc123",
    }

    with (
        patch("src.activities.get_or_create_trace", return_value=mock_trace),
        patch("src.activities.fetch_skill", new_callable=AsyncMock, return_value=skill),
        patch(
            "src.activities.skill_executor_client.open_session", new_callable=AsyncMock
        ) as mock_open,
        pytest.raises(ApplicationError, match="missing a 'command' arg"),
    ):
        await activities.execute_skill("skill-code", {}, "conv-1")

    mock_open.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_skill_session_fetches_skill_and_opens_session() -> None:
    skill = {
        "id": "skill-code",
        "type": "code",
        "bundle_storage_key": "skills/org/abc.zip",
        "bundle_checksum": "abc123",
    }
    session = {"session_id": "sess-1", "skill_md": "instructions"}

    with (
        patch("src.activities.fetch_skill", new_callable=AsyncMock, return_value=skill) as mock_fetch,
        patch(
            "src.activities.skill_executor_client.open_session",
            new_callable=AsyncMock,
            return_value=session,
        ) as mock_open,
    ):
        result = await activities.open_skill_session("skill-code", "conv-1", "a-token")

    assert result == session
    mock_fetch.assert_awaited_once_with("skill-code", session_token="a-token")
    mock_open.assert_awaited_once_with(skill, conversation_id="conv-1")


@pytest.mark.asyncio
async def test_exec_in_skill_session_delegates_to_client() -> None:
    exec_result = {"stdout": "hi", "stderr": "", "exit_code": 0, "timed_out": False}

    with patch(
        "src.activities.skill_executor_client.exec_command",
        new_callable=AsyncMock,
        return_value=exec_result,
    ) as mock_exec:
        result = await activities.exec_in_skill_session("sess-1", "echo hi", 30.0)

    assert result == exec_result
    mock_exec.assert_awaited_once_with("sess-1", "echo hi", timeout=30.0)


@pytest.mark.asyncio
async def test_close_skill_session_delegates_to_client() -> None:
    with patch(
        "src.activities.skill_executor_client.close_session", new_callable=AsyncMock
    ) as mock_close:
        await activities.close_skill_session("sess-1")

    mock_close.assert_awaited_once_with("sess-1")
