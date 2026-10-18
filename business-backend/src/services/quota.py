import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal, TypeVar

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.database import async_session
from src.models.llm_usage import LLMUsageEvent, OrganizationUsageMonthly, UsageEventStatus
from src.models.organization import Organization

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class UsageReservation:
    request_id: uuid.UUID
    period_start: date


@dataclass(frozen=True)
class UsageSnapshot:
    period_start: date
    period_end: date
    org_requests_used: int
    org_tokens_used: int
    user_requests_used: int
    user_tokens_used: int
    request_limit: int | None
    token_limit: int | None


class QuotaExceededError(Exception):
    def __init__(
        self,
        resource: Literal["requests", "tokens"],
        used: int,
        limit: int,
        period_end: date,
    ) -> None:
        self.resource = resource
        self.used = used
        self.limit = limit
        self.period_end = period_end
        super().__init__(f"monthly {resource} quota exceeded")


class QuotaInvariantError(Exception):
    pass


def _period_bounds(now: datetime | None) -> tuple[date, date]:
    current = _utc_now(now)
    period_start = date(current.year, current.month, 1)
    if current.month == 12:
        return period_start, date(current.year + 1, 1, 1)
    return period_start, date(current.year, current.month + 1, 1)


def _utc_now(now: datetime | None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("quota timestamps must be timezone-aware")
    return current.astimezone(timezone.utc)


class QuotaService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] = async_session,
        *,
        retry_attempts: int = 3,
        reservation_ttl_seconds: int = 900,
    ) -> None:
        self._session_factory = session_factory
        self._retry_attempts = retry_attempts
        # A reservation is only ever `reserved` between reserve() and the
        # provider's response headers — on_accepted() flips it to `accepted`
        # before the first chunk is streamed. So this TTL never races a long
        # generation, however slow: it only has to outlast the gateway
        # handshake (llm_client's httpx timeout is 120s). 15 minutes leaves
        # an order of magnitude of headroom while still reclaiming a leak
        # long before the monthly counter would otherwise carry it to the
        # end of the period.
        self._reservation_ttl = timedelta(seconds=reservation_ttl_seconds)

    async def _retry_transient(self, operation: Callable[[], Awaitable[T]]) -> T:
        for attempt in range(self._retry_attempts):
            try:
                return await operation()
            except OperationalError:
                if attempt + 1 >= self._retry_attempts:
                    raise
                await asyncio.sleep(0.05 * (2**attempt))
        raise RuntimeError("quota retry loop exhausted")

    @staticmethod
    def _raise_if_exhausted(
        organization: Organization,
        requests_used: int,
        tokens_used: int,
        period_end: date,
    ) -> None:
        request_limit = organization.quota_requests
        if request_limit is not None and requests_used >= request_limit:
            raise QuotaExceededError("requests", requests_used, request_limit, period_end)
        token_limit = organization.quota_tokens
        if token_limit is not None and tokens_used >= token_limit:
            raise QuotaExceededError("tokens", tokens_used, token_limit, period_end)

    async def _reclaim_stale_reservations(
        self,
        org_id: uuid.UUID,
        period_start: date,
        *,
        now: datetime | None = None,
    ) -> int:
        """Give back reservations that were counted but never resolved.

        reserve() raises requests_used before the provider is contacted, and
        the only rollback is the stream generator's `finally`. A backend
        killed between those two points — or a StreamingResponse that is
        never iterated — leaves the event `reserved` and the counter raised
        for the rest of the month, with nothing to bring it back down.

        This runs on both quota entry points, not just reserve(), because
        preflight() gates /v1/chat/stream and raises QuotaExceededError
        before reserve() is ever reached: a sweep living only in reserve()
        would never fire for exactly the org that is locked out.

        Deliberately its own transaction, taken before reserve() locks the
        aggregate row — nesting it inside that lock would self-deadlock.
        DELETE ... RETURNING and the GREATEST() decrement are each atomic, so
        a concurrent cancel() or a second sweeper either wins a row outright
        or sees nothing; neither can double-decrement.
        """
        cutoff = _utc_now(now) - self._reservation_ttl

        async def sweep() -> int:
            async with self._session_factory() as session:
                session.info["org_id"] = str(org_id)
                async with session.begin():
                    stale = (
                        await session.execute(
                            delete(LLMUsageEvent)
                            .where(
                                LLMUsageEvent.org_id == org_id,
                                LLMUsageEvent.period_start == period_start,
                                LLMUsageEvent.status == UsageEventStatus.RESERVED.value,
                                LLMUsageEvent.created_at < cutoff,
                            )
                            .returning(LLMUsageEvent.id)
                        )
                    ).all()
                    if not stale:
                        return 0
                    await session.execute(
                        update(OrganizationUsageMonthly)
                        .where(
                            OrganizationUsageMonthly.org_id == org_id,
                            OrganizationUsageMonthly.period_start == period_start,
                        )
                        .values(
                            requests_used=func.greatest(
                                OrganizationUsageMonthly.requests_used - len(stale), 0
                            ),
                            updated_at=_utc_now(None),
                        )
                    )
                    return len(stale)

        reclaimed = await self._retry_transient(sweep)
        if reclaimed:
            logger.warning(
                "reclaimed %d stale quota reservation(s) (org_id=%s period_start=%s)",
                reclaimed,
                org_id,
                period_start,
            )
        return reclaimed

    async def preflight(
        self,
        org_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> UsageSnapshot:
        period_start, period_end = _period_bounds(now)
        await self._reclaim_stale_reservations(org_id, period_start, now=now)
        async with self._session_factory() as session:
            session.info["org_id"] = str(org_id)
            organization = await session.scalar(
                select(Organization).where(Organization.id == org_id)
            )
            if organization is None:
                raise QuotaInvariantError("quota organization is missing")
            aggregate = await session.get(
                OrganizationUsageMonthly,
                {"org_id": org_id, "period_start": period_start},
            )
            requests_used = aggregate.requests_used if aggregate else 0
            tokens_used = aggregate.total_tokens if aggregate else 0
            self._raise_if_exhausted(
                organization, requests_used, tokens_used, period_end
            )
            return UsageSnapshot(
                period_start=period_start,
                period_end=period_end,
                org_requests_used=requests_used,
                org_tokens_used=tokens_used,
                user_requests_used=0,
                user_tokens_used=0,
                request_limit=organization.quota_requests,
                token_limit=organization.quota_tokens,
            )

    async def reserve(
        self,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        model: str,
        request_id: uuid.UUID,
        *,
        workflow_run_id: uuid.UUID | None = None,
        workflow_phase: str | None = None,
        workflow_node_key: str | None = None,
        now: datetime | None = None,
    ) -> UsageReservation:
        if workflow_run_id is None:
            if workflow_phase is not None or workflow_node_key is not None:
                raise QuotaInvariantError("workflow correlation is incomplete")
        elif (
            workflow_phase == "node" and workflow_node_key is not None
        ) or (
            workflow_phase in {"supervisor_prepare", "supervisor_synthesize"}
            and workflow_node_key is None
        ):
            pass
        else:
            raise QuotaInvariantError("workflow correlation is invalid")
        period_start, period_end = _period_bounds(now)
        await self._reclaim_stale_reservations(org_id, period_start, now=now)
        async with self._session_factory() as session:
            session.info["org_id"] = str(org_id)
            async with session.begin():
                await session.execute(
                    insert(OrganizationUsageMonthly)
                    .values(org_id=org_id, period_start=period_start)
                    .on_conflict_do_nothing(
                        index_elements=["org_id", "period_start"]
                    )
                )
                aggregate = await session.get(
                    OrganizationUsageMonthly,
                    {"org_id": org_id, "period_start": period_start},
                    with_for_update=True,
                )
                if aggregate is None:
                    raise QuotaInvariantError("monthly quota aggregate is missing")

                existing = await session.scalar(
                    select(LLMUsageEvent).where(
                        LLMUsageEvent.org_id == org_id,
                        LLMUsageEvent.request_id == request_id,
                    )
                )
                if existing is not None:
                    expected = (
                        org_id,
                        user_id,
                        conversation_id,
                        period_start,
                        model,
                        workflow_run_id,
                        workflow_phase,
                        workflow_node_key,
                    )
                    actual = (
                        existing.org_id,
                        existing.user_id,
                        existing.conversation_id,
                        existing.period_start,
                        existing.model,
                        existing.workflow_run_id,
                        existing.workflow_phase,
                        existing.workflow_node_key,
                    )
                    if actual != expected:
                        raise QuotaInvariantError(
                            "request id has conflicting quota attribution"
                        )
                    return UsageReservation(
                        request_id=request_id, period_start=period_start
                    )

                organization = await session.scalar(
                    select(Organization).where(Organization.id == org_id)
                )
                if organization is None:
                    raise QuotaInvariantError("quota organization is missing")
                self._raise_if_exhausted(
                    organization,
                    aggregate.requests_used,
                    aggregate.total_tokens,
                    period_end,
                )

                session.add(
                    LLMUsageEvent(
                        org_id=org_id,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        request_id=request_id,
                        period_start=period_start,
                        model=model,
                        workflow_run_id=workflow_run_id,
                        workflow_phase=workflow_phase,
                        workflow_node_key=workflow_node_key,
                        status=UsageEventStatus.RESERVED.value,
                    )
                )
                aggregate.requests_used += 1
                aggregate.updated_at = _utc_now(None)

        return UsageReservation(request_id=request_id, period_start=period_start)

    async def accept(
        self,
        org_id: uuid.UUID,
        request_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> None:
        accepted_at = _utc_now(now)

        async def transition() -> None:
            async with self._session_factory() as session:
                session.info["org_id"] = str(org_id)
                async with session.begin():
                    event = await session.scalar(
                        select(LLMUsageEvent)
                        .where(
                            LLMUsageEvent.org_id == org_id,
                            LLMUsageEvent.request_id == request_id,
                        )
                        .with_for_update()
                    )
                    if event is None:
                        raise QuotaInvariantError("quota event is missing during accept")
                    if event.status in (
                        UsageEventStatus.ACCEPTED.value,
                        UsageEventStatus.COMPLETED.value,
                    ):
                        return
                    if event.status != UsageEventStatus.RESERVED.value:
                        raise QuotaInvariantError("quota event has invalid accept state")
                    event.status = UsageEventStatus.ACCEPTED.value
                    event.accepted_at = accepted_at

        await self._retry_transient(transition)

    async def cancel(self, org_id: uuid.UUID, request_id: uuid.UUID) -> None:
        # Retried like accept()/complete(): this is the only path that gives a
        # reservation back, and it runs in the stream generator's `finally`,
        # where a single transient database error used to leak the counter for
        # the rest of the month behind nothing but a warning.
        async def transition() -> None:
            async with self._session_factory() as session:
                session.info["org_id"] = str(org_id)
                async with session.begin():
                    existing = await session.scalar(
                        select(LLMUsageEvent).where(
                            LLMUsageEvent.org_id == org_id,
                            LLMUsageEvent.request_id == request_id,
                        )
                    )
                    if existing is None or existing.status != UsageEventStatus.RESERVED.value:
                        return
                    aggregate = await session.get(
                        OrganizationUsageMonthly,
                        {
                            "org_id": org_id,
                            "period_start": existing.period_start,
                        },
                        with_for_update=True,
                    )
                    if aggregate is None:
                        raise QuotaInvariantError(
                            "monthly quota aggregate is missing during cancel"
                        )
                    event = await session.scalar(
                        select(LLMUsageEvent)
                        .where(
                            LLMUsageEvent.org_id == org_id,
                            LLMUsageEvent.request_id == request_id,
                        )
                        .with_for_update()
                    )
                    if event is None or event.status != UsageEventStatus.RESERVED.value:
                        return
                    if aggregate.requests_used <= 0:
                        raise QuotaInvariantError(
                            "monthly request counter cannot be decremented"
                        )
                    await session.delete(event)
                    aggregate.requests_used -= 1
                    aggregate.updated_at = _utc_now(None)

        await self._retry_transient(transition)

    async def complete(
        self,
        org_id: uuid.UUID,
        request_id: uuid.UUID,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        *,
        now: datetime | None = None,
    ) -> None:
        usage = (prompt_tokens, completion_tokens, total_tokens)
        if any(type(value) is not int or value < 0 for value in usage):
            raise QuotaInvariantError("quota usage must contain non-negative integers")
        completed_at = _utc_now(now)

        async def transition() -> None:
            async with self._session_factory() as session:
                session.info["org_id"] = str(org_id)
                async with session.begin():
                    existing = await session.scalar(
                        select(LLMUsageEvent).where(
                            LLMUsageEvent.org_id == org_id,
                            LLMUsageEvent.request_id == request_id,
                        )
                    )
                    if existing is None:
                        raise QuotaInvariantError("quota event is missing during completion")
                    aggregate = await session.get(
                        OrganizationUsageMonthly,
                        {
                            "org_id": org_id,
                            "period_start": existing.period_start,
                        },
                        with_for_update=True,
                    )
                    if aggregate is None:
                        raise QuotaInvariantError(
                            "monthly quota aggregate is missing during completion"
                        )
                    event = await session.scalar(
                        select(LLMUsageEvent)
                        .where(
                            LLMUsageEvent.org_id == org_id,
                            LLMUsageEvent.request_id == request_id,
                        )
                        .with_for_update()
                    )
                    if event is None:
                        raise QuotaInvariantError("quota event disappeared during completion")
                    if event.status == UsageEventStatus.COMPLETED.value:
                        recorded = (
                            event.prompt_tokens,
                            event.completion_tokens,
                            event.total_tokens,
                        )
                        if recorded != usage:
                            raise QuotaInvariantError(
                                "quota event has conflicting completion usage"
                            )
                        return
                    if event.status != UsageEventStatus.ACCEPTED.value:
                        raise QuotaInvariantError(
                            "quota event must be accepted before completion"
                        )

                    event.status = UsageEventStatus.COMPLETED.value
                    event.prompt_tokens = prompt_tokens
                    event.completion_tokens = completion_tokens
                    event.total_tokens = total_tokens
                    event.completed_at = completed_at
                    aggregate.prompt_tokens += prompt_tokens
                    aggregate.completion_tokens += completion_tokens
                    aggregate.total_tokens += total_tokens
                    aggregate.updated_at = _utc_now(None)

        await self._retry_transient(transition)

    async def snapshot(
        self,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> UsageSnapshot:
        period_start, period_end = _period_bounds(now)
        async with self._session_factory() as session:
            session.info["org_id"] = str(org_id)
            organization = await session.scalar(
                select(Organization).where(Organization.id == org_id)
            )
            if organization is None:
                raise QuotaInvariantError("quota organization is missing")

            aggregate = await session.get(
                OrganizationUsageMonthly,
                {"org_id": org_id, "period_start": period_start},
            )
            user_usage = (
                await session.execute(
                    select(
                        func.count(LLMUsageEvent.id),
                        func.coalesce(
                            func.sum(LLMUsageEvent.total_tokens).filter(
                                LLMUsageEvent.status == UsageEventStatus.COMPLETED.value
                            ),
                            0,
                        ),
                    ).where(
                        LLMUsageEvent.org_id == org_id,
                        LLMUsageEvent.user_id == user_id,
                        LLMUsageEvent.period_start == period_start,
                    )
                )
            ).one()

            return UsageSnapshot(
                period_start=period_start,
                period_end=period_end,
                org_requests_used=aggregate.requests_used if aggregate else 0,
                org_tokens_used=aggregate.total_tokens if aggregate else 0,
                user_requests_used=int(user_usage[0]),
                user_tokens_used=int(user_usage[1]),
                request_limit=organization.quota_requests,
                token_limit=organization.quota_tokens,
            )


quota_service = QuotaService()


def get_quota_service() -> QuotaService:
    return quota_service
