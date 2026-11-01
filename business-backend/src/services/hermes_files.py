"""Files Hermes Agent attaches to its answer (docs/HERMES_AGENT.md).

The MCP call reserves a ``file_attach`` operation of the session's bound
execution, commits it and starts one tool-worker workflow per operation. The
workflow reads the file from the sandbox through skill-executor and posts the
bytes to the upload route, which checks what they are and stores them as a
conversation image (PNG, JPEG, WebP) or a conversation file. The call waits a
bounded time; one that gives up fails the operation, so bytes arriving later
are refused rather than attached twice when the model tries again.
"""

import asyncio
import hashlib
import json
from datetime import timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from src.core.authorization_types import AuthorizationSubject
from src.core.config import settings
from src.core.temporal import get_temporal_client
from src.models.conversation_file import ConversationFile
from src.models.conversation_image import ConversationImage
from src.models.interaction import InteractionTurn, ToolOperation
from src.repositories.interaction_repository import InteractionRepository
from src.services.conversation_files import (
    MAX_FILE_BYTES,
    FileRejected,
    display_name,
    file_key,
    sniff,
)
from src.services.image_content import ImageContentError, inspect_image
from src.services.image_tools import image_storage_key
from src.services.object_storage import object_storage
from src.services.session_token import session_token_service

HERMES_FILE_WORKFLOW = "HermesFileAttachWorkflow"
# What the model may be told; anything else is file_unavailable.
FILE_ERROR_CODES = frozenset(
    {
        "file_not_found",
        "file_not_allowed",
        "file_too_large",
        "file_empty",
        "file_type_unsupported",
        "file_limit",
        "file_timeout",
        "file_unavailable",
    }
)
_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")
_OPEN = ("reserved", "running")


class FileToolError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AttachFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1024, strict=True)

    @field_validator("path")
    @classmethod
    def plain_path(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("path must name a file")
        return value


def _arguments_hash(request: AttachFileRequest) -> str:
    payload = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


async def attach_file(
    session: AsyncSession,
    subject: AuthorizationSubject,
    request: AttachFileRequest,
    *,
    session_id: UUID,
    conversation_id: UUID,
    execution_id: UUID,
    generation: int,
    invocation: UUID,
    client: Client | None = None,
) -> dict[str, Any]:
    """The attached file's metadata; raises FileToolError or InteractionError."""
    operation = await InteractionRepository(subject.org_id).reserve_operation(
        session, execution_id, invocation, "file_attach", _arguments_hash(request)
    )
    operation_id, status, result, error_code = (
        operation.id,
        operation.status,
        operation.result,
        operation.error_code,
    )
    # The upload route must see the operation: commit before waiting.
    await session.commit()
    if status == "completed" and result is not None:
        return result
    if status not in _OPEN:
        raise FileToolError(error_code if error_code in FILE_ERROR_CODES else "file_unavailable")
    token = session_token_service.issue(
        str(conversation_id), str(subject.user_id), str(subject.org_id), generation=generation
    )
    temporal = client or get_temporal_client()
    workflow_id = f"hermes-file-{operation_id}"
    try:
        handle = await temporal.start_workflow(
            HERMES_FILE_WORKFLOW,
            {
                "session_id": session_id.hex,
                "operation_id": str(operation_id),
                "path": request.path,
                "session_token": token,
            },
            id=workflow_id,
            task_queue=settings.HERMES_SKILL_TASK_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            execution_timeout=timedelta(seconds=settings.HERMES_FILE_TIMEOUT_SECONDS + 60),
        )
    except WorkflowAlreadyStartedError:
        # The same call again: wait for the run it started.
        handle = temporal.get_workflow_handle(workflow_id)
    try:
        async with asyncio.timeout(settings.HERMES_FILE_TIMEOUT_SECONDS):
            outcome = await handle.result()
    except TimeoutError:
        outcome = {"error": "file_timeout"}
    except WorkflowFailureError:
        outcome = {"error": "file_unavailable"}
    if isinstance(outcome, dict) and isinstance(outcome.get("file"), dict):
        attached: dict[str, Any] = outcome["file"]
        return attached
    code = outcome.get("error") if isinstance(outcome, dict) else None
    code = code if code in FILE_ERROR_CODES else "file_unavailable"
    # The upload may have stored the file although its answer never came
    # back: then the model gets it, not an error it would retry.
    stored = await _close(session, subject.org_id, operation_id, code)
    if stored is not None:
        return stored
    raise FileToolError(code)


async def _locked(
    session: AsyncSession, org_id: UUID, operation_id: UUID
) -> tuple[InteractionTurn, ToolOperation] | None:
    turn_id = await session.scalar(
        select(ToolOperation.turn_id).where(
            ToolOperation.org_id == org_id, ToolOperation.id == operation_id
        )
    )
    if turn_id is None:
        return None
    turn = await session.scalar(
        select(InteractionTurn)
        .where(InteractionTurn.org_id == org_id, InteractionTurn.id == turn_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    operation = await session.scalar(
        select(ToolOperation)
        .where(ToolOperation.org_id == org_id, ToolOperation.id == operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if turn is None or operation is None or operation.kind != "file_attach":
        return None
    return turn, operation


async def _close(
    session: AsyncSession, org_id: UUID, operation_id: UUID, code: str
) -> dict[str, Any] | None:
    """Fail an operation still open. Under the lock an upload holds while it
    stores, so a file stored meanwhile is returned instead."""
    locked = await _locked(session, org_id, operation_id)
    stored: dict[str, Any] | None = None
    if locked is not None:
        operation = locked[1]
        if operation.status == "completed" and operation.result is not None:
            stored = operation.result
        elif operation.status in _OPEN:
            operation.status = "failed"
            operation.error_code = code
    await session.commit()
    return stored


async def store_upload(
    session: AsyncSession,
    org_id: UUID,
    conversation_id: UUID,
    operation_id: UUID,
    path: str,
    data: bytes,
) -> dict[str, Any]:
    """Store the bytes for an open operation of the conversation's active
    turn; raises FileToolError. A repeated upload returns the stored result."""
    locked = await _locked(session, org_id, operation_id)
    if locked is None or locked[0].conversation_id != conversation_id:
        raise FileToolError("file_operation_not_found")
    turn, operation = locked
    if operation.status == "completed" and operation.result is not None:
        return operation.result
    if operation.status not in _OPEN or turn.status != "active":
        raise FileToolError("hermes_turn_inactive")
    try:
        result = await _store(session, org_id, conversation_id, operation, path, data)
    except FileRejected as exc:
        operation.status = "failed"
        operation.error_code = exc.code
        await session.commit()
        raise FileToolError(exc.code) from None
    operation.status = "completed"
    operation.result = result
    operation.error_code = None
    await session.commit()
    return result


async def _store(
    session: AsyncSession,
    org_id: UUID,
    conversation_id: UUID,
    operation: ToolOperation,
    path: str,
    data: bytes,
) -> dict[str, Any]:
    if len(data) > MAX_FILE_BYTES:
        raise FileRejected("file_too_large")
    if data.startswith(_IMAGE_MAGIC) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        # A picture is shown with the answer, as a generated image.
        try:
            info = inspect_image(data, max_bytes=MAX_FILE_BYTES)
        except ImageContentError:
            raise FileRejected("file_type_unsupported") from None
        key = image_storage_key(org_id, conversation_id, operation.id, info.extension)
        await object_storage.put_bytes(key, data)
        image = ConversationImage(
            org_id=org_id,
            conversation_id=conversation_id,
            turn_id=operation.turn_id,
            execution_id=operation.execution_id,
            operation_id=operation.id,
            origin="generated",
            storage_key=key,
            content_type=info.content_type,
            size_bytes=info.size_bytes,
            width=info.width,
            height=info.height,
        )
        session.add(image)
        await session.flush()
        return {
            "imageId": str(image.id),
            "contentType": info.content_type,
            "width": info.width,
            "height": info.height,
        }
    kind = sniff(data, path)
    name = display_name(path, kind)
    key = file_key(org_id, conversation_id, operation.id, kind)
    await object_storage.put_bytes(key, data)
    attached = ConversationFile(
        org_id=org_id,
        conversation_id=conversation_id,
        turn_id=operation.turn_id,
        execution_id=operation.execution_id,
        operation_id=operation.id,
        name=name,
        storage_key=key,
        content_type=kind.content_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    session.add(attached)
    await session.flush()
    return {
        "fileId": str(attached.id),
        "name": name,
        "contentType": kind.content_type,
        "sizeBytes": len(data),
    }
