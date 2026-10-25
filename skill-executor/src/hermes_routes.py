"""HTTP surface for Hermes sandboxes (see src/hermes_runtime.py).

Off unless SKILL_EXECUTOR_HERMES_ENABLED; Docker only for now, so the
Kubernetes runtime refuses explicitly. Run and event streams are NDJSON of the
relay's lines, which callers treat as untrusted data.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from src.config import settings
from src.hermes_runtime import DockerHermesSandboxes, HermesSandbox, HermesSandboxError

router = APIRouter(prefix="/hermes")
sandboxes = DockerHermesSandboxes(settings)
_SESSION = r"^[0-9a-f]{32}$"
_STATUS = {
    "hermes_sandbox_not_found": 404,
    "hermes_sandbox_owner_mismatch": 409,
    "hermes_subnet_pool_exhausted": 503,
    "hermes_subnet_allocation_failed": 503,
    "hermes_start_timeout": 504,
    "docker_too_old_for_isolated_networks": 503,
    "hermes_egress_guard_missing": 503,
}


class CreateSandboxRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(pattern=_SESSION)
    org_id: str = Field(min_length=1, max_length=64)
    conversation_id: str = Field(min_length=1, max_length=64)
    token: str = Field(min_length=16, max_length=8192)
    model: str = Field(min_length=1, max_length=255)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=255)
    input: str = Field(min_length=1, max_length=1_000_000)
    session_id: str = Field(min_length=1, max_length=255)
    conversation_history: list[dict[str, Any]] | None = None


def _available() -> None:
    if not settings.hermes_enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    if settings.runtime != "docker":
        raise HTTPException(status_code=501, detail="Hermes sandboxes need the Docker runtime")


def _error(exc: HermesSandboxError) -> HTTPException:
    return HTTPException(status_code=_STATUS.get(exc.code, 502), detail={"code": exc.code})


def _view(sandbox: HermesSandbox) -> dict[str, Any]:
    return {
        "session_id": sandbox.session_id,
        "created_at": sandbox.created_at,
        "last_active": sandbox.last_active,
        "expires_at": sandbox.created_at + settings.hermes_max_lifetime_seconds,
    }


async def _ndjson(lines: AsyncIterator[dict[str, Any]]) -> AsyncIterator[bytes]:
    try:
        async for line in lines:
            yield (json.dumps(line, separators=(",", ":")) + "\n").encode()
    except HermesSandboxError as exc:
        yield (json.dumps({"type": "error", "code": exc.code}) + "\n").encode()


@router.post("/sandboxes", status_code=201)
async def create_sandbox(data: CreateSandboxRequest, response: Response) -> dict[str, Any]:
    _available()
    try:
        sandbox, created = await sandboxes.create(
            session_id=data.session_id,
            org_id=data.org_id,
            conversation_id=data.conversation_id,
            token=data.token,
            model=data.model,
        )
    except HermesSandboxError as exc:
        raise _error(exc) from None
    if not created:
        response.status_code = 200
    return _view(sandbox)


@router.get("/sandboxes/{session_id}")
async def get_sandbox(session_id: str) -> dict[str, Any]:
    _available()
    try:
        return _view(sandboxes.get(session_id))
    except HermesSandboxError as exc:
        raise _error(exc) from None


@router.post("/sandboxes/{session_id}/runs")
async def start_run(session_id: str, data: RunRequest) -> StreamingResponse:
    _available()
    try:
        sandboxes.get(session_id)
    except HermesSandboxError as exc:
        raise _error(exc) from None
    return StreamingResponse(
        _ndjson(sandboxes.run(session_id, data.model_dump(exclude_none=True))),
        media_type="application/x-ndjson",
    )


@router.get("/sandboxes/{session_id}/runs/{run_id}/events")
async def run_events(session_id: str, run_id: str) -> StreamingResponse:
    _available()
    if not run_id.replace("_", "").isalnum() or len(run_id) > 128:
        raise HTTPException(status_code=400, detail="Invalid run id")
    try:
        sandboxes.get(session_id)
    except HermesSandboxError as exc:
        raise _error(exc) from None
    return StreamingResponse(_ndjson(sandboxes.events(session_id, run_id)), media_type="application/x-ndjson")


@router.post("/sandboxes/{session_id}/runs/{run_id}/stop", status_code=202)
async def stop_run(session_id: str, run_id: str) -> dict[str, str]:
    _available()
    if not run_id.replace("_", "").isalnum() or len(run_id) > 128:
        raise HTTPException(status_code=400, detail="Invalid run id")
    try:
        await sandboxes.stop(session_id, run_id)
    except HermesSandboxError as exc:
        raise _error(exc) from None
    return {"status": "stopping"}


@router.delete("/sandboxes/{session_id}", status_code=204)
async def close_sandbox(session_id: str) -> Response:
    _available()
    if not session_id.isalnum() or len(session_id) != 32:
        raise HTTPException(status_code=400, detail="Invalid session id")
    await sandboxes.close(session_id)
    return Response(status_code=204)
