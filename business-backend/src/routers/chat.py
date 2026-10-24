from sqlalchemy import select
from pydantic import BaseModel
import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any, Optional
from datetime import datetime, timezone
import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from pydantic import ValidationError
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from src.core.config import settings
from src.core.dependencies import get_org_db_session, get_service_db_session
from src.core.auth import get_current_user, get_current_user_or_session, UserInfo
from src.core.authorization_dependencies import authorization_subject, require_capability
from src.core.capability_catalog import Capability
from src.core.events import event_bus
from src.core.exceptions import PlatformException
from src.core.langfuse_client import get_langfuse, sanitize_content, sanitize_messages
from src.schemas.chat import (
    ChatCompletionMessage as ChatCompletionMessage,
    ChatCompletionRequest,
    ChatRequest,
    MessageScoreRequest,
    PersistConversationMessageRequest,
    PersistedImageClaims,
    PersistedWebSourceClaims,
)
from src.schemas.conversation import (
    ConversationCreate,
    ConversationUpdate,
    ConversationResponse,
    MessageResponse,
)
from src.schemas.common import ApiResponse, Meta, Pagination
from src.schemas.document import DocumentResponse
from src.core.temporal import get_temporal_client
from src.services.streaming import sse_event, create_streaming_response
from src.services.chat_service import chat_service
from src.services.conversation_attachments import attach_file
from src.services.image_tools import organization_image_models, recent_conversation_images
from src.services.model_access import ensure_model_allowed
from src.services.authorization import authorization_service
from src.services.session_manager import session_manager
from src.services.llm_client import llm_client
from src.services.model_parameters import resolve_temperature
from src.services.organization_model_settings import organization_model_settings_service
from src.services.runner_runtime import RunnerRuntimeError
from src.services.quota import (
    QuotaExceededError,
    QuotaService,
    get_quota_service,
)
from src.models.conversation import Conversation
from src.models.message import Message
from src.models.workflow_run import WorkflowRun, WorkflowRunStatus
from src.repositories.conversation_repository import conversation_repository
from src.repositories.message_repository import message_repository
from src.repositories.workflow_definition_repository import workflow_definition_repository
from src.repositories.image_repository import ImageRepository, ImageRepositoryError
from src.repositories.web_source_repository import (
    WebSourceRepository,
    WebSourceRepositoryError,
)
from src.workflows.conversation import ConversationWorkflow, ConversationInput
from src.services.workflow_authorization import WorkflowDefinitionNotReadable
from src.services.workflow_graph import WorkflowGraphError
from src.services.workflow_run import WorkflowRunNotFound, workflow_run_service
from src.services.interaction import (
    InteractionConflict,
    InteractionNotFound,
    interaction_service,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _meta() -> Meta:
    return Meta(
        requestId=f"req_{uuid.uuid4().hex}", timestamp=datetime.now(timezone.utc).isoformat()
    )


def _conv_response(conv: Conversation, org_default_temperature: float | None) -> ConversationResponse:
    return ConversationResponse(
        id=str(conv.id),
        title=conv.title,
        model=conv.model,
        status=conv.status or "active",
        lastMessageAt=conv.last_message_at.isoformat() if conv.last_message_at else None,
        createdAt=conv.created_at.isoformat(),
        temperature=resolve_temperature(conv.temperature, org_default_temperature),
    )


def _msg_response(msg: Message) -> MessageResponse:
    extra = msg.extra if isinstance(msg.extra, dict) else {}
    return MessageResponse(
        id=str(msg.id),
        role=msg.role,
        content=msg.content,
        model=msg.model,
        tokensIn=msg.tokens_in or 0,
        tokensOut=msg.tokens_out or 0,
        costUsd=float(msg.cost_usd or 0),
        latencyMs=msg.latency_ms,
        toolCalls=msg.tool_calls if isinstance(msg.tool_calls, list) else [],
        toolResults=msg.tool_results if isinstance(msg.tool_results, dict) else {},
        createdAt=msg.created_at.isoformat(),
        faithfulnessScore=extra.get("faithfulness_score"),
        faithfulnessReason=extra.get("faithfulness_reason"),
        turnId=(
            str(turn_id)
            if (turn_id := getattr(msg, "turn_id", None)) is not None
            else None
        ),
        sourceIds=extra.get("web_source_ids", []),
        unsupportedSourceIds=extra.get("unsupported_web_source_ids", []),
        imageIds=extra.get("image_ids", []),
    )


def _parse_conversation_id(conversation_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(conversation_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid conversation id",
        ) from exc


def _quota_platform_exception(exc: QuotaExceededError) -> PlatformException:
    return PlatformException(
        code="quota_exceeded",
        message="Monthly quota exceeded",
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        details={
            "resource": exc.resource,
            "used": exc.used,
            "limit": exc.limit,
            "periodEnd": exc.period_end.isoformat(),
        },
    )


def _quota_uuid(value: str | None, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value or "")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid session {field}",
        ) from exc


def _parse_provider_usage(
    chunk_json: str,
    *,
    request_id: uuid.UUID,
    org_id: uuid.UUID,
    model: str,
) -> tuple[int, int, int] | None:
    try:
        payload = json.loads(chunk_json)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or "usage" not in payload:
        return None
    usage = payload["usage"]
    if usage is None:
        # Not a defect: with stream_options {"include_usage": true} an
        # OpenAI-compatible provider puts `"usage": null` on *every* chunk and
        # the real figures only on the last one. Warning here logged a line
        # per token of every stream.
        return None
    values: tuple[object, object, object] | None = None
    if isinstance(usage, dict):
        values = (
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            usage.get("total_tokens"),
        )
    if values is None or any(type(value) is not int or value < 0 for value in values):
        logger.warning(
            "invalid LLM usage ignored (request_id=%s org_id=%s model=%s)",
            request_id,
            org_id,
            model,
        )
        return None
    return values  # type: ignore[return-value]


async def _get_owned_conversation(
    session: AsyncSession,
    conversation_id: str,
    user: UserInfo,
    *,
    allow_deleted: bool = False,
    write: bool = False,
) -> "Conversation":
    conversation_uuid = _parse_conversation_id(conversation_id)
    if write:
        from src.models.data_identity import ConversationIdentity
        from src.services.conversation_lifecycle import lock_active_conversation
        owner = await session.scalar(select(ConversationIdentity).where(ConversationIdentity.id == conversation_uuid, ConversationIdentity.org_id == uuid.UUID(user.org_id or "")))
        if owner is not None and str(owner.user_id) == user.user_id:
            await lock_active_conversation(session, owner.org_id, owner.id)
            session.info["conversation_fence"] = (owner.org_id, owner.id, owner.generation)
    conv = await conversation_repository.get(session, str(conversation_uuid))
    if conv is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found",
        )
    if str(conv.user_id) != user.user_id or str(conv.org_id) != user.org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )
    # DELETE only sets status="deleted" (soft delete) — every other route
    # through this helper used to keep working on a "deleted" conversation
    # exactly as if nothing happened (GET/PATCH/cancel/stream), only the
    # list endpoint filtered it out.
    if conv.status == "deleted" and not allow_deleted:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Conversation was deleted",
        )
    return conv


async def _signal_or_start_conversation_workflow(
    temporal_client: Client,
    conversation_id: str,
    message: dict[str, Any],
) -> str | None:
    # A plain signal-then-catch-NOT_FOUND-then-start here used to race:
    # two concurrent first messages for a brand-new conversation both get
    # NOT_FOUND from the signal attempt, then both call start_workflow with
    # the same workflow_id — the loser's call raised an uncaught
    # WorkflowAlreadyStartedError (500) *after* its message was already
    # saved to the DB. start_signal +
    # id_conflict_policy=USE_EXISTING makes Temporal's server do this
    # atomically in one RPC: start if nothing's running, or just signal
    # whatever's already there — no client-side race window at all.
    handle = await temporal_client.start_workflow(
        ConversationWorkflow.run,
        ConversationInput(conversation_id=conversation_id, lifecycle_generation=int(message.get("lifecycle_generation", 1))),
        id=f"conversation-{conversation_id}",
        task_queue=f"session-{conversation_id}",
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        start_signal="user_message",
        start_signal_args=[message],
    )

    return handle.result_run_id


async def _mark_workflow_runner_unavailable(
    session: AsyncSession,
    run: WorkflowRun,
) -> None:
    try:
        await workflow_run_service.transition(
            session,
            run.id,
            WorkflowRunStatus.FAILED,
            "runner_unavailable",
        )
        await session.commit()
    except Exception:
        await session.rollback()
        logger.error(
            "failed to mark workflow run unavailable (run_id=%s)",
            run.id,
            exc_info=True,
        )


async def _mark_interaction_turn_unavailable(
    session: AsyncSession,
    user: UserInfo,
    conversation_id: uuid.UUID,
    turn_id: uuid.UUID,
) -> None:
    try:
        await interaction_service.finish_turn(
            session,
            uuid.UUID(user.org_id or ""),
            uuid.UUID(user.user_id),
            conversation_id,
            turn_id,
            status="failed",
        )
        await session.commit()
    except Exception:
        await session.rollback()
        logger.error(
            "failed to mark interaction turn unavailable (turn_id=%s)",
            turn_id,
            exc_info=True,
        )


@router.post("/chat/{conversation_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_generation(
    conversation_id: str,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> None:
    conv = await _get_owned_conversation(session, conversation_id, user, write=True)
    cancellation = await interaction_service.cancel_active_turn(
        session, authorization_subject(user), conv.id
    )
    if cancellation is not None:
        # Persist cancellation before notifying workers: an in-flight web
        # response must fail its live-turn check even while the worker awaits I/O.
        turn_id, targets = cancellation
        await session.commit()
        await interaction_service.signal_turn_cancel(targets, turn_id, conv.id)
        return
    # Preserve cancellation for legacy histories without a durable turn row.
    temporal_client = get_temporal_client()
    handle = temporal_client.get_workflow_handle(f"conversation-{conv.id}")
    await handle.signal("cancel_generation")


def _extract_workflow_call(text: str) -> uuid.UUID | None:
    """Parse a <workflow_call>{"workflow_id": "..."}</workflow_call> marker.

    Mirrors conversation-runner's extract_tool_calls tag/JSON-parse shape
    (conversation-runner/src/workflows/tools.py) — a same-convention sibling,
    not a shared import, since the two services don't share code.

    Tolerant of a missing closing tag: live verification against a real
    (small/fast) model showed it correctly picking a workflow but wrapping
    the reply in a markdown code fence and never emitting
    `</workflow_call>` — reading to end-of-string and parsing the first
    balanced JSON object recovers that case instead of silently discarding
    a correct classification.
    """
    start_tag = "<workflow_call>"
    end_tag = "</workflow_call>"
    idx = text.find(start_tag)
    if idx == -1:
        return None
    payload_start = idx + len(start_tag)
    end = text.find(end_tag, payload_start)
    payload = text[payload_start:end] if end != -1 else text[payload_start:]
    payload = payload.strip()
    if payload.endswith("```"):
        payload = payload[:-3].rstrip()
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        try:
            parsed, _ = json.JSONDecoder().raw_decode(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, dict):
        return None
    raw_id = parsed.get("workflow_id")
    if not isinstance(raw_id, str):
        return None
    try:
        return uuid.UUID(raw_id)
    except ValueError:
        return None


async def _maybe_classify_workflow(
    session: AsyncSession,
    quota: QuotaService,
    user: UserInfo,
    request: ChatRequest,
) -> uuid.UUID | None:
    """Auto-route a turn to a workflow the caller never explicitly picked.

    Only ever runs the classification LLM call when the org has at least one
    accessible, auto_routable workflow — every org without an opt-in
    definition pays zero extra LLM calls per turn. Any failure (no match,
    malformed output, an id outside the accessible set, a network error)
    falls back to None (ordinary chat) rather than raising — a user who
    never touched the workflow picker should never see a workflow-specific
    error surfaced from a routing attempt they didn't ask for.
    """
    subject = authorization_subject(user)
    candidates = await workflow_definition_repository.list_accessible(
        session, subject, auto_routable_only=True
    )
    if not candidates:
        return None

    try:
        await ensure_model_allowed(
            session,
            user.org_id,
            request.model,
            user_id=user.user_id,
            role=user.role,
        )
        await quota.preflight(_quota_uuid(user.org_id, "organization id"))
    except (HTTPException, QuotaExceededError):
        return None

    candidate_ids = {candidate.id for candidate in candidates}
    listing = "\n".join(
        f"- id={candidate.id} name={candidate.name}: {candidate.description or ''}"
        for candidate in candidates
    )
    system_prompt = (
        "You route chat turns to background multi-agent workflows. Available "
        f"workflows:\n{listing}\n\n"
        "If the user's message clearly matches exactly one workflow, reply "
        "with exactly this and nothing else, on one line, with no markdown "
        "code fence and no other text: "
        '<workflow_call>{"workflow_id": "<id>"}</workflow_call>. If none '
        "clearly match, reply with nothing."
    )
    llm_request = {
        "model": request.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": request.messages[-1].content},
        ],
        "temperature": 0.0,
        "max_tokens": 200,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    org_id = _quota_uuid(user.org_id, "organization id")
    user_id = _quota_uuid(user.user_id, "user id")
    conversation_id = (
        _quota_uuid(request.conversation_id, "conversation id") if request.conversation_id else None
    )
    quota_request_id = uuid.uuid4()
    try:
        await quota.reserve(org_id, user_id, conversation_id, request.model, quota_request_id)
    except QuotaExceededError:
        return None

    full_output = ""
    last_usage: tuple[int, int, int] | None = None
    provider_accepted = False

    async def on_accepted() -> None:
        nonlocal provider_accepted
        provider_accepted = True
        await quota.accept(org_id, quota_request_id)

    try:
        async for chunk_json in llm_client.stream_chat(llm_request, on_accepted=on_accepted):
            if not chunk_json:
                continue
            usage = _parse_provider_usage(
                chunk_json, request_id=quota_request_id, org_id=org_id, model=request.model
            )
            if usage is not None:
                last_usage = usage
            try:
                data = json.loads(chunk_json)
                delta = data["choices"][0].get("delta", {}).get("content", "")
                full_output += delta
            except Exception:
                pass
    except Exception:
        logger.warning("workflow auto-routing classification failed", exc_info=True)
        return None
    finally:
        # CancelledError does not derive from Exception. Match the completion
        # proxy's bounded, shielded refund so task cancellation cannot strand
        # an unaccepted reservation or be replaced by a cleanup exception.
        if not provider_accepted:
            try:
                await asyncio.wait_for(
                    asyncio.shield(quota.cancel(org_id, quota_request_id)),
                    timeout=3.0,
                )
            except Exception:
                logger.warning(
                    "quota cancellation failed for auto-routing classification",
                    exc_info=True,
                )

    if last_usage is not None:
        try:
            await quota.complete(org_id, quota_request_id, *last_usage)
        except Exception:
            logger.warning("quota completion failed for auto-routing classification", exc_info=True)
    workflow_id = _extract_workflow_call(full_output)
    if workflow_id is None or workflow_id not in candidate_ids:
        return None
    return workflow_id


@router.post("/chat/stream")
async def chat_stream(
    request: ChatRequest,
    user: UserInfo = Depends(require_capability(Capability.CHAT_SEND)),
    session: AsyncSession = Depends(get_org_db_session),
    quota: QuotaService = Depends(get_quota_service),
) -> StreamingResponse:
    if not request.conversation_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="conversation_id is required",
        )
    conversation_id = request.conversation_id
    user_message = request.messages[-1]
    turn_id = uuid.uuid4()
    workflow_run: WorkflowRun | None = None

    conv = await _get_owned_conversation(session, conversation_id, user, write=True)

    turn_model = request.model
    effective_workflow_id = request.workflow_id
    auto_routed = False
    if effective_workflow_id is None:
        effective_workflow_id = await _maybe_classify_workflow(session, quota, user, request)
        auto_routed = effective_workflow_id is not None

    if effective_workflow_id is not None:
        message = Message(
            id=uuid.uuid4(),
            conversation_id=_parse_conversation_id(conversation_id),
            org_id=_quota_uuid(user.org_id, "organization id"),
            user_id=_quota_uuid(user.user_id, "user id"),
            role="user",
            content=user_message.content,
            model=None,
            turn_id=turn_id,
        )
        session.add(message)
        try:
            await session.flush()
            workflow_run = await workflow_run_service.create_authorized_run(
                session,
                authorization_subject(user),
                effective_workflow_id,
                _parse_conversation_id(conversation_id),
                turn_id,
                message.id,
            )
            supervisor = workflow_run.execution_snapshot.get("supervisor")
            if not isinstance(supervisor, dict) or not isinstance(supervisor.get("model"), str):
                raise WorkflowGraphError("invalid_snapshot")
            turn_model = supervisor["model"]
            message.model = turn_model
            await quota.preflight(_quota_uuid(user.org_id, "organization id"))
            await interaction_service.begin_turn(
                session,
                authorization_subject(user),
                _parse_conversation_id(conversation_id),
                turn_id,
                workflow_run.id,
            )
            conv.last_message_at = datetime.now(timezone.utc)
            await session.commit()
        except InteractionConflict as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Conversation already has an active turn",
            ) from exc
        except (
            WorkflowDefinitionNotReadable,
            WorkflowRunNotFound,
            WorkflowGraphError,
            QuotaExceededError,
        ) as exc:
            await session.rollback()
            if not auto_routed:
                if isinstance(exc, (WorkflowDefinitionNotReadable, WorkflowRunNotFound)):
                    raise HTTPException(status_code=404, detail="Workflow not found") from exc
                if isinstance(exc, WorkflowGraphError):
                    raise HTTPException(status_code=409, detail="Workflow unavailable") from exc
                raise _quota_platform_exception(exc) from exc
            # Auto-routed pick failed (stale id, TOCTOU race, quota) — the
            # user never asked for this workflow, so fall back to ordinary
            # chat instead of surfacing a workflow-specific error.
            logger.info(
                "workflow auto-routing failed, falling back to ordinary chat",
                exc_info=True,
            )
            effective_workflow_id = None
            workflow_run = None
            turn_model = request.model

    if effective_workflow_id is None:
        await ensure_model_allowed(
            session,
            user.org_id,
            request.model,
            user_id=user.user_id,
            role=user.role,
        )
        try:
            await quota.preflight(_quota_uuid(user.org_id, "organization id"))
        except QuotaExceededError as exc:
            raise _quota_platform_exception(exc) from exc

        try:
            await interaction_service.begin_turn(
                session,
                authorization_subject(user),
                _parse_conversation_id(conversation_id),
                turn_id,
                None,
            )
        except InteractionConflict as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Conversation already has an active turn",
            ) from exc

        await message_repository.create(
            session,
            conversation_id=conversation_id,
            org_id=user.org_id,
            user_id=user.user_id,
            role="user",
            content=user_message.content,
            model=request.model,
            turn_id=turn_id,
        )
        conv.last_message_at = datetime.now(timezone.utc)
        await session.commit()

    langfuse = get_langfuse()
    trace = None
    if langfuse:
        trace = langfuse.trace(
            id=conversation_id,
            session_id=user.user_id,
            user_id=user.user_id,
            metadata={
                "org_id": user.org_id,
                "model": turn_model,
                "conversation_id": conversation_id,
                "workflow_run_id": str(workflow_run.id) if workflow_run else None,
            },
        )
        trace.span(
            name="chat_stream",
            input={
                "messages": sanitize_messages([{"role": "user", "content": user_message.content}]),
                "model": turn_model,
            },
        )

    # Subscribe before the workflow can possibly start publishing — Redis
    # pub/sub drops messages sent before a subscriber joins, and the runner
    # starts emitting events (message-start, thinking-delta, ...) within
    # milliseconds of being signaled.
    # user.org_id, not conv.org_id: the auto-routing fallback above may have
    # rolled back the session, which expires conv's attributes — re-reading
    # conv.org_id here would need a fresh SELECT the async session can't do
    # implicitly. user.org_id is already proven equal (checked above).
    channel = f"{user.org_id}:session:{conversation_id}:events"
    pubsub = await event_bus.subscribe_channel(channel)

    from src.services.conversation_lifecycle import begin_operation, settle_operation
    signal_operation = await begin_operation(session, uuid.UUID(user.org_id or ""), uuid.UUID(conversation_id), kind="workflow_signal", operation_key=turn_id)
    signal_operation.workflow_id = f"conversation-{conversation_id}"
    await session.commit()
    try:
        await session_manager.get_or_create(conversation_id, user.user_id, user.org_id, generation=signal_operation.generation)
    except RunnerRuntimeError as exc:
        if workflow_run is not None:
            await _mark_workflow_runner_unavailable(session, workflow_run)
        else:
            await _mark_interaction_turn_unavailable(session, user, conv.id, turn_id)
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()  # type: ignore[no-untyped-call]
        logger.warning("runner_runtime_unavailable", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Runner runtime unavailable",
        ) from exc

    try:
        temporal_client = get_temporal_client()
        web_tools_enabled = settings.WEB_ENABLED and await authorization_service.has_capability(
            session,
            authorization_subject(user),
            Capability.WEB_ACCESS,
        )
        image_generation_enabled = image_editing_enabled = False
        if settings.IMAGES_ENABLED and await authorization_service.has_capability(
            session,
            authorization_subject(user),
            Capability.IMAGES_GENERATE,
        ):
            generation_model, edit_model = await organization_image_models(
                session, uuid.UUID(user.org_id or "")
            )
            image_generation_enabled = bool(generation_model)
            image_editing_enabled = bool(edit_model)
        recent_images: list[dict[str, str]] = []
        if image_editing_enabled:
            recent_images = await recent_conversation_images(
                session, uuid.UUID(user.org_id or ""), conv.id
            )
        signal_payload: dict[str, Any] = {
            "role": "user",
            "content": user_message.content,
            "turn_id": str(turn_id),
            "user_questions_enabled": settings.USER_QUESTIONS_ENABLED,
            "web_tools_enabled": web_tools_enabled,
            "image_generation_enabled": image_generation_enabled,
            "image_editing_enabled": image_editing_enabled,
        }
        if recent_images:
            signal_payload["recent_images"] = recent_images
        if workflow_run is None:
            signal_payload["model"] = request.model
        else:
            signal_payload["workflow_run_id"] = str(workflow_run.id)
        signal_payload["lifecycle_operation_id"] = str(signal_operation.id)
        signal_payload["lifecycle_generation"] = signal_operation.generation
        exact_run_id = await _signal_or_start_conversation_workflow(
            temporal_client,
            conversation_id,
            signal_payload,
        )
        if exact_run_id is None:
            raise RuntimeError("Exact workflow run unavailable")
        from src.services.execution_closure import register_execution
        await register_execution(session, temporal_client, org_id=uuid.UUID(user.org_id or ""), conversation_id=uuid.UUID(conversation_id), generation=signal_operation.generation, workflow_id=f"conversation-{conversation_id}", run_id=uuid.UUID(exact_run_id))
        await settle_operation(session, signal_operation, workflow_id=f"conversation-{conversation_id}", run_id=uuid.UUID(exact_run_id))
        await session.commit()
    except Exception as exc:
        logger.warning(
            "conversation_start_unavailable",
            exc_info=True,
            extra={"conversation_id": str(conversation_id)},
        )
        if workflow_run is not None:
            await _mark_workflow_runner_unavailable(session, workflow_run)
        else:
            await _mark_interaction_turn_unavailable(session, user, conv.id, turn_id)
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()  # type: ignore[no-untyped-call]
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Runner runtime unavailable",
        ) from exc

    async def event_generator() -> AsyncGenerator[str, None]:
        async for event in event_bus.listen(
            pubsub,
            channel,
            timeout_seconds=settings.EVENT_SUBSCRIBE_TIMEOUT,
            reset_timeout_on_message=workflow_run is not None,
        ):
            event_turn_id = event.get("turn_id")
            if event_turn_id is not None and event_turn_id != str(turn_id):
                continue
            yield sse_event(event["type"], event["data"])
            if event["type"] == "message-score":
                break
            # The background faithfulness judge (EvaluationWorkflow) publishes
            # its own "message-score" event once it finishes, separately from
            # "done" — if it's running for this turn, keep the connection open
            # a bit longer instead of closing right away, so the UI can pick
            # up the score live instead of only on next reload. Bounded by the
            # same EVENT_SUBSCRIBE_TIMEOUT either way.
            if event["type"] == "done" and not event["data"].get("evaluation_pending"):
                break

    return create_streaming_response(event_generator())


@router.post("/conversations/{conversation_id}/messages", status_code=status.HTTP_201_CREATED)
async def persist_conversation_message(
    conversation_id: str,
    data: PersistConversationMessageRequest,
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> ApiResponse[MessageResponse]:
    if user.raw_claims.get("type") != "session":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only runner session tokens can persist generated messages",
        )
    try:
        session_conversation_id = uuid.UUID(
            str(user.raw_claims.get("conversation_id", ""))
        )
        path_conversation_id = uuid.UUID(conversation_id)
        # Parsed once here so every later use is a 403 on a malformed session
        # identity rather than an unhandled ValueError deeper in the handler.
        session_org_id = uuid.UUID(str(user.org_id or ""))
        session_user_id = uuid.UUID(str(user.user_id or ""))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Session is not bound to this conversation",
        ) from exc
    if session_conversation_id != path_conversation_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Session is not bound to this conversation",
        )

    conv = await conversation_repository.get(session, conversation_id)
    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    if str(conv.user_id) != user.user_id or str(conv.org_id) != user.org_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    if conv.status == "deleted":
        # The conversation was deleted while a turn was in flight — a 4xx
        # here is caught cleanly by persist_assistant_message's own
        # non-retryable-4xx wrapping (conversation-runner/src/
        # workflows/activities.py, via raise_non_retryable_on_4xx) rather
        # than retried forever.
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Conversation was deleted")

    clean_tool_results = dict(data.tool_results or {})
    raw_source_claims = clean_tool_results.pop("__web_sources__", None)
    message_extra: dict[str, Any] | None = None
    if raw_source_claims is not None:
        try:
            source_claims = PersistedWebSourceClaims.model_validate(raw_source_claims)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid web source claims",
            ) from exc
        claimed_ids = tuple(str(source_id) for source_id in source_claims.source_ids)
        try:
            owned_ids = await WebSourceRepository(session_org_id).owned_source_ids(
                session,
                conv.id,
                session_user_id,
                data.turn_id,
                claimed_ids,
            )
        except WebSourceRepositoryError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Sources not found",
            ) from exc
        accepted = list(dict.fromkeys(source_id for source_id in claimed_ids if source_id in owned_ids))
        unsupported = list(
            dict.fromkeys(
                [
                    *(source_id for source_id in claimed_ids if source_id not in owned_ids),
                    *source_claims.unsupported_source_ids,
                ]
            )
        )
        message_extra = {
            "web_source_ids": accepted,
            "unsupported_web_source_ids": unsupported,
        }

    raw_image_claims = clean_tool_results.pop("__images__", None)
    if raw_image_claims is not None:
        try:
            image_claims = PersistedImageClaims.model_validate(raw_image_claims)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid image claims",
            ) from exc
        claimed_images = tuple(dict.fromkeys(str(image_id) for image_id in image_claims.image_ids))
        try:
            owned_images = await ImageRepository(session_org_id).owned_image_ids(
                session, conv.id, session_user_id, data.turn_id, claimed_images
            )
        except ImageRepositoryError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Images not found",
            ) from exc
        message_extra = {
            **(message_extra or {}),
            "image_ids": [image_id for image_id in claimed_images if image_id in owned_images],
        }

    now = datetime.now(timezone.utc)
    message = await message_repository.upsert_assistant_by_turn(
        session,
        conversation_id=conversation_id,
        turn_id=data.turn_id,
        role=data.role,
        content=data.content,
        model=data.model,
        org_id=user.org_id,
        tool_calls=data.tool_calls,
        tool_results=clean_tool_results or None,
        extra=message_extra,
    )
    if data.role == "assistant":
        try:
            await interaction_service.finish_turn(
                session,
                session_org_id,
                session_user_id,
                conv.id,
                data.turn_id,
                status="completed",
            )
        except InteractionNotFound:
            # Rolling deploy compatibility for turns accepted before the
            # interaction tables/routes became active.
            logger.info("assistant persisted for legacy turn without interaction row")
    conv.last_message_at = now
    await session.commit()
    return ApiResponse(data=_msg_response(message), meta=_meta())


@router.patch("/conversations/{conversation_id}/messages/{turn_id}/score")
async def update_message_score(
    conversation_id: str,
    turn_id: str,
    data: MessageScoreRequest,
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> ApiResponse[MessageResponse]:
    """Called by the runner's background EvaluationWorkflow once the
    faithfulness judge finishes (or fails) — see conversation-runner's
    evaluator.py. Persists the score/reason on the assistant message and
    publishes a `message-score` event so an SSE stream still listening for
    this turn (see chat_stream below) can relay it live."""
    if user.raw_claims.get("type") != "session":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only runner session tokens can record evaluation scores",
        )

    conv = await conversation_repository.get(session, conversation_id)
    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    if str(conv.user_id) != user.user_id or str(conv.org_id) != user.org_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    message = await message_repository.update_score_by_turn(
        session,
        conversation_id=conversation_id,
        turn_id=turn_id,
        role="assistant",
        score=data.score,
        reason=data.reason,
    )
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")

    await event_bus.publish(
        f"{conv.org_id}:session:{conversation_id}:events",
        {
            "type": "message-score",
            "data": {"turn_id": turn_id, "score": data.score, "reason": data.reason},
            "turn_id": turn_id,
        },
    )

    return ApiResponse(data=_msg_response(message), meta=_meta())


@router.post("/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    http_request: Request,
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
    quota: QuotaService = Depends(get_quota_service),
) -> StreamingResponse:
    """OpenAI-compatible chat completions endpoint used by runner containers."""
    if not await authorization_service.has_capability(
        session, authorization_subject(user), Capability.CHAT_SEND
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing capability: chat.send",
        )
    await ensure_model_allowed(
        session,
        user.org_id,
        request.model,
        user_id=user.user_id,
        role=user.role,
    )
    # Authorization can reopen the generation-fenced transaction. Release it
    # before quota/provider I/O and the response's request-scoped dependency
    # lifetime. Durable execution receipts keep archive blocked while work runs.
    await session.commit()
    llm_request = {
        "model": request.model,
        "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        # Always stream upstream regardless of request.stream — this
        # endpoint always returns a StreamingResponse (below), so honoring
        # stream=False would need an entirely different, buffered code path
        # that doesn't exist. llm_client.stream_chat only parses SSE
        # ("data: " lines); requesting a non-streamed completion from the
        # gateway silently yields zero matching lines — an empty response
        # with no error. The only real caller
        # (conversation-runner) already always sends stream=True.
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    org_id = _quota_uuid(user.org_id, "organization id")
    user_id = _quota_uuid(user.user_id, "user id")
    raw_conversation_id = user.raw_claims.get("conversation_id")
    conversation_id = (
        _quota_uuid(str(raw_conversation_id), "conversation id")
        if raw_conversation_id is not None
        else None
    )
    quota_request_id = uuid.uuid4()
    try:
        await quota.reserve(
            org_id,
            user_id,
            conversation_id,
            request.model,
            quota_request_id,
            workflow_run_id=request.workflow_run_id,
            workflow_phase=request.workflow_phase,
            workflow_node_key=request.workflow_node_key,
        )
    except QuotaExceededError as exc:
        raise _quota_platform_exception(exc) from exc

    langfuse = get_langfuse()
    generation = None
    if langfuse:
        generation = langfuse.generation(
            trace_id=user.raw_claims.get("conversation_id"),
            name="chat_completions",
            model=request.model,
            input={
                "messages": sanitize_messages(
                    [{"role": m.role, "content": m.content} for m in request.messages]
                )
            },
            model_parameters={
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
            },
            metadata={
                "workflow_run_id": str(request.workflow_run_id),
                "workflow_phase": request.workflow_phase,
                "workflow_node_key": request.workflow_node_key,
            }
            if request.workflow_run_id is not None
            else None,
        )

    full_output = ""

    async def event_generator() -> AsyncGenerator[str, None]:
        nonlocal full_output
        provider_accepted = False
        last_usage: tuple[int, int, int] | None = None

        async def on_accepted() -> None:
            nonlocal provider_accepted
            provider_accepted = True
            await quota.accept(org_id, quota_request_id)

        try:
            try:
                async for chunk_json in llm_client.stream_chat(
                    llm_request, on_accepted=on_accepted
                ):
                    if chunk_json:
                        yield f"data: {chunk_json}\n\n"
                        usage = _parse_provider_usage(
                            chunk_json,
                            request_id=quota_request_id,
                            org_id=org_id,
                            model=request.model,
                        )
                        if usage is not None:
                            last_usage = usage
                        try:
                            data = json.loads(chunk_json)
                            delta = data["choices"][0].get("delta", {}).get("content", "")
                            full_output += delta
                        except Exception:
                            pass
            except httpx.HTTPStatusError as exc:
                # By the time the gateway's status is known, StreamingResponse
                # has already sent 200 to the caller (conversation-runner) — the
                # real HTTP status can't change now. Without this, the
                # connection just dropped mid-stream with nothing in the body
                # and nothing in our own logs: the runner's
                # llm_call sees a truncated response and treats it as an empty
                # completion instead of a real failure. Logging the gateway's
                # actual error body here is the only place that's ever visible.
                # `.text` is guarded, not just null-checked: on a streamed
                # response httpx leaves the body unread and raises
                # ResponseNotRead. That secondary exception used to kill this
                # handler before either the log line or the error chunk below —
                # so the stream ended silently and the runner recorded an empty
                # assistant message (llm_client now pre-reads the body, this is
                # the second line of defence so a diagnostic can never again
                # throw over the failure it is describing).
                try:
                    body = exc.response.text if exc.response is not None else ""
                except Exception:
                    body = "<unread>"
                logger.error(
                    "LLM gateway error mid-stream (model=%s): %s — %s",
                    request.model,
                    exc,
                    body[:2000],
                )
                # The gateway's own body goes to the runner too, not just to our
                # logs: "Client error '404 Not Found'" alone never explains that
                # the selected model can't do chat completions.
                message = f"{exc} — {body[:2000]}" if body else str(exc)
                yield f"data: {json.dumps({'error': {'message': message}})}\n\n"
            else:
                if last_usage is not None:
                    # Guarded exactly like the cancel() below: by this point
                    # every content chunk has already been delivered, so an
                    # accounting failure (a QuotaInvariantError on a
                    # conflicting repeat completion, a database still down
                    # after _retry_transient) must not be allowed to escape.
                    # Unguarded it skipped generation.end() — leaking the
                    # Langfuse span — and the `[DONE]` line below, leaving the
                    # runner with a truncated transport instead of a clean end
                    # to a request that actually succeeded.
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(
                                quota.complete(
                                    org_id,
                                    quota_request_id,
                                    last_usage[0],
                                    last_usage[1],
                                    last_usage[2],
                                )
                            ),
                            timeout=3.0,
                        )
                    except Exception:
                        logger.error(
                            "quota completion failed, tokens unaccounted "
                            "(request_id=%s org_id=%s model=%s)",
                            quota_request_id,
                            org_id,
                            request.model,
                            exc_info=True,
                        )
                elif provider_accepted:
                    # The stream ended cleanly but the provider never sent a
                    # usage block (a gateway that drops stream_options, a model
                    # that reports nothing). The request itself stays counted —
                    # it was accepted — but its tokens never will be, and that
                    # has to be visible somewhere. Gated on provider_accepted
                    # so the DEBUG mock stream, which by design never reaches a
                    # gateway and has its reservation cancelled below, stays
                    # silent instead of warning once per local request.
                    logger.warning(
                        "LLM stream ended without usage, tokens unaccounted "
                        "(request_id=%s org_id=%s model=%s)",
                        quota_request_id,
                        org_id,
                        request.model,
                    )
            if generation:
                generation.end(output={"completion": sanitize_content(full_output)})
            yield "data: [DONE]\n\n"
        finally:
            if not provider_accepted:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(quota.cancel(org_id, quota_request_id)),
                        timeout=3.0,
                    )
                except Exception:
                    logger.warning(
                        "quota reservation cleanup failed (request_id=%s org_id=%s model=%s)",
                        quota_request_id,
                        org_id,
                        request.model,
                        exc_info=True,
                    )

    return create_streaming_response(event_generator())


@router.get("/conversations")
async def list_conversations(
    limit: int = 20,
    cursor: Optional[str] = None,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[list[ConversationResponse]]:
    conversations, has_more = await conversation_repository.list_by_user(
        session, user.user_id, user.org_id or "", limit, cursor
    )
    next_cursor = str(conversations[-1].id) if conversations and has_more else None
    org_default_temperature = await organization_model_settings_service.default_temperature(
        session, uuid.UUID(user.org_id or "")
    )
    return ApiResponse(
        data=[_conv_response(c, org_default_temperature) for c in conversations],
        meta=_meta(),
        pagination=Pagination(cursor=next_cursor, hasMore=has_more),
    )


@router.post("/conversations")
async def create_conversation(
    data: ConversationCreate,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[ConversationResponse]:
    conv = await chat_service.create_conversation(session, user, data)
    org_default_temperature = await organization_model_settings_service.default_temperature(
        session, uuid.UUID(user.org_id or "")
    )
    return ApiResponse(data=_conv_response(conv, org_default_temperature), meta=_meta())


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: uuid.UUID | None = None,
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> ApiResponse[dict[str, Any]]:
    conv = await _get_owned_conversation(session, conversation_id, user, write=False)
    try:
        page = await message_repository.list_by_conversation(
            session, conversation_id, limit=limit + 1,
            cursor=str(cursor) if cursor is not None else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid message history cursor") from exc
    has_more = len(page) > limit
    messages = page[:limit]
    org_default_temperature = await organization_model_settings_service.default_temperature(
        session, uuid.UUID(user.org_id or "")
    )
    return ApiResponse(
        data={
            **_conv_response(conv, org_default_temperature).model_dump(),
            "messages": {
                "data": [_msg_response(m) for m in messages],
                "pagination": {
                    "hasMore": has_more,
                    "cursor": str(messages[-1].id) if has_more else None,
                },
            },
        },
        meta=_meta(),
    )


@router.patch("/conversations/{conversation_id}")
async def update_conversation(
    conversation_id: str,
    data: ConversationUpdate,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[ConversationResponse]:
    conv = await _get_owned_conversation(session, conversation_id, user, write=True)
    update_data = data.model_dump(exclude_unset=True)
    # ConversationUpdate.metadata has no matching Conversation column —
    # repository.update()'s setattr(obj, "metadata", ...) silently created
    # a plain (non-mapped) instance attribute that was never flushed to the
    # DB, so a client setting metadata got a 200 back and the value just
    # vanished. The column is named `extra`.
    if "metadata" in update_data:
        update_data["extra"] = update_data.pop("metadata")
    conv = await conversation_repository.update(session, conv, **update_data)
    org_default_temperature = await organization_model_settings_service.default_temperature(
        session, uuid.UUID(user.org_id or "")
    )
    return ApiResponse(data=_conv_response(conv, org_default_temperature), meta=_meta())


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: str,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> None:
    # allow_deleted=True — DELETE stays idempotent: calling it again on an
    # already-deleted conversation (e.g. a UI retry) just succeeds again
    # instead of surfacing the 410 meant for the other, user-facing routes.
    conv = await _get_owned_conversation(session, conversation_id, user, allow_deleted=True, write=True)
    cancellation = await interaction_service.cancel_active_turn(
        session,
        authorization_subject(user),
        conv.id,
    )
    conv.status = "deleted"
    await session.commit()
    if cancellation is not None:
        turn_id, targets = cancellation
        await interaction_service.signal_turn_cancel(targets, turn_id, conv.id)


@router.post("/conversations/{conversation_id}/attachments", status_code=202)
async def attach_file_to_conversation(
    conversation_id: str,
    file: UploadFile = File(...),
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[DocumentResponse]:
    """Attach a file directly to this conversation — lazily provisions a
    hidden, conversation-only knowledge base (and its execute_hybrid_search
    skill) on the first attachment, and shares both across every subsequent
    one. Browser-only: conversation-runner never calls this."""
    conv = await _get_owned_conversation(session, conversation_id, user, write=True)
    doc = await attach_file(session, conv, file)
    return ApiResponse(data=DocumentResponse.model_validate(doc), meta=_meta())


class LifecycleExecutionRequest(BaseModel):
    workflow_id: str
    run_id: uuid.UUID


@router.post("/conversations/{conversation_id}/executions")
async def register_conversation_execution(
    conversation_id: uuid.UUID,
    data: LifecycleExecutionRequest,
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> dict[str, Any]:
    from src.services.execution_closure import ClosureBlocked, register_execution
    if user.raw_claims.get("type") != "session" or user.raw_claims.get("conversation_id") != str(conversation_id):
        raise HTTPException(403, "Session is not bound to this conversation")
    # History RPCs must not retain admission locks while sibling activities or
    # cancellation need the same owner. register_execution reacquires the exact
    # original generation only when persisting the verified identities.
    await session.commit()
    try:
        async with asyncio.timeout(20):
            row = await register_execution(session, get_temporal_client(), org_id=uuid.UUID(user.org_id or ""), conversation_id=conversation_id, generation=int(user.raw_claims.get("generation", 1)), workflow_id=data.workflow_id, run_id=data.run_id)
    except TimeoutError as exc:
        raise HTTPException(503, "execution_observation_unavailable") from exc
    except ClosureBlocked as exc:
        raise HTTPException(503 if exc.retryable_registration else 409, str(exc)) from exc
    await session.commit()
    return {"data": {"id":str(row.id)}}
