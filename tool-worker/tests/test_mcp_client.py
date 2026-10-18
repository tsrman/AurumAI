import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.mcp_client import MCPClient


def make_fake_process(
    stdout_line: bytes = b"",
    stderr_data: bytes = b"",
    returncode: int | None = None,
) -> MagicMock:
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.close = MagicMock()
    proc.stdin.wait_closed = AsyncMock()
    proc.stdout = MagicMock()
    proc.stdout.readline = AsyncMock(return_value=stdout_line)
    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(return_value=stderr_data)
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


@pytest.mark.asyncio
async def test_call_stdio_sends_json_rpc_request_and_returns_parsed_response():
    response = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    proc = make_fake_process(
        stdout_line=(json.dumps(response) + "\n").encode(), returncode=0
    )

    with patch(
        "src.mcp_client.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=proc,
    ):
        result = await MCPClient.call_server(
            "stdio", "python tool.py", "search", {"q": "hi"}
        )

    assert result == response
    sent = json.loads(proc.stdin.write.call_args.args[0].decode().strip())
    assert sent == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "search", "arguments": {"q": "hi"}},
    }
    proc.stdin.close.assert_called_once()
    proc.stdin.wait_closed.assert_awaited_once()
    # Process already exited (returncode set) - cleanup must not try to kill it.
    proc.kill.assert_not_called()


@pytest.mark.asyncio
async def test_call_stdio_empty_output_raises_with_stderr_content():
    proc = make_fake_process(stdout_line=b"", stderr_data=b"boom: server crashed")

    with patch(
        "src.mcp_client.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=proc,
    ):
        with pytest.raises(RuntimeError, match="boom: server crashed"):
            await MCPClient.call_server("stdio", "python tool.py", "search", {})


@pytest.mark.asyncio
async def test_call_stdio_invalid_json_output_raises_runtime_error():
    proc = make_fake_process(stdout_line=b"not valid json\n")

    with patch(
        "src.mcp_client.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=proc,
    ):
        with pytest.raises(RuntimeError, match="invalid JSON"):
            await MCPClient.call_server("stdio", "python tool.py", "search", {})


@pytest.mark.asyncio
async def test_call_stdio_passes_line_size_limit_to_subprocess():
    """the cap must actually reach create_subprocess_exec's
    own `limit`, which is what makes readline() enforce it."""
    response = {"jsonrpc": "2.0", "id": 1, "result": {}}
    proc = make_fake_process(stdout_line=(json.dumps(response) + "\n").encode(), returncode=0)

    with patch(
        "src.mcp_client.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=proc,
    ) as mock_spawn:
        await MCPClient.call_server("stdio", "python tool.py", "search", {})

    assert mock_spawn.call_args.kwargs["limit"] == MCPClient.MCP_STDIO_MAX_LINE_BYTES


@pytest.mark.asyncio
async def test_call_stdio_oversized_line_raises_clean_runtime_error():
    """a hostile/buggy MCP server sending an unbounded
    line (no newline) must fail with a clear error, not exhaust memory or
    surface asyncio's own LimitOverrunError raw."""
    proc = make_fake_process(returncode=0)
    proc.stdout.readline = AsyncMock(
        side_effect=asyncio.LimitOverrunError("line too long", consumed=0)
    )

    with patch(
        "src.mcp_client.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=proc,
    ):
        with pytest.raises(RuntimeError, match="exceeded"):
            await MCPClient.call_server("stdio", "python tool.py", "search", {})


@pytest.mark.asyncio
async def test_call_stdio_empty_command_raises_value_error_without_spawning():
    with patch(
        "src.mcp_client.asyncio.create_subprocess_exec", new_callable=AsyncMock
    ) as mock_spawn:
        with pytest.raises(ValueError, match="Empty stdio command"):
            await MCPClient.call_server("stdio", "   ", "search", {})
    mock_spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_call_stdio_kills_still_running_process_during_cleanup():
    response = {"jsonrpc": "2.0", "id": 1, "result": {}}
    proc = make_fake_process(
        stdout_line=(json.dumps(response) + "\n").encode(), returncode=None
    )

    with patch(
        "src.mcp_client.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=proc,
    ):
        await MCPClient.call_server("stdio", "python tool.py", "search", {})

    proc.kill.assert_called_once()
    proc.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_call_server_unsupported_transport_raises_value_error():
    with pytest.raises(ValueError, match="Unsupported MCP transport: bogus"):
        await MCPClient.call_server("bogus", "endpoint", "search", {})


@pytest.mark.asyncio
async def test_call_sse_passes_bearer_header_to_sse_client_when_auth_token_given():
    tool_result = MagicMock()
    tool_result.model_dump.return_value = {"content": [{"type": "text", "text": "hi"}]}

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.initialize = AsyncMock()
    session.call_tool = AsyncMock(return_value=tool_result)

    streams = (MagicMock(), MagicMock())
    sse_ctx = MagicMock()
    sse_ctx.__aenter__ = AsyncMock(return_value=streams)
    sse_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_sse_client = MagicMock(return_value=sse_ctx)

    with (
        patch("mcp.client.sse.sse_client", mock_sse_client),
        patch("mcp.ClientSession", MagicMock(return_value=session)),
    ):
        result = await MCPClient.call_server(
            "sse", "http://mcp.local/sse", "search", {"q": "hi"}, auth_token="secret-token"
        )

    assert result == {"result": {"content": [{"type": "text", "text": "hi"}]}}
    mock_sse_client.assert_called_once_with(
        url="http://mcp.local/sse", headers={"Authorization": "Bearer secret-token"}
    )
    session.call_tool.assert_awaited_once_with("search", {"q": "hi"})


@pytest.mark.asyncio
async def test_call_sse_falls_back_to_httpx_when_mcp_sdk_unavailable():
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"answer": "ok"}
    http_client = MagicMock()
    http_client.__aenter__.return_value = http_client
    http_client.__aexit__ = AsyncMock(return_value=False)
    http_client.post = AsyncMock(return_value=response)

    def _raise_import_error(name, *args, **kwargs):
        if name in ("mcp.client.sse", "mcp"):
            raise ModuleNotFoundError(name)
        return __import__(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_raise_import_error), patch(
        "httpx.AsyncClient", return_value=http_client
    ):
        result = await MCPClient.call_server(
            "sse", "http://mcp.local/sse", "search", {"q": "hi"}
        )

    assert result == {"result": {"answer": "ok"}}
    http_client.post.assert_awaited_once_with(
        "http://mcp.local/sse",
        json={"tool": "search", "arguments": {"q": "hi"}},
        headers=None,
    )


@pytest.mark.asyncio
async def test_call_sse_httpx_propagates_http_error():
    response = MagicMock()
    response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("boom", request=None, response=None)
    )
    http_client = MagicMock()
    http_client.__aenter__.return_value = http_client
    http_client.__aexit__ = AsyncMock(return_value=False)
    http_client.post = AsyncMock(return_value=response)

    with patch("httpx.AsyncClient", return_value=http_client):
        with pytest.raises(httpx.HTTPStatusError):
            await MCPClient()._call_sse_httpx("http://mcp.local/sse", "search", {})
