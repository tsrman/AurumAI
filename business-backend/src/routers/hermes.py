"""Native-tools chat completions for Hermes Agent sandboxes.

The runner's /v1/chat/completions keeps its text tool protocol. This route
accepts OpenAI `tools`, assistant `tool_calls` and `tool` messages, streams the
gateway's chunks (tool call deltas included) unchanged, and applies the same
live capability, model grant, vision and quota checks. Only a Hermes session
token is accepted, and only while its open session is bound to an active turn;
the call is billed to that turn's workflow correlation, never to the body's.
"""

import logging
import uuid
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, StreamingResponse
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
from src.services.completion_stream import (
    UpstreamCompletionError,
    collect_completion,
    proxy_completion_events,
    quota_exceeded_exception,
)
from src.services.hermes_access import HermesAccessDenied, require_hermes_capabilities
from src.services.hermes_completions import HermesRequestError, gateway_messages, has_images
from src.services.hermes_sessions import authorize_token
from src.services.image_uploads import vision_enabled
from src.services.model_access import ensure_model_allowed
from src.services.quota import QuotaExceededError, QuotaService, get_quota_service
from src.services.streaming import create_streaming_response

router = APIRouter(dependencies=[Depends(require_hermes_enabled)])
logger = logging.getLogger(__name__)


def _denied(exc: HermesAccessDenied) -> NoReturn:
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": exc.code})


@router.post("/hermes/chat/completions", response_model=None)
async def hermes_chat_completions(
    request: HermesCompletionRequest,
    user: UserInfo = Depends(get_hermes_session_user),
    session: AsyncSession = Depends(get_hermes_db_session),
    quota: QuotaService = Depends(get_quota_service),
) -> StreamingResponse | JSONResponse:
    subject = authorization_subject(user)
    conversation_id = hermes_conversation_id(user)
    try:
        await require_hermes_capabilities(session, subject)
        hermes_session = await authorize_token(session, subject, user.raw_claims)
    except HermesAccessDenied as exc:
        _denied(exc)
    # Read before the commit below expires the row.
    hermes_session_id = hermes_session.id
    correlation: dict[str, Any] = {
        "workflow_run_id": hermes_session.workflow_run_id,
        "workflow_phase": hermes_session.workflow_phase,
        "workflow_node_key": hermes_session.workflow_node_key,
    }
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
        await quota.reserve(
            subject.org_id,
            subject.user_id,
            conversation_id,
            request.model,
            request_id,
            workflow_run_id=correlation["workflow_run_id"],
            workflow_phase=correlation["workflow_phase"],
            workflow_node_key=correlation["workflow_node_key"],
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
            metadata={
                "hermes_session_id": str(hermes_session_id),
                **{key: str(value) for key, value in correlation.items() if value is not None},
            },
        )
    events = proxy_completion_events(
        llm_request,
        quota=quota,
        org_id=subject.org_id,
        request_id=request_id,
        model=request.model,
        generation=generation,
        log=logger,
    )
    if request.stream:
        return create_streaming_response(events)
    try:
        completion = await collect_completion(events, model=request.model, request_id=request_id)
    except UpstreamCompletionError as exc:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"error": {"message": exc.message, "type": "upstream_error"}},
        )
    return JSONResponse(content=completion)
