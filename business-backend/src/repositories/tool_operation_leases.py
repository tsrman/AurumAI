"""Durable tool-operation leases shared by platform tools.

A reserved ``tool_operations`` row is claimed under a short lease before any
external I/O, retried a bounded number of times, and failed or completed under
that lease. Subclasses pick the deadline and the error codes they report.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, ClassVar, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.interaction import InteractionTurn, ToolOperation


MAX_PLATFORM_ATTEMPTS = 2


class ToolOperationLeaseError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class OperationClaim:
    operation_id: UUID
    lease_id: UUID
    attempt: int
    turn_id: UUID
    execution_id: UUID
    remaining_seconds: float


class ToolOperationLeases:
    deadline_seconds: ClassVar[float]
    timeout_code: ClassVar[str]
    unavailable_code: ClassVar[str]
    error: ClassVar[type[ToolOperationLeaseError]] = ToolOperationLeaseError

    def __init__(self, org_id: UUID) -> None:
        self.org_id = org_id

    async def _locked_operation(
        self, session: AsyncSession, operation_id: UUID
    ) -> tuple[InteractionTurn, ToolOperation]:
        identity = (
            await session.execute(
                select(ToolOperation.turn_id).where(
                    ToolOperation.org_id == self.org_id,
                    ToolOperation.id == operation_id,
                )
            )
        ).one_or_none()
        if identity is None:
            raise self.error("interaction_not_found")
        turn = await session.scalar(
            select(InteractionTurn)
            .where(
                InteractionTurn.org_id == self.org_id,
                InteractionTurn.id == identity[0],
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if turn is None:
            raise self.error("interaction_not_found")
        operation = await session.scalar(
            select(ToolOperation)
            .where(
                ToolOperation.org_id == self.org_id,
                ToolOperation.turn_id == turn.id,
                ToolOperation.id == operation_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if operation is None:
            raise self.error("interaction_not_found")
        return turn, operation

    async def claim_operation(
        self,
        session: AsyncSession,
        operation_id: UUID,
        *,
        lease_seconds: int = 35,
    ) -> OperationClaim:
        if not 1 <= lease_seconds <= 300:
            raise ValueError("lease_seconds out of range")
        turn, operation = await self._locked_operation(session, operation_id)
        now = await self._clock(session)
        if turn.status != "active":
            raise self.error("interaction_conflict")
        if operation.status == "completed":
            raise self.error("operation_completed")
        if operation.status in {"failed", "cancelled"}:
            raise self.error(operation.error_code or self.unavailable_code)
        remaining_seconds = self._remaining_seconds(operation, now)
        if remaining_seconds <= 0:
            self._mark_failed(operation, self.timeout_code)
            await session.flush()
            raise self.error(self.timeout_code)
        if (
            operation.status == "running"
            and operation.lease_until is not None
            and operation.lease_until > now
        ):
            raise self.error("operation_in_progress")
        if operation.attempts >= MAX_PLATFORM_ATTEMPTS:
            operation.status = "failed"
            operation.error_code = self.unavailable_code
            operation.lease_id = None
            operation.lease_until = None
            await session.flush()
            raise self.error(self.unavailable_code)
        lease_id = uuid4()
        operation.status = "running"
        operation.attempts += 1
        operation.lease_id = lease_id
        operation.lease_until = now + timedelta(seconds=lease_seconds)
        await session.flush()
        return OperationClaim(
            operation.id,
            lease_id,
            operation.attempts,
            operation.turn_id,
            operation.execution_id,
            remaining_seconds,
        )

    @staticmethod
    async def _clock(session: AsyncSession) -> datetime:
        return cast(datetime, await session.scalar(select(func.clock_timestamp())))

    async def operation_result(
        self, session: AsyncSession, operation_id: UUID
    ) -> tuple[str, dict[str, Any] | None, str | None]:
        operation = await session.scalar(
            select(ToolOperation).where(
                ToolOperation.org_id == self.org_id,
                ToolOperation.id == operation_id,
            )
        )
        if operation is None:
            raise self.error("interaction_not_found")
        return operation.status, operation.result, operation.error_code

    @classmethod
    def _remaining_seconds(cls, operation: ToolOperation, now: datetime) -> float:
        return cls.deadline_seconds - (now - operation.created_at).total_seconds()

    @staticmethod
    def _mark_failed(operation: ToolOperation, code: str) -> None:
        operation.status = "failed"
        operation.error_code = code
        operation.result = None
        operation.lease_id = None
        operation.lease_until = None

    async def fail_operation(
        self,
        session: AsyncSession,
        operation_id: UUID,
        lease_id: UUID,
        code: str,
    ) -> None:
        _, operation = await self._locked_operation(session, operation_id)
        if operation.status == "completed":
            return
        if operation.status != "running" or operation.lease_id != lease_id:
            raise self.error("operation_in_progress")
        self._mark_failed(operation, code)
        await session.flush()

    async def release_operation_for_retry(
        self,
        session: AsyncSession,
        operation_id: UUID,
        lease_id: UUID,
    ) -> None:
        _, operation = await self._locked_operation(session, operation_id)
        if operation.status != "running" or operation.lease_id != lease_id:
            raise self.error("operation_in_progress")
        if operation.attempts >= MAX_PLATFORM_ATTEMPTS:
            raise self.error(self.unavailable_code)
        operation.status = "reserved"
        operation.lease_id = None
        operation.lease_until = None
        await session.flush()
