from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from src.core.config import settings
from src.middleware.error_handler import setup_exception_handlers
from src.middleware.audit import AuditMiddleware
from src.middleware.metrics import MetricsMiddleware
from src.middleware.rate_limit import RateLimitMiddleware

RouterMount = tuple[APIRouter, str, str]


def create_app(
    title: str,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]],
    routers: list[RouterMount],
) -> FastAPI:
    """Shared FastAPI wiring for both entrypoints (main_business.py,
    main_admin.py) — CORS, exception handlers, rate limiting, and router
    mounting are identical between the two; only the lifespan (which
    background tasks/connections it opens) and the router list differ."""
    app = FastAPI(
        title=title,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.DEBUG else None,
        redoc_url="/redoc" if settings.DEBUG else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.FRONTEND_URL],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    )

    setup_exception_handlers(app)
    # Middleware order matters: AuditMiddleware is added first and therefore
    # wraps RateLimitMiddleware, observing rejected429 mutation attempts too.
    # A rate-limited mutation attempt is still an auditable event.
    app.add_middleware(AuditMiddleware)
    # MetricsMiddleware also wraps RateLimitMiddleware, like AuditMiddleware,
    # so it observes limiter429 responses.
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(RateLimitMiddleware)

    for router, prefix, tag in routers:
        app.include_router(router, prefix=prefix, tags=[tag])

    FastAPIInstrumentor.instrument_app(app)

    return app
