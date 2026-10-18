import asyncio
import json
import logging
from typing import Any, cast

import httpx


logger = logging.getLogger(__name__)


class MCPClient:
    """Minimal MCP client supporting stdio (subprocess JSON-RPC) and SSE transports."""

    MCP_TIMEOUT = 30
    # a hostile/buggy MCP server could otherwise send an
    # unbounded line and exhaust memory in this shared multitenant worker —
    # skill-executor already caps its own exec output (exec_output_max_bytes),
    # this is the equivalent for MCP stdio responses. Passed to
    # create_subprocess_exec's own `limit` (default 64KiB) so readline()
    # enforces it directly rather than us re-implementing bounded reads.
    MCP_STDIO_MAX_LINE_BYTES = 4 * 1024 * 1024

    def __init__(self, timeout: float | None = None):
        self.timeout = timeout if timeout is not None else self.MCP_TIMEOUT

    @classmethod
    async def call_server(
        cls,
        transport: str,
        endpoint: str,
        tool_name: str,
        args: dict[str, Any],
        auth_token: str | None = None,
    ) -> dict[str, Any]:
        return await cls()._call_server(transport, endpoint, tool_name, args, auth_token)

    async def _call_server(
        self,
        transport: str,
        endpoint: str,
        tool_name: str,
        args: dict[str, Any],
        auth_token: str | None = None,
    ) -> dict[str, Any]:
        if transport == "stdio":
            return await self._call_stdio(endpoint, tool_name, args)
        if transport == "sse":
            return await self._call_sse(endpoint, tool_name, args, auth_token)
        raise ValueError(f"Unsupported MCP transport: {transport}")

    async def _call_stdio(self, endpoint: str, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        command = endpoint.split() if isinstance(endpoint, str) else endpoint
        if not command:
            raise ValueError("Empty stdio command")

        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=self.MCP_STDIO_MAX_LINE_BYTES,
                ),
                timeout=self.timeout,
            )
        except (FileNotFoundError, OSError) as exc:
            raise RuntimeError(f"Failed to start MCP stdio server: {exc}") from exc

        try:
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": args},
            }
            await asyncio.wait_for(
                self._send_request(proc, request), timeout=self.timeout
            )

            # We always pass stdout=PIPE/stderr=PIPE above, so these are
            # never actually None at runtime — asyncio's own stubs just
            # can't express "PIPE was requested" in Process's return type.
            assert proc.stdout is not None
            assert proc.stderr is not None
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=self.timeout)
            except asyncio.LimitOverrunError as exc:
                raise RuntimeError(
                    f"MCP stdio server response exceeded "
                    f"{self.MCP_STDIO_MAX_LINE_BYTES} bytes without a newline"
                ) from exc
            if not line:
                stderr = await asyncio.wait_for(
                    proc.stderr.read(), timeout=self.timeout
                )
                raise RuntimeError(
                    f"MCP stdio server returned no output: {stderr.decode().strip()}"
                )

            try:
                return cast(dict[str, Any], json.loads(line))
            except json.JSONDecodeError as exc:
                raw = line.decode(errors="replace").strip()
                raise RuntimeError(
                    f"MCP stdio server returned invalid JSON: {raw}"
                ) from exc
        finally:
            await self._cleanup_process(proc)

    async def _send_request(self, proc: asyncio.subprocess.Process, request: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(request).encode() + b"\n")
        await proc.stdin.drain()
        proc.stdin.close()
        await proc.stdin.wait_closed()

    async def _cleanup_process(self, proc: asyncio.subprocess.Process) -> None:
        try:
            if proc.returncode is None:
                proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=self.timeout)
        except (ProcessLookupError, OSError):
            pass
        except asyncio.TimeoutError:
            pass

    async def _call_sse(
        self, endpoint: str, tool_name: str, args: dict[str, Any], auth_token: str | None = None
    ) -> dict[str, Any]:
        try:
            from mcp.client.sse import sse_client
            from mcp import ClientSession
        except (ImportError, ModuleNotFoundError) as exc:
            logger.info(
                "MCP SDK not available, falling back to httpx for SSE: %s", exc
            )
            return await self._call_sse_httpx(endpoint, tool_name, args, auth_token)

        headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else None
        async with sse_client(url=endpoint, headers=headers) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, args)
                return {
                    "result": result.model_dump()
                    if hasattr(result, "model_dump")
                    else result
                }

    async def _call_sse_httpx(
        self, endpoint: str, tool_name: str, args: dict[str, Any], auth_token: str | None = None
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else None
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                endpoint,
                json={"tool": tool_name, "arguments": args},
                headers=headers,
            )
            response.raise_for_status()
            return {"result": response.json()}
