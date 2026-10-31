from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import WSMsgType

from src.sandbox_runtime.base import SandboxExecError
from src.sandbox_runtime import kubernetes_exec
from src.sandbox_runtime.kubernetes_exec import KubernetesExecClient


@dataclass
class Message:
    type: WSMsgType
    data: bytes


class FakeSocket:
    def __init__(self, frames: list[bytes], *, hang: bool = False) -> None:
        self.frames = list(frames)
        self.hang = hang
        self.sent: list[bytes] = []
        self.closed = False

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def receive(self) -> Message:
        if self.hang:
            await asyncio.Event().wait()
        if self.frames:
            return Message(WSMsgType.BINARY, self.frames.pop(0))
        return Message(WSMsgType.CLOSE, b"")

    async def close(self) -> None:
        self.closed = True


def status_frame(payload: dict[str, Any]) -> bytes:
    return bytes([3]) + json.dumps(payload).encode()


def success() -> bytes:
    return status_frame({"status": "Success"})


def client_for(socket: FakeSocket) -> KubernetesExecClient:
    async def connect(**kwargs: Any) -> FakeSocket:
        connect.kwargs = kwargs  # type: ignore[attr-defined]
        return socket

    return KubernetesExecClient(socket_factory=connect)


@pytest.mark.asyncio
async def test_exec_sends_exact_binary_stdin_and_closes_channel() -> None:
    tar_bytes = b"\x00tar\xffpayload"
    socket = FakeSocket([success()])
    client = client_for(socket)

    result = await client.exec(
        namespace="aurum-ai-sandboxes",
        pod="skill-sandbox-abc",
        container="sandbox",
        command=["tar", "-x", "-C", "/skill", "-f", "-"],
        stdin=tar_bytes,
        timeout_seconds=60,
        max_bytes=262_144,
    )

    assert socket.sent == [bytes([0]) + tar_bytes, bytes([255, 0])]
    assert result.exit_code == 0
    assert socket.closed is True


@pytest.mark.asyncio
async def test_exec_collects_streams_independently_and_drains_after_truncation() -> None:
    socket = FakeSocket(
        [
            bytes([1]) + b"abcdef",
            bytes([2]) + b"123456",
            bytes([1]) + b"drained",
            success(),
        ]
    )

    result = await client_for(socket).exec(
        namespace="ns",
        pod="pod",
        container="sandbox",
        command=["sh", "-c", "echo"],
        timeout_seconds=1,
        max_bytes=4,
    )

    assert result.stdout == "abcd"
    assert result.stderr == "1234"
    assert result.exit_code == 0
    assert socket.frames == []


@pytest.mark.asyncio
async def test_exec_extracts_nonzero_exit_code() -> None:
    socket = FakeSocket(
        [
            status_frame(
                {
                    "status": "Failure",
                    "reason": "NonZeroExitCode",
                    "details": {
                        "causes": [{"reason": "ExitCode", "message": "42"}]
                    },
                }
            )
        ]
    )

    result = await client_for(socket).exec(
        namespace="ns",
        pod="pod",
        container="sandbox",
        command=["false"],
        timeout_seconds=1,
        max_bytes=10,
    )

    assert result.exit_code == 42
    assert result.timed_out is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frames",
    [
        [],
        [bytes([3]) + b"not-json"],
        [status_frame({"status": "Failure", "reason": "NonZeroExitCode"})],
    ],
)
async def test_exec_rejects_missing_or_malformed_status(frames: list[bytes]) -> None:
    socket = FakeSocket(frames)

    with pytest.raises(SandboxExecError):
        await client_for(socket).exec(
            namespace="ns",
            pod="pod",
            container="sandbox",
            command=["false"],
            timeout_seconds=1,
            max_bytes=10,
        )

    assert socket.closed is True


@pytest.mark.asyncio
async def test_exec_timeout_closes_socket_and_returns_partial_result() -> None:
    socket = FakeSocket([], hang=True)

    result = await client_for(socket).exec(
        namespace="ns",
        pod="pod",
        container="sandbox",
        command=["sleep", "60"],
        timeout_seconds=0.01,
        max_bytes=10,
    )

    assert result.timed_out is True
    assert result.exit_code is None
    assert socket.closed is True


@pytest.mark.asyncio
async def test_exec_passes_expected_connection_flags() -> None:
    socket = FakeSocket([success()])
    captured: dict[str, Any] = {}

    async def connect(**kwargs: Any) -> FakeSocket:
        captured.update(kwargs)
        return socket

    await KubernetesExecClient(socket_factory=connect).exec(
        namespace="ns",
        pod="pod",
        container="sandbox",
        command=["true"],
        timeout_seconds=1,
        max_bytes=10,
    )

    assert captured == {
        "namespace": "ns",
        "pod": "pod",
        "container": "sandbox",
        "command": ["true"],
        "stdin": False,
        "stdout": True,
        "stderr": True,
        "tty": False,
        "_preload_content": False,
    }


@pytest.mark.asyncio
async def test_overall_timeout_includes_socket_connection() -> None:
    async def connect(**kwargs: Any) -> FakeSocket:
        del kwargs
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    result = await KubernetesExecClient(socket_factory=connect).exec(
        namespace="ns",
        pod="pod",
        container="sandbox",
        command=["true"],
        timeout_seconds=0.01,
        max_bytes=10,
    )

    assert result.timed_out is True
    assert result.exit_code is None


@pytest.mark.asyncio
async def test_production_factory_requests_v5_and_owns_all_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket = FakeSocket([success()])
    socket.protocol = "v5.channel.k8s.io"  # type: ignore[attr-defined]
    context = Mock()
    context.__aenter__ = AsyncMock(return_value=socket)
    context.__aexit__ = AsyncMock()
    api_client = Mock()
    api_client.close = AsyncMock()
    ws_constructor = Mock(return_value=api_client)
    connect = AsyncMock(return_value=context)
    core = Mock()
    core.connect_get_namespaced_pod_exec = connect
    monkeypatch.setattr(kubernetes_exec, "_V5WsApiClient", ws_constructor)
    monkeypatch.setattr(
        kubernetes_exec.k8s_client, "CoreV1Api", Mock(return_value=core)
    )

    managed = await kubernetes_exec._production_socket_factory(
        namespace="ns",
        pod="pod",
        container="sandbox",
        command=["true"],
        stdin=False,
        stdout=True,
        stderr=True,
        tty=False,
        _preload_content=False,
    )
    await managed.close()

    ws_constructor.assert_called_once_with()
    context.__aexit__.assert_awaited_once()
    api_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_production_factory_closes_api_client_when_upgrade_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = Mock()
    context.__aenter__ = AsyncMock(side_effect=RuntimeError("upgrade failed"))
    context.__aexit__ = AsyncMock()
    api_client = Mock()
    api_client.close = AsyncMock()
    monkeypatch.setattr(
        kubernetes_exec, "_V5WsApiClient", Mock(return_value=api_client)
    )
    core = Mock()
    core.connect_get_namespaced_pod_exec = AsyncMock(return_value=context)
    monkeypatch.setattr(
        kubernetes_exec.k8s_client, "CoreV1Api", Mock(return_value=core)
    )

    with pytest.raises(RuntimeError, match="upgrade failed"):
        await kubernetes_exec._production_socket_factory(
            namespace="ns", pod="pod", container="sandbox", command=["true"]
        )

    api_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_production_factory_rejects_non_v5_negotiation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket = FakeSocket([])
    socket.protocol = "v4.channel.k8s.io"  # type: ignore[attr-defined]
    context = Mock()
    context.__aenter__ = AsyncMock(return_value=socket)
    context.__aexit__ = AsyncMock()
    api_client = Mock()
    api_client.close = AsyncMock()
    monkeypatch.setattr(
        kubernetes_exec, "_V5WsApiClient", Mock(return_value=api_client)
    )
    core = Mock()
    core.connect_get_namespaced_pod_exec = AsyncMock(return_value=context)
    monkeypatch.setattr(
        kubernetes_exec.k8s_client, "CoreV1Api", Mock(return_value=core)
    )

    with pytest.raises(SandboxExecError, match="protocol v5"):
        await kubernetes_exec._production_socket_factory(
            namespace="ns", pod="pod", container="sandbox", command=["true"]
        )

    assert socket.closed is True
    context.__aexit__.assert_awaited_once()
    api_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_v5_api_client_passes_protocols_to_aiohttp() -> None:
    ws_connect = Mock(return_value="context")
    client = object.__new__(kubernetes_exec._V5WsApiClient)
    client.rest_client = SimpleNamespace(
        pool_manager=SimpleNamespace(ws_connect=ws_connect)
    )
    client.heartbeat = 12.0

    result = await client.request(
        "GET",
        "https://kubernetes/api/v1/pods/pod/exec",
        query_params=[("command", ["sh", "-c", "true"])],
        headers={"authorization": "Bearer redacted"},
        _preload_content=False,
    )

    assert result == "context"
    kwargs = ws_connect.call_args.kwargs
    assert kwargs["protocols"] == ("v5.channel.k8s.io",)
    assert "sec-websocket-protocol" not in kwargs["headers"]
    assert ws_connect.call_args.args[0].startswith("wss://kubernetes/")
    assert ws_connect.call_args.args[0].count("command=") == 3


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_mask_status_error() -> None:
    socket = FakeSocket([bytes([3]) + b"not-json"])
    socket.close = AsyncMock(side_effect=RuntimeError("close failed"))  # type: ignore[method-assign]

    with pytest.raises(SandboxExecError, match="malformed status"):
        await client_for(socket).exec(
            namespace="ns",
            pod="pod",
            container="sandbox",
            command=["false"],
            timeout_seconds=1,
            max_bytes=10,
        )


@pytest.mark.asyncio
async def test_nonzero_status_skips_malformed_exit_code_cause() -> None:
    socket = FakeSocket(
        [
            status_frame(
                {
                    "status": "Failure",
                    "reason": "NonZeroExitCode",
                    "details": {
                        "causes": [
                            {"reason": "ExitCode", "message": "not-a-number"},
                            {"reason": "ExitCode", "message": "7"},
                        ]
                    },
                }
            )
        ]
    )

    result = await client_for(socket).exec(
        namespace="ns",
        pod="pod",
        container="sandbox",
        command=["false"],
        timeout_seconds=1,
        max_bytes=10,
    )

    assert result.exit_code == 7


def frame(channel: int, data: bytes) -> bytes:
    return bytes([channel]) + data


@pytest.mark.asyncio
async def test_open_streams_stdout_as_it_arrives_and_reports_the_exit_code() -> None:
    socket = FakeSocket([frame(1, b"one"), frame(2, b"noise"), frame(1, b"two"), success()])
    client = client_for(socket)

    stream = await client.open(
        namespace="hermes", pod="aurum-ai-hermes-a", container="sandbox", command=["hermes-relay", "events", "r1"]
    )
    chunks = []
    while (chunk := await stream.read()) is not None:
        chunks.append(chunk)

    assert chunks == [b"one", b"two"]
    assert stream.exit_code == 0
    assert client_for  # the factory saw an exec without stdin
    await stream.close()
    await stream.close()
    assert socket.closed is True


@pytest.mark.asyncio
async def test_open_with_stdin_writes_channel_zero_and_half_closes() -> None:
    socket = FakeSocket([success()])
    seen: dict[str, Any] = {}

    async def connect(**kwargs: Any) -> FakeSocket:
        seen.update(kwargs)
        return socket

    stream = await KubernetesExecClient(socket_factory=connect).open(
        namespace="hermes", pod="p", container="sandbox", command=["tar", "-x"], stdin=True
    )
    await stream.write(b"\x00bytes")
    await stream.close_stdin()

    assert seen["stdin"] is True and seen["command"] == ["tar", "-x"] and seen["_preload_content"] is False
    assert socket.sent == [bytes([0]) + b"\x00bytes", bytes([255, 0])]
    await stream.close()


@pytest.mark.asyncio
async def test_open_reports_a_failing_command_and_a_broken_socket() -> None:
    failing = FakeSocket(
        [status_frame({"status": "Failure", "reason": "NonZeroExitCode", "details": {"causes": [{"reason": "ExitCode", "message": "3"}]}})]
    )
    stream = await client_for(failing).open(namespace="n", pod="p", container="c", command=["false"])
    assert await stream.read() is None
    assert stream.exit_code == 3

    class Broken(FakeSocket):
        async def receive(self) -> Message:
            return Message(WSMsgType.ERROR, b"")

    stream = await client_for(Broken([])).open(namespace="n", pod="p", container="c", command=["x"])
    with pytest.raises(SandboxExecError):
        await stream.read()


@pytest.mark.asyncio
async def test_exit_code_is_unknown_until_the_stream_ends() -> None:
    stream = await client_for(FakeSocket([frame(1, b"x"), success()])).open(
        namespace="n", pod="p", container="c", command=["x"]
    )
    assert stream.exit_code is None
    await stream.read()
    assert stream.exit_code is None
    assert await stream.read() is None
    assert stream.exit_code == 0
