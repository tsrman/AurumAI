"""hermes-egress proxy: platform allowlist, CONNECT policy and tunnel revocation."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import logging
import ssl
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("hermes_proxy", ROOT / "infra" / "web-egress" / "hermes_proxy.py")
assert _spec is not None and _spec.loader is not None
proxy = importlib.util.module_from_spec(_spec)
sys.modules["hermes_proxy"] = proxy
_spec.loader.exec_module(proxy)

TOKEN = "hermes-session-token-value"
PUBLIC = "93.184.216.34"


def client_hello(host: str) -> bytes:
    context = ssl.create_default_context()
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = context.wrap_bio(incoming, outgoing, server_hostname=host)
    with pytest.raises(ssl.SSLWantReadError):
        tls.do_handshake()
    return outgoing.read()


async def _server(handler) -> tuple[asyncio.AbstractServer, int]:  # noqa: ANN001
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _exchange(port: int, data: bytes, *, read_until_close: bool = True) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(data)
    await writer.drain()
    try:
        return await asyncio.wait_for(reader.read(-1), 5) if read_until_close else b""
    finally:
        writer.close()


# Platform endpoints -----------------------------------------------------------


class Backend:
    def __init__(self, reply: bytes = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok") -> None:
        self.received = b""
        self.reply = reply

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.received = await reader.read(-1) if False else b""
        try:
            while True:
                chunk = await asyncio.wait_for(reader.read(65536), 0.3)
                if not chunk:
                    break
                self.received += chunk
        except TimeoutError:
            pass
        writer.write(self.reply)
        await writer.drain()
        writer.close()


async def _platform(backend: Backend) -> tuple[list[Any], int]:
    upstream, upstream_port = await _server(backend.handle)
    config = proxy.PlatformConfig("backend", 8000)

    async def connect(host: str, port: int):  # noqa: ANN202
        assert (host, port) == ("backend", 8000)
        return await asyncio.open_connection("127.0.0.1", upstream_port)

    config.connect = connect
    server, port = await _server(lambda r, w: proxy.handle_platform(r, w, config))
    return [upstream, server], port


def _request(target: str = "/v1/hermes/chat/completions", body: bytes = b'{"a":1}', extra: str = "") -> bytes:
    return (
        f"POST {target} HTTP/1.1\r\nHost: hermes-egress\r\nAuthorization: Bearer t\r\n"
        f"Proxy-Authorization: Basic secret\r\nConnection: keep-alive\r\n{extra}"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode() + body


@pytest.mark.asyncio
async def test_platform_forwards_exactly_one_parsed_request() -> None:
    backend = Backend()
    servers, port = await _platform(backend)
    smuggled = b"GET /v1/internal/hermes/sessions HTTP/1.1\r\nHost: x\r\n\r\n"
    try:
        reply = await _exchange(port, _request() + smuggled)
    finally:
        for server in servers:
            server.close()

    assert reply.endswith(b"ok")
    head, _, body = backend.received.partition(b"\r\n\r\n")
    assert body == b'{"a":1}'
    assert b"/v1/internal" not in backend.received
    lines = head.decode().split("\r\n")
    assert lines[0] == "POST /v1/hermes/chat/completions HTTP/1.1"
    assert "Host: backend:8000" in lines and "Connection: close" in lines
    assert "authorization: Bearer t" in lines
    assert not any(line.lower().startswith(("proxy-authorization", "connection: keep")) for line in lines)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        "/v1/internal/hermes/sessions",
        "/v1/hermes/chat%2Fcompletions",
        "/v1/hermes/chat/completions?x=1",
        "//v1/hermes/chat/completions",
        "/v1/hermes/../internal/hermes/sessions",
        "http://backend:8000/v1/hermes/chat/completions",
    ],
)
async def test_platform_refuses_every_other_target(target: str) -> None:
    backend = Backend()
    servers, port = await _platform(backend)
    try:
        reply = await _exchange(port, _request(target))
    finally:
        for server in servers:
            server.close()

    assert reply.startswith(b"HTTP/1.1 404")
    assert backend.received == b""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "status"),
    [
        (b"POST /v1/hermes/chat/completions HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n", b"400"),
        (b"POST /v1/hermes/chat/completions HTTP/1.1\r\nContent-Length: 1\r\nContent-Length: 1\r\n\r\nxx", b"411"),
        (b"POST /v1/hermes/chat/completions HTTP/1.1\r\n\r\n", b"411"),
        (b"POST /v1/hermes/chat/completions HTTP/1.1\r\nContent-Length: 99999999999\r\n\r\n", b"413"),
        (b"POST /v1/hermes/chat/completions HTTP/1.0\r\nContent-Length: 0\r\n\r\n", b"400"),
        (b"POST /v1/hermes/chat/completions HTTP/1.1\r\nX: a\r\n folded\r\nContent-Length: 0\r\n\r\n", b"400"),
    ],
)
async def test_platform_refuses_ambiguous_framing(raw: bytes, status: bytes) -> None:
    backend = Backend()
    servers, port = await _platform(backend)
    try:
        reply = await _exchange(port, raw)
    finally:
        for server in servers:
            server.close()

    assert reply.split(b" ")[1] == status
    assert backend.received == b""


@pytest.mark.asyncio
async def test_platform_streams_the_response_without_buffering() -> None:
    release = asyncio.Event()

    async def streaming(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        await reader.readexactly(7)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\ndata: first\n\n")
        await writer.drain()
        await release.wait()
        writer.write(b"data: [DONE]\n\n")
        writer.close()

    upstream, upstream_port = await _server(streaming)
    config = proxy.PlatformConfig("backend", 8000)
    config.connect = lambda host, port: asyncio.open_connection("127.0.0.1", upstream_port)
    server, port = await _server(lambda r, w: proxy.handle_platform(r, w, config))
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(_request())
        await writer.drain()
        first = await asyncio.wait_for(reader.readuntil(b"data: first\n\n"), 5)
        release.set()
        rest = await asyncio.wait_for(reader.read(-1), 5)
    finally:
        writer.close()
        server.close()
        upstream.close()

    assert first.startswith(b"HTTP/1.1 200")
    assert rest == b"data: [DONE]\n\n"


# Forward proxy ----------------------------------------------------------------


class Authority:
    def __init__(self, mode: str = "packages", allow: bool = True) -> None:
        self.mode = mode
        self.allow = allow
        self.tokens: list[str] = []

    async def __call__(self, token: str) -> Any:
        self.tokens.append(token)
        return proxy.Decision(
            allow=self.allow and token == TOKEN,
            mode=self.mode,
            session_id="session-1",
            turn_id="turn-1",
            reason="" if self.allow else "hermes_session_revoked",
            max_age=0,
        )


class Upstreams:
    """Records where the proxy connects and serves an echo endpoint for all of it."""

    def __init__(self) -> None:
        self.connected: list[tuple[str, int]] = []
        self.port = 0
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while chunk := await reader.read(65536):
                    writer.write(chunk)
                    await writer.drain()
            except OSError:
                pass
            writer.close()

        self.server, self.port = await _server(echo)

    async def connect(self, host: str, port: int):  # noqa: ANN201
        self.connected.append((host, port))
        return await asyncio.open_connection("127.0.0.1", self.port)


async def _forward(
    authority: Authority,
    *,
    records: dict[str, list[str]] | None = None,
    **config: Any,
) -> tuple[Any, int, Upstreams, list[str]]:
    upstreams = Upstreams()
    await upstreams.start()
    resolved: list[str] = []
    table = {"pypi.org": [PUBLIC], "example.com": [PUBLIC], **(records or {})}

    async def resolve(host: str, port: int) -> list[str]:
        resolved.append(host)
        if host not in table:
            raise OSError("unknown host")
        return table[host]

    settings = proxy.ProxyConfig(
        max_mode=config.pop("max_mode", "public"),
        package_hosts=config.pop("package_hosts", ("pypi.org", "files.pythonhosted.org")),
        resolve=resolve,
        connect=upstreams.connect,
        **config,
    )
    state = proxy.ProxyState(settings, authority)
    server, port = await _server(lambda r, w: proxy.handle_proxy(r, w, state))
    return (server, state), port, upstreams, resolved


def _connect(target: str, token: str | None = TOKEN, scheme: str = "basic") -> bytes:
    auth = ""
    if token is not None:
        credential = base64.b64encode(f"hermes:{token}".encode()).decode() if scheme == "basic" else token
        auth = f"Proxy-Authorization: {'Basic' if scheme == 'basic' else 'Bearer'} {credential}\r\n"
    return f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n{auth}\r\n".encode()


async def _tunnel(port: int, target: str, payload: bytes, **kwargs: Any) -> tuple[bytes, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(_connect(target, **kwargs))
        await writer.drain()
        status = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        if not status.startswith(b"HTTP/1.1 200"):
            return status + await asyncio.wait_for(reader.read(-1), 5), b""
        writer.write(payload)
        await writer.drain()
        try:
            echoed = await asyncio.wait_for(reader.read(len(payload)), 2)
        except TimeoutError:
            echoed = b""
        return status, echoed
    finally:
        writer.close()


async def _stop(handle: tuple[Any, Any], upstreams: Upstreams) -> None:
    handle[0].close()
    if upstreams.server is not None:
        upstreams.server.close()


@pytest.mark.asyncio
async def test_packages_mode_tunnels_to_a_listed_index_with_matching_sni() -> None:
    handle, port, upstreams, resolved = await _forward(Authority("packages"))
    hello = client_hello("pypi.org")
    try:
        status, echoed = await _tunnel(port, "pypi.org:443", hello)
    finally:
        await _stop(handle, upstreams)

    assert status.startswith(b"HTTP/1.1 200")
    assert echoed == hello
    assert resolved == ["pypi.org"]
    assert upstreams.connected == [(PUBLIC, 443)]


@pytest.mark.asyncio
async def test_packages_mode_closes_a_tunnel_whose_sni_differs() -> None:
    handle, port, upstreams, _ = await _forward(Authority("packages"))
    try:
        status, echoed = await _tunnel(port, "pypi.org:443", client_hello("attacker.example"))
        _, plain = await _tunnel(port, "pypi.org:443", b"GET / HTTP/1.1\r\n\r\n")
    finally:
        await _stop(handle, upstreams)

    assert status.startswith(b"HTTP/1.1 200")
    assert echoed == b"" and plain == b""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "target", "reason"),
    [
        ("none", "pypi.org:443", b"egress_disabled"),
        ("packages", "example.com:443", b"host_not_allowed"),
        ("packages", "pypi.org:80", b"host_not_allowed"),
        ("public", "example.com:22", b"port_not_allowed"),
    ],
)
async def test_modes_limit_hosts_and_ports(mode: str, target: str, reason: bytes) -> None:
    handle, port, upstreams, _ = await _forward(Authority(mode))
    try:
        status, _ = await _tunnel(port, target, b"x")
    finally:
        await _stop(handle, upstreams)

    assert status.startswith(b"HTTP/1.1 403") and reason in status
    assert upstreams.connected == []


@pytest.mark.asyncio
async def test_the_operator_maximum_caps_the_backend_mode() -> None:
    handle, port, upstreams, _ = await _forward(Authority("public"), max_mode="packages")
    try:
        status, _ = await _tunnel(port, "example.com:443", b"x")
    finally:
        await _stop(handle, upstreams)

    assert b"host_not_allowed" in status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "addresses",
    [["10.0.0.5"], ["127.0.0.1"], ["::ffff:192.168.1.1"], [PUBLIC, "172.17.0.2"], ["169.254.169.254"], ["fd00::1"]],
)
async def test_public_mode_refuses_any_private_resolution(addresses: list[str]) -> None:
    handle, port, upstreams, _ = await _forward(Authority("public"), records={"internal.example": addresses})
    try:
        status, _ = await _tunnel(port, "internal.example:443", b"x")
    finally:
        await _stop(handle, upstreams)

    assert b"address_forbidden" in status
    assert upstreams.connected == []


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["10.0.0.5:443", "[::1]:443", "127.0.0.1:80"])
async def test_private_ip_literals_are_refused(target: str) -> None:
    handle, port, upstreams, _ = await _forward(
        Authority("public"), records={"10.0.0.5": ["10.0.0.5"], "::1": ["::1"], "127.0.0.1": ["127.0.0.1"]}
    )
    try:
        status, _ = await _tunnel(port, target, b"x")
    finally:
        await _stop(handle, upstreams)

    assert b"address_forbidden" in status


@pytest.mark.asyncio
async def test_operator_forbidden_ranges_apply_to_public_addresses() -> None:
    import ipaddress

    handle, port, upstreams, _ = await _forward(
        Authority("public"),
        forbidden_networks=proxy.FORBIDDEN_NETWORKS + (ipaddress.ip_network("93.184.216.0/24"),),
    )
    try:
        status, _ = await _tunnel(port, "example.com:443", b"x")
    finally:
        await _stop(handle, upstreams)

    assert b"address_forbidden" in status


@pytest.mark.asyncio
async def test_an_explicit_internal_mirror_is_the_only_private_exception() -> None:
    handle, port, upstreams, _ = await _forward(
        Authority("packages"),
        records={"mirror.internal": ["10.1.2.3"], "other.internal": ["10.1.2.4"]},
        internal_exceptions=frozenset({("mirror.internal", 8443)}),
        package_hosts=("pypi.org", "mirror.internal", "other.internal"),
    )
    try:
        allowed, echoed = await _tunnel(port, "mirror.internal:8443", b"plain")
        refused, _ = await _tunnel(port, "other.internal:443", client_hello("other.internal"))
    finally:
        await _stop(handle, upstreams)

    assert allowed.startswith(b"HTTP/1.1 200") and echoed == b"plain"
    assert b"address_forbidden" in refused
    assert upstreams.connected == [("10.1.2.3", 8443)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "status"),
    [
        (b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n", b"405"),
        (b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n", b"407"),
        (b"CONNECT example.com HTTP/1.1\r\nHost: example.com\r\n\r\n", b"400"),
        (b"CONNECT exa_mple.com:443 HTTP/1.1\r\nProxy-Authorization: Bearer x\r\n\r\n", b"400"),
    ],
)
async def test_forward_proxy_accepts_only_authenticated_connect(raw: bytes, status: bytes) -> None:
    handle, port, upstreams, _ = await _forward(Authority("public"))
    try:
        reply = await _exchange(port, raw)
    finally:
        await _stop(handle, upstreams)

    assert reply.split(b" ")[1] == status
    if status == b"407":
        assert b"Proxy-Authenticate: Basic" in reply


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["basic", "bearer"])
async def test_token_is_accepted_as_basic_password_or_bearer(scheme: str) -> None:
    authority = Authority("public")
    handle, port, upstreams, _ = await _forward(authority)
    try:
        status, _ = await _tunnel(port, "Example.COM.:443", b"x", scheme=scheme)
    finally:
        await _stop(handle, upstreams)

    assert status.startswith(b"HTTP/1.1 200")
    assert authority.tokens == [TOKEN]


@pytest.mark.asyncio
async def test_a_denied_session_cannot_open_tunnels() -> None:
    handle, port, upstreams, _ = await _forward(Authority("public", allow=False))
    try:
        status, _ = await _tunnel(port, "example.com:443", b"x")
    finally:
        await _stop(handle, upstreams)

    assert status.startswith(b"HTTP/1.1 403") and b"hermes_session_revoked" in status


@pytest.mark.asyncio
async def test_recheck_closes_open_tunnels_after_revocation() -> None:
    authority = Authority("public")
    handle, port, upstreams, _ = await _forward(authority)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(_connect("example.com:443"))
        await writer.drain()
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        writer.write(b"ping")
        assert await asyncio.wait_for(reader.read(4), 5) == b"ping"
        authority.allow = False
        await proxy.recheck_once(handle[1])
        closed = await asyncio.wait_for(reader.read(-1), 5)
        # The handler forgets the tunnel once both directions have finished.
        for _ in range(100):
            if not handle[1].tunnels:
                break
            await asyncio.sleep(0.02)
    finally:
        writer.close()
        await _stop(handle, upstreams)

    assert closed == b""
    assert handle[1].tunnels == set()


@pytest.mark.asyncio
async def test_recheck_closes_tunnels_the_lowered_mode_no_longer_allows() -> None:
    authority = Authority("public")
    handle, port, upstreams, _ = await _forward(authority)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(_connect("example.com:443"))
        await writer.drain()
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        authority.mode = "packages"
        await proxy.recheck_once(handle[1])
        closed = await asyncio.wait_for(reader.read(-1), 5)
    finally:
        writer.close()
        await _stop(handle, upstreams)

    assert closed == b""


@pytest.mark.asyncio
async def test_connection_and_byte_limits_per_session() -> None:
    handle, port, upstreams, _ = await _forward(
        Authority("public"), max_connections_per_session=1, max_bytes_per_turn=8
    )
    first_reader, first_writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        first_writer.write(_connect("example.com:443"))
        await first_writer.drain()
        await asyncio.wait_for(first_reader.readuntil(b"\r\n\r\n"), 5)
        second, _ = await _tunnel(port, "example.com:443", b"x")
        first_writer.write(b"0123456789")
        await first_writer.drain()
        tail = await asyncio.wait_for(first_reader.read(-1), 5)
    finally:
        first_writer.close()
        await _stop(handle, upstreams)

    assert b"connection_limit" in second
    assert len(tail) < 10
    third_handle, third_port, third_upstreams, _ = await _forward(Authority("public"), max_bytes_per_turn=8)
    third_handle[1].turn_bytes[("session-1", "turn-1")] = 8
    try:
        exhausted, _ = await _tunnel(third_port, "example.com:443", b"x")
    finally:
        await _stop(third_handle, third_upstreams)
    assert b"turn_byte_limit" in exhausted


@pytest.mark.asyncio
async def test_journal_records_decisions_without_credentials(caplog) -> None:
    handle, port, upstreams, _ = await _forward(Authority("packages"))
    with caplog.at_level(logging.INFO, logger="hermes-egress"):
        try:
            await _tunnel(port, "example.com:443", b"x")
            await _tunnel(port, "pypi.org:443", client_hello("pypi.org"))
            await asyncio.sleep(0.2)
        finally:
            await _stop(handle, upstreams)

    records = [json.loads(r.getMessage()) for r in caplog.records if r.name == "hermes-egress"]
    refused = next(r for r in records if r.get("reason") == "host_not_allowed")
    assert (refused["session"], refused["turn"], refused["mode"]) == ("session-1", "turn-1", "packages")
    assert any(r.get("outcome") == "tunnel" for r in records)
    assert TOKEN not in caplog.text
    assert "Proxy-Authorization" not in caplog.text


def test_server_name_parser_reads_real_client_hellos() -> None:
    assert proxy.tls_server_name(client_hello("files.pythonhosted.org")) == "files.pythonhosted.org"
    assert proxy.tls_server_name(b"GET / HTTP/1.1\r\n\r\n") is None
    assert proxy.tls_server_name(client_hello("pypi.org")[:20]) is None


def test_host_patterns_match_exactly_or_by_subdomain() -> None:
    assert proxy.host_matches("pypi.org", ("pypi.org",))
    assert not proxy.host_matches("evilpypi.org", ("pypi.org",))
    assert proxy.host_matches("a.mirror.example", ("*.mirror.example",))
    assert not proxy.host_matches("mirror.example", ("*.mirror.example",))
    assert not proxy.host_matches("xmirror.example", ("*.mirror.example",))


@pytest.mark.asyncio
async def test_repeated_failed_rechecks_close_open_tunnels() -> None:
    authority = Authority("public")
    handle, port, upstreams, _ = await _forward(authority)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)

    async def unavailable(token: str) -> Any:
        raise OSError("backend unreachable")

    try:
        writer.write(_connect("example.com:443"))
        await writer.drain()
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        handle[1].authorize = unavailable
        for _ in range(proxy.MAX_FAILED_RECHECKS - 1):
            await proxy.recheck_once(handle[1])
        still_open = len(handle[1].tunnels)
        await proxy.recheck_once(handle[1])
        closed = await asyncio.wait_for(reader.read(-1), 5)
    finally:
        writer.close()
        await _stop(handle, upstreams)

    assert still_open == 1
    assert closed == b""


@pytest.mark.asyncio
async def test_state_does_not_grow_with_finished_turns_and_connections() -> None:
    authority = Authority("public")
    handle, port, upstreams, _ = await _forward(authority)
    state = handle[1]
    try:
        await _tunnel(port, "example.com:443", b"x")
        state.turn_bytes[("session-1", "turn-0")] = 100
        await state.decision(TOKEN, fresh=True)
        await asyncio.sleep(0.05)
        state.prune()
    finally:
        await _stop(handle, upstreams)

    assert ("session-1", "turn-0") not in state.turn_bytes
    assert state.connections == {}
    assert state.cache == {}


@pytest.mark.asyncio
async def test_a_tunnel_both_sides_close_ends_cleanly(caplog) -> None:
    handle, port, upstreams, _ = await _forward(Authority("public"))
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    with caplog.at_level(logging.ERROR):
        try:
            writer.write(_connect("example.com:443"))
            await writer.drain()
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            writer.write(b"bye")
            await writer.drain()
            writer.write_eof()
            assert await asyncio.wait_for(reader.read(-1), 5) == b"bye"
            for _ in range(100):
                if not handle[1].tunnels:
                    break
                await asyncio.sleep(0.02)
        finally:
            writer.close()
            await _stop(handle, upstreams)

    assert handle[1].tunnels == set()
    assert "Set of Tasks/Futures is empty" not in caplog.text
