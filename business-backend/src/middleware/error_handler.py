from typing import Any

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from jwt.exceptions import PyJWTError, ExpiredSignatureError
from sqlalchemy.exc import IntegrityError
from src.core.exceptions import PlatformException
from src.core.logging import logger
import uuid


def build_error_body(
    code: str, message: str, request: Request, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    return {
        "error": {"code": code, "message": message, "details": details or {}},
        "meta": {"requestId": request_id, "timestamp": "2026-06-19T00:00:00Z"},
    }


def setup_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        body = build_error_body(
            exc.status_code == 401 and "UNAUTHORIZED" or "FORBIDDEN", exc.detail, request,
        )
        evidence = getattr(request.state, "keycloak_rejection", None)
        if exc.status_code == 401 and evidence is not None:
            # Emit only for a final rejection, not a successful session-token
            # fallback. Never include headers, URL, exception text or claims.
            logger.warning("keycloak_jwt_rejected", request_id=body["meta"]["requestId"], **evidence)
        return JSONResponse(
            status_code=exc.status_code,
            content=body,
        )

    @app.exception_handler(PlatformException)
    async def platform_exception_handler(request: Request, exc: PlatformException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=build_error_body(exc.code, exc.message, request, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=build_error_body("VALIDATION_ERROR", str(exc), request),
        )

    @app.exception_handler(PyJWTError)
    async def jwt_error_handler(request: Request, exc: PyJWTError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=build_error_body("UNAUTHORIZED", "Invalid token", request),
        )

    @app.exception_handler(ExpiredSignatureError)
    async def expired_signature_handler(request: Request, exc: ExpiredSignatureError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=build_error_body("UNAUTHORIZED", "Token expired", request),
        )

    @app.exception_handler(IntegrityError)
    async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=build_error_body("CONFLICT", "Resource already exists", request),
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content=build_error_body("INTERNAL_ERROR", "Internal server error", request),
        )
