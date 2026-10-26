"""Hermes Agent session records (docs/HERMES_AGENT.md).

A runner opens a session for its conversation and gets a token, binds it to the
active turn and the execution acting in it (with the turn's workflow
correlation, when the turn runs a workflow), unbinds it when the turn ends and closes it when the sandbox goes
away. Opening again rotates the token version; closing bumps it as well, so
every earlier token of the session stops working at once.
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import AuthorizationSubject
from src.core.config import settings
from src.models.hermes_session import HermesSession
from src.models.interaction import InteractionExecution, InteractionTurn
from src.models.workflow_run import WorkflowRun, WorkflowRunStatus
from src.services.hermes_access import HermesAccessDenied, active_hermes_turn
from src.services.hermes_session_token import (
    IssuedHermesSessionToken,
    hermes_session_token_service,
)

_TERMINAL_RUN_STATES = {
    WorkflowRunStatus.COMPLETED.value,
    WorkflowRunStatus.FAILED.value,
    WorkflowRunStatus.CANCELLED.value,
}
_SUPERVISOR_PHASES = {"supervisor_prepare", "supervisor_synthesize"}


class HermesSessionError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


async def _owned(
    session: AsyncSession,
    subject: AuthorizationSubject,
    conversation_id: UUID,
    session_id: UUID,
    *,
    lock: bool = True,
) -> HermesSession:
    query = select(HermesSession).where(
        HermesSession.org_id == subject.org_id,
        HermesSession.id == session_id,
        HermesSession.conversation_id == conversation_id,
        HermesSession.user_id == subject.user_id,
    )
    row = await session.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise HermesSessionError("hermes_session_not_found")
    return row


def _issue(row: HermesSession, generation: int) -> IssuedHermesSessionToken:
    return hermes_session_token_service.issue(
        org_id=row.org_id,
        user_id=row.user_id,
        conversation_id=row.conversation_id,
        generation=generation,
        session_id=row.id,
        token_version=row.token_version,
        expires_at=row.expires_at,
    )


def _clear_binding(row: HermesSession) -> None:
    row.active_turn_id = None
    row.active_execution_id = None
    row.workflow_run_id = None
    row.workflow_phase = None
    row.workflow_node_key = None


def _close(row: HermesSession, now: datetime) -> None:
    row.state = "closed"
    row.closed_at = now
    _clear_binding(row)
    row.token_version += 1


async def open_session(
    session: AsyncSession,
    subject: AuthorizationSubject,
    conversation_id: UUID,
    *,
    generation: int,
    now: datetime | None = None,
) -> tuple[HermesSession, IssuedHermesSessionToken]:
    """The conversation's open session with a fresh token; earlier tokens stop working."""
    current = _now(now)
    row = await session.scalar(
        select(HermesSession)
        .where(
            HermesSession.org_id == subject.org_id,
            HermesSession.conversation_id == conversation_id,
            HermesSession.state == "open",
        )
        .with_for_update()
    )
    if row is not None and row.user_id != subject.user_id:
        raise HermesSessionError("hermes_session_not_found")
    if row is not None and row.expires_at <= current:
        _close(row, current)
        await session.flush()
        row = None
    if row is None:
        row = HermesSession(
            id=uuid4(),
            org_id=subject.org_id,
            user_id=subject.user_id,
            conversation_id=conversation_id,
            expires_at=current + timedelta(seconds=settings.HERMES_SESSION_MAX_LIFETIME_SECONDS),
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError:
            # Another runner request opened it first; let the caller retry.
            raise HermesSessionError("hermes_session_conflict") from None
    else:
        row.token_version += 1
        await session.flush()
    return row, _issue(row, generation)


async def bind_turn(
    session: AsyncSession,
    subject: AuthorizationSubject,
    conversation_id: UUID,
    session_id: UUID,
    *,
    turn_id: UUID,
    execution_id: UUID,
    workflow_run_id: UUID | None = None,
    workflow_phase: str | None = None,
    workflow_node_key: str | None = None,
    now: datetime | None = None,
) -> HermesSession:
    row = await _owned(session, subject, conversation_id, session_id)
    if row.state != "open" or row.expires_at <= _now(now):
        raise HermesSessionError("hermes_session_closed")
    turn = await active_hermes_turn(session, subject, conversation_id)
    if turn.id != turn_id:
        raise HermesAccessDenied("hermes_turn_inactive")
    if workflow_run_id is None:
        if workflow_phase is not None or workflow_node_key is not None:
            raise HermesSessionError("hermes_correlation_invalid")
    else:
        shape_ok = (workflow_phase == "node" and workflow_node_key is not None) or (
            workflow_phase in _SUPERVISOR_PHASES and workflow_node_key is None
        )
        run_status = await session.scalar(
            select(WorkflowRun.status).where(
                WorkflowRun.org_id == subject.org_id,
                WorkflowRun.id == workflow_run_id,
                WorkflowRun.conversation_id == conversation_id,
            )
        )
        if (
            not shape_ok
            or turn.workflow_run_id != workflow_run_id
            or run_status is None
            or str(run_status) in _TERMINAL_RUN_STATES
        ):
            raise HermesSessionError("hermes_correlation_invalid")
    execution = await session.scalar(
        select(InteractionExecution).where(
            InteractionExecution.org_id == subject.org_id,
            InteractionExecution.turn_id == turn.id,
            InteractionExecution.id == execution_id,
        )
    )
    # The execution acts in the bound step: an ordinary turn's, or the
    # workflow phase and node the binding is billed to.
    if execution is None or (execution.phase, execution.node_key) != (
        workflow_phase or "ordinary",
        workflow_node_key,
    ):
        raise HermesSessionError("hermes_correlation_invalid")
    row.active_turn_id = turn.id
    row.active_execution_id = execution.id
    row.workflow_run_id = workflow_run_id
    row.workflow_phase = workflow_phase
    row.workflow_node_key = workflow_node_key
    await session.flush()
    return row


async def unbind_turn(
    session: AsyncSession,
    subject: AuthorizationSubject,
    conversation_id: UUID,
    session_id: UUID,
) -> None:
    row = await _owned(session, subject, conversation_id, session_id)
    _clear_binding(row)
    await session.flush()


async def close_session(
    session: AsyncSession,
    subject: AuthorizationSubject,
    conversation_id: UUID,
    session_id: UUID,
    *,
    now: datetime | None = None,
) -> None:
    """Idempotent: a closed session stays closed."""
    row = await _owned(session, subject, conversation_id, session_id)
    if row.state == "open":
        _close(row, _now(now))
        await session.flush()


async def authorize_token(
    session: AsyncSession,
    subject: AuthorizationSubject,
    claims: dict[str, Any],
    *,
    now: datetime | None = None,
    require_turn: bool = True,
) -> HermesSession:
    """The session a token may act for right now, bound to a still-active turn.

    Without `require_turn`, an open session on the token's version is enough:
    for requests that read nothing and act on nothing, such as the MCP
    handshake Hermes makes when its sandbox starts.
    """
    try:
        row = await _owned(
            session,
            subject,
            UUID(str(claims["conversation_id"])),
            UUID(str(claims["sid"])),
            lock=False,
        )
    except HermesSessionError:
        raise HermesAccessDenied("hermes_session_revoked") from None
    if (
        row.state != "open"
        or row.expires_at <= _now(now)
        or row.token_version != claims.get("tv")
    ):
        raise HermesAccessDenied("hermes_session_revoked")
    if not require_turn:
        return row
    if row.active_turn_id is None:
        raise HermesAccessDenied("hermes_turn_inactive")
    status = await session.scalar(
        select(InteractionTurn.status).where(
            InteractionTurn.org_id == subject.org_id,
            InteractionTurn.id == row.active_turn_id,
        )
    )
    if status != "active":
        raise HermesAccessDenied("hermes_turn_inactive")
    return row
