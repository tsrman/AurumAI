import asyncio
import json

import httpx
import pytest

from src.services.web_tools_client import WebToolsClient, WebToolsClientError


SECRET = "unit-web-secret-1234"


@pytest.mark.parametrize(
    "flag,expected",
    [(True, True), (False, False), ("true", False), (None, False), ("missing", False)],
)
async def test_retry_requires_explicit_boolean_service_permission(
    flag: object, expected: bool
) -> None:
    detail: dict[str, object] = {"code": "web_unavailable"}
    if flag != "missing":
        detail["retryable"] = flag
    client = WebToolsClient(
        base_url="http://web-tools:8120",
        service_secret=SECRET,
        transport=httpx.MockTransport(lambda _: httpx.Response(503, json={"detail": detail})),
    )
    with pytest.raises(WebToolsClientError) as failure:
        await client.execute("web_search", {"query": "public report"})
    assert getattr(failure.value, "retryable", None) is expected


async def test_client_sends_only_service_auth_and_model_arguments() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://web-tools:8120/search"
        assert request.headers["authorization"] == f"Bearer {SECRET}"
        assert "cookie" not in request.headers
        assert json.loads(request.content) == {
            "query": "release notes",
            "language": None,
            "time_range": None,
        }
        return httpx.Response(200, json={"results": []})

    client = WebToolsClient(
        base_url="http://web-tools:8120",
        service_secret=SECRET,
        transport=httpx.MockTransport(handler),
    )

    assert await client.execute(
        "web_search",
        {"query": "release notes", "language": None, "time_range": None},
    ) == {"results": []}


async def test_client_preserves_stable_service_error_without_internal_body() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            422,
            json={"detail": {"code": "web_url_blocked"}, "internal": "do not expose"},
        )

    client = WebToolsClient(
        base_url="http://web-tools:8120",
        service_secret=SECRET,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(WebToolsClientError, match="web_url_blocked"):
        await client.execute("web_read", {"url": "https://example.org"})


async def test_client_maps_transport_failure_to_unavailable() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client = WebToolsClient(
        base_url="http://web-tools:8120",
        service_secret=SECRET,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(WebToolsClientError, match="web_unavailable"):
        await client.execute("web_read", {"url": "https://example.org"})


async def test_client_rejects_oversized_internal_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b'"' + b"x" * (512 * 1024) + b'"',
        )

    client = WebToolsClient(
        base_url="http://web-tools:8120",
        service_secret=SECRET,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(WebToolsClientError, match="web_unavailable"):
        await client.execute("web_read", {"url": "https://example.org"})


async def test_client_enforces_one_wall_clock_deadline() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"results": []})

    client = WebToolsClient(
        base_url="http://web-tools:8120",
        service_secret=SECRET,
        timeout_seconds=0.01,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(WebToolsClientError, match="web_timeout"):
        await client.execute(
            "web_search",
            {"query": "release notes", "language": None, "time_range": None},
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://web-tools",
        "ftp://web-tools:8120",
        "http://user:pass@web-tools:8120",
        "http://web-tools:8120/api",
        "http://web-tools:8120?target=outside",
        "http://web-tools:8120#outside",
    ],
)
def test_client_rejects_non_origin_service_urls(base_url: str) -> None:
    with pytest.raises(ValueError, match="WEB_TOOLS_URL"):
        WebToolsClient(base_url=base_url, service_secret=SECRET)
