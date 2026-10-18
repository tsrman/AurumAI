import asyncio
from typing import Any

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from src.core.cache import redis_client
from src.core.config import settings
from src.core.database import engine
from src.core.keycloak import keycloak_client
from src.core.logging import logger
from src.services.session_manager import session_manager

router = APIRouter()

# Readiness probes must not cause their own timeout: Kubernetes probes
# usually allow seconds, not tens of seconds.
_CHECK_TIMEOUT_SECONDS = 3.0


@router.get("/health", status_code=status.HTTP_200_OK)
async def health() -> dict[str, str]:
    """Process liveness without external dependency checks.

    A responsive process need not restart merely because its database is
    temporarily unavailable; /ready checks that separate condition.
    """
    return {"status": "ok"}


async def _check_database() -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def _check_redis() -> None:
    await redis_client.client.ping()


async def _check_keycloak() -> None:
    # JWKS is cached for a day. A warm cache proves readiness without a
    # network request because it is already validating current tokens.
    if keycloak_client.jwks_is_fresh:
        return
    await keycloak_client.load_jwks()


async def _check_runner_runtime() -> None:
    await session_manager.ready()


async def _run(name: str, check: Any) -> tuple[str, str | None]:
    try:
        await asyncio.wait_for(check(), timeout=_CHECK_TIMEOUT_SECONDS)
        return name, None
    except asyncio.TimeoutError:
        return name, "TimeoutError"
    except Exception as exc:  # noqa: BLE001 — expose only the exception class
        return name, type(exc).__name__


@router.get("/ready")
async def ready(response: Response) -> dict[str, Any]:
    """Traffic readiness based on PostgreSQL, Redis and Keycloak JWKS.

    Return503 if a required dependency is unavailable instead of directing
    requests to an instance that cannot serve them. Report check names and
    safe status only; exception text can contain credential-bearing URLs.
    """
    checks_to_run = [
        _run("database", _check_database),
        _run("redis", _check_redis),
        _run("keycloak", _check_keycloak),
    ]
    if settings.RUNNER_RUNTIME == "kubernetes":
        checks_to_run.append(_run("runner_runtime", _check_runner_runtime))

    results = await asyncio.gather(*checks_to_run)

    checks = {name: ("ok" if err is None else "fail") for name, err in results}
    failed = {name: err for name, err in results if err is not None}

    if failed:
        logger.warning("readiness_check_failed", checks=failed)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not ready", "checks": checks, "errors": failed}

    return {"status": "ready", "checks": checks}
