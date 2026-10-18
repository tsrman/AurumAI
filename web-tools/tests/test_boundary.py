import hmac

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from pydantic import ValidationError

from src.config import Settings
from src.main import create_app
from src.parser_sandbox import ParserFailed
from src.reader import ReadResult
from src.search import SearchFailed, SearchHit, TimeRange
from src.transport import FetchFailed


SECRET = "unit-only-secret"
AUTH = {"Authorization": f"Bearer {SECRET}"}


@pytest.mark.parametrize("retryable", [False, True])
@pytest.mark.parametrize("path", ["/search", "/read"])
async def test_service_preserves_upstream_retry_decision(retryable: bool, path: str) -> None:
    class FailedSearch:
        async def search(
            self, query: str, language: str | None, time_range: TimeRange | None
        ) -> list[SearchHit]:
            raise SearchFailed("web_unavailable", retryable=retryable)

    class FailedRead:
        async def read(self, url: str) -> ReadResult:
            raise FetchFailed("web_unavailable", retryable=retryable)

    response = await _request(
        create_app(
            SECRET,
            search_provider=FailedSearch(),
            read_provider=FailedRead(),
            sandbox_checker=lambda: True,
        ),
        "POST",
        path,
        headers=AUTH,
        json={"query": "public report"} if path == "/search" else {"url": "https://example.org"},
    )
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "web_unavailable", "retryable": retryable}}


def test_configuration_requires_a_nonblank_service_secret(monkeypatch) -> None:
    monkeypatch.delenv("WEB_TOOLS_SERVICE_SECRET", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)

    with pytest.raises(ValueError, match="service secret"):
        create_app(service_secret="   ")


async def _request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json: dict[str, object] | None = None,
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.request(method, path, headers=headers, json=json)


async def test_read_without_service_credential_is_denied_but_ready_is_public() -> None:
    app = create_app(service_secret=SECRET)

    denied = await _request(app, "POST", "/read", json={"url": "https://example.org"})
    ready = await _request(app, "GET", "/ready")

    assert denied.status_code == 401
    assert ready.json() == {"status": "ready"}


@pytest.mark.parametrize(
    "authorization",
    [
        "Basic dXNlcjpwYXNz",
        "Bearer wrong-secret",
        "Bearer",
        f"Bearer {SECRET} trailing",
    ],
)
async def test_malformed_or_wrong_credentials_are_denied(authorization: str) -> None:
    response = await _request(
        create_app(service_secret=SECRET),
        "POST",
        "/search",
        headers={"Authorization": authorization},
        json={"query": "release notes"},
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_wrong_secret_uses_constant_time_comparison(monkeypatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    original = hmac.compare_digest

    def observed(candidate: bytes, expected: bytes) -> bool:
        calls.append((candidate, expected))
        return original(candidate, expected)

    monkeypatch.setattr(hmac, "compare_digest", observed)

    response = await _request(
        create_app(service_secret=SECRET),
        "POST",
        "/read",
        headers={"Authorization": "Bearer x"},
        json={"url": "https://example.org"},
    )

    assert response.status_code == 401
    assert calls == [(b"x", SECRET.encode())]


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/search", {"query": "release notes", "headers": {"Cookie": "secret"}}),
        ("/read", {"url": "https://example.org", "serviceSecret": SECRET}),
        ("/read", {"url": "https://example.org", "method": "POST"}),
    ],
)
async def test_request_body_rejects_credentials_and_transport_controls(
    path: str,
    payload: dict[str, object],
) -> None:
    response = await _request(
        create_app(service_secret=SECRET), "POST", path, headers=AUTH, json=payload
    )

    assert response.status_code == 422


class _SearchProvider:
    async def search(
        self,
        query: str,
        language: str | None,
        time_range: TimeRange | None,
    ) -> list[SearchHit]:
        assert (query, language, time_range) == ("release notes", "en", "week")
        return [
            SearchHit(
                title="Release",
                url="https://example.org/release",
                snippet="A search result snippet",
                published_at=None,
            )
        ]


class _FailedSearchProvider:
    def __init__(self, code: str) -> None:
        self._code = code

    async def search(
        self,
        query: str,
        language: str | None,
        time_range: TimeRange | None,
    ) -> list[SearchHit]:
        del query, language, time_range
        raise SearchFailed(self._code)


class _ReadProvider:
    async def read(self, url: str) -> ReadResult:
        assert url == "https://example.org/report.pdf"
        return ReadResult(
            requested_url=url,
            final_url="https://cdn.example.org/report.pdf",
            content_type="application/pdf",
            text="[Page 1] Report",
            title="Report",
            truncated=False,
            page_count=1,
        )


class _FailedReadProvider:
    def __init__(self, code: str) -> None:
        self._code = code

    async def read(self, url: str) -> ReadResult:
        del url
        raise ParserFailed(self._code)


async def test_authenticated_search_and_read_use_bounded_providers() -> None:
    app = create_app(
        service_secret=SECRET,
        search_provider=_SearchProvider(),
        read_provider=_ReadProvider(),
        sandbox_checker=lambda: True,
    )
    search = await _request(
        app,
        "POST",
        "/search",
        headers=AUTH,
        json={"query": "release notes", "language": "en", "time_range": "week"},
    )
    read = await _request(
        app,
        "POST",
        "/read",
        headers=AUTH,
        json={"url": "https://example.org/report.pdf"},
    )

    assert search.status_code == 200
    assert search.json() == {
        "results": [
            {
                "title": "Release",
                "url": "https://example.org/release",
                "snippet": "A search result snippet",
                "published_at": None,
            }
        ]
    }
    assert read.status_code == 200
    assert read.json() == {
        "requested_url": "https://example.org/report.pdf",
        "final_url": "https://cdn.example.org/report.pdf",
        "content_type": "application/pdf",
        "text": "[Page 1] Report",
        "title": "Report",
        "truncated": False,
        "page_count": 1,
    }


async def test_read_and_readiness_fail_closed_when_seccomp_is_unavailable() -> None:
    app = create_app(
        service_secret=SECRET,
        search_provider=_SearchProvider(),
        read_provider=_ReadProvider(),
        sandbox_checker=lambda: False,
    )

    ready = await _request(app, "GET", "/ready")
    read = await _request(
        app,
        "POST",
        "/read",
        headers=AUTH,
        json={"url": "https://example.org/report.pdf"},
    )

    assert ready.status_code == 503
    assert read.status_code == 503
    assert read.json() == {"detail": {"code": "web_unavailable", "retryable": False}}


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [("web_unavailable", 503), ("web_timeout", 504)],
)
async def test_search_failure_code_maps_to_bounded_http_status(
    code: str,
    expected_status: int,
) -> None:
    response = await _request(
        create_app(
            service_secret=SECRET,
            search_provider=_FailedSearchProvider(code),
        ),
        "POST",
        "/search",
        headers=AUTH,
        json={"query": "release notes"},
    )

    assert response.status_code == expected_status
    assert response.json() == {"detail": {"code": code, "retryable": False}}


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        ("web_unavailable", 503),
        ("web_timeout", 504),
        ("web_too_large", 413),
        ("unsupported_document", 422),
    ],
)
async def test_read_failure_code_maps_to_bounded_http_status(
    code: str,
    expected_status: int,
) -> None:
    response = await _request(
        create_app(
            service_secret=SECRET,
            search_provider=_SearchProvider(),
            read_provider=_FailedReadProvider(code),
            sandbox_checker=lambda: True,
        ),
        "POST",
        "/read",
        headers=AUTH,
        json={"url": "https://example.org/report"},
    )

    assert response.status_code == expected_status
    assert response.json() == {"detail": {"code": code, "retryable": False}}


@pytest.mark.parametrize(
    "value",
    ["10.0.0.0/8,not-a-cidr", "10.0.0.1/8", "10.0.0.0/8,,192.168.0.0/16", "192.168.0.0"],
)
def test_operator_forbidden_cidrs_must_be_canonical(monkeypatch, value: str) -> None:
    """A typo must fail at startup, not silently block every fetch later.

    validate_target turns an unparseable operator CIDR into URLBlocked, so an
    unvalidated value would make every web_read look like a policy denial.
    """
    monkeypatch.setenv("WEB_TOOLS_SERVICE_SECRET", SECRET)
    monkeypatch.setenv("WEB_TOOLS_FORBIDDEN_CIDRS", value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_operator_forbidden_cidrs_reach_both_outbound_paths(monkeypatch) -> None:
    """The deny-list is useless unless it is actually handed to the adapters.

    Kubernetes runs no nftables egress sidecar, so this application check is
    the only place an operator range is enforced there.
    """
    monkeypatch.setenv("WEB_TOOLS_SERVICE_SECRET", SECRET)
    monkeypatch.setenv("WEB_TOOLS_FORBIDDEN_CIDRS", "203.0.113.0/24, 2001:db8::/32")
    settings = Settings(_env_file=None)
    assert settings.forbidden_cidrs == ("203.0.113.0/24", "2001:db8::/32")

    captured: dict[str, tuple[str, ...]] = {}

    class RecordingSearx:
        def __init__(self, **kwargs) -> None:
            captured["search"] = kwargs["forbidden_cidrs"]

    class RecordingTransport:
        def __init__(self, **kwargs) -> None:
            captured["read"] = kwargs["forbidden_cidrs"]

    monkeypatch.setattr("src.main.SearxngAdapter", RecordingSearx)
    monkeypatch.setattr("src.main.PinnedTransport", RecordingTransport)
    create_app(SECRET, forbidden_cidrs=settings.forbidden_cidrs)

    assert captured["search"] == ("203.0.113.0/24", "2001:db8::/32")
    assert captured["read"] == ("203.0.113.0/24", "2001:db8::/32")
