"""HTTP surface for Hermes sandboxes (see src/hermes_runtime.py).

Off unless SKILL_EXECUTOR_HERMES_ENABLED; Docker only for now, so the
Kubernetes runtime refuses explicitly. Run and event streams are NDJSON of the
relay's lines, which callers treat as untrusted data. Snapshot metadata is
returned to the caller, which phase 4 records in the session row.
"""

import json
import re
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from src.config import settings
from src.hermes_platform_skills import MAX_SKILLS, PlatformSkill
from src.hermes_runtime import FILE_MAX_BYTES, DockerHermesSandboxes, HermesSandbox, HermesSandboxError
from src.hermes_snapshots import Snapshot

router = APIRouter(prefix="/hermes")
sandboxes = DockerHermesSandboxes(settings)
_SESSION = r"^[0-9a-f]{32}$"
# Both become parts of snapshot object keys.
_OWNER = r"^[A-Za-z0-9-]{1,64}$"
_STATUS = {
    "hermes_sandbox_not_found": 404,
    "hermes_sandbox_owner_mismatch": 409,
    "hermes_subnet_pool_exhausted": 503,
    "hermes_subnet_allocation_failed": 503,
    "hermes_start_timeout": 504,
    "docker_too_old_for_isolated_networks": 503,
    "hermes_egress_guard_missing": 503,
    "hermes_sandbox_stopped": 409,
    "hermes_snapshot_unavailable": 409,
    "hermes_snapshot_not_found": 404,
    "hermes_snapshot_too_large": 507,
    "hermes_snapshot_store_failed": 503,
    "hermes_snapshot_timeout": 504,
    "hermes_resume_failed": 502,
    "hermes_capacity_exhausted": 503,
    "hermes_workspace_full": 507,
    "hermes_workspace_lost": 410,
    "hermes_clarify_settled": 409,
    "hermes_clarify_failed": 502,
    "hermes_snapshot_key_invalid": 400,
    "hermes_memory_failed": 502,
    "hermes_skills_failed": 502,
    "file_not_found": 404,
    "file_not_allowed": 403,
    "file_too_large": 413,
    "hermes_file_failed": 502,
    "hermes_inputs_failed": 502,
    "hermes_input_invalid": 400,
}


class CreateSandboxRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(pattern=_SESSION)
    org_id: str = Field(pattern=_OWNER)
    conversation_id: str = Field(pattern=_OWNER)
    token: str = Field(min_length=16, max_length=8192)
    model: str = Field(min_length=1, max_length=255)
    # The snapshot to restore when a new sandbox is created; the latest one
    # if unset, none with restore=false. A running or stopped sandbox keeps
    # its own workspace.
    snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    restore: bool = True


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=255)
    input: str = Field(min_length=1, max_length=1_000_000)
    session_id: str = Field(min_length=1, max_length=255)
    conversation_history: list[dict[str, Any]] | None = None


class MemoryFiles(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory: str = Field(max_length=2200)
    user: str = Field(max_length=1375)


class PlatformSkillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dir: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    bundle_storage_key: str = Field(min_length=1, max_length=500)
    bundle_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")


class PlatformSkills(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skills: list[PlatformSkillRequest] = Field(max_length=MAX_SKILLS)


class ClarifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    execution_generation: int = Field(ge=0, strict=True)
    answer: str = Field(min_length=1, max_length=16000)


_IDENTIFIER = re.compile(r"^[A-Za-z0-9-]{1,64}$")


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
        "state": sandbox.state,
        "workspace_full": sandbox.workspace_full,
    }


def _snapshot_view(snapshot: Snapshot | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    return {"sha256": snapshot.sha256, "size": snapshot.size, "created_at": snapshot.created_at}


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
        sandbox, outcome = await sandboxes.create(
            session_id=data.session_id,
            org_id=data.org_id,
            conversation_id=data.conversation_id,
            token=data.token,
            model=data.model,
            snapshot_sha256=data.snapshot_sha256,
            restore=data.restore,
        )
    except HermesSandboxError as exc:
        raise _error(exc) from None
    if outcome == "existing":
        response.status_code = 200
    return {**_view(sandbox), "outcome": outcome}


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
        if sandboxes.get_running(session_id).workspace_full:
            raise HermesSandboxError("hermes_workspace_full")
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
        sandboxes.get_running(session_id)
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


@router.put("/sandboxes/{session_id}/memory", status_code=204)
async def write_memory(session_id: str, files: MemoryFiles) -> Response:
    _available()
    try:
        await sandboxes.write_memory(session_id, files.model_dump())
    except HermesSandboxError as exc:
        raise _error(exc) from None
    return Response(status_code=204)


@router.get("/sandboxes/{session_id}/memory")
async def read_memory(session_id: str) -> dict[str, str | None]:
    _available()
    try:
        return await sandboxes.read_memory(session_id)
    except HermesSandboxError as exc:
        raise _error(exc) from None


@router.put("/sandboxes/{session_id}/platform-skills")
async def hydrate_skills(session_id: str, data: PlatformSkills) -> dict[str, int]:
    _available()
    try:
        return await sandboxes.hydrate_skills(
            session_id, [PlatformSkill(**item.model_dump()) for item in data.skills]
        )
    except HermesSandboxError as exc:
        raise _error(exc) from None


@router.get("/sandboxes/{session_id}/skills")
async def read_skills(session_id: str) -> dict[str, Any]:
    _available()
    try:
        return await sandboxes.read_skills(session_id)
    except HermesSandboxError as exc:
        raise _error(exc) from None


@router.get("/sandboxes/{session_id}/inputs")
async def list_inputs(session_id: str) -> dict[str, Any]:
    _available()
    try:
        return {"files": await sandboxes.list_inputs(session_id)}
    except HermesSandboxError as exc:
        raise _error(exc) from None


@router.put("/sandboxes/{session_id}/inputs", status_code=204)
async def write_input(session_id: str, request: Request, name: str = Query(min_length=1, max_length=255)) -> Response:
    """One of the user's uploads, for the workspace's inputs folder."""
    _available()
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > FILE_MAX_BYTES:
            raise HTTPException(status_code=413, detail={"code": "file_too_large"})
        chunks.append(chunk)
    try:
        await sandboxes.write_input(session_id, name, b"".join(chunks))
    except HermesSandboxError as exc:
        raise _error(exc) from None
    return Response(status_code=204)


@router.get("/sandboxes/{session_id}/files")
async def read_file(session_id: str, path: str = Query(min_length=1, max_length=1024)) -> Response:
    """A file from the sandbox's workspace, which the relay bounds; its
    bytes are the agent's and the caller checks what they are."""
    _available()
    if "\x00" in path:
        raise HTTPException(status_code=400, detail="Invalid path")
    try:
        data = await sandboxes.read_file(session_id, path)
    except HermesSandboxError as exc:
        raise _error(exc) from None
    return Response(content=data, media_type="application/octet-stream")


@router.post("/sandboxes/{session_id}/runs/{run_id}/clarify")
async def clarify_run(session_id: str, run_id: str, request: ClarifyRequest) -> dict[str, str]:
    _available()
    if not run_id.replace("_", "").isalnum() or len(run_id) > 128:
        raise HTTPException(status_code=400, detail="Invalid run id")
    try:
        await sandboxes.clarify(session_id, run_id, request.model_dump())
    except HermesSandboxError as exc:
        raise _error(exc) from None
    return {"status": "answered"}


@router.delete("/conversations/{org_id}/{conversation_id}")
async def purge_conversation(org_id: str, conversation_id: str) -> dict[str, int]:
    """The archive purge: a conversation's sandboxes and snapshots go."""
    _available()
    if not all(_IDENTIFIER.fullmatch(part) for part in (org_id, conversation_id)):
        raise HTTPException(status_code=400, detail="Invalid identifier")
    try:
        return await sandboxes.purge_conversation(org_id, conversation_id)
    except HermesSandboxError as exc:
        raise _error(exc) from None


@router.post("/sandboxes/{session_id}/snapshot")
async def snapshot_sandbox(session_id: str) -> dict[str, Any]:
    _available()
    try:
        return _snapshot_view(await sandboxes.snapshot(session_id)) or {}
    except HermesSandboxError as exc:
        raise _error(exc) from None


@router.delete("/sandboxes/{session_id}")
async def close_sandbox(session_id: str, snapshot: bool = False) -> Response:
    """With ?snapshot=true: stop, snapshot, then remove, answering with the
    snapshot; a failed snapshot leaves the sandbox stopped and is an error,
    except over the snapshot bound (507) or without a workspace (410), where
    the sandbox is removed anyway."""
    _available()
    if not session_id.isalnum() or len(session_id) != 32:
        raise HTTPException(status_code=400, detail="Invalid session id")
    try:
        taken = await sandboxes.close(session_id, snapshot=snapshot)
    except HermesSandboxError as exc:
        raise _error(exc) from None
    if taken is None:
        return Response(status_code=204)
    return Response(
        content=json.dumps({"snapshot": _snapshot_view(taken)}), media_type="application/json", status_code=200
    )
