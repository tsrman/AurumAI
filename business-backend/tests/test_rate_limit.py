from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aredis
from fastapi import FastAPI
from httpx import ASGITransport
from jwt.exceptions import PyJWTError
from starlette.requests import Request
from testcontainers.redis import RedisContainer

from src.core.cache import redis_client
from src.core.config import settings
from src.middleware.error_handler import setup_exception_handlers
from src.middleware.rate_limit import RateLimitMiddleware, _rate_limit_identity
from src.services.session_token import session_token_service


def _request(headers: list[tuple[bytes, bytes]]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/test",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
        }
    )


@pytest.mark.asyncio
async def test_rate_limit_identity_uses_keycloak_token_not_spoofed_header() -> None:
    request = _request(
        [
            (b"authorization", b"Bearer keycloak-token"),
            (b"x-user-id", b"attacker-controlled"),
        ]
    )

    with patch(
        "src.middleware.rate_limit.keycloak_client.decode_token",
        new_callable=AsyncMock,
        return_value={"sub": "user-1", "org_id": "org-1"},
    ):
        assert await _rate_limit_identity(request) == ("org-1", "user-1")


@pytest.mark.asyncio
async def test_rate_limit_identity_uses_runner_session_token() -> None:
    token = session_token_service.issue("conv-1", "user-2", "org-2")
    request = _request([(b"authorization", f"Bearer {token}".encode())])

    with patch(
        "src.middleware.rate_limit.keycloak_client.decode_token",
        new_callable=AsyncMock,
        side_effect=PyJWTError("not a keycloak token"),
    ):
        assert await _rate_limit_identity(request) == ("org-2", "user-2")


@pytest.mark.asyncio
async def test_rate_limit_identity_falls_back_to_anonymous_ip() -> None:
    request = _request([])

    assert await _rate_limit_identity(request) == ("anonymous", "127.0.0.1")


@pytest_asyncio.fixture
async def redis_url() -> None:
    with RedisContainer("redis:7-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        redis_client._client = aredis.from_url(
            f"redis://{host}:{port}/0",
            encoding="utf-8",
            decode_responses=True,
            max_connections=50,
        )
        yield
        await redis_client.disconnect()


@pytest.mark.asyncio
async def test_exceeding_rate_limit_returns_clean_429_not_raw_500(redis_url, monkeypatch) -> None:
    """Regression test: HTTPException raised inside RateLimitMiddleware.dispatch()
    (a BaseHTTPMiddleware) runs outside the ExceptionMiddleware layer that
    setup_exception_handlers() registers against, so it used to bypass our
    handlers entirely and surface as a raw, unhandled 500 instead of a clean
    429 — reproduced here with the real app middleware stack, not a unit
    test of dispatch() in isolation, since that's what actually exposed the
    bug."""
    monkeypatch.setattr(settings, "RATE_LIMIT_PER_MINUTE", 1)

    test_app = FastAPI()
    setup_exception_handlers(test_app)
    test_app.add_middleware(RateLimitMiddleware)

    @test_app.get("/v1/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    transport = ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/v1/ping")
        assert first.status_code == 200

        second = await client.get("/v1/ping")

    assert second.status_code == 429
    body = second.json()
    assert body["error"]["code"] == "RATE_LIMIT_EXCEEDED"


def _bucket_app() -> FastAPI:
    test_app = FastAPI()
    setup_exception_handlers(test_app)
    test_app.add_middleware(RateLimitMiddleware)

    @test_app.get("/v1/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    @test_app.post("/v1/chat/stream")
    async def stream() -> dict[str, bool]:
        return {"ok": True}

    return test_app


@pytest.mark.asyncio
async def test_ordinary_requests_do_not_consume_the_chat_stream_budget(redis_url, monkeypatch) -> None:
    # Browsing the UI must not block starting a chat: the stream limit is a
    # separate budget, not a lower ceiling on one shared per-user counter.
    monkeypatch.setattr(settings, "RATE_LIMIT_PER_MINUTE", 10)
    monkeypatch.setattr(settings, "RATE_LIMIT_PER_MINUTE_STREAM", 2)
    transport = ASGITransport(app=_bucket_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(5):
            assert (await client.get("/v1/ping")).status_code == 200
        assert (await client.post("/v1/chat/stream")).status_code == 200
        assert (await client.post("/v1/chat/stream")).status_code == 200
        assert (await client.post("/v1/chat/stream")).status_code == 429
        # Exhausting the stream budget leaves ordinary requests available.
        assert (await client.get("/v1/ping")).status_code == 200


def test_default_ordinary_request_limit_fits_a_single_page_application() -> None:
    from src.core.config import Settings

    defaults = Settings.model_fields
    assert defaults["RATE_LIMIT_PER_MINUTE"].default == 300
    assert defaults["RATE_LIMIT_PER_MINUTE_STREAM"].default == 30
