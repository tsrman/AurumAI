import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Response, status
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from pydantic import BaseModel

from src.config import settings
from src.metrics import MetricsMiddleware, metrics_endpoint
from src.sandbox_runtime import (
    BundleExtractionError,
    NetworkPolicyError,
    SessionNotFoundError,
    create_sandbox_runtime,
)
from src.object_storage import ObjectStorageError, object_storage
from src.skill_md import SkillMdError, extract_skill_md_body
from src.telemetry import init_telemetry

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

tracer = init_telemetry("skill-executor")
HTTPXClientInstrumentor().instrument()

sandbox_runtime = create_sandbox_runtime(settings)
_READY_TIMEOUT_SECONDS = 3.0


class OpenSessionRequest(BaseModel):
    skill_id: str
    bundle_storage_key: str
    bundle_checksum: str
    conversation_id: str | None = None
    network_policy: str = "none"


class OpenSessionResponse(BaseModel):
    session_id: str
    skill_md: str
    expires_at: float


class ExecRequest(BaseModel):
    command: str
    timeout_seconds: float | None = None


class ExecResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Startup sweep: containers left behind by a previous process (whose
    # in-memory session map died with it) would otherwise live forever.
    try:
        await sandbox_runtime.reap_orphans()
    except Exception:
        logger.warning("startup orphan sweep failed", exc_info=True)
    try:
        await sandbox_runtime.ready()
    except Exception:
        logger.warning("sandbox runtime readiness failed", exc_info=True)
    reaper_task = asyncio.create_task(sandbox_runtime.run_reaper())
    logger.info(
        "skill-executor started (sandbox_image=%s, session_ttl=%ss)",
        settings.sandbox_image,
        settings.session_ttl_seconds,
    )
    yield
    reaper_task.cancel()
    try:
        await reaper_task
    except asyncio.CancelledError:
        pass
    await sandbox_runtime.shutdown()


app = FastAPI(title="skill-executor", lifespan=lifespan)
app.add_middleware(MetricsMiddleware)
app.add_api_route("/metrics", metrics_endpoint, methods=["GET"])
FastAPIInstrumentor.instrument_app(app)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
async def ready(response: Response) -> dict[str, object]:
    try:
        await asyncio.wait_for(
            sandbox_runtime.ready(), timeout=_READY_TIMEOUT_SECONDS
        )
    except TimeoutError:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "not ready",
            "checks": {"sandbox_runtime": "fail"},
            "errors": {"sandbox_runtime": "TimeoutError"},
        }
    except Exception as exc:
        logger.warning("sandbox runtime readiness failed", exc_info=True)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "not ready",
            "checks": {"sandbox_runtime": "fail"},
            "errors": {"sandbox_runtime": type(exc).__name__},
        }
    return {"status": "ready", "checks": {"sandbox_runtime": "ok"}}


@app.post("/sessions", response_model=OpenSessionResponse, status_code=201)
async def open_session(data: OpenSessionRequest) -> OpenSessionResponse:
    try:
        bundle_bytes = await object_storage.get_bytes(data.bundle_storage_key)
    except ObjectStorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    actual_checksum = hashlib.sha256(bundle_bytes).hexdigest()
    if actual_checksum != data.bundle_checksum:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Bundle checksum mismatch for skill {data.skill_id}: "
                f"expected {data.bundle_checksum}, got {actual_checksum}"
            ),
        )

    try:
        skill_md = extract_skill_md_body(bundle_bytes)
    except SkillMdError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    try:
        session = await sandbox_runtime.create_session(
            bundle_bytes, skill_md, network_policy=data.network_policy
        )
    except BundleExtractionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except NetworkPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return OpenSessionResponse(
        session_id=session.session_id,
        skill_md=session.skill_md,
        expires_at=session.created_at + settings.session_ttl_seconds,
    )


@app.post("/sessions/{session_id}/exec", response_model=ExecResponse)
async def exec_in_session(session_id: str, data: ExecRequest) -> ExecResponse:
    try:
        result = await sandbox_runtime.exec_command(
            session_id, data.command, timeout=data.timeout_seconds
        )
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ExecResponse(**result)


@app.delete("/sessions/{session_id}", status_code=204)
async def close_session(session_id: str) -> None:
    await sandbox_runtime.close_session(session_id)


if __name__ == "__main__":
    uvicorn.run(app, host=settings.bind_host, port=settings.bind_port)
