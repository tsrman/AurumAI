from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from fastapi import FastAPI
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

from src.app_factory import create_app
from src.core.config import settings
from src.core.database import engine, init_db
from src.core.cache import redis_client
from src.core.keycloak import keycloak_client
from src.core.logging import configure_logging, logger
from src.core.telemetry import init_telemetry
# Register FK targets used by skills/knowledge bases and workflow run models
# in this standalone process. ORM metadata is shared; chat routes/runtime are not.
from src.models import conversation, message, workflow_template  # noqa: F401
from src.routers import (
    auth,
    health,
    metrics,
    skills as skills_router,
    skills_admin,
    skill_bundles,
    groups as groups_router,
    models as models_router,
    authorization_admin,
    admin_analytics,
    admin_hermes,
    admin_models,
    admin_users,
    agent_definitions,
    knowledge_bases,
    workflows_admin,
    workflow_templates_admin,
)

tracer = init_telemetry("business-backend-admin", endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT)
HTTPXClientInstrumentor().instrument()
SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """No Temporal, no session_manager, no Docker socket — the admin
    plane never spawns runner containers, so it doesn't need any of the
    chat-plane's background tasks (reaper, warm pool)."""
    configure_logging(settings.DEBUG)
    if settings.INIT_DB_ON_STARTUP:
        await init_db()
    await redis_client.connect()
    await keycloak_client.load_jwks()
    logger.info("admin_backend_started")
    yield
    await engine.dispose()
    await redis_client.disconnect()
    logger.info("admin_backend_stopped")


app = create_app(
    title=f"{settings.APP_NAME} (Admin)",
    lifespan=lifespan,
    routers=[
        (health.router, "", "health"),
        (metrics.router, "", "metrics"),
        (auth.router, "/v1/auth", "auth"),
        (skills_router.router, "/v1/skills", "skills"),
        (skills_admin.router, "/v1/skills", "skills-admin"),
        (skill_bundles.router, "/v1/skills", "skills-admin"),
        (groups_router.router, "/v1/admin/groups", "admin"),
        (authorization_admin.router, "/v1/admin/authorization", "authorization-admin"),
        (admin_analytics.router, "/v1/admin", "admin-analytics"),
        (admin_models.router, "/v1/admin", "admin-models"),
        (admin_hermes.router, "/v1/admin", "admin-hermes"),
        (admin_users.router, "/v1/admin", "admin-users"),
        (agent_definitions.router, "/v1", "agent-definitions"),
        (knowledge_bases.router, "/v1", "knowledge-bases"),
        (workflows_admin.router, "/v1/admin", "admin-workflows"),
        (workflow_templates_admin.router, "/v1/admin", "admin-workflow-templates"),
        (workflows_admin.diagnostics_router, "/v1/admin", "admin-workflow-runs"),
        (models_router.router, "/v1", "models"),
    ],
)
