import logging
from typing import Any

logger = logging.getLogger(__name__)

MCP_DISCOVERY_TIMEOUT = 15.0


class McpDiscoveryError(Exception):
    """Raised when an MCP server can't be reached or doesn't speak the protocol."""


async def discover_mcp_tools(endpoint: str, auth_token: str | None = None) -> list[dict[str, Any]]:
    """Connect to an MCP server over SSE and list its available tools."""
    try:
        from mcp.client.sse import sse_client
        from mcp import ClientSession
    except (ImportError, ModuleNotFoundError) as exc:
        raise McpDiscoveryError(f"MCP SDK not available: {exc}") from exc

    headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else None

    try:
        async with sse_client(
            url=endpoint, headers=headers, timeout=MCP_DISCOVERY_TIMEOUT
        ) as (
            read_stream,
            write_stream,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
    except McpDiscoveryError:
        raise
    except Exception as exc:
        logger.warning("MCP tool discovery failed for %s: %s", endpoint, exc)
        raise McpDiscoveryError(f"Could not reach MCP server at {endpoint}: {exc}") from exc

    return [
        {
            "name": tool.name,
            "description": tool.description or "",
            "inputSchema": tool.inputSchema or {},
        }
        for tool in result.tools
    ]
