import time
import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from jwt.exceptions import PyJWTError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from src.core.cache import redis_client
from src.core.config import settings
from src.core.keycloak import keycloak_client
from src.middleware.error_handler import build_error_body
from src.services.hermes_session_token import (
    InvalidHermesSessionToken,
    hermes_session_token_service,
)
from src.services.session_token import session_token_service

HERMES_EGRESS_AUTHORIZE_PATH = "/v1/internal/hermes/egress/authorize"
HERMES_PREVIEW_CHECK_PATH = "/v1/internal/hermes/previews/check"


def _bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.cookies.get("access_token")


def _fallback_identity(request: Request) -> tuple[str, str]:
    host = request.client.host if request.client else "unknown"
    return "anonymous", host


async def _rate_limit_identity(request: Request) -> tuple[str, str]:
    token = _bearer_token(request)
    if not token:
        return _fallback_identity(request)

    try:
        payload = await keycloak_client.decode_token(token)
        return str(payload.get("org_id") or "no-org"), str(payload["sub"])
    except (PyJWTError, KeyError, httpx.HTTPError):
        # httpx.HTTPError covers a failed JWKS reload (Keycloak unreachable)
        # — rate limiting degrades to IP-based identity below rather than
        # failing the request outright.
        pass

    try:
        payload = session_token_service.decode(token)
        return str(payload.get("org_id") or "no-org"), str(payload["sub"])
    except (PyJWTError, KeyError):
        pass

    # A Hermes sandbox counts against its user's budget, like the runner.
    try:
        payload = hermes_session_token_service.decode(token)
        return str(payload["org_id"]), str(payload["sub"])
    except (PyJWTError, InvalidHermesSessionToken):
        return _fallback_identity(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not request.url.path.startswith("/v1"):
            return await call_next(request)
        # Called by hermes-egress once per sandbox connection, and by the
        # preview service once per browser connection; each applies its own
        # limits, and all of its calls share one address.
        if request.url.path in (HERMES_EGRESS_AUTHORIZE_PATH, HERMES_PREVIEW_CHECK_PATH):
            return await call_next(request)

        org_id, user_id = await _rate_limit_identity(request)
        window = int(time.time()) // 60
        # Chat streams and ordinary requests are separate budgets: browsing the
        # UI must not use up the allowance for starting a conversation turn.
        is_stream = request.url.path.endswith("/stream")
        bucket = "stream" if is_stream else "api"
        key = f"rate_limit:{org_id}:{user_id}:{bucket}:{window}"
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.expire(key, 60)

        limit = settings.RATE_LIMIT_PER_MINUTE_STREAM if is_stream else settings.RATE_LIMIT_PER_MINUTE

        if count > limit:
            # Raising HTTPException here would not work — BaseHTTPMiddleware.
            # dispatch() runs outside the ExceptionMiddleware layer that
            # setup_exception_handlers() registers against, so a raised
            # HTTPException bypasses our handlers entirely and surfaces as a
            # raw, unhandled 500 (confirmed via server logs) instead of a
            # clean 429 — including on CORS preflight OPTIONS requests,
            # which then aborts the real request client-side before it's
            # even sent. Returning the response directly sidesteps that.
            return JSONResponse(
                status_code=429,
                content=build_error_body("RATE_LIMIT_EXCEEDED", "Rate limit exceeded", request),
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(max(0, limit - count))
        return response
