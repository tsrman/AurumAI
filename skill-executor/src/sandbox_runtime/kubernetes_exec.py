"""Kubernetes remote-exec WebSocket framing adapter."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, cast
from urllib.parse import urlencode

from aiohttp import WSMsgType
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.stream import WsApiClient
from kubernetes_asyncio.stream.ws_client import (
    STDERR_CHANNEL,
    STDOUT_CHANNEL,
    WsResponse,
    get_websocket_url,
)

from src.sandbox_runtime.base import SandboxExecError

logger = logging.getLogger(__name__)
_V5_PROTOCOL = "v5.channel.k8s.io"


class _Socket(Protocol):
    async def send_bytes(self, data: bytes) -> None: ...

    async def receive(self) -> Any: ...

    async def close(self) -> Any: ...


SocketFactory = Callable[..., Awaitable[_Socket]]


@dataclass(frozen=True)
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool


class _V5WsApiClient(WsApiClient):
    """WsApiClient variant that negotiates the Kubernetes exec v5 protocol."""

    async def request(
        self,
        method: str,
        url: str,
        query_params: list[tuple[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
        post_params: Any = None,
        body: Any = None,
        _preload_content: bool = True,
        _request_timeout: Any = None,
    ) -> Any:
        del method, post_params, body, _request_timeout
        expanded_query_params: list[tuple[str, Any]] = []
        for key, value in query_params or []:
            if key == "command" and isinstance(value, list):
                expanded_query_params.extend((key, command) for command in value)
            else:
                expanded_query_params.append((key, value))

        request_headers = {
            key: value
            for key, value in (headers or {}).items()
            if key.lower() != "sec-websocket-protocol"
        }
        if expanded_query_params:
            url += "?" + urlencode(expanded_query_params)
        url = get_websocket_url(url)
        if not _preload_content:
            return self.rest_client.pool_manager.ws_connect(
                url,
                headers=request_headers,
                heartbeat=self.heartbeat,
                protocols=(_V5_PROTOCOL,),
            )

        response = bytearray()
        async with self.rest_client.pool_manager.ws_connect(
            url,
            headers=request_headers,
            heartbeat=self.heartbeat,
            protocols=(_V5_PROTOCOL,),
        ) as socket:
            async for message in socket:
                raw = (
                    message.data.encode()
                    if isinstance(message.data, str)
                    else bytes(message.data)
                )
                if raw and raw[0] in {STDOUT_CHANNEL, STDERR_CHANNEL}:
                    response.extend(raw[1:])
        return WsResponse(200, bytes(response))


class _ManagedSocket:
    def __init__(self, socket: Any, context: Any, api_client: WsApiClient) -> None:
        self._socket = socket
        self._context = context
        self._api_client = api_client

    async def send_bytes(self, data: bytes) -> None:
        await self._socket.send_bytes(data)

    async def receive(self) -> Any:
        return await self._socket.receive()

    async def close(self) -> None:
        try:
            await self._socket.close()
        except Exception:
            logger.warning("Kubernetes exec socket close failed", exc_info=True)
        finally:
            try:
                await self._context.__aexit__(None, None, None)
            except Exception:
                logger.warning("Kubernetes exec context close failed", exc_info=True)
            finally:
                try:
                    await self._api_client.close()
                except Exception:
                    logger.warning("Kubernetes exec API client close failed", exc_info=True)


async def _production_socket_factory(**kwargs: Any) -> _Socket:
    api_client = _V5WsApiClient()
    core = k8s_client.CoreV1Api(api_client=api_client)
    context: Any | None = None
    managed: _ManagedSocket | None = None
    try:
        context = cast(
            Any,
            await core.connect_get_namespaced_pod_exec(
                kwargs.pop("pod"), kwargs.pop("namespace"), **kwargs
            ),
        )
        socket = await context.__aenter__()
        managed = _ManagedSocket(socket, context, api_client)
        if getattr(socket, "protocol", None) != _V5_PROTOCOL:
            raise SandboxExecError("Kubernetes exec protocol v5 was not negotiated")
        return managed
    except BaseException:
        if managed is not None:
            await managed.close()
        else:
            if context is not None:
                try:
                    await context.__aexit__(None, None, None)
                except Exception:
                    logger.warning("Kubernetes exec context cleanup failed", exc_info=True)
            try:
                await api_client.close()
            except Exception:
                logger.warning("Kubernetes exec API client cleanup failed", exc_info=True)
        raise


class ExecStream:
    """One running command: stdout as it arrives, stdin as it is written."""

    def __init__(self, socket: _Socket) -> None:
        self._socket = socket
        self._status: dict[str, Any] | None = None
        self._ended = False
        self._closed = False

    @property
    def exit_code(self) -> int | None:
        """The command's exit code once the stream has ended; None before."""
        if not self._ended or self._status is None:
            return None
        return KubernetesExecClient._exit_code(self._status)

    async def read(self) -> bytes | None:
        """The next piece of standard output, or None when the command is done."""
        while not self._ended:
            message = await self._socket.receive()
            if message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING}:
                self._ended = True
                break
            if message.type == WSMsgType.ERROR:
                raise SandboxExecError("Kubernetes exec WebSocket failed")
            if message.type not in {WSMsgType.BINARY, WSMsgType.TEXT}:
                continue
            raw = message.data.encode() if isinstance(message.data, str) else bytes(message.data)
            if not raw:
                continue
            channel, payload = raw[0], raw[1:]
            if channel == 1 and payload:
                return payload
            if channel == 3:
                self._status = KubernetesExecClient._decode_status(payload)
        return None

    async def write(self, data: bytes) -> None:
        await self._socket.send_bytes(bytes([0]) + data)

    async def close_stdin(self) -> None:
        """Half-close: the command sees end of input and may still answer."""
        await self._socket.send_bytes(bytes([255, 0]))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._socket.close()
        except Exception:
            logger.warning("Kubernetes exec socket cleanup failed", exc_info=True)


class KubernetesExecClient:
    def __init__(self, socket_factory: SocketFactory | None = None) -> None:
        self._socket_factory = socket_factory or _production_socket_factory

    async def open(
        self,
        *,
        namespace: str,
        pod: str,
        container: str,
        command: list[str],
        stdin: bool = False,
        connect_timeout_seconds: float = 30.0,
    ) -> ExecStream:
        """Start a command and return its stream; the caller closes it."""
        async with asyncio.timeout(connect_timeout_seconds):
            socket = await self._socket_factory(
                namespace=namespace,
                pod=pod,
                container=container,
                command=command,
                stdin=stdin,
                stdout=True,
                stderr=True,
                tty=False,
                _preload_content=False,
            )
        return ExecStream(socket)

    async def exec(
        self,
        *,
        namespace: str,
        pod: str,
        container: str,
        command: list[str],
        stdin: bytes | None = None,
        timeout_seconds: float,
        max_bytes: int,
    ) -> ExecResult:
        socket: _Socket | None = None
        stdout = bytearray()
        stderr = bytearray()
        status_payload: dict[str, Any] | None = None
        try:
            try:
                async with asyncio.timeout(timeout_seconds):
                    socket = await self._socket_factory(
                        namespace=namespace,
                        pod=pod,
                        container=container,
                        command=command,
                        stdin=stdin is not None,
                        stdout=True,
                        stderr=True,
                        tty=False,
                        _preload_content=False,
                    )
                    if stdin is not None:
                        await socket.send_bytes(bytes([0]) + stdin)
                        await socket.send_bytes(bytes([255, 0]))
                    while True:
                        message = await socket.receive()
                        if message.type in {
                            WSMsgType.CLOSE,
                            WSMsgType.CLOSED,
                            WSMsgType.CLOSING,
                        }:
                            break
                        if message.type == WSMsgType.ERROR:
                            raise SandboxExecError("Kubernetes exec WebSocket failed")
                        if message.type not in {WSMsgType.BINARY, WSMsgType.TEXT}:
                            continue
                        raw = (
                            message.data.encode()
                            if isinstance(message.data, str)
                            else bytes(message.data)
                        )
                        if not raw:
                            continue
                        channel, payload = raw[0], raw[1:]
                        if channel == 1:
                            self._append_limited(stdout, payload, max_bytes)
                        elif channel == 2:
                            self._append_limited(stderr, payload, max_bytes)
                        elif channel == 3:
                            status_payload = self._decode_status(payload)
            except TimeoutError:
                return ExecResult(
                    stdout.decode(errors="replace"),
                    stderr.decode(errors="replace"),
                    None,
                    True,
                )

            exit_code = self._exit_code(status_payload)
            return ExecResult(
                stdout.decode(errors="replace"),
                stderr.decode(errors="replace"),
                exit_code,
                False,
            )
        finally:
            if socket is not None:
                try:
                    await socket.close()
                except Exception:
                    logger.warning("Kubernetes exec socket cleanup failed", exc_info=True)

    @staticmethod
    def _append_limited(target: bytearray, payload: bytes, max_bytes: int) -> None:
        remaining = max(0, max_bytes - len(target))
        if remaining:
            target.extend(payload[:remaining])

    @staticmethod
    def _decode_status(payload: bytes) -> dict[str, Any]:
        try:
            decoded = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
            raise SandboxExecError("Kubernetes exec returned malformed status") from exc
        if not isinstance(decoded, dict):
            raise SandboxExecError("Kubernetes exec returned malformed status")
        return cast(dict[str, Any], decoded)

    @staticmethod
    def _exit_code(status_payload: dict[str, Any] | None) -> int:
        if status_payload is None:
            raise SandboxExecError("Kubernetes exec returned no status")
        if status_payload.get("status") == "Success":
            return 0
        if status_payload.get("reason") != "NonZeroExitCode":
            raise SandboxExecError("Kubernetes exec failed without an exit code")
        details = status_payload.get("details")
        causes = details.get("causes") if isinstance(details, dict) else None
        if isinstance(causes, list):
            for cause in causes:
                if isinstance(cause, dict) and cause.get("reason") == "ExitCode":
                    try:
                        return int(cause["message"])
                    except (KeyError, TypeError, ValueError):
                        continue
        raise SandboxExecError("Kubernetes exec returned no exit code")
