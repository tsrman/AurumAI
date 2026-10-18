"""Lease-based at-least-once delivery for durable question wake-ups."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from prometheus_client import Counter
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from src.core.database import async_session
from src.core.logging import logger
from src.core.temporal import get_temporal_client
_LEASE_SECONDS = 30
_SETTLEMENT_GRACE_SECONDS = 300
_IDLE_SECONDS = 1.0

OUTBOX_DELIVERY_TOTAL = Counter(
    "interaction_outbox_delivery_total",
    "Question outbox Temporal delivery attempts by safe terminal outcome.",
    ("outcome",),
)


@dataclass(frozen=True, slots=True)
class DeliveryClaim:
    outbox_id: UUID
    lease_id: UUID
    workflow_id: str
    run_id: UUID
    question_id: UUID
    question_version: int
    attempt: int
    settlement_until: datetime


def question_signal_payload(question_id: str, version: int) -> list[str | int]:
    """Metadata-only Temporal envelope; answer/text never leave the owner API."""
    return [question_id, version]


async def _claim_batch(
    session_factory: async_sessionmaker[AsyncSession],
    batch_size: int,
) -> list[DeliveryClaim]:
    if batch_size < 1:
        return []
    async with session_factory.begin() as session:
        await session.execute(text("SELECT cancel_terminal_question_status(:limit)"), {"limit":batch_size})
        rows = (
            await session.execute(
                text(
                    "SELECT * FROM public.claim_interaction_outbox("
                    ":batch_size, :lease_seconds, :settlement_grace_seconds)"
                ),
                {
                    "batch_size": batch_size,
                    "lease_seconds": _LEASE_SECONDS,
                    "settlement_grace_seconds": _SETTLEMENT_GRACE_SECONDS,
                },
            )
        ).mappings()
        return [
            DeliveryClaim(
                outbox_id=row["outbox_id"],
                lease_id=row["lease_id"],
                workflow_id=row["workflow_id"],
                run_id=row["run_id"],
                question_id=row["question_id"],
                question_version=row["question_version"],
                attempt=row["attempt"],
                settlement_until=row["settlement_until"],
            )
            for row in rows
        ]


async def _ack_claim(
    session_factory: async_sessionmaker[AsyncSession],
    claim: DeliveryClaim,
    outcome: Literal["delivered", "retry", "failed"],
) -> bool:
    async with session_factory.begin() as session:
        retry_seconds = min(2 ** min(claim.attempt, 5), 30)
        return bool(
            await session.scalar(
                text(
                    "SELECT public.ack_interaction_outbox("
                    ":outbox_id, :lease_id, :outcome, :settlement_until, :retry_seconds)"
                ),
                {
                    "outbox_id": claim.outbox_id,
                    "lease_id": claim.lease_id,
                    "outcome": outcome,
                    "settlement_until": claim.settlement_until,
                    "retry_seconds": retry_seconds,
                },
            )
        )


def _permanent_target_failure(exc: Exception) -> bool:
    return isinstance(exc, RPCError) and exc.status == RPCStatusCode.NOT_FOUND


async def _deliver(temporal_client: Any, claim: DeliveryClaim) -> Literal[
    "delivered", "retry", "failed"
]:
    try:
        handle = temporal_client.get_workflow_handle(
            claim.workflow_id,
            run_id=str(claim.run_id),
        )
        description = await handle.describe()
        if (
            description.id != claim.workflow_id
            or str(description.run_id) != str(claim.run_id)
            or description.status != WorkflowExecutionStatus.RUNNING
        ):
            return "failed"
        await handle.signal(
            "question_available",
            args=question_signal_payload(str(claim.question_id), claim.question_version),
        )
        return "delivered"
    except Exception as exc:
        if _permanent_target_failure(exc):
            return "failed"
        logger.warning(
            "question delivery deferred (outbox_id=%s attempt=%d)",
            claim.outbox_id,
            claim.attempt,
        )
        return "retry"


async def dispatch_batch(
    session_factory: async_sessionmaker[AsyncSession],
    temporal_client: Any,
    batch_size: int = 50,
) -> int:
    """Deliver one claimed batch. Network IO occurs only after claim commit."""
    delivered = 0
    for claim in await _claim_batch(session_factory, batch_size):
        outcome = await _deliver(temporal_client, claim)
        OUTBOX_DELIVERY_TOTAL.labels(outcome=outcome).inc()
        if outcome == "failed":
            logger.error(
                "question delivery failed closed (outbox_id=%s attempt=%d)",
                claim.outbox_id,
                claim.attempt,
            )
        if await _ack_claim(session_factory, claim, outcome):
            delivered += 1
    return delivered


async def run_interaction_outbox_dispatcher() -> None:
    while True:
        try:
            await dispatch_batch(async_session, get_temporal_client())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("interaction outbox dispatcher failed", exc_info=True)
        await asyncio.sleep(_IDLE_SECONDS)
