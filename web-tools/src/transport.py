"""Pinned-address HTTP transport with bounded redirects, bytes and deadline."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit
import zlib

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

from src.policy import URLBlocked, canonicalize_url, validate_target


MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
Resolver = Callable[[str, int], Awaitable[tuple[str, ...]]]
Validator = Callable[[str, tuple[str, ...], tuple[str, ...]], str]
URLValidator = Callable[[str], str]


class FetchFailed(ValueError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Download:
    final_url: str
    content_type: str
    body: bytes
    # Declared response encoding, kept apart from content_type so extraction
    # can honour it instead of assuming UTF-8.
    charset: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    url: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


class DownloadBuffer:
    def __init__(self, *, content_encoding: str, max_bytes: int = MAX_DOWNLOAD_BYTES) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        encoding = (content_encoding or "identity").strip().lower()
        if encoding not in {"identity", "gzip", "deflate"}:
            raise FetchFailed("web_unavailable")
        self._encoding = encoding
        self._maximum = max_bytes
        self._wire_size = 0
        self._body = bytearray()
        self._decoder = (
            None
            if encoding == "identity"
            else zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
        )

    def _append(self, decoded: bytes) -> None:
        if len(self._body) + len(decoded) > self._maximum:
            raise FetchFailed("web_too_large")
        self._body.extend(decoded)

    def consume(self, chunk: bytes) -> None:
        self._wire_size += len(chunk)
        if self._wire_size > self._maximum:
            raise FetchFailed("web_too_large")
        if self._decoder is None:
            self._append(chunk)
            return
        remaining = self._maximum - len(self._body)
        try:
            decoded = self._decoder.decompress(chunk, remaining + 1)
        except zlib.error as exc:
            raise FetchFailed("web_unavailable") from exc
        if len(decoded) > remaining or self._decoder.unconsumed_tail:
            raise FetchFailed("web_too_large")
        self._append(decoded)

    def finish(self) -> bytes:
        if self._decoder is not None:
            remaining = self._maximum - len(self._body)
            try:
                tail = self._decoder.flush(remaining + 1)
            except zlib.error as exc:
                raise FetchFailed("web_unavailable") from exc
            if len(tail) > remaining:
                raise FetchFailed("web_too_large")
            self._append(tail)
            if not self._decoder.eof:
                raise FetchFailed("web_unavailable")
        return bytes(self._body)


class _PinnedResolver(AbstractResolver):
    def __init__(self, hostname: str, addresses: tuple[str, ...]) -> None:
        self._hostname = hostname
        self._addresses = addresses
        self.calls = 0

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        del family
        self.calls += 1
        if host.rstrip(".").lower() != self._hostname.rstrip(".").lower():
            raise OSError("unexpected resolver hostname")
        return [
            ResolveResult(
                hostname=host,
                host=address,
                port=port,
                family=socket.AF_INET6 if ":" in address else socket.AF_INET,
                proto=socket.IPPROTO_TCP,
                flags=socket.AI_NUMERICHOST,
            )
            for address in self._addresses
        ]

    async def close(self) -> None:
        return None


async def system_resolver(host: str, port: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    try:
        rows = await loop.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError as exc:
        raise FetchFailed("web_unavailable", retryable=True) from exc
    return tuple(dict.fromkeys(str(row[4][0]) for row in rows))


async def resolve_target(
    url: str,
    *,
    resolver: Resolver = system_resolver,
    url_validator: URLValidator = canonicalize_url,
    validator: Validator = validate_target,
    forbidden_cidrs: tuple[str, ...] = (),
) -> ResolvedTarget:
    canonical_input = url_validator(url)
    try:
        parsed = urlsplit(canonical_input)
        host = parsed.hostname
        if host is None:
            raise URLBlocked("web_url_blocked")
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise URLBlocked("web_url_blocked") from exc
    addresses = await resolver(host, port)
    canonical = validator(canonical_input, addresses, forbidden_cidrs)
    canonical_host = urlsplit(canonical).hostname
    if canonical_host is None:
        raise URLBlocked("web_url_blocked")
    return ResolvedTarget(canonical, canonical_host, port, addresses)


class PinnedTransport:
    def __init__(
        self,
        *,
        resolver: Resolver = system_resolver,
        url_validator: URLValidator = canonicalize_url,
        validator: Validator = validate_target,
        forbidden_cidrs: tuple[str, ...] = (),
        max_bytes: int = MAX_DOWNLOAD_BYTES,
        max_redirects: int = 3,
        max_attempts: int = 2,
        ssl_context: ssl.SSLContext | bool = True,
    ) -> None:
        self._resolver = resolver
        self._url_validator = url_validator
        self._validator = validator
        self._forbidden_cidrs = forbidden_cidrs
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._max_attempts = max_attempts
        self._ssl_context = ssl_context

    async def fetch(self, url: str, *, deadline_seconds: float = 30) -> Download:
        if deadline_seconds <= 0:
            raise FetchFailed("web_timeout")
        deadline = time.monotonic() + deadline_seconds
        last_error = FetchFailed("web_unavailable")
        for attempt in range(self._max_attempts):
            try:
                return await self._fetch_chain(url, deadline)
            except URLBlocked:
                raise
            except FetchFailed as exc:
                last_error = exc
                if not exc.retryable or attempt + 1 >= self._max_attempts:
                    raise
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
                last_error = FetchFailed("web_unavailable", retryable=True)
                if attempt + 1 >= self._max_attempts:
                    raise last_error from exc
            if time.monotonic() >= deadline:
                raise FetchFailed("web_timeout")
        raise last_error

    async def _fetch_chain(self, url: str, deadline: float) -> Download:
        current = url
        for redirects in range(self._max_redirects + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FetchFailed("web_timeout")
            try:
                async with asyncio.timeout(remaining):
                    target = await resolve_target(
                        current,
                        resolver=self._resolver,
                        url_validator=self._url_validator,
                        validator=self._validator,
                        forbidden_cidrs=self._forbidden_cidrs,
                    )
            except TimeoutError as exc:
                raise FetchFailed("web_timeout") from exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FetchFailed("web_timeout")
            outcome = await self._request_once(
                target.url,
                target.hostname,
                target.addresses,
                remaining,
            )
            if isinstance(outcome, Download):
                return outcome
            if redirects >= self._max_redirects:
                raise FetchFailed("web_unavailable")
            current = urljoin(target.url, outcome)
        raise FetchFailed("web_unavailable")

    async def _request_once(
        self,
        url: str,
        host: str,
        addresses: tuple[str, ...],
        remaining: float,
    ) -> Download | str:
        resolver = _PinnedResolver(host, addresses)
        connector = aiohttp.TCPConnector(
            resolver=resolver,
            use_dns_cache=False,
            force_close=True,
            ssl=self._ssl_context,
        )
        timeout = aiohttp.ClientTimeout(total=remaining, connect=min(10.0, remaining))
        headers = {
            "User-Agent": "aurum-ai-web-tools/1",
            "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.1",
        }
        try:
            async with aiohttp.ClientSession(
                connector=connector,
                trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                auto_decompress=False,
                headers=headers,
            ) as client:
                async with client.get(url, allow_redirects=False, timeout=timeout) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise FetchFailed("web_unavailable")
                        return location
                    if response.status < 200 or 300 <= response.status < 400:
                        raise FetchFailed("web_unavailable")
                    if response.status == 429 or 400 <= response.status < 500:
                        raise FetchFailed("web_unavailable")
                    if response.status >= 500:
                        raise FetchFailed("web_unavailable", retryable=True)
                    content_length = response.content_length
                    if content_length is not None and content_length > self._max_bytes:
                        raise FetchFailed("web_too_large")
                    buffer = DownloadBuffer(
                        content_encoding=response.headers.get("Content-Encoding", "identity"),
                        max_bytes=self._max_bytes,
                    )
                    async for chunk in response.content.iter_chunked(65536):
                        buffer.consume(chunk)
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                    return Download(
                        final_url=url,
                        content_type=content_type,
                        body=buffer.finish(),
                        charset=response.charset,
                    )
        except asyncio.TimeoutError as exc:
            raise FetchFailed("web_timeout", retryable=True) from exc
        finally:
            await connector.close()


async def fetch_public(url: str, deadline_seconds: float = 30) -> Download:
    return await PinnedTransport().fetch(url, deadline_seconds=deadline_seconds)
