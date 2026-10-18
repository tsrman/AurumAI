#!/usr/bin/env python3
"""Minimal backend process for the quota E2E boundary.

It mounts production auth/chat/usage routers and their real dependencies, but
does not start Redis, Temporal, or runner lifecycle services. The quota smoke
only reaches paths which complete before those services are needed.
"""

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
from src.routers import auth, chat, health, models, settings, usage  # noqa: E402


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await keycloak_client.load_jwks()
    try:
        yield
    finally:
        await engine.dispose()


app = FastAPI(title="Quota E2E backend", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=False,
    allow_methods=["GET", "PUT", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
setup_exception_handlers(app)
app.include_router(health.router)
app.include_router(auth.router, prefix="/v1/auth")
app.include_router(chat.router, prefix="/v1")
app.include_router(usage.router, prefix="/v1")
app.include_router(settings.router, prefix="/v1/settings")
app.include_router(models.router, prefix="/v1")


if __name__ == "__main__":
    port = int(os.environ.get("QUOTA_E2E_BACKEND_PORT", "18000"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
