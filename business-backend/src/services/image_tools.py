"""Authorization, idempotency and storage for model image-tool invocations.

Mirrors the web tools: a runner invocation reserves one durable
``tool_operations`` row, claims it under a lease, calls the Images API once
(a bounded retry only for transient outages), stores the validated bytes in
object storage and records a ``conversation_images`` row. A retried activity
with the same invocation id returns the stored result instead of paying for
a second image.
"""

from collections.abc import Awaitable, Callable
import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Protocol
from uuid import UUID, uuid4

from prometheus_client import Counter, Histogram
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import AuthorizationSubject
from src.core.config import settings
from src.models.conversation_image import ConversationImage
from src.models.interaction import InteractionExecution, InteractionTurn
from src.models.llm_usage import UsageEventKind
from src.repositories.image_repository import ImageRepository, ImageRepositoryError, StoredImage
from src.repositories.interaction_repository import InteractionError, InteractionRepository
from src.repositories.tool_operation_leases import MAX_PLATFORM_ATTEMPTS
from src.schemas.image_tools import (
    IMAGE_TOOL_KINDS,
    EditImageRequest,
    ImageOperationRequest,
)
from src.services.image_api_client import (
    ImageApiError,
    ImageResult,
    ImageUsage,
    configured_image_api_client,
)
from src.services.image_content import ImageContentError, inspect_image
from src.services.interaction import (
    InteractionAccessDenied,
    InteractionConflict,
    InteractionNotFound,
    interaction_service,
)
from src.services.object_storage import object_storage
from src.services.organization_model_settings import organization_model_settings_service
from src.services.quota import QuotaExceededError, QuotaService, get_quota_service

logger = logging.getLogger(__name__)


Authorize = Callable[
    [AsyncSession, AuthorizationSubject, UUID, UUID, UUID],
    Awaitable[None],
]

IMAGE_ERROR_CODES = {
    "image_disabled",
    "image_access_denied",
    "image_limit",
    "image_timeout",
    "image_unavailable",
    "image_rejected",
    "image_not_found",
    "image_cancelled",
    "image_quota_exceeded",
}
_RETRYABLE_ERROR_CODES = {"image_unavailable"}
_DURABLE_ERROR_CODES = {"user_input_cancelled": "image_cancelled"}
_OPERATION_POLL_SECONDS = 0.2

IMAGE_TOOL_ATTEMPTS_TOTAL = Counter(
    "image_tool_attempts_total",
    "Backend-to-Images-API attempts by bounded tool and safe outcome.",
    ("tool", "status"),
)
IMAGE_TOOL_ATTEMPT_DURATION_SECONDS = Histogram(
    "image_tool_attempt_duration_seconds",
    "Backend-to-Images-API attempt duration by bounded tool and safe outcome.",
    ("tool", "status"),
    buckets=(1, 2.5, 5, 10, 20, 40, 60, 90, 120, 180, 300),
)


class ImageClient(Protocol):
    async def generate(self, *, model: str, prompt: str, size: str) -> ImageResult: ...

    async def edit(
        self, *, model: str, prompt: str, image: bytes, content_type: str, size: str
    ) -> ImageResult: ...


class ImageStorage(Protocol):
    async def put_bytes(self, key: str, data: bytes) -> None: ...

    async def get_bytes(self, key: str) -> bytes: ...


class ImageToolExecutionError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _conflict_code(exc: Exception, fallback: str) -> str:
    """A cancelled turn is not an outage and not a permission failure."""
    if isinstance(exc, InteractionConflict) and exc.code == "user_input_cancelled":
        return "image_cancelled"
    return fallback


async def _default_authorize(
    session: AsyncSession,
    subject: AuthorizationSubject,
    conversation_id: UUID,
    turn_id: UUID,
    execution_id: UUID,
) -> None:
    await interaction_service.authorize_image_operation(
        session, subject, conversation_id, turn_id, execution_id
    )


def image_storage_key(org_id: UUID, conversation_id: UUID, operation_id: UUID, extension: str) -> str:
    # Keyed by the operation, so a retried attempt overwrites its own object
    # instead of leaving a second copy behind.
    return f"conversation-images/{org_id}/{conversation_id}/{operation_id}.{extension}"


async def organization_image_models(
    session: AsyncSession, org_id: UUID
) -> tuple[str | None, str | None]:
    return await organization_model_settings_service.image_models(session, org_id)


RECENT_IMAGE_LIMIT = 8
RECENT_IMAGE_PROMPT_CHARS = 200


async def recent_conversation_images(
    session: AsyncSession, org_id: UUID, conversation_id: UUID
) -> list[dict[str, str]]:
    """The newest images of a conversation, so a later turn can edit them.

    Ids alone are not secrets (the content route checks ownership); prompts are
    shortened because they are repeated into every image-enabled turn.
    """
    rows = await session.execute(
        select(ConversationImage.id, ConversationImage.prompt, ConversationImage.origin)
        .where(
            ConversationImage.org_id == org_id,
            ConversationImage.conversation_id == conversation_id,
            # An upload counts once a sent message carries it.
            ConversationImage.turn_id.is_not(None),
        )
        .order_by(ConversationImage.created_at.desc(), ConversationImage.id)
        .limit(RECENT_IMAGE_LIMIT)
    )
    return [
        {
            "id": str(image_id),
            "prompt": (prompt or "")[:RECENT_IMAGE_PROMPT_CHARS],
            "origin": origin,
        }
        for image_id, prompt, origin in rows.all()
    ]


class ImageToolsService:
    def __init__(
        self,
        *,
        client: ImageClient | None = None,
        storage: ImageStorage | None = None,
        authorize: Authorize = _default_authorize,
        quota: QuotaService | None = None,
    ) -> None:
        self._client = client
        self._storage = storage
        self._authorize = authorize
        self._quota = quota

    async def execute(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        execution_id: UUID,
        invocation_id: UUID,
        tool: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            request = ImageOperationRequest.model_validate({"tool": tool, "args": args})
        except ValueError as exc:
            raise ImageToolExecutionError("image_rejected") from exc
        normalized_args = request.args.model_dump(mode="json")
        kind = IMAGE_TOOL_KINDS[request.tool]
        turn = await self._context(session, subject.org_id, execution_id)
        await self._authorize_or_raise(session, subject, turn, execution_id)
        correlation = await self._workflow_correlation(session, subject.org_id, execution_id, turn)

        generation_model, edit_model = await organization_image_models(session, subject.org_id)
        model = generation_model if kind == "image_generate" else edit_model
        if not model:
            await session.rollback()
            raise ImageToolExecutionError("image_disabled")

        interaction = InteractionRepository(subject.org_id)
        repository = ImageRepository(subject.org_id)
        try:
            operation = await interaction.reserve_operation(
                session, execution_id, invocation_id, kind, self._arguments_hash(normalized_args)
            )
            state, result, error_code = await repository.operation_result(session, operation.id)
            if state == "completed":
                await session.rollback()
                if result is not None:
                    return result
                raise ImageToolExecutionError("image_unavailable")
            if state in {"failed", "cancelled"}:
                await session.rollback()
                translated = _DURABLE_ERROR_CODES.get(error_code or "", error_code)
                raise ImageToolExecutionError(
                    translated if translated in IMAGE_ERROR_CODES else "image_unavailable"
                )
        except InteractionError as exc:
            await session.rollback()
            code = {
                "tool_limit": "image_limit",
                "interaction_not_found": "interaction_not_found",
                "interaction_conflict": "interaction_conflict",
            }.get(exc.code, "image_unavailable")
            raise ImageToolExecutionError(code) from exc

        source = None
        if isinstance(request.args, EditImageRequest):
            try:
                source = await repository.conversation_image(
                    session, turn.conversation_id, request.args.image_id
                )
            except ImageRepositoryError as exc:
                await session.rollback()
                raise ImageToolExecutionError("image_not_found") from exc
        operation_id = operation.id
        client = self._client or configured_image_api_client()
        storage = self._storage or object_storage
        while True:
            claim_started = time.monotonic()
            try:
                claim = await repository.claim_operation(session, operation_id, lease_seconds=300)
                await session.commit()
            except ImageRepositoryError as exc:
                if exc.code == "operation_in_progress":
                    await session.rollback()
                    # A duplicate activity may arrive while the first call is
                    # still running; never pay for a second image because the
                    # runner retried. Wait for the result or the lease expiry.
                    await asyncio.sleep(_OPERATION_POLL_SECONDS)
                    continue
                return await self._raise_claim_error(session, repository, operation_id, exc)

            attempt_deadline = claim_started + claim.remaining_seconds
            try:
                await self._authorize(
                    session, subject, turn.conversation_id, turn.id, execution_id
                )
                # Release the turn lock before slow provider I/O.
                await session.commit()
            except (InteractionAccessDenied, InteractionConflict, InteractionNotFound) as exc:
                code = _conflict_code(exc, "image_access_denied")
                await self._fail_claim(session, repository, operation_id, claim.lease_id, code)
                raise ImageToolExecutionError(code) from exc

            remaining_seconds = min(
                attempt_deadline - time.monotonic(), settings.IMAGES_TIMEOUT_SECONDS
            )
            if remaining_seconds <= 0:
                await self._fail_claim(
                    session, repository, operation_id, claim.lease_id, "image_timeout"
                )
                raise ImageToolExecutionError("image_timeout")
            # Every provider attempt is one request against the organization's
            # quota, reserved before the call like a chat completion.
            quota = self._quota or get_quota_service()
            usage_request = uuid4()
            try:
                await quota.reserve(
                    subject.org_id,
                    subject.user_id,
                    turn.conversation_id,
                    model,
                    usage_request,
                    kind=UsageEventKind(kind),
                    **correlation,
                )
            except QuotaExceededError as exc:
                await self._fail_claim(
                    session, repository, operation_id, claim.lease_id, "image_quota_exceeded"
                )
                raise ImageToolExecutionError("image_quota_exceeded") from exc
            except Exception as exc:
                logger.warning("image quota reservation failed", exc_info=True)
                await self._fail_claim(
                    session, repository, operation_id, claim.lease_id, "image_unavailable"
                )
                raise ImageToolExecutionError("image_unavailable") from exc
            started = time.perf_counter()
            try:
                async with asyncio.timeout(remaining_seconds):
                    if source is None:
                        provider_result = await client.generate(
                            model=model, prompt=request.args.prompt, size=request.args.size
                        )
                    else:
                        source_bytes = await storage.get_bytes(source.storage_key)
                        provider_result = await client.edit(
                            model=model,
                            prompt=request.args.prompt,
                            image=source_bytes,
                            content_type=source.content_type,
                            size=request.args.size,
                        )
                # Outside the timeout: an answered, billed call must be counted
                # even when the deadline is about to expire.
                await self._settle_usage(
                    quota, subject.org_id, usage_request, accepted=True, usage=provider_result.usage
                )
                raw = provider_result.data
                info = inspect_image(raw, max_bytes=settings.IMAGES_MAX_BYTES)
                key = image_storage_key(
                    subject.org_id, turn.conversation_id, operation_id, info.extension
                )
                await storage.put_bytes(key, raw)
            except TimeoutError as exc:
                # Cancelling is a no-op once the provider's answer was counted.
                await self._settle_usage(quota, subject.org_id, usage_request, accepted=False)
                self._record_attempt(request.tool, "image_timeout", started)
                await self._fail_claim(
                    session, repository, operation_id, claim.lease_id, "image_timeout"
                )
                raise ImageToolExecutionError("image_timeout") from exc
            except ImageApiError as exc:
                await self._settle_usage(
                    quota, subject.org_id, usage_request, accepted=exc.accepted, usage=exc.usage
                )
                self._record_attempt(request.tool, exc.code, started)
                if exc.code in _RETRYABLE_ERROR_CODES and claim.attempt < MAX_PLATFORM_ATTEMPTS:
                    try:
                        await repository.release_operation_for_retry(
                            session, operation_id, claim.lease_id
                        )
                        await session.commit()
                    except ImageRepositoryError as retry_exc:
                        await session.rollback()
                        await self._fail_claim(
                            session, repository, operation_id, claim.lease_id, "image_unavailable"
                        )
                        raise ImageToolExecutionError("image_unavailable") from retry_exc
                    continue
                code = exc.code if exc.code in IMAGE_ERROR_CODES else "image_unavailable"
                await self._fail_claim(session, repository, operation_id, claim.lease_id, code)
                raise ImageToolExecutionError(code) from exc
            except ImageContentError as exc:
                await self._settle_usage(quota, subject.org_id, usage_request, accepted=False)
                self._record_attempt(request.tool, "image_unavailable", started)
                await self._fail_claim(
                    session, repository, operation_id, claim.lease_id, "image_unavailable"
                )
                raise ImageToolExecutionError("image_unavailable") from exc
            except Exception as exc:
                await self._settle_usage(quota, subject.org_id, usage_request, accepted=False)
                self._record_attempt(request.tool, "image_unavailable", started)
                await self._fail_claim(
                    session, repository, operation_id, claim.lease_id, "image_unavailable"
                )
                raise ImageToolExecutionError("image_unavailable") from exc
            self._record_attempt(request.tool, "success", started)

            try:
                await self._authorize(
                    session, subject, turn.conversation_id, turn.id, execution_id
                )
            except (InteractionAccessDenied, InteractionConflict, InteractionNotFound) as exc:
                code = _conflict_code(exc, "image_access_denied")
                await self._fail_claim(session, repository, operation_id, claim.lease_id, code)
                raise ImageToolExecutionError(code) from exc

            try:
                result = await repository.complete_operation(
                    session,
                    operation_id,
                    claim.lease_id,
                    turn.conversation_id,
                    StoredImage(
                        storage_key=key,
                        content_type=info.content_type,
                        size_bytes=info.size_bytes,
                        width=info.width,
                        height=info.height,
                        prompt=request.args.prompt,
                        model=model,
                        source_image_id=source.id if source is not None else None,
                    ),
                )
                await session.commit()
                return result
            except ImageRepositoryError as exc:
                if exc.code == "image_timeout":
                    await session.commit()
                    raise ImageToolExecutionError("image_timeout") from exc
                await session.rollback()
                await self._fail_claim(
                    session, repository, operation_id, claim.lease_id, "image_unavailable"
                )
                raise ImageToolExecutionError("image_unavailable") from exc

    async def _authorize_or_raise(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        turn: InteractionTurn,
        execution_id: UUID,
    ) -> None:
        try:
            await self._authorize(session, subject, turn.conversation_id, turn.id, execution_id)
        except InteractionAccessDenied as exc:
            await session.rollback()
            raise ImageToolExecutionError("image_access_denied") from exc
        except InteractionNotFound as exc:
            await session.rollback()
            raise ImageToolExecutionError("interaction_not_found") from exc
        except InteractionConflict as exc:
            await session.rollback()
            raise ImageToolExecutionError(_conflict_code(exc, "interaction_conflict")) from exc

    @staticmethod
    async def _raise_claim_error(
        session: AsyncSession,
        repository: ImageRepository,
        operation_id: UUID,
        exc: ImageRepositoryError,
    ) -> dict[str, Any]:
        if exc.code == "operation_completed":
            await session.rollback()
            state, result, _ = await repository.operation_result(session, operation_id)
            await session.rollback()
            if state == "completed" and result is not None:
                return result
            raise ImageToolExecutionError("image_unavailable") from exc
        if exc.code in {"image_timeout", "image_unavailable"}:
            await session.commit()
        else:
            await session.rollback()
        translated = _DURABLE_ERROR_CODES.get(exc.code, exc.code)
        code = (
            translated
            if translated in {"interaction_not_found", "interaction_conflict", *IMAGE_ERROR_CODES}
            else "image_unavailable"
        )
        raise ImageToolExecutionError(code) from exc

    @staticmethod
    async def _settle_usage(
        quota: QuotaService,
        org_id: UUID,
        request_id: UUID,
        *,
        accepted: bool,
        usage: ImageUsage | None = None,
    ) -> None:
        """Count an answered call (with its tokens when reported) or give the
        reservation back. Accounting never fails the image itself."""
        try:
            if not accepted:
                await quota.cancel(org_id, request_id)
                return
            await quota.accept(org_id, request_id)
            if usage is not None:
                await quota.complete(
                    org_id,
                    request_id,
                    usage.prompt_tokens,
                    usage.completion_tokens,
                    usage.total_tokens,
                )
        except Exception:
            logger.error(
                "image usage accounting failed (request_id=%s org_id=%s)",
                request_id,
                org_id,
                exc_info=True,
            )

    @staticmethod
    def _record_attempt(tool: str, status: str, started: float) -> None:
        IMAGE_TOOL_ATTEMPTS_TOTAL.labels(tool=tool, status=status).inc()
        IMAGE_TOOL_ATTEMPT_DURATION_SECONDS.labels(tool=tool, status=status).observe(
            time.perf_counter() - started
        )

    async def turn_for_execution(
        self, session: AsyncSession, org_id: UUID, execution_id: UUID
    ) -> InteractionTurn:
        return await self._context(session, org_id, execution_id)

    @staticmethod
    async def _context(
        session: AsyncSession, org_id: UUID, execution_id: UUID
    ) -> InteractionTurn:
        turn = await session.scalar(
            select(InteractionTurn)
            .join(
                InteractionExecution,
                (InteractionExecution.org_id == InteractionTurn.org_id)
                & (InteractionExecution.turn_id == InteractionTurn.id),
            )
            .where(
                InteractionTurn.org_id == org_id,
                InteractionExecution.id == execution_id,
            )
        )
        if turn is None:
            raise ImageToolExecutionError("interaction_not_found")
        return turn

    @staticmethod
    async def _workflow_correlation(
        session: AsyncSession, org_id: UUID, execution_id: UUID, turn: InteractionTurn
    ) -> dict[str, Any]:
        """Workflow images are billed to their run, phase and node, like the
        node's own model calls."""
        if turn.workflow_run_id is None:
            return {}
        phase, node_key = (
            await session.execute(
                select(InteractionExecution.phase, InteractionExecution.node_key).where(
                    InteractionExecution.org_id == org_id,
                    InteractionExecution.id == execution_id,
                )
            )
        ).one()
        if phase == "ordinary":
            return {}
        return {
            "workflow_run_id": turn.workflow_run_id,
            "workflow_phase": phase,
            "workflow_node_key": node_key if phase == "node" else None,
        }

    @staticmethod
    def _arguments_hash(args: dict[str, Any]) -> str:
        payload = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    async def _fail_claim(
        session: AsyncSession,
        repository: ImageRepository,
        operation_id: UUID,
        lease_id: UUID,
        code: str,
    ) -> None:
        try:
            await repository.fail_operation(session, operation_id, lease_id, code)
            await session.commit()
        except ImageRepositoryError:
            await session.rollback()


image_tools_service = ImageToolsService()
