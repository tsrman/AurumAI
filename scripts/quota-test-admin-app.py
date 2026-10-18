#!/usr/bin/env python3
"""Minimal admin-plane process for the isolated quota E2E test."""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPOSITORY_ROOT / "business-backend"
sys.path.insert(0, str(BACKEND_ROOT))

from src.core.database import engine  # noqa: E402
from src.core.keycloak import keycloak_client  # noqa: E402
from src.middleware.error_handler import setup_exception_handlers  # noqa: E402
from src.routers import (  # noqa: E402
    admin_analytics,
    admin_models,
    admin_users,
    authorization_admin,
    auth,
    health,
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await keycloak_client.load_jwks()
    try:
        yield
    finally:
        await engine.dispose()


app = FastAPI(title="Quota E2E admin backend", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3100"],
    allow_credentials=False,
    allow_methods=["GET", "PUT", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
setup_exception_handlers(app)
app.include_router(health.router)
app.include_router(auth.router, prefix="/v1/auth")
app.include_router(admin_models.router, prefix="/v1/admin")
app.include_router(admin_users.router, prefix="/v1/admin")
app.include_router(admin_analytics.router, prefix="/v1/admin")
app.include_router(authorization_admin.router, prefix="/v1/admin/authorization")


if __name__ == "__main__":
    port = int(os.environ.get("QUOTA_E2E_ADMIN_PORT", "18001"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
