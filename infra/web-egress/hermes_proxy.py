#!/usr/bin/env python3
"""hermes-egress: the only way out of a Hermes Agent sandbox (docs/HERMES_AGENT.md).

Two listeners, standard library only, behind the web-egress nftables guard:

* platform (reverse proxy): exactly the allowed backend paths. One request per
  connection with a Content-Length body; exactly the parsed bytes are forwarded,
  so nothing pipelined behind them can reach another backend path.
* proxy (forward proxy): HTTP CONNECT tunnels authenticated with the sandbox's
  Hermes session token. Every new tunnel asks the backend for the session's live
  state and mode; open tunnels are re-checked periodically and closed on a deny.
  The host is resolved once, every address is checked against the forbidden
  ranges, and the tunnel connects to a checked address, never re-resolving. In
  `packages` mode the TLS ClientHello's server name must equal the CONNECT host.

Each decision and each closed tunnel is one JSON log line: session, turn, host,
port, mode, outcome and byte counts; never credentials, paths or payloads.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import ipaddress
import json
import logging
import os
import re
import signal
import sys
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

LOG = logging.getLogger("hermes-egress")
MODES = ("none", "packages", "public")
MAX_HEAD_BYTES = 16 * 1024
HEAD_TIMEOUT_SECONDS = 30.0
# The model proxy and the platform tools' MCP endpoint, which answers every
# message with one JSON body and so needs nothing but POST.
PLATFORM_ROUTES = frozenset({("POST", "/v1/hermes/chat/completions"), ("POST", "/v1/hermes/mcp")})
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "expect",
    }
)
_TOKEN_RE = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")

# Same reserved ranges as rules.nft; the guard enforces them underneath.
FORBIDDEN_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(value)
    for value in (
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
        "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16",
        "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
        "::/128", "::1/128", "::ffff:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48", "100::/64",
        "2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20", "fc00::/7", "fe80::/10",
        "fec0::/10", "ff00::/8",
    )
)


class Refused(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass(frozen=True)
class Head:
    method: str
    target: str
    headers: list[tuple[str, str]]
    raw_length: int

    def values(self, name: str) -> list[str]:
        return [value for key, value in self.headers if key == name]


async def read_head(reader: asyncio.StreamReader) -> tuple[Head, bytes]:
    """The request head and any body bytes already read past it."""
    buffer = b""
    async with asyncio.timeout(HEAD_TIMEOUT_SECONDS):
        while b"\r\n\r\n" not in buffer:
            chunk = await reader.read(4096)
            if not chunk:
                raise Refused(400, "incomplete_head")
            buffer += chunk
            if len(buffer) > MAX_HEAD_BYTES and b"\r\n\r\n" not in buffer[:MAX_HEAD_BYTES]:
                raise Refused(431, "head_too_large")
    head_bytes, rest = buffer.split(b"\r\n\r\n", 1)
    if len(head_bytes) > MAX_HEAD_BYTES:
        raise Refused(431, "head_too_large")
    lines = head_bytes.split(b"\r\n")
    parts = lines[0].split(b" ")
    if len(parts) != 3 or parts[2] != b"HTTP/1.1" or not _TOKEN_RE.match(parts[0]):
        raise Refused(400, "bad_request_line")
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line or line[:1] in (b" ", b"\t"):
            raise Refused(400, "bad_header")
        name, sep, value = line.partition(b":")
        if not sep or not _TOKEN_RE.match(name):
            raise Refused(400, "bad_header")
        try:
            text = value.strip(b" \t").decode("ascii")
        except UnicodeDecodeError:
            raise Refused(400, "bad_header") from None
        if any(ord(char) < 0x20 and char != "\t" for char in text):
            raise Refused(400, "bad_header")
        headers.append((name.decode("ascii").lower(), text))
    try:
        target = parts[1].decode("ascii")
    except UnicodeDecodeError:
        raise Refused(400, "bad_request_line") from None
    return Head(parts[0].decode("ascii"), target, headers, len(head_bytes) + 4), rest


def response(status: int, reason: str, extra: Iterable[tuple[str, str]] = ()) -> bytes:
    body = json.dumps({"error": reason}).encode()
    lines = [f"HTTP/1.1 {status} {reason}"]
    lines += [f"{name}: {value}" for name, value in extra]
    lines += ["Content-Type: application/json", f"Content-Length: {len(body)}", "Connection: close"]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


async def _pipe(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    count: Callable[[int], None] | None = None,
    *,
    idle_seconds: float | None = None,
) -> None:
    try:
        while True:
            if idle_seconds is None:
                chunk = await reader.read(65536)
            else:
                chunk = await asyncio.wait_for(reader.read(65536), idle_seconds)
            if not chunk:
                break
            if count is not None:
                count(len(chunk))
            writer.write(chunk)
            await writer.drain()
    finally:
        try:
            writer.write_eof()
        except (OSError, RuntimeError):
            pass


def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
    except Exception:
        pass


# Platform endpoints -----------------------------------------------------------


@dataclass
class PlatformConfig:
    backend_host: str
    backend_port: int
    max_body_bytes: int = 64 * 1024 * 1024
    connect: Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]] = (
        asyncio.open_connection
    )


def platform_request(head: Head, config: PlatformConfig) -> tuple[bytes, int]:
    """The exact head forwarded to the backend and the body length to copy."""
    if (head.method, head.target) not in PLATFORM_ROUTES:
        raise Refused(404, "route_not_allowed")
    if head.values("transfer-encoding"):
        raise Refused(400, "transfer_encoding_not_allowed")
    lengths = head.values("content-length")
    if len(lengths) != 1 or not lengths[0].isdigit():
        raise Refused(411, "content_length_required")
    length = int(lengths[0])
    if length > config.max_body_bytes:
        raise Refused(413, "body_too_large")
    if head.values("upgrade") or head.values("expect"):
        raise Refused(400, "unsupported_header")
    lines = [f"{head.method} {head.target} HTTP/1.1", f"Host: {config.backend_host}:{config.backend_port}"]
    lines += [f"{name}: {value}" for name, value in head.headers if name not in HOP_BY_HOP]
    lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode(), length


async def handle_platform(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, config: PlatformConfig
) -> None:
    upstream_writer: asyncio.StreamWriter | None = None
    try:
        try:
            head, rest = await read_head(reader)
            forwarded, length = platform_request(head, config)
        except Refused as exc:
            writer.write(response(exc.status, exc.reason))
            await writer.drain()
            return
        upstream_reader, upstream_writer = await config.connect(config.backend_host, config.backend_port)
        upstream_writer.write(forwarded)
        body = rest[:length]
        upstream_writer.write(body)
        remaining = length - len(body)
        while remaining > 0:
            chunk = await reader.read(min(65536, remaining))
            if not chunk:
                return
            upstream_writer.write(chunk)
            remaining -= len(chunk)
            await upstream_writer.drain()
        await upstream_writer.drain()
        # Nothing more is read from the sandbox on this connection.
        await _pipe(upstream_reader, writer)
    except (OSError, TimeoutError, asyncio.IncompleteReadError):
        pass
    finally:
        if upstream_writer is not None:
            _close(upstream_writer)
        _close(writer)


# Forward proxy ----------------------------------------------------------------


def normalize_host(raw: str) -> str:
    host = raw.strip().lower().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if not _HOST_RE.match(host):
        raise Refused(400, "bad_host")
    return host


def parse_connect_target(target: str) -> tuple[str, int]:
    host, sep, port = target.rpartition(":")
    if not sep or not port.isdigit() or not 0 < int(port) < 65536:
        raise Refused(400, "bad_connect_target")
    return normalize_host(host), int(port)


def proxy_token(head: Head) -> str:
    values = head.values("proxy-authorization")
    if len(values) != 1:
        raise Refused(407, "proxy_authentication_required")
    scheme, _, credential = values[0].partition(" ")
    if scheme.lower() == "bearer" and credential:
        return credential.strip()
    if scheme.lower() == "basic":
        try:
            decoded = base64.b64decode(credential.strip(), validate=True).decode()
        except (binascii.Error, UnicodeDecodeError):
            raise Refused(407, "proxy_authentication_required") from None
        user, _, password = decoded.partition(":")
        token = password or user
        if token:
            return token
    raise Refused(407, "proxy_authentication_required")


def host_matches(host: str, patterns: Iterable[str]) -> bool:
    for pattern in patterns:
        if pattern.startswith("*."):
            if host.endswith(pattern[1:]) and host != pattern[2:]:
                return True
        elif host == pattern:
            return True
    return False


def forbidden(address: ipaddress.IPv4Address | ipaddress.IPv6Address, networks: Iterable[Any]) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in networks)


def tls_server_name(data: bytes) -> str | None:
    """The SNI host_name of a TLS ClientHello in `data`, or None."""
    try:
        if len(data) < 5 or data[0] != 22:
            return None
        record_length = int.from_bytes(data[3:5], "big")
        body = data[5 : 5 + record_length]
        if len(body) < record_length or body[0] != 1:
            return None
        position = 4 + 2 + 32
        position += 1 + body[position]
        position += 2 + int.from_bytes(body[position : position + 2], "big")
        position += 1 + body[position]
        end = position + 2 + int.from_bytes(body[position : position + 2], "big")
        position += 2
        while position + 4 <= end:
            kind = int.from_bytes(body[position : position + 2], "big")
            size = int.from_bytes(body[position + 2 : position + 4], "big")
            position += 4
            if kind == 0:
                names = body[position : position + size]
                if names[2] != 0:
                    return None
                name_length = int.from_bytes(names[3:5], "big")
                return names[5 : 5 + name_length].decode("ascii").lower().rstrip(".")
            position += size
    except (IndexError, UnicodeDecodeError):
        return None
    return None


@dataclass
class Decision:
    allow: bool
    mode: str = "none"
    session_id: str = ""
    turn_id: str = ""
    reason: str = ""
    max_age: float = 5.0


Authorizer = Callable[[str], Awaitable[Decision]]


@dataclass
class ProxyConfig:
    max_mode: str
    package_hosts: tuple[str, ...]
    internal_exceptions: frozenset[tuple[str, int]] = frozenset()
    forbidden_networks: tuple[Any, ...] = FORBIDDEN_NETWORKS
    connect_timeout: float = 10.0
    idle_timeout: float = 300.0
    recheck_interval: float = 30.0
    max_connections_per_session: int = 16
    max_bytes_per_turn: int = 2 * 1024 * 1024 * 1024
    resolve: Callable[[str, int], Awaitable[list[str]]] | None = None
    connect: Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]] = (
        asyncio.open_connection
    )


@dataclass(eq=False)
class _Tunnel:
    token: str
    writers: list[asyncio.StreamWriter]
    record: dict[str, Any]
    failed_checks: int = 0


# Consecutive failed re-checks (backend unreachable) after which an open tunnel
# is closed: a restart may pass, an outage must not keep tunnels open.
MAX_FAILED_RECHECKS = 3


@dataclass
class ProxyState:
    config: ProxyConfig
    authorize: Authorizer
    cache: dict[str, tuple[float, Decision]] = field(default_factory=dict)
    connections: dict[str, int] = field(default_factory=dict)
    turn_bytes: dict[tuple[str, str], int] = field(default_factory=dict)
    tunnels: set[Any] = field(default_factory=set)

    async def decision(self, token: str, *, fresh: bool = False) -> Decision:
        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.monotonic()
        cached = self.cache.get(key)
        if not fresh and cached is not None and cached[0] > now:
            return cached[1]
        decision = await self.authorize(token)
        self.cache[key] = (now + max(0.0, min(decision.max_age, 5.0)), decision)
        if decision.allow:
            # Byte counters of the session's earlier turns are finished.
            for stale in [k for k in self.turn_bytes if k[0] == decision.session_id and k[1] != decision.turn_id]:
                del self.turn_bytes[stale]
        return decision

    def prune(self) -> None:
        now = time.monotonic()
        for key in [key for key, (expires, _) in self.cache.items() if expires <= now]:
            del self.cache[key]
        for session in [session for session, count in self.connections.items() if count <= 0]:
            del self.connections[session]


def effective_mode(mode: str, max_mode: str) -> str:
    if mode not in MODES:
        return "none"
    return MODES[min(MODES.index(mode), MODES.index(max_mode))]


def check_destination(config: ProxyConfig, mode: str, host: str, port: int) -> bool:
    """Host and port rules of the mode; True when it is an operator internal exception."""
    if (host, port) in config.internal_exceptions:
        if mode == "none":
            raise Refused(403, "egress_disabled")
        return True
    if mode == "none":
        raise Refused(403, "egress_disabled")
    if mode == "packages":
        if port != 443 or not host_matches(host, config.package_hosts):
            raise Refused(403, "host_not_allowed")
    elif port not in (80, 443):
        raise Refused(403, "port_not_allowed")
    return False


async def _resolve(config: ProxyConfig, host: str, port: int) -> list[str]:
    if config.resolve is not None:
        return await config.resolve(host, port)
    rows = await asyncio.get_running_loop().getaddrinfo(host, port, type=1)
    return list(dict.fromkeys(row[4][0] for row in rows))


def _log(record: dict[str, Any]) -> None:
    LOG.info(json.dumps(record, sort_keys=True))


async def handle_proxy(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, state: ProxyState
) -> None:
    config = state.config
    record: dict[str, Any] = {"event": "hermes_egress"}
    session_key: str | None = None
    upstream_writer: asyncio.StreamWriter | None = None
    started = time.monotonic()
    try:
        try:
            head, rest = await read_head(reader)
            if head.method != "CONNECT":
                raise Refused(405, "connect_only")
            host, port = parse_connect_target(head.target)
            record.update(host=host, port=port)
            token = proxy_token(head)
            decision = await state.decision(token)
            if not decision.allow:
                raise Refused(403, decision.reason or "denied")
            mode = effective_mode(decision.mode, config.max_mode)
            record.update(session=decision.session_id, turn=decision.turn_id, mode=mode)
            exception = check_destination(config, mode, host, port)
            turn_key = (decision.session_id, decision.turn_id)
            if state.turn_bytes.get(turn_key, 0) >= config.max_bytes_per_turn:
                raise Refused(403, "turn_byte_limit")
            if state.connections.get(decision.session_id, 0) >= config.max_connections_per_session:
                raise Refused(429, "connection_limit")
            try:
                addresses = [ipaddress.ip_address(value) for value in await _resolve(config, host, port)]
            except (OSError, ValueError):
                raise Refused(502, "resolve_failed") from None
            if not addresses:
                raise Refused(502, "resolve_failed")
            if not exception and any(forbidden(address, config.forbidden_networks) for address in addresses):
                raise Refused(403, "address_forbidden")
            upstream_reader = None
            for address in addresses:
                try:
                    async with asyncio.timeout(config.connect_timeout):
                        upstream_reader, upstream_writer = await config.connect(str(address), port)
                    record["address"] = str(address)
                    break
                except (OSError, TimeoutError):
                    continue
            if upstream_reader is None or upstream_writer is None:
                raise Refused(502, "connect_failed")
        except Refused as exc:
            record.update(outcome="refused", reason=exc.reason)
            extra = [("Proxy-Authenticate", 'Basic realm="hermes-egress"')] if exc.status == 407 else []
            writer.write(response(exc.status, exc.reason, extra))
            await writer.drain()
            return
        session_key = decision.session_id
        state.connections[session_key] = state.connections.get(session_key, 0) + 1
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        first = rest
        if mode == "packages" and not exception:
            async with asyncio.timeout(HEAD_TIMEOUT_SECONDS):
                while tls_server_name(first) is None and len(first) < 32 * 1024:
                    chunk = await reader.read(16384)
                    if not chunk:
                        break
                    first += chunk
            if tls_server_name(first) != host:
                record.update(outcome="refused", reason="sni_mismatch")
                return
        record["outcome"] = "tunnel"
        _log({**record, "phase": "open"})
        counters = {"up": 0, "down": 0}

        def counted(direction: str) -> Callable[[int], None]:
            def add(size: int) -> None:
                counters[direction] += size
                total = state.turn_bytes.get(turn_key, 0) + size
                state.turn_bytes[turn_key] = total
                if total > config.max_bytes_per_turn:
                    record["reason"] = "turn_byte_limit"
                    raise ConnectionAbortedError

            return add

        if first:
            counted("up")(len(first))
            upstream_writer.write(first)
            await upstream_writer.drain()
        tunnel = _Tunnel(token, [writer, upstream_writer], record)
        state.tunnels.add(tunnel)
        try:
            tasks = [
                asyncio.create_task(_pipe(reader, upstream_writer, counted("up"), idle_seconds=config.idle_timeout)),
                asyncio.create_task(
                    _pipe(upstream_reader, writer, counted("down"), idle_seconds=config.idle_timeout)
                ),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            if any(task.exception() for task in done if not task.cancelled()):
                for task in pending:
                    task.cancel()
            elif pending:
                await asyncio.wait(pending, timeout=config.idle_timeout)
                for task in pending:
                    task.cancel()
        finally:
            state.tunnels.discard(tunnel)
        record.update(bytes_up=counters["up"], bytes_down=counters["down"])
    except (OSError, TimeoutError, ConnectionError):
        record.setdefault("outcome", "error")
    finally:
        if session_key is not None:
            remaining = state.connections.get(session_key, 1) - 1
            if remaining > 0:
                state.connections[session_key] = remaining
            else:
                state.connections.pop(session_key, None)
        if upstream_writer is not None:
            _close(upstream_writer)
        _close(writer)
        record["duration_ms"] = int((time.monotonic() - started) * 1000)
        _log({**record, "phase": "close"} if record.get("outcome") == "tunnel" else record)


async def recheck_tunnels(state: ProxyState) -> None:
    """Close open tunnels whose session no longer allows their mode."""
    while True:
        await asyncio.sleep(state.config.recheck_interval)
        await recheck_once(state)


async def recheck_once(state: ProxyState) -> None:
    state.prune()
    for tunnel in list(state.tunnels):
        try:
            decision = await state.decision(tunnel.token, fresh=True)
            tunnel.failed_checks = 0
        except Exception:
            tunnel.failed_checks += 1
            if tunnel.failed_checks >= MAX_FAILED_RECHECKS:
                tunnel.record["reason"] = "recheck_unavailable"
                for writer in tunnel.writers:
                    _close(writer)
            continue
        record = tunnel.record
        still = decision.allow and decision.session_id == record.get("session")
        if still:
            try:
                check_destination(
                    state.config,
                    effective_mode(decision.mode, state.config.max_mode),
                    record["host"],
                    record["port"],
                )
            except Refused:
                still = False
        if not still:
            record["reason"] = decision.reason or "mode_lowered"
            for writer in tunnel.writers:
                _close(writer)


# Backend check and process ------------------------------------------------------


def backend_authorizer(host: str, port: int, secret: str) -> Authorizer:
    async def authorize(token: str) -> Decision:
        body = json.dumps({"token": token}).encode()
        reader, writer = await asyncio.open_connection(host, port)
        try:
            writer.write(
                (
                    "POST /v1/internal/hermes/egress/authorize HTTP/1.1\r\n"
                    f"Host: {host}:{port}\r\nAuthorization: Bearer {secret}\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                ).encode()
                + body
            )
            await writer.drain()
            async with asyncio.timeout(10):
                raw = await reader.read(-1)
        finally:
            _close(writer)
        head, _, payload = raw.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].split(b" ")
        if len(status_line) < 2 or status_line[1] != b"200":
            return Decision(False, reason="authorization_unavailable", max_age=1.0)
        if b"transfer-encoding: chunked" in head.lower():
            payload = _dechunk(payload)
        data = json.loads(payload)
        return Decision(
            allow=bool(data.get("allow")),
            mode=str(data.get("mode", "none")),
            session_id=str(data.get("sessionId", "")),
            turn_id=str(data.get("turnId", "")),
            reason=str(data.get("reason", "")),
            max_age=float(data.get("maxAgeSeconds", 5)),
        )

    return authorize


def _dechunk(data: bytes) -> bytes:
    out = b""
    while data:
        size_line, _, data = data.partition(b"\r\n")
        size = int(size_line.split(b";")[0], 16)
        if size == 0:
            break
        out += data[:size]
        data = data[size + 2 :]
    return out


def _env_port(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if not 0 < value < 65536:
        raise SystemExit(f"{name} must be a port")
    return value


def _list(name: str) -> tuple[str, ...]:
    return tuple(item.strip().lower() for item in os.environ.get(name, "").split(",") if item.strip())


def config_from_env() -> tuple[PlatformConfig, ProxyConfig, Authorizer, int, int]:
    max_mode = os.environ.get("HERMES_EGRESS_MAX_MODE", "packages")
    if max_mode not in MODES:
        raise SystemExit("HERMES_EGRESS_MAX_MODE must be none, packages or public")
    backend_host = os.environ["HERMES_EGRESS_BACKEND_HOST"]
    backend_port = _env_port("HERMES_EGRESS_BACKEND_PORT", 8000)
    secret = os.environ.get("HERMES_EGRESS_SERVICE_SECRET", "")
    if max_mode != "none" and len(secret) < 16:
        raise SystemExit("HERMES_EGRESS_SERVICE_SECRET must contain at least 16 characters")
    exceptions = set()
    for item in _list("HERMES_EGRESS_INTERNAL_PACKAGE_HOSTS"):
        host, _, port = item.rpartition(":")
        if not host or not port.isdigit():
            raise SystemExit("HERMES_EGRESS_INTERNAL_PACKAGE_HOSTS entries must be host:port")
        exceptions.add((normalize_host(host), int(port)))
    operator = tuple(
        ipaddress.ip_network(item, strict=True) for item in _list("WEB_EGRESS_FORBIDDEN_CIDRS")
    )
    proxy = ProxyConfig(
        max_mode=max_mode,
        package_hosts=_list("HERMES_EGRESS_PACKAGE_HOSTS") or ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org"),
        internal_exceptions=frozenset(exceptions),
        forbidden_networks=FORBIDDEN_NETWORKS + operator,
        idle_timeout=float(os.environ.get("HERMES_EGRESS_IDLE_TIMEOUT_SECONDS", "300")),
        recheck_interval=float(os.environ.get("HERMES_EGRESS_RECHECK_SECONDS", "30")),
        max_connections_per_session=int(os.environ.get("HERMES_EGRESS_MAX_CONNECTIONS", "16")),
        max_bytes_per_turn=int(os.environ.get("HERMES_EGRESS_MAX_BYTES_PER_TURN", str(2 * 1024**3))),
    )
    platform = PlatformConfig(backend_host, backend_port)
    return (
        platform,
        proxy,
        backend_authorizer(backend_host, backend_port, secret),
        _env_port("HERMES_EGRESS_PLATFORM_PORT", 8080),
        _env_port("HERMES_EGRESS_PROXY_PORT", 3128),
    )


async def serve() -> None:
    platform, proxy, authorizer, platform_port, proxy_port = config_from_env()
    state = ProxyState(proxy, authorizer)
    servers = [
        await asyncio.start_server(lambda r, w: handle_platform(r, w, platform), "0.0.0.0", platform_port),
        await asyncio.start_server(lambda r, w: handle_proxy(r, w, state), "0.0.0.0", proxy_port),
    ]
    recheck = asyncio.create_task(recheck_tunnels(state))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    LOG.info(json.dumps({"event": "hermes_egress_started", "max_mode": proxy.max_mode}))
    await stop.wait()
    recheck.cancel()
    for server in servers:
        server.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    asyncio.run(serve())
