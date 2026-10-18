import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
import json
from urllib.parse import parse_qs

import pytest

from src.search import SearchFailed, SearxngAdapter, validate_search_query


@pytest.mark.parametrize(
    "query",
    [
        "!google confidential",
        "research !ddg",
        "!!wfr topic",
        "topic !!",
        ":fr !wp Paris",
        "topic :de",
        "topic :english",
        "topic :en_us",
        "topic :afrikaans",
        "topic <3",
        "<850 topic",
        "hello!",
        "\x00control",
        "hidden\u200bcontrol",
        "   ",
        "x" * 2001,
    ],
)
def test_search_cannot_select_engine_language_or_redirect(query: str) -> None:
    with pytest.raises(ValueError):
        validate_search_query(query)


def test_ordinary_search_operators_remain_available() -> None:
    assert (
        validate_search_query("site:example.org annual report -draft")
        == "site:example.org annual report -draft"
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://user:secret@searxng:8080",
        "http://searxng:8080/custom/search",
        "http://searxng:8080/?engine=other",
        "ftp://searxng:21",
    ],
)
def test_searxng_endpoint_is_an_operator_origin(endpoint: str) -> None:
    with pytest.raises(ValueError, match="invalid SearXNG endpoint"):
        SearxngAdapter(endpoint=endpoint)


async def _server(
    response_factory: Callable[[bytes, bytes], bytes],
):
    requests: list[tuple[bytes, bytes]] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        headers = {
            line.split(b":", 1)[0].lower(): line.split(b":", 1)[1].strip()
            for line in head.split(b"\r\n")[1:]
            if b":" in line
        }
        body = await reader.readexactly(int(headers.get(b"content-length", b"0")))
        requests.append((head, body))
        writer.write(response_factory(head, body))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port, requests


def _http_response(body: bytes, content_type: str = "application/json", status: int = 200) -> bytes:
    reason = "OK" if status == 200 else "Unavailable"
    return (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + body


async def _public_resolver(host: str, port: int) -> tuple[str, ...]:
    del port
    return ("169.254.169.254",) if host == "169.254.169.254" else ("8.8.8.8",)


async def test_adapter_posts_only_allowlisted_fields_and_bounds_results() -> None:
    payload = {
        "results": [
            {
                "title": f"Title {index}" + "x" * 2200,
                "url": f"https://example.org/{index}",
                "content": "s" * 4500,
                "publishedDate": "2026-09-01T12:30:00Z" if index == 0 else None,
            }
            for index in range(7)
        ]
    }
    server, port, requests = await _server(
        lambda _head, _body: _http_response(json.dumps(payload).encode())
    )
    try:
        results = await SearxngAdapter(
            endpoint=f"http://127.0.0.1:{port}",
            result_resolver=_public_resolver,
        ).search("site:example.org report", "en", "week")
    finally:
        server.close()
        await server.wait_closed()

    assert len(results) == 5
    assert len(results[0].title) == 2000
    assert len(results[0].snippet) == 4000
    assert results[0].published_at == datetime(2026, 9, 1, 12, 30, tzinfo=UTC)
    assert results[1].published_at is None
    head, body = requests[0]
    assert head.startswith(b"POST /search HTTP/1.1")
    assert b"authorization:" not in head.lower()
    assert b"cookie:" not in head.lower()
    assert parse_qs(body.decode()) == {
        "q": ["site:example.org report"],
        "format": ["json"],
        "language": ["en"],
        "time_range": ["week"],
    }


async def test_blocked_or_malformed_result_urls_are_dropped() -> None:
    payload = {
        "results": [
            {"title": "metadata", "url": "http://169.254.169.254/latest", "content": "x"},
            {"title": "file", "url": "file:///etc/passwd", "content": "x"},
            {"title": "public", "url": "https://example.org/", "content": "ok"},
        ]
    }
    server, port, _ = await _server(
        lambda _head, _body: _http_response(json.dumps(payload).encode())
    )
    try:
        results = await SearxngAdapter(
            endpoint=f"http://127.0.0.1:{port}",
            result_resolver=_public_resolver,
        ).search("public", None, None)
    finally:
        server.close()
        await server.wait_closed()

    assert [result.title for result in results] == ["public"]


async def test_empty_results_are_distinct_from_provider_outage() -> None:
    empty_server, empty_port, _ = await _server(
        lambda _head, _body: _http_response(b'{"results":[],"unresponsive_engines":[]}')
    )
    outage_server, outage_port, _ = await _server(
        lambda _head, _body: _http_response(
            b'{"results":[],"unresponsive_engines":[["duckduckgo","timeout"]]}'
        )
    )
    try:
        assert (
            await SearxngAdapter(endpoint=f"http://127.0.0.1:{empty_port}").search(
                "nothing", None, None
            )
            == []
        )
        with pytest.raises(SearchFailed, match="web_unavailable"):
            await SearxngAdapter(endpoint=f"http://127.0.0.1:{outage_port}").search(
                "outage", None, None
            )
    finally:
        empty_server.close()
        outage_server.close()
        await empty_server.wait_closed()
        await outage_server.wait_closed()


async def test_result_dns_timeout_fails_the_search_instead_of_returning_partial_results() -> None:
    payload = {
        "results": [
            {"title": "slow", "url": "https://slow.example/", "content": "snippet"}
        ]
    }
    server, port, _ = await _server(
        lambda _head, _body: _http_response(json.dumps(payload).encode())
    )

    async def slow_resolver(host: str, requested_port: int) -> tuple[str, ...]:
        del host, requested_port
        await asyncio.sleep(0.05)
        return ("8.8.8.8",)

    try:
        with pytest.raises(SearchFailed, match="web_timeout"):
            await SearxngAdapter(
                endpoint=f"http://127.0.0.1:{port}",
                result_resolver=slow_resolver,
                deadline_seconds=0.01,
            ).search("slow", None, None)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    "body,content_type",
    [
        (b"<html>not json</html>", "text/html"),
        (b"{}", "application/json"),
        (b'{"results":"wrong"}', "application/json"),
        (b"x" * (1024 * 1024 + 1), "application/json"),
    ],
)
async def test_malformed_or_oversized_searxng_response_is_unavailable(
    body: bytes,
    content_type: str,
) -> None:
    server, port, _ = await _server(
        lambda _head, _body: _http_response(body, content_type)
    )
    try:
        with pytest.raises(SearchFailed, match="web_unavailable"):
            await SearxngAdapter(endpoint=f"http://127.0.0.1:{port}").search(
                "report", None, None
            )
    finally:
        server.close()
        await server.wait_closed()


async def test_searxng_429_is_not_retried() -> None:
    server, port, requests = await _server(
        lambda _head, _body: _http_response(b"{}", status=429)
    )
    try:
        with pytest.raises(SearchFailed, match="web_unavailable"):
            await SearxngAdapter(endpoint=f"http://127.0.0.1:{port}").search(
                "report", None, None
            )
    finally:
        server.close()
        await server.wait_closed()

    assert len(requests) == 1


async def test_searxng_5xx_is_retried_only_once() -> None:
    server, port, requests = await _server(
        lambda _head, _body: _http_response(b"{}", status=503)
    )
    try:
        with pytest.raises(SearchFailed, match="web_unavailable"):
            await SearxngAdapter(endpoint=f"http://127.0.0.1:{port}").search(
                "report", None, None
            )
    finally:
        server.close()
        await server.wait_closed()

    assert len(requests) == 2
