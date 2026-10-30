import asyncio
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
from src.core.temporal import connect_temporal, disconnect_temporal
from src.models import workflow_template  # noqa: F401
from src.services.session_manager import session_manager
from src.services.interaction_maintenance import (
    run_interaction_maintenance,
    run_web_result_scrubber,
)
from src.services.interaction_outbox import run_interaction_outbox_dispatcher
from src.services.workflow_run import workflow_run_service
from src.services.hermes_egress import require_egress_secret
from src.services.hermes_sessions import run_idle_session_closer
from src.routers import (
    auth,
    chat,
    documents,
    health,
    knowledge_bases,
    agent_definitions,
    metrics,
    models as models_router,
    settings as settings_router,
    skills as skills_router,
    usage as usage_router,
    questions,
    questions_internal,
    turns,
    web_tools_internal,
    web_sources,
    image_tools_internal,
    conversation_files,
    conversation_images,
    hermes,
    hermes_egress_internal,
    hermes_internal,
    hermes_mcp,
    hermes_memory,
    hermes_skill_drafts,
    workflow_runs_internal,
    workflows,
)

tracer = init_telemetry("business-backend", endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT)
# Instrumented at import time, not in lifespan(): both only need the module-
# level `engine`/httpx classes to exist, not a running event loop, and doing
# it here means tests that monkeypatch `engine` for the lifespan itself
# (test_main_business_lifespan.py) don't need to fake this too.
HTTPXClientInstrumentor().instrument()
SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings.DEBUG)
    if settings.INIT_DB_ON_STARTUP:
        await init_db()
    await redis_client.connect()
    await keycloak_client.load_jwks()
    await connect_temporal()
    try:
        removed = await session_manager.cleanup_stale()
        if removed:
            logger.info("cleanup_stale removed %d stale runner containers", removed)
    except Exception:
        logger.warning("cleanup_stale failed at startup", exc_info=True)
    try:
        # Runs right after cleanup_stale because it covers what cleanup_stale
        # structurally cannot: containers whose Redis record is gone entirely,
        # which is every runner container after a host reboot (redis has no
        # volume, so its state does not survive one).
        await session_manager.sweep_orphans()
    except Exception:
        logger.warning("sweep_orphans failed at startup", exc_info=True)
    background_tasks = [
        asyncio.create_task(session_manager.run_reaper()),
        asyncio.create_task(workflow_run_service.run_retention_reaper()),
        asyncio.create_task(run_interaction_outbox_dispatcher()),
        asyncio.create_task(run_interaction_maintenance(session_manager)),
        asyncio.create_task(run_web_result_scrubber()),
        asyncio.create_task(run_idle_session_closer()),
    ]
    if settings.RUNNER_RUNTIME != "kubernetes":
        background_tasks.append(asyncio.create_task(session_manager.maintain_pool()))
    logger.info("business_backend_started")
    try:
        yield
    finally:
        for task in background_tasks:
            task.cancel()
        for task in background_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        await session_manager.close()
        await disconnect_temporal()
        await engine.dispose()
        await redis_client.disconnect()
        logger.info("business_backend_stopped")


require_egress_secret()
app = create_app(
    title=settings.APP_NAME,
    lifespan=lifespan,
    routers=[
        (health.router, "", "health"),
        (metrics.router, "", "metrics"),
        (auth.router, "/v1/auth", "auth"),
        (chat.router, "/v1", "chat"),
        (settings_router.router, "/v1/settings", "settings"),
        (skills_router.router, "/v1/skills", "skills"),
        (models_router.router, "/v1", "models"),
        (knowledge_bases.router, "/v1", "knowledge-bases"),
        (agent_definitions.router, "/v1", "agent-definitions"),
        (documents.router, "/v1", "documents"),
        (usage_router.router, "/v1", "usage"),
        (workflows.router, "/v1", "workflows"),
        (workflow_runs_internal.router, "/v1/internal", "internal-workflow-runs"),
        (questions.router, "/v1", "questions"),
        (questions_internal.router, "/v1/internal", "internal-questions"),
        (web_tools_internal.router, "/v1/internal", "internal-web-tools"),
        (web_sources.router, "/v1", "web-sources"),
        (image_tools_internal.router, "/v1/internal", "internal-image-tools"),
        (conversation_images.router, "/v1", "conversation-images"),
        (conversation_files.router, "/v1", "conversation-files"),
        (hermes.router, "/v1", "hermes"),
        (hermes_mcp.router, "/v1", "hermes"),
        (hermes_memory.router, "/v1", "hermes-memory"),
        (hermes_skill_drafts.router, "/v1", "hermes-skill-drafts"),
        (hermes_internal.router, "/v1/internal", "internal-hermes"),
        (hermes_egress_internal.router, "/v1/internal", "internal-hermes-egress"),
        (turns.router, "/v1", "turns"),
    ],
)
