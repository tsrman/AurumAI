"""The preview service against a fake backend and a fake skill-executor whose
stream reaches a small app: entering with a ticket, the cookie, the rewritten
requests and responses, refusals, and an upgraded connection spliced and cut."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import h11
import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from src.preview import COOKIE, Connection, Previews, Settings

LABEL = "a" * 32
OTHER = "b" * 32
SESSION = "c" * 32


class Backend:
    def __init__(self) -> None:
        self.allow = True
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.used: set[str] = set()

    async def handle(self, request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == "Bearer service-secret"
        body = await request.json()
        kind = request.match_info["kind"]
        self.calls.append((kind, body))
        ok = self.allow and body["preview"] == LABEL
        if kind == "redeem":
            ok = ok and body["ticket"] == "good-ticket" and "t" not in self.used
            self.used.add("t")
            answer = {"grant": "grant-1", "grantMaxAgeSeconds": 3600}
        else:
            ok = ok and body["grant"] == "grant-1"
            answer = {}
        if not ok:
            return web.json_response({"allow": False, "reason": "hermes_preview_denied", "maxAgeSeconds": 5})
        return web.json_response({"allow": True, "sessionId": SESSION, "port": 5173, "maxAgeSeconds": 0, **answer})


class App:
    """What the relay reaches: an HTTP app, spoken to over the stream."""

    def __init__(self) -> None:
        self.requests: list[h11.Request] = []
        self.streams = 0

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        assert request.headers["Authorization"] == "Bearer executor-secret"
        assert request.match_info["session"] == SESSION and request.query["port"] == "5173"
        self.streams += 1
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        http = h11.Connection(h11.SERVER)
        upgraded = False
        async for message in socket:
            if message.type != WSMsgType.BINARY:
                break
            if upgraded:
                await socket.send_bytes(b"echo:" + message.data)
                continue
            http.receive_data(message.data)
            while True:
                event = http.next_event()
                if event is h11.NEED_DATA or isinstance(event, h11.ConnectionClosed):
                    break
                if isinstance(event, h11.Request):
                    self.requests.append(event)
                    current = event
                if isinstance(event, h11.EndOfMessage):
                    if current.target == b"/ws":
                        await socket.send_bytes(http.send(h11.InformationalResponse(status_code=101, headers=[(b"upgrade", b"websocket"), (b"connection", b"Upgrade")])))
                        upgraded = True
                        break
                    if current.target == b"/redirect":
                        response = h11.Response(status_code=302, headers=[(b"location", b"http://localhost:5173/next"), (b"content-length", b"0")])
                        await socket.send_bytes(http.send(response) + http.send(h11.EndOfMessage()))
                    else:
                        body = json.dumps({"path": current.target.decode()}).encode()
                        response = h11.Response(
                            status_code=200,
                            headers=[
                                (b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode()),
                                (b"set-cookie", COOKIE.encode() + b"=stolen; Path=/"),
                                (b"set-cookie", b"theirs=1; Domain=evil.example; Path=/"),
                                (b"set-cookie", b"session=app; Path=/; HttpOnly"),
                            ],
                        )
                        await socket.send_bytes(http.send(response) + http.send(h11.Data(data=body)) + http.send(h11.EndOfMessage()))
                    http.start_next_cycle()
        return socket


@pytest.fixture
async def stack() -> AsyncIterator[tuple[int, Backend, App]]:
    backend, app = Backend(), App()
    service = web.Application()
    service.router.add_post("/v1/internal/hermes/previews/{kind}", backend.handle)
    service.router.add_get("/hermes/sandboxes/{session}/connect", app.handle)
    upstream = TestServer(service)
    await upstream.start_server()
    settings = Settings(
        origin="http://{preview}.preview.localhost:8095",
        backend_url=str(upstream.make_url("")).rstrip("/"),
        service_secret="service-secret",
        executor_url=str(upstream.make_url("")).rstrip("/").replace("http://", "ws://"),
        executor_secret="executor-secret",
        recheck_seconds=0.2,
    )
    import aiohttp

    async with aiohttp.ClientSession() as http:
        previews = Previews(settings, http)
        server = await asyncio.start_server(
            lambda reader, writer: Connection(previews, reader, writer).run(), "127.0.0.1", 0
        )
        port = server.sockets[0].getsockname()[1]
        async with server:
            yield port, backend, app
    await upstream.close()


async def _exchange(port: int, raw: bytes) -> tuple[int, dict[bytes, list[bytes]], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    client = h11.Connection(h11.CLIENT)
    client.send(h11.Request(method="GET", target="/", headers=[("host", "x")]))
    client.send(h11.EndOfMessage())
    status, headers, body = 0, {}, b""
    while True:
        event = client.next_event()
        if event is h11.NEED_DATA:
            client.receive_data(await reader.read(65536))
            continue
        if isinstance(event, h11.Response):
            status = event.status_code
            for name, value in event.headers:
                headers.setdefault(name, []).append(value)
        elif isinstance(event, h11.Data):
            body += event.data
        elif isinstance(event, (h11.EndOfMessage, h11.ConnectionClosed)):
            break
    writer.close()
    return status, headers, body


def _get(path: str, host: str = f"{LABEL}.preview.localhost:8095", cookie: str = "") -> bytes:
    lines = [f"GET {path} HTTP/1.1", f"Host: {host}"]
    if cookie:
        lines.append(f"Cookie: {cookie}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


async def test_a_ticket_enters_once_and_leaves_a_host_only_cookie(stack) -> None:
    port, backend, _ = stack

    status, headers, _ = await _exchange(port, _get("/__aurum/enter?ticket=good-ticket"))
    again, _, _ = await _exchange(port, _get("/__aurum/enter?ticket=good-ticket"))

    assert status == 303 and headers[b"location"] == [b"/"]
    [cookie] = headers[b"set-cookie"]
    assert cookie.startswith(f"{COOKIE}=grant-1;".encode())
    for attribute in (b"HttpOnly", b"Secure", b"SameSite=Lax", b"Path=/"):
        assert attribute in cookie
    assert b"Domain" not in cookie
    assert headers[b"referrer-policy"] == [b"no-referrer"]
    assert again == 403
    assert backend.calls[0] == ("redeem", {"preview": LABEL, "ticket": "good-ticket"})


async def test_requests_reach_the_app_rewritten_and_responses_come_back_cleaned(stack) -> None:
    port, _, app = stack

    status, headers, body = await _exchange(
        port, _get("/api/items?x=1", cookie=f"theme=dark; {COOKIE}=grant-1; other=2")
    )
    redirected, moved, _ = await _exchange(port, _get("/redirect", cookie=f"{COOKIE}=grant-1"))

    assert status == 200 and json.loads(body) == {"path": "/api/items?x=1"}
    sent = dict(app.requests[0].headers)
    # The app sees its own host and none of the preview's cookie.
    assert sent[b"host"] == b"localhost:5173"
    assert sent[b"cookie"] == b"theme=dark; other=2"
    assert headers[b"set-cookie"] == [b"session=app; Path=/; HttpOnly"]
    assert redirected == 302 and moved[b"location"] == [f"http://{LABEL}.preview.localhost:8095/next".encode()]


@pytest.mark.parametrize(
    ("request_bytes", "expected"),
    [
        (_get("/"), 403),
        (_get("/", cookie=f"{COOKIE}=forged"), 403),
        (_get("/", host=f"{OTHER}.preview.localhost:8095", cookie=f"{COOKIE}=grant-1"), 403),
        (_get("/", host="evil.example", cookie=f"{COOKIE}=grant-1"), 404),
        (_get("/health", host="localhost:8095"), 200),
    ],
)
async def test_without_a_valid_grant_for_this_origin_nothing_reaches_the_app(stack, request_bytes, expected) -> None:
    port, _, app = stack

    status, _, _ = await _exchange(port, request_bytes)

    assert status == expected and app.streams == 0


async def test_an_upgraded_connection_is_spliced_and_cut_once_the_preview_closes(stack) -> None:
    port, backend, _ = stack
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        (
            f"GET /ws HTTP/1.1\r\nHost: {LABEL}.preview.localhost:8095\r\nCookie: {COOKIE}=grant-1\r\n"
            "Connection: Upgrade\r\nUpgrade: websocket\r\n\r\n"
        ).encode()
    )
    head = await reader.readuntil(b"\r\n\r\n")
    writer.write(b"frame")
    echoed = await asyncio.wait_for(reader.read(100), 5)

    backend.allow = False
    closed = await asyncio.wait_for(reader.read(100), 5)

    assert head.startswith(b"HTTP/1.1 101")
    # RFC 6455: a browser, and any proxy in front, refuse the answer without both headers.
    lowered = {line.split(b":", 1)[0].strip().lower(): line.split(b":", 1)[1].strip().lower() for line in head.split(b"\r\n")[1:] if b":" in line}
    assert lowered.get(b"upgrade") == b"websocket" and lowered.get(b"connection") == b"upgrade"
    assert echoed == b"echo:frame"
    assert closed == b""


async def test_one_connection_carries_the_entry_and_the_requests_after_it(stack) -> None:
    """A browser keeps its connection after the redirect: the next request on
    it reaches the app."""
    port, _, app = stack
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    client = h11.Connection(h11.CLIENT)

    async def get(path: str, cookie: str = "") -> tuple[int, bytes]:
        headers = [("host", f"{LABEL}.preview.localhost:8095")] + ([("cookie", cookie)] if cookie else [])
        writer.write(client.send(h11.Request(method="GET", target=path, headers=headers)) + client.send(h11.EndOfMessage()))
        status, body = 0, b""
        while True:
            event = client.next_event()
            if event is h11.NEED_DATA:
                client.receive_data(await reader.read(65536))
            elif isinstance(event, h11.Response):
                status = event.status_code
            elif isinstance(event, h11.Data):
                body += event.data
            elif isinstance(event, h11.EndOfMessage):
                client.start_next_cycle()
                return status, body

    entered, _ = await get("/__aurum/enter?ticket=good-ticket")
    first, body = await get("/one", f"{COOKIE}=grant-1")
    second, _ = await get("/two", f"{COOKIE}=grant-1")
    writer.close()

    assert (entered, first, second) == (303, 200, 200) and json.loads(body) == {"path": "/one"}
    # The app's connection is reused across the requests.
    assert app.streams == 1
