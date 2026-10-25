"""Native-tools chat completions for Hermes Agent sandboxes.

The runner's /v1/chat/completions keeps its text tool protocol. This route
accepts OpenAI `tools`, assistant `tool_calls` and `tool` messages, streams the
gateway's chunks (tool call deltas included) unchanged, and applies the same
live capability, model grant, vision and quota checks. Only a Hermes session
token is accepted, and only while its conversation has an active turn.
"""

import logging
import uuid
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo
from src.core.authorization_dependencies import authorization_subject
from src.core.hermes_auth import (
    get_hermes_db_session,
    get_hermes_session_user,
    hermes_conversation_id,
    require_hermes_enabled,
)
from src.core.langfuse_client import get_langfuse, sanitize_native_messages
from src.schemas.hermes import HermesCompletionRequest
from src.services.completion_stream import proxy_completion_events, quota_exceeded_exception
from src.services.hermes_access import (
    HermesAccessDenied,
    active_hermes_turn,
    require_hermes_capabilities,
)
from src.services.hermes_completions import HermesRequestError, gateway_messages, has_images
from src.services.image_uploads import vision_enabled
from src.services.model_access import ensure_model_allowed
from src.services.quota import QuotaExceededError, QuotaService, get_quota_service
from src.services.streaming import create_streaming_response

router = APIRouter(dependencies=[Depends(require_hermes_enabled)])
logger = logging.getLogger(__name__)


def _denied(exc: HermesAccessDenied) -> NoReturn:
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": exc.code})


@router.post("/hermes/chat/completions")
async def hermes_chat_completions(
    request: HermesCompletionRequest,
    user: UserInfo = Depends(get_hermes_session_user),
    session: AsyncSession = Depends(get_hermes_db_session),
    quota: QuotaService = Depends(get_quota_service),
) -> StreamingResponse:
    subject = authorization_subject(user)
    conversation_id = hermes_conversation_id(user)
    try:
        await require_hermes_capabilities(session, subject)
        await active_hermes_turn(session, subject, conversation_id)
    except HermesAccessDenied as exc:
        _denied(exc)
    await ensure_model_allowed(
        session, user.org_id, request.model, user_id=user.user_id, role=user.role
    )
    vision = has_images(request.messages) and await vision_enabled(
        session, subject.org_id, request.model
    )
    try:
        messages = gateway_messages(request.messages, vision=vision)
    except HermesRequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail={"code": exc.code}
        ) from None
    # Release the fenced transaction before quota/provider I/O, as on the
    # runner route; the active turn keeps the conversation from being archived.
    await session.commit()
    llm_request: dict[str, Any] = {
        "model": request.model,
        "messages": messages,
        **request.gateway_fields(),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    request_id = uuid.uuid4()
    try:
        # Workflow correlation is deliberately absent: the sandbox cannot
        # choose which run or node it bills until session rows bind it.
        await quota.reserve(
            subject.org_id, subject.user_id, conversation_id, request.model, request_id
        )
    except QuotaExceededError as exc:
        raise quota_exceeded_exception(exc) from exc
    langfuse = get_langfuse()
    generation = None
    if langfuse:
        generation = langfuse.generation(
            trace_id=str(conversation_id),
            name="hermes_chat_completions",
            model=request.model,
            input={"messages": sanitize_native_messages(messages)},
            model_parameters={
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
            },
            metadata={"hermes_session_id": user.raw_claims["sid"]},
        )
    return create_streaming_response(
        proxy_completion_events(
            llm_request,
            quota=quota,
            org_id=subject.org_id,
            request_id=request_id,
            model=request.model,
            generation=generation,
            log=logger,
        )
    )
