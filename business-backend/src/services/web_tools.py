"""Authorization, idempotency and provenance for model web-tool invocations."""

from collections.abc import Awaitable, Callable
import asyncio
import hashlib
import json
import time
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from prometheus_client import Counter, Histogram

from src.core.authorization_types import AuthorizationSubject
from src.models.conversation import Conversation
from src.models.interaction import InteractionExecution, InteractionTurn
from src.repositories.interaction_repository import InteractionError, InteractionRepository
from src.repositories.web_source_repository import (
    MAX_PLATFORM_ATTEMPTS,
    WebSourceRepository,
    WebSourceRepositoryError,
)
from src.schemas.web_tools import WebErrorCode, WebOperationRequest, WebSourceResponse
from src.services.interaction import (
    InteractionAccessDenied,
    InteractionConflict,
    InteractionNotFound,
    interaction_service,
)
from src.services.web_tools_client import WebToolsClientError, configured_web_tools_client


Authorize = Callable[
    [AsyncSession, AuthorizationSubject, UUID, UUID, UUID],
    Awaitable[None],
]

_WEB_ERROR_CODES = {
    "web_disabled",
    "web_access_denied",
    "web_limit",
    "web_timeout",
    "web_unavailable",
    "web_url_blocked",
    "web_too_large",
    "unsupported_document",
    "web_cancelled",
}
_RETRYABLE_ERROR_CODES = {"web_timeout", "web_unavailable"}
# cancel_turn stores the shared interaction code on the durable operation; a
# retried activity reads it back here and must not see a generic outage.
_DURABLE_ERROR_CODES = {"user_input_cancelled": "web_cancelled"}


def _conflict_code(exc: Exception, fallback: str) -> str:
    """A cancelled turn is not an outage and not a permission failure."""
    if isinstance(exc, InteractionConflict) and exc.code == "user_input_cancelled":
        return "web_cancelled"
    return fallback
_OPERATION_POLL_SECONDS = 0.1

WEB_TOOL_ATTEMPTS_TOTAL = Counter(
    "web_tool_network_attempts_total",
    "Backend-to-web-tools attempts by bounded tool and safe outcome.",
    ("tool", "status"),
)
WEB_TOOL_ATTEMPT_DURATION_SECONDS = Histogram(
    "web_tool_network_attempt_duration_seconds",
    "Backend-to-web-tools attempt duration by bounded tool and safe outcome.",
    ("tool", "status"),
)
WEB_TOOL_RESPONSE_BYTES_TOTAL = Counter(
    "web_tool_network_response_bytes_total",
    "Validated web-tools response bytes by bounded tool and safe outcome.",
    ("tool", "status"),
)


class WebClient(Protocol):
    async def execute(self, tool: str, args: dict[str, Any]) -> dict[str, Any]: ...


class WebToolExecutionError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def citation_is_owned(
    org_id: str,
    turn_id: str,
    source_org_id: str,
    source_turn_id: str,
) -> bool:
    return org_id == source_org_id and turn_id == source_turn_id


async def _default_authorize(
    session: AsyncSession,
    subject: AuthorizationSubject,
    conversation_id: UUID,
    turn_id: UUID,
    execution_id: UUID,
) -> None:
    await interaction_service.authorize_web_operation(
        session, subject, conversation_id, turn_id, execution_id
    )


class WebToolsService:
    def __init__(
        self,
        *,
        client: WebClient | None = None,
        authorize: Authorize = _default_authorize,
    ) -> None:
        self._client = client
        self._authorize = authorize

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
            request = WebOperationRequest.model_validate({"tool": tool, "args": args})
        except ValueError as exc:
            raise WebToolExecutionError("web_unavailable") from exc
        normalized_args = request.args.model_dump(mode="json")
        context = await self._context(session, subject.org_id, execution_id)
        try:
            await self._authorize(
                session,
                subject,
                context.conversation_id,
                context.id,
                execution_id,
            )
        except InteractionAccessDenied as exc:
            await session.rollback()
            raise WebToolExecutionError("web_access_denied") from exc
        except InteractionNotFound as exc:
            await session.rollback()
            raise WebToolExecutionError("interaction_not_found") from exc
        except InteractionConflict as exc:
            await session.rollback()
            raise WebToolExecutionError(_conflict_code(exc, "interaction_conflict")) from exc
        interaction = InteractionRepository(subject.org_id)
        repository = WebSourceRepository(subject.org_id)
        try:
            operation = await interaction.reserve_operation(
                session,
                execution_id,
                invocation_id,
                request.tool,
                self._arguments_hash(normalized_args),
            )
            state, result, error_code = await repository.operation_result(session, operation.id)
            if state == "completed":
                await session.rollback()
                if result is not None:
                    return result
                raise WebToolExecutionError("web_unavailable")
            if state in {"failed", "cancelled"}:
                await session.rollback()
                translated = _DURABLE_ERROR_CODES.get(error_code or "", error_code)
                code = translated if translated in _WEB_ERROR_CODES else "web_unavailable"
                raise WebToolExecutionError(code)
        except InteractionError as exc:
            await session.rollback()
            code = {
                "tool_limit": "web_limit",
                "interaction_not_found": "interaction_not_found",
                "interaction_conflict": "interaction_conflict",
            }.get(exc.code, "web_unavailable")
            raise WebToolExecutionError(code) from exc
        operation_id = operation.id
        client = self._client or configured_web_tools_client()
        while True:
            claim_started = time.monotonic()
            try:
                claim = await repository.claim_operation(session, operation_id)
                await session.commit()
            except WebSourceRepositoryError as exc:
                if exc.code == "operation_in_progress":
                    await session.rollback()
                    # A duplicate activity may arrive after the first HTTP
                    # response was lost while its durable operation is still
                    # running. Poll the same invocation until it completes or
                    # its bounded lease can be taken over; never launch an
                    # immediate second public request merely because the
                    # runner retried its activity.
                    await asyncio.sleep(_OPERATION_POLL_SECONDS)
                    continue
                return await self._raise_claim_error(session, repository, operation_id, exc)

            attempt_deadline = claim_started + claim.remaining_seconds
            try:
                await self._authorize(
                    session,
                    subject,
                    context.conversation_id,
                    context.id,
                    execution_id,
                )
                # Authorization locks the turn. Release that lock before I/O
                # so cancellation and other nodes can proceed. Authorization
                # and the active turn are checked again before persistence.
                await session.commit()
            except (InteractionAccessDenied, InteractionConflict, InteractionNotFound) as exc:
                code = cast(WebErrorCode, _conflict_code(exc, "web_access_denied"))
                await self._fail_claim(
                    session, repository, operation_id, claim.lease_id, code
                )
                raise WebToolExecutionError(code) from exc

            remaining_seconds = attempt_deadline - time.monotonic()
            if remaining_seconds <= 0:
                await self._fail_claim(
                    session,
                    repository,
                    operation_id,
                    claim.lease_id,
                    "web_timeout",
                )
                raise WebToolExecutionError("web_timeout")
            started = time.perf_counter()
            try:
                async with asyncio.timeout(remaining_seconds):
                    raw_result = await client.execute(request.tool, normalized_args)
            except TimeoutError as exc:
                self._record_attempt(request.tool, "web_timeout", started)
                await self._fail_claim(
                    session,
                    repository,
                    operation_id,
                    claim.lease_id,
                    "web_timeout",
                )
                raise WebToolExecutionError("web_timeout") from exc
            except WebToolsClientError as exc:
                self._record_attempt(request.tool, exc.code, started)
                if (
                    exc.retryable
                    and exc.code in _RETRYABLE_ERROR_CODES
                    and claim.attempt < MAX_PLATFORM_ATTEMPTS
                ):
                    try:
                        await repository.release_operation_for_retry(
                            session, operation_id, claim.lease_id
                        )
                        await session.commit()
                    except WebSourceRepositoryError as retry_exc:
                        await session.rollback()
                        await self._fail_claim(
                            session,
                            repository,
                            operation_id,
                            claim.lease_id,
                            "web_unavailable",
                        )
                        raise WebToolExecutionError("web_unavailable") from retry_exc
                    continue
                await self._fail_claim(
                    session, repository, operation_id, claim.lease_id, exc.code
                )
                raise WebToolExecutionError(exc.code) from exc
            except Exception as exc:
                self._record_attempt(request.tool, "web_unavailable", started)
                await self._fail_claim(
                    session,
                    repository,
                    operation_id,
                    claim.lease_id,
                    "web_unavailable",
                )
                raise WebToolExecutionError("web_unavailable") from exc
            self._record_attempt(request.tool, "success", started, raw_result)

            try:
                await self._authorize(
                    session,
                    subject,
                    context.conversation_id,
                    context.id,
                    execution_id,
                )
            except (InteractionAccessDenied, InteractionConflict, InteractionNotFound) as exc:
                code = cast(WebErrorCode, _conflict_code(exc, "web_access_denied"))
                await self._fail_claim(
                    session, repository, operation_id, claim.lease_id, code
                )
                raise WebToolExecutionError(code) from exc

            try:
                result = await repository.complete_operation(
                    session, operation_id, claim.lease_id, raw_result
                )
                await session.commit()
                return result
            except WebSourceRepositoryError as exc:
                if exc.code == "web_timeout":
                    await session.commit()
                    raise WebToolExecutionError("web_timeout") from exc
                await session.rollback()
                await self._fail_claim(
                    session, repository, operation_id, claim.lease_id, "web_unavailable"
                )
                raise WebToolExecutionError("web_unavailable") from exc

    async def list_sources(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        conversation_id: UUID,
        turn_id: UUID,
    ) -> list[WebSourceResponse]:
        conversation = await session.scalar(
            select(Conversation).where(
                Conversation.org_id == subject.org_id,
                Conversation.id == conversation_id,
                Conversation.user_id == subject.user_id,
            )
        )
        if conversation is None:
            raise WebToolExecutionError("interaction_not_found")
        turn = await session.scalar(
            select(InteractionTurn).where(
                InteractionTurn.org_id == subject.org_id,
                InteractionTurn.id == turn_id,
                InteractionTurn.conversation_id == conversation_id,
                InteractionTurn.owner_id == subject.user_id,
            )
        )
        if turn is None:
            raise WebToolExecutionError("interaction_not_found")
        rows = await WebSourceRepository(subject.org_id).list_sources(session, turn_id)
        return [
            WebSourceResponse(
                source_id=row.source.id,
                kind=cast(Literal["search", "read"], row.source.kind),
                requested_url=row.source.requested_url,
                final_url=row.source.final_url,
                title=row.source.title,
                content_type=row.source.content_type,
                snippet=row.content[:4000] if row.content is not None else None,
                published_at=row.source.published_at,
                retrieved_at=row.source.retrieved_at,
                truncated=row.source.truncated,
                page_count=row.source.page_count,
            )
            for row in rows
        ]

    @staticmethod
    async def _raise_claim_error(
        session: AsyncSession,
        repository: WebSourceRepository,
        operation_id: UUID,
        exc: WebSourceRepositoryError,
    ) -> dict[str, Any]:
        if exc.code == "operation_completed":
            await session.rollback()
            state, result, _ = await repository.operation_result(session, operation_id)
            await session.rollback()
            if state == "completed" and result is not None:
                return result
            raise WebToolExecutionError("web_unavailable") from exc
        if exc.code in {"web_timeout", "web_unavailable"}:
            await session.commit()
        else:
            await session.rollback()
        code = (
            exc.code
            if exc.code in {"interaction_not_found", "interaction_conflict", "web_timeout"}
            else "web_unavailable"
        )
        raise WebToolExecutionError(code) from exc

    @staticmethod
    def _record_attempt(
        tool: str,
        status: str,
        started: float,
        result: dict[str, Any] | None = None,
    ) -> None:
        WEB_TOOL_ATTEMPTS_TOTAL.labels(tool=tool, status=status).inc()
        WEB_TOOL_ATTEMPT_DURATION_SECONDS.labels(tool=tool, status=status).observe(
            time.perf_counter() - started
        )
        if result is not None:
            response_bytes = len(
                json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode()
            )
            WEB_TOOL_RESPONSE_BYTES_TOTAL.labels(tool=tool, status=status).inc(
                response_bytes
            )

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
            raise WebToolExecutionError("interaction_not_found")
        return turn

    @staticmethod
    def _arguments_hash(args: dict[str, Any]) -> str:
        payload = json.dumps(
            args,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    async def _fail_claim(
        session: AsyncSession,
        repository: WebSourceRepository,
        operation_id: UUID,
        lease_id: UUID,
        code: WebErrorCode,
    ) -> None:
        try:
            await repository.fail_operation(session, operation_id, lease_id, code)
            await session.commit()
        except WebSourceRepositoryError:
            await session.rollback()


web_tools_service = WebToolsService()
