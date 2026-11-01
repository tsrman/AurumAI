"""Previews of the apps Hermes Agent runs in its sandbox (docs/HERMES_AGENT.md).

Each preview has an origin of its own, `<preview>` in HERMES_PREVIEW_ORIGIN,
never the platform's. A browser arrives with a one-time ticket at
`/__aurum/enter`, which the backend exchanges for the preview's grant; that is
kept in a host-only cookie, and every new connection is checked with the
backend. Requests then go, rewritten, to the app through skill-executor's
stream into the sandbox (`hermes-relay connect`); an upgraded connection
(WebSocket, a dev server's reload) is spliced as bytes and rechecked while it
lasts. Nothing the app says reaches the platform; the platform's credentials
never reach the app.
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp
import h11

logger = logging.getLogger("hermes-preview")

COOKIE = "__Host-aurum-preview"
ENTER_PATH = "/__aurum/enter"
_LABEL = re.compile(r"^[0-9a-f]{32}$")
# Not passed on in either direction: they describe one hop.
_HOP = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"proxy-connection",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
        b"content-length",
    }
)


@dataclass(frozen=True)
class Settings:
    origin: str
    backend_url: str
    service_secret: str
    executor_url: str
    executor_secret: str
    port: int = 8095
    max_connections_per_preview: int = 32
    max_body_bytes: int = 50 * 1024 * 1024
    idle_seconds: float = 300.0
    recheck_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "Settings":
        required = ("HERMES_PREVIEW_ORIGIN", "HERMES_PREVIEW_SERVICE_SECRET", "HERMES_PREVIEW_EXECUTOR_SECRET")
        missing = [name for name in required if not os.environ.get(name)]
        if missing or "{preview}" not in os.environ["HERMES_PREVIEW_ORIGIN"]:
            raise SystemExit(f"hermes-preview needs {', '.join(missing) or 'an origin with {preview}'}")
        return cls(
            origin=os.environ["HERMES_PREVIEW_ORIGIN"],
            backend_url=os.environ["HERMES_PREVIEW_BACKEND_URL"].rstrip("/"),
            service_secret=os.environ["HERMES_PREVIEW_SERVICE_SECRET"],
            executor_url=os.environ["HERMES_PREVIEW_EXECUTOR_URL"].rstrip("/"),
            executor_secret=os.environ["HERMES_PREVIEW_EXECUTOR_SECRET"],
            port=int(os.environ.get("HERMES_PREVIEW_PORT", "8095")),
            max_connections_per_preview=int(os.environ.get("HERMES_PREVIEW_MAX_CONNECTIONS", "32")),
            max_body_bytes=int(os.environ.get("HERMES_PREVIEW_MAX_BODY_BYTES", str(50 * 1024 * 1024))),
            idle_seconds=float(os.environ.get("HERMES_PREVIEW_IDLE_SECONDS", "300")),
            recheck_seconds=float(os.environ.get("HERMES_PREVIEW_RECHECK_SECONDS", "30")),
        )

    @property
    def secure(self) -> bool:
        return self.origin.startswith("https://")

    def host_pattern(self) -> re.Pattern[str]:
        """The preview's host, as the origin template names it."""
        netloc = urlsplit(self.origin.replace("{preview}", "PREVIEWLABEL")).netloc.lower()
        return re.compile("^" + re.escape(netloc).replace("previewlabel", "([0-9a-f]{32})") + "$")


@dataclass
class Decision:
    allow: bool
    reason: str = ""
    session_id: str = ""
    port: int = 0
    grant: str = ""
    grant_max_age: int = 0
    expires: float = 0.0


@dataclass
class Previews:
    """Answers of the backend, reused for a few seconds, and live connections."""

    settings: Settings
    http: aiohttp.ClientSession
    decisions: dict[tuple[str, str], Decision] = field(default_factory=dict)
    connections: dict[str, int] = field(default_factory=dict)

    async def _ask(self, path: str, body: dict[str, str]) -> Decision:
        try:
            async with self.http.post(
                f"{self.settings.backend_url}/v1/internal/hermes/previews/{path}",
                json=body,
                headers={"Authorization": f"Bearer {self.settings.service_secret}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                answer = await response.json() if response.status == 200 else {}
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            answer = {}
        if not answer.get("allow"):
            return Decision(False, str(answer.get("reason") or "hermes_preview_unavailable"))
        return Decision(
            True,
            session_id=str(answer["sessionId"]),
            port=int(answer["port"]),
            grant=str(answer.get("grant") or ""),
            grant_max_age=int(answer.get("grantMaxAgeSeconds") or 0),
            expires=time.monotonic() + float(answer.get("maxAgeSeconds") or 0),
        )

    async def redeem(self, label: str, ticket: str) -> Decision:
        return await self._ask("redeem", {"preview": label, "ticket": ticket})

    async def check(self, label: str, grant: str, fresh: bool = False) -> Decision:
        key = (label, grant)
        cached = self.decisions.get(key)
        if not fresh and cached is not None and cached.expires > time.monotonic():
            return cached
        decision = await self._ask("check", {"preview": label, "grant": grant})
        if decision.allow:
            self.decisions[key] = decision
        else:
            self.decisions.pop(key, None)
        return decision


class Upstream:
    """One stream to the app in the sandbox, as an HTTP/1.1 client."""

    def __init__(self, socket: aiohttp.ClientWebSocketResponse) -> None:
        self.socket = socket
        self.http = h11.Connection(h11.CLIENT)

    async def send(self, event: Any) -> None:
        data = self.http.send(event)
        if data:
            await self.socket.send_bytes(data)

    async def next_event(self) -> Any:
        while True:
            event = self.http.next_event()
            if event is not h11.NEED_DATA:
                return event
            message = await self.socket.receive()
            if message.type == aiohttp.WSMsgType.BINARY:
                self.http.receive_data(message.data)
            else:
                self.http.receive_data(b"")


def _cookie(header: bytes) -> str:
    cookies: SimpleCookie = SimpleCookie()
    try:
        cookies.load(header.decode("latin-1"))
    except Exception:  # noqa: BLE001 - a malformed header carries no grant
        return ""
    morsel = cookies.get(COOKIE)
    return morsel.value if morsel else ""


def _without_our_cookie(header: bytes) -> bytes:
    parts = [part.strip() for part in header.split(b";")]
    return b"; ".join(part for part in parts if part and not part.startswith(COOKIE.encode() + b"="))


def _page(status: int, title: str, text: str) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    body = (
        "<!doctype html><meta charset=utf-8><title>{0}</title>"
        "<body style='font-family:system-ui;margin:4rem auto;max-width:36rem'><h1>{0}</h1><p>{1}</p>"
    ).format(title, text).encode()
    headers = [
        (b"content-type", b"text/html; charset=utf-8"),
        (b"cache-control", b"no-store"),
        (b"referrer-policy", b"no-referrer"),
    ]
    return status, headers, body


def _denied(reason: str) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    if reason == "hermes_preview_closed":
        return _page(
            410,
            "This preview has stopped",
            "Hermes Agent's session has ended. Ask Hermes to start the app again and open the new preview.",
        )
    return _page(403, "This preview is not available", "Open it again from your conversation.")


class Connection:
    """One browser connection to a preview's origin."""

    def __init__(self, previews: Previews, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.previews = previews
        self.settings = previews.settings
        self.reader = reader
        self.writer = writer
        self.http = h11.Connection(h11.SERVER, max_incomplete_event_size=64 * 1024)
        self.upstream: Upstream | None = None
        self.target: tuple[str, int] | None = None
        self.label = ""

    async def _send(self, event: Any) -> None:
        data = self.http.send(event)
        if data:
            self.writer.write(data)
            await self.writer.drain()

    async def _respond(self, status: int, headers: list[tuple[bytes, bytes]], body: bytes) -> None:
        await self._send(h11.Response(status_code=status, headers=[*headers, (b"content-length", str(len(body)).encode())]))
        await self._send(h11.Data(data=body))
        await self._send(h11.EndOfMessage())

    async def _next_event(self) -> Any:
        while True:
            event = self.http.next_event()
            if event is not h11.NEED_DATA:
                return event
            data = await asyncio.wait_for(self.reader.read(65536), self.settings.idle_seconds)
            self.http.receive_data(data)

    async def run(self) -> None:
        try:
            while True:
                event = await self._next_event()
                if not isinstance(event, h11.Request):
                    return
                if not await self._handle(event):
                    return
                if self.http.our_state is h11.MUST_CLOSE or self.http.their_state is h11.MUST_CLOSE:
                    return
                self.http.start_next_cycle()
        except (h11.RemoteProtocolError, asyncio.TimeoutError, ConnectionError, aiohttp.ClientError):
            return
        finally:
            await self._close_upstream()
            self.writer.close()

    async def _close_upstream(self) -> None:
        if self.upstream is not None:
            await self.upstream.socket.close()
            self.upstream = None
            self.target = None

    async def _drain(self) -> None:
        """Read the rest of a request the service answers itself, so the
        connection can carry the next one."""
        received = 0
        while self.http.their_state is h11.SEND_BODY:
            event = await self._next_event()
            if isinstance(event, h11.Data):
                received += len(event.data)
                if received > self.settings.max_body_bytes:
                    raise h11.RemoteProtocolError("request body too large")

    async def _handle(self, request: h11.Request) -> bool:
        headers = {name.lower(): value for name, value in request.headers}
        target = urlsplit(request.target.decode("latin-1"))
        grant = _cookie(headers.get(b"cookie", b""))
        host = self.settings.host_pattern().match(headers.get(b"host", b"").decode("latin-1").lower())
        if host is None or target.path == ENTER_PATH or not grant:
            await self._drain()
        match = host
        if match is None and request.target == b"/health":
            await self._respond(200, [(b"content-type", b"text/plain")], b"ok")
            return False
        if match is None:
            await self._respond(*_page(404, "Not found", "There is no preview here."))
            return False
        self.label = match.group(1)
        if target.path == ENTER_PATH:
            return await self._enter(target.query)
        decision = await self.previews.check(self.label, grant) if grant else Decision(False, "hermes_preview_denied")
        if not decision.allow:
            await self._drain()
            await self._respond(*_denied(decision.reason))
            return False
        return await self._proxy(request, decision, grant)

    async def _enter(self, query: str) -> bool:
        ticket = (parse_qs(query).get("ticket") or [""])[0]
        decision = await self.previews.redeem(self.label, ticket) if ticket else Decision(False, "hermes_preview_denied")
        if not decision.allow:
            await self._respond(*_denied(decision.reason))
            return False
        # Host-only, never readable by the app's scripts, gone from the address bar.
        cookie = f"{COOKIE}={decision.grant}; Path=/; Max-Age={decision.grant_max_age}; HttpOnly; Secure; SameSite=Lax"
        await self._respond(
            303,
            [
                (b"location", b"/"),
                (b"set-cookie", cookie.encode()),
                (b"cache-control", b"no-store"),
                (b"referrer-policy", b"no-referrer"),
            ],
            b"",
        )
        return True

    async def _connect(self, decision: Decision) -> Upstream:
        target = (decision.session_id, decision.port)
        if self.upstream is not None and self.target == target and not self.upstream.socket.closed:
            return self.upstream
        await self._close_upstream()
        socket = await self.previews.http.ws_connect(
            f"{self.settings.executor_url}/hermes/sandboxes/{decision.session_id}/connect?port={decision.port}",
            headers={"Authorization": f"Bearer {self.settings.executor_secret}"},
            max_msg_size=0,
        )
        self.upstream, self.target = Upstream(socket), target
        return self.upstream

    def _request_headers(self, request: h11.Request, port: int, upgrade: bool) -> list[tuple[bytes, bytes]]:
        headers: list[tuple[bytes, bytes]] = [(b"host", f"localhost:{port}".encode())]
        for name, value in request.headers:
            lowered = name.lower()
            if lowered == b"host" or (lowered in _HOP and not (upgrade and lowered == b"upgrade")):
                continue
            if lowered == b"cookie":
                value = _without_our_cookie(value)
                if not value:
                    continue
            headers.append((lowered, value))
        if upgrade:
            headers.append((b"connection", b"Upgrade"))
        return headers

    def _response_headers(self, response: Any, port: int) -> list[tuple[bytes, bytes]]:
        origin = self.settings.origin.replace("{preview}", self.label).rstrip("/").encode()
        local = re.compile(rb"^https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::" + str(port).encode() + rb")?(?=/|$)")
        headers: list[tuple[bytes, bytes]] = []
        for name, value in response.headers:
            lowered = name.lower()
            if lowered in _HOP and lowered != b"upgrade":
                continue
            if lowered == b"location":
                value = local.sub(origin, value)
            if lowered == b"set-cookie":
                # The app cannot replace the preview's cookie or reach another origin.
                cookie_name = value.split(b"=", 1)[0].strip()
                if cookie_name == COOKIE.encode() or re.search(rb";\s*domain\s*=", value, re.IGNORECASE):
                    continue
            headers.append((lowered, value))
        return headers

    async def _proxy(self, request: h11.Request, decision: Decision, grant: str) -> bool:
        connection_header = dict((n.lower(), v.lower()) for n, v in request.headers).get(b"connection", b"")
        upgrade = b"upgrade" in connection_header and any(n.lower() == b"upgrade" for n, _ in request.headers)
        connections = self.previews.connections
        if connections.get(self.label, 0) >= self.settings.max_connections_per_preview:
            await self._respond(*_page(503, "Too many connections", "Try again in a moment."))
            return False
        connections[self.label] = connections.get(self.label, 0) + 1
        started, sent = time.monotonic(), 0
        try:
            upstream = await self._connect(decision)
            await upstream.send(
                h11.Request(
                    method=request.method,
                    target=request.target,
                    headers=self._request_headers(request, decision.port, upgrade),
                )
            )
            received = 0
            while True:
                event = await self._next_event()
                if isinstance(event, h11.Data):
                    received += len(event.data)
                    if received > self.settings.max_body_bytes:
                        await self._respond(*_page(413, "Too large", "The request is too large."))
                        return False
                    await upstream.send(event)
                elif isinstance(event, h11.EndOfMessage):
                    await upstream.send(h11.EndOfMessage())
                    break
                else:
                    return False
            response = await upstream.next_event()
            if isinstance(response, h11.InformationalResponse) and response.status_code == 101 and upgrade:
                # `Connection: Upgrade` is a hop header the filter drops, yet the handshake
                # (RFC 6455) needs it: a browser, and a proxy in front, refuse the answer without.
                headers = [(n, v) for n, v in self._response_headers(response, decision.port) if n != b"connection"]
                await self._send(
                    h11.InformationalResponse(
                        status_code=101, headers=[*headers, (b"connection", b"Upgrade")], reason=response.reason
                    )
                )
                await self._splice(upstream, grant)
                return False
            if not isinstance(response, h11.Response):
                await self._respond(*_page(502, "The app did not answer", "Check that it is running."))
                return False
            body_headers = self._response_headers(response, decision.port)
            # The app's length stands; without one, h11 frames the body itself.
            length = dict((n.lower(), v) for n, v in response.headers).get(b"content-length")
            if length is not None:
                body_headers.append((b"content-length", length))
            await self._send(h11.Response(status_code=response.status_code, headers=body_headers, reason=response.reason))
            while True:
                event = await upstream.next_event()
                if isinstance(event, h11.Data):
                    sent += len(event.data)
                    await self._send(h11.Data(data=event.data))
                elif isinstance(event, h11.EndOfMessage):
                    await self._send(h11.EndOfMessage())
                    break
                else:
                    return False
            if upstream.http.our_state is h11.DONE and upstream.http.their_state is h11.DONE:
                upstream.http.start_next_cycle()
            else:
                await self._close_upstream()
            logger.info(
                json.dumps(
                    {
                        "preview": self.label,
                        "method": request.method.decode(),
                        "path": urlsplit(request.target.decode("latin-1")).path[:200],
                        "status": response.status_code,
                        "bytes": sent,
                        "ms": round((time.monotonic() - started) * 1000),
                    }
                )
            )
            return True
        except (aiohttp.ClientError, aiohttp.WSServerHandshakeError, h11.ProtocolError):
            await self._close_upstream()
            try:
                await self._respond(*_page(502, "The app is not reachable", "Ask Hermes to start it again."))
            except h11.LocalProtocolError:
                pass
            return False
        finally:
            connections[self.label] -= 1

    async def _splice(self, upstream: Upstream, grant: str) -> None:
        """Bytes both ways after an upgrade, rechecked while they flow."""
        pending, _ = self.http.trailing_data
        if pending:
            await upstream.socket.send_bytes(pending)
        tail, _ = upstream.http.trailing_data
        if tail:
            self.writer.write(tail)

        async def browser_to_app() -> None:
            while data := await self.reader.read(65536):
                await upstream.socket.send_bytes(data)

        async def app_to_browser() -> None:
            async for message in upstream.socket:
                if message.type != aiohttp.WSMsgType.BINARY:
                    return
                self.writer.write(message.data)
                await self.writer.drain()

        async def recheck() -> None:
            while True:
                await asyncio.sleep(self.settings.recheck_seconds)
                if not (await self.previews.check(self.label, grant, fresh=True)).allow:
                    return

        tasks = {asyncio.create_task(job()) for job in (browser_to_app, app_to_browser, recheck)}
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await self._close_upstream()


async def serve(settings: Settings) -> None:
    async with aiohttp.ClientSession() as http:
        previews = Previews(settings, http)

        async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await Connection(previews, reader, writer).run()

        server = await asyncio.start_server(accept, "0.0.0.0", settings.port)
        async with server:
            await server.serve_forever()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(serve(Settings.from_env()))


if __name__ == "__main__":
    main()
