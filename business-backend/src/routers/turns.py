"""Owner-only durable turn snapshots and metadata-only status streams."""

import logging
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user
from src.core.authorization_dependencies import authorization_subject
from src.core.config import settings
from src.core.dependencies import get_org_db_session
from src.core.events import event_bus
from src.schemas.common import ApiResponse, Meta
from src.schemas.interaction import TurnSnapshot
from src.services.interaction import InteractionNotFound, interaction_service
from src.services.streaming import (
    create_streaming_response,
    reconciliation_ticks,
    sse_event,
)


router = APIRouter()
logger = logging.getLogger(__name__)
_RECONCILE_INTERVAL_SECONDS = 5.0
_TERMINAL_TURN_STATUSES = {"completed", "failed", "cancelled"}


def _meta() -> Meta:
    return Meta(
        requestId=f"req_{uuid.uuid4().hex}",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def question_event(
    question_id: str, turn_id: str, status: str, version: int
) -> dict[str, str | int]:
    return {
        "questionId": question_id,
        "turnId": turn_id,
        "status": status,
        "version": version,
    }


def turn_event(turn_id: str, status: str, version: int) -> dict[str, str | int]:
    return {"turnId": turn_id, "status": status, "version": version}


def _snapshot_events(
    snapshot: TurnSnapshot,
    seen: dict[tuple[str, str], int],
) -> list[tuple[str, dict[str, str | int]]]:
    events: list[tuple[str, dict[str, str | int]]] = []
    turn_id = str(snapshot.turn_id)
    turn_key = ("turn", turn_id)
    if snapshot.version > seen.get(turn_key, 0):
        seen[turn_key] = snapshot.version
        events.append(("turn-status", turn_event(turn_id, snapshot.status, snapshot.version)))
    for question in snapshot.questions:
        question_id = str(question.question_id)
        question_key = ("question", question_id)
        if question.version <= seen.get(question_key, 0):
            continue
        seen[question_key] = question.version
        events.append(
            (
                "question-status",
                question_event(question_id, turn_id, question.status, question.version),
            )
        )
    return events


async def turn_event_stream(
    initial: TurnSnapshot,
    load_snapshot: Callable[[], Awaitable[TurnSnapshot]],
    notifications: AsyncIterator[dict[str, Any]],
    *,
    timeout_seconds: float,
) -> AsyncGenerator[str, None]:
    """Project durable snapshots; Redis payloads are wake-up hints only."""
    seen: dict[tuple[str, str], int] = {}
    snapshot = initial
    first = True
    ticks = reconciliation_ticks(
        notifications,
        interval_seconds=_RECONCILE_INTERVAL_SECONDS,
        timeout_seconds=timeout_seconds,
    )
    async with aclosing(ticks):
        async for hint in ticks:
            if not first:
                hinted_turn = hint.get("turn_id") if hint is not None else None
                if hinted_turn is not None and hinted_turn != str(initial.turn_id):
                    continue
                snapshot = await load_snapshot()
            first = False
            for event_type, data in _snapshot_events(snapshot, seen):
                yield sse_event(event_type, data)
            if snapshot.status in _TERMINAL_TURN_STATUSES:
                return


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Turn not found")


@router.get("/conversations/{conversation_id}/turns/active")
async def get_active_turn(
    conversation_id: UUID,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[TurnSnapshot | None]:
    try:
        snapshot = await interaction_service.get_active_turn_snapshot(
            session, authorization_subject(user), conversation_id
        )
    except InteractionNotFound as exc:
        raise _not_found() from exc
    return ApiResponse(data=snapshot, meta=_meta())


@router.get("/conversations/{conversation_id}/turns/{turn_id}")
async def get_turn(
    conversation_id: UUID,
    turn_id: UUID,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[TurnSnapshot]:
    try:
        snapshot = await interaction_service.get_turn_snapshot(
            session, authorization_subject(user), conversation_id, turn_id
        )
    except InteractionNotFound as exc:
        raise _not_found() from exc
    return ApiResponse(data=snapshot, meta=_meta())


@router.get("/conversations/{conversation_id}/turns/{turn_id}/events")
async def get_turn_events(
    conversation_id: UUID,
    turn_id: UUID,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> StreamingResponse:
    subject = authorization_subject(user)
    channel = f"{user.org_id}:session:{conversation_id}:events"
    pubsub = await event_bus.subscribe_channel(channel)
    try:
        initial = await interaction_service.get_turn_snapshot(
            session, subject, conversation_id, turn_id
        )
    except InteractionNotFound as exc:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()  # type: ignore[no-untyped-call]
        raise _not_found() from exc

    async def load_snapshot() -> TurnSnapshot:
        try:
            return await interaction_service.get_turn_snapshot(
                session, subject, conversation_id, turn_id
            )
        except InteractionNotFound as exc:
            # An exact turn row cannot disappear without its owned conversation;
            # close the stream rather than retargeting another active turn.
            raise _not_found() from exc

    notifications = event_bus.listen(
        pubsub,
        channel,
        timeout_seconds=settings.EVENT_SUBSCRIBE_TIMEOUT,
    )
    return create_streaming_response(
        turn_event_stream(
            initial,
            load_snapshot,
            notifications,
            timeout_seconds=settings.EVENT_SUBSCRIBE_TIMEOUT,
        )
    )
