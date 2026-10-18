import asyncio
from collections.abc import Callable
import gzip
from pathlib import Path
import ssl
import subprocess
import time

import pytest

from src.policy import URLBlocked, validate_target
from src.transport import DownloadBuffer, FetchFailed, PinnedTransport, resolve_target


ResponseFactory = Callable[[bytes], bytes]


async def _server(
    response: bytes | ResponseFactory,
    *,
    ssl_context: ssl.SSLContext | None = None,
):
    requests: list[bytes] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.readuntil(b"\r\n\r\n")
        requests.append(request)
        payload = response(request) if callable(response) else response
        writer.write(payload)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=ssl_context)
    port = server.sockets[0].getsockname()[1]
    return server, port, requests


def _allow_test_target(url: str, addresses: tuple[str, ...], forbidden_cidrs=()) -> str:  # noqa: ANN001
    del addresses, forbidden_cidrs
    return url


def _allow_test_url(url: str) -> str:
    return url


def _tls_contexts(tmp_path: Path) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    key = tmp_path / "key.pem"
    certificate = tmp_path / "certificate.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=public.test",
            "-addext",
            "subjectAltName=DNS:public.test",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
        ],
        check=True,
        capture_output=True,
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate, key)
    client_context = ssl.create_default_context(cafile=certificate)
    return server_context, client_context


async def test_invalid_url_is_rejected_before_any_dns_lookup() -> None:
    resolutions: list[tuple[str, int]] = []

    async def resolver(host: str, port: int) -> tuple[str, ...]:
        resolutions.append((host, port))
        return ("8.8.8.8",)

    for url in (
        "gopher://backend/secret",
        "https://backend:8443/secret",
        "https://user:password@backend/secret",
    ):
        with pytest.raises(URLBlocked):
            await resolve_target(url, resolver=resolver)

    assert resolutions == []


async def test_connector_dials_only_pinned_ip_and_keeps_host_for_tls(
    monkeypatch,
    tmp_path: Path,
) -> None:
    response = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 2\r\n\r\nok"
    server_tls, client_tls = _tls_contexts(tmp_path)
    server, port, requests = await _server(response, ssl_context=server_tls)
    resolutions: list[tuple[str, int]] = []

    async def resolver(host: str, requested_port: int) -> tuple[str, ...]:
        resolutions.append((host, requested_port))
        return ("127.0.0.1",)

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    try:
        result = await PinnedTransport(
            resolver=resolver,
            url_validator=_allow_test_url,
            validator=_allow_test_target,
            ssl_context=client_tls,
        ).fetch(f"https://public.test:{port}/page")
    finally:
        server.close()
        await server.wait_closed()

    assert result.body == b"ok"
    assert resolutions == [("public.test", port)]
    assert b"Host: public.test:" + str(port).encode() in requests[0]


async def test_private_redirect_is_blocked_before_second_connection() -> None:
    response = (
        b"HTTP/1.1 302 Found\r\n"
        b"Location: http://169.254.169.254/latest/meta-data\r\n"
        b"Content-Length: 0\r\n\r\n"
    )
    server, port, requests = await _server(response)

    async def resolver(host: str, requested_port: int) -> tuple[str, ...]:
        del requested_port
        return ("127.0.0.1",) if host == "public.test" else ("169.254.169.254",)

    def validator(url: str, addresses: tuple[str, ...], forbidden_cidrs=()) -> str:  # noqa: ANN001
        if url.startswith("http://public.test:"):
            return url
        return validate_target(url, addresses, forbidden_cidrs)

    try:
        with pytest.raises(URLBlocked):
            await PinnedTransport(
                resolver=resolver,
                url_validator=_allow_test_url,
                validator=validator,
            ).fetch(
                f"http://public.test:{port}/"
            )
    finally:
        server.close()
        await server.wait_closed()

    assert len(requests) == 1


async def test_429_is_not_retried() -> None:
    response = b"HTTP/1.1 429 Too Many Requests\r\nContent-Length: 0\r\n\r\n"
    server, port, requests = await _server(response)

    async def resolver(host: str, requested_port: int) -> tuple[str, ...]:
        del host, requested_port
        return ("127.0.0.1",)

    try:
        with pytest.raises(FetchFailed, match="web_unavailable"):
            await PinnedTransport(
                resolver=resolver,
                url_validator=_allow_test_url,
                validator=_allow_test_target,
            ).fetch(f"http://public.test:{port}/")
    finally:
        server.close()
        await server.wait_closed()

    assert len(requests) == 1


async def test_redirect_limit_is_three_hops() -> None:
    response = (
        b"HTTP/1.1 302 Found\r\nLocation: /loop\r\nContent-Length: 0\r\n\r\n"
    )
    server, port, requests = await _server(response)

    async def resolver(host: str, requested_port: int) -> tuple[str, ...]:
        del host, requested_port
        return ("127.0.0.1",)

    try:
        with pytest.raises(FetchFailed, match="web_unavailable"):
            await PinnedTransport(
                resolver=resolver,
                url_validator=_allow_test_url,
                validator=_allow_test_target,
            ).fetch(f"http://public.test:{port}/loop")
    finally:
        server.close()
        await server.wait_closed()

    assert len(requests) == 4


async def test_cancellation_closes_the_owned_connection() -> None:
    request_started = asyncio.Event()
    connection_closed = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            request_started.set()
            await reader.read()
            connection_closed.set()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def resolver(host: str, requested_port: int) -> tuple[str, ...]:
        del host, requested_port
        return ("127.0.0.1",)

    task = asyncio.create_task(
        PinnedTransport(
            resolver=resolver,
            url_validator=_allow_test_url,
            validator=_allow_test_target,
        ).fetch(
            f"http://public.test:{port}/"
        )
    )
    try:
        await asyncio.wait_for(request_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(connection_closed.wait(), timeout=1)
    finally:
        if not task.done():
            task.cancel()
        server.close()
        await server.wait_closed()


async def test_transient_connect_failure_has_at_most_two_attempts() -> None:
    resolutions = 0

    async def resolver(host: str, requested_port: int) -> tuple[str, ...]:
        nonlocal resolutions
        del host, requested_port
        resolutions += 1
        return ("127.0.0.1",)

    with pytest.raises(FetchFailed, match="web_unavailable"):
        await PinnedTransport(
            resolver=resolver,
            url_validator=_allow_test_url,
            validator=_allow_test_target,
        ).fetch("http://public.test:9/", deadline_seconds=1)

    assert resolutions == 2


async def test_dns_resolution_is_inside_the_shared_deadline() -> None:
    async def resolver(host: str, requested_port: int) -> tuple[str, ...]:
        del host, requested_port
        await asyncio.sleep(0.1)
        return ("8.8.8.8",)

    started = time.monotonic()
    with pytest.raises(FetchFailed, match="web_timeout"):
        await PinnedTransport(resolver=resolver).fetch(
            "https://example.org/",
            deadline_seconds=0.01,
        )

    assert time.monotonic() - started < 0.05


def test_download_buffer_bounds_wire_and_decoded_bytes() -> None:
    wire_limited = DownloadBuffer(content_encoding="identity", max_bytes=4)
    wire_limited.consume(b"1234")
    with pytest.raises(FetchFailed, match="web_too_large"):
        wire_limited.consume(b"5")

    compressed = gzip.compress(b"a" * 100)
    decoded_limited = DownloadBuffer(content_encoding="gzip", max_bytes=32)
    with pytest.raises(FetchFailed, match="web_too_large"):
        decoded_limited.consume(compressed)


def test_download_buffer_rejects_unknown_content_encoding() -> None:
    with pytest.raises(FetchFailed, match="web_unavailable"):
        DownloadBuffer(content_encoding="br", max_bytes=1024)
