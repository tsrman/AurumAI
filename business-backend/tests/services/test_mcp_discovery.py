from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.mcp_discovery import discover_mcp_tools, McpDiscoveryError


def _make_tool(name: str, description: str, input_schema: dict) -> MagicMock:
    tool = MagicMock()
    tool.name = name
    tool.description = description
    tool.inputSchema = input_schema
    return tool


def _patch_mcp_sdk(tools: list[MagicMock] | None = None, side_effect: Exception | None = None):
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.initialize = AsyncMock()
    if side_effect is not None:
        session.list_tools = AsyncMock(side_effect=side_effect)
    else:
        result = MagicMock()
        result.tools = tools or []
        session.list_tools = AsyncMock(return_value=result)

    streams = (MagicMock(), MagicMock())
    sse_ctx = MagicMock()
    sse_ctx.__aenter__ = AsyncMock(return_value=streams)
    sse_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_sse_client = MagicMock(return_value=sse_ctx)
    mock_client_session_cls = MagicMock(return_value=session)

    return (
        patch("mcp.client.sse.sse_client", mock_sse_client),
        patch("mcp.ClientSession", mock_client_session_cls),
        mock_sse_client,
    )


@pytest.mark.asyncio
async def test_discover_mcp_tools_returns_normalized_tool_list():
    tools = [
        _make_tool("echo", "Echoes back the input string", {"type": "object", "properties": {"message": {"type": "string"}}}),
        _make_tool("get-sum", "Returns the sum of two numbers", {"type": "object", "properties": {"a": {}, "b": {}}}),
    ]
    patch_sse, patch_session, mock_sse_client = _patch_mcp_sdk(tools=tools)

    with patch_sse, patch_session:
        result = await discover_mcp_tools("http://mcp-everything:3001/sse")

    assert result == [
        {
            "name": "echo",
            "description": "Echoes back the input string",
            "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}},
        },
        {
            "name": "get-sum",
            "description": "Returns the sum of two numbers",
            "inputSchema": {"type": "object", "properties": {"a": {}, "b": {}}},
        },
    ]
    mock_sse_client.assert_called_once_with(
        url="http://mcp-everything:3001/sse", headers=None, timeout=15.0
    )


@pytest.mark.asyncio
async def test_discover_mcp_tools_passes_bearer_header_when_auth_token_given():
    patch_sse, patch_session, mock_sse_client = _patch_mcp_sdk(tools=[])

    with patch_sse, patch_session:
        await discover_mcp_tools("http://mcp-everything:3001/sse", auth_token="secret-token")

    mock_sse_client.assert_called_once_with(
        url="http://mcp-everything:3001/sse",
        headers={"Authorization": "Bearer secret-token"},
        timeout=15.0,
    )


@pytest.mark.asyncio
async def test_discover_mcp_tools_defaults_missing_description_and_schema():
    tools = [_make_tool("noop", None, None)]
    patch_sse, patch_session, _ = _patch_mcp_sdk(tools=tools)

    with patch_sse, patch_session:
        result = await discover_mcp_tools("http://mcp-everything:3001/sse")

    assert result == [{"name": "noop", "description": "", "inputSchema": {}}]


@pytest.mark.asyncio
async def test_discover_mcp_tools_wraps_connection_errors():
    patch_sse, patch_session, _ = _patch_mcp_sdk(side_effect=ConnectionRefusedError("refused"))

    with patch_sse, patch_session:
        with pytest.raises(McpDiscoveryError, match="Could not reach MCP server"):
            await discover_mcp_tools("http://unreachable:9999/sse")


@pytest.mark.asyncio
async def test_discover_mcp_tools_raises_when_sdk_not_installed():
    def _raise_import_error(name, *args, **kwargs):
        if name in ("mcp.client.sse", "mcp"):
            raise ModuleNotFoundError(name)
        return __import__(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_raise_import_error):
        with pytest.raises(McpDiscoveryError, match="MCP SDK not available"):
            await discover_mcp_tools("http://mcp-everything:3001/sse")
