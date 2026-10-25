import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.config import settings
from src.models.llm_usage import LLMUsageEvent, OrganizationUsageMonthly
from src.models.agent_definition import AgentDefinition
from src.models.conversation import Conversation
from src.models.message import Message
from src.models.organization import Organization
from src.models.user import User
from src.models.workflow_definition import WorkflowDefinition
from src.models.workflow_run import WorkflowRun
from src.models.llm_usage import UsageEventKind
from src.services.quota import QuotaExceededError, QuotaInvariantError, QuotaService


@dataclass(frozen=True)
class QuotaIdentity:
    org_id: uuid.UUID
    user_id: uuid.UUID
    service: QuotaService
    session_factory: async_sessionmaker[AsyncSession]


class TransientlyFailingSessionFactory:
    def __init__(
        self, delegate: async_sessionmaker[AsyncSession], failures: int
    ) -> None:
        self._delegate = delegate
        self._failures = failures
        self.attempts = 0

    def __call__(self) -> AsyncSession:
        self.attempts += 1
        if self.attempts <= self._failures:
            raise OperationalError("quota transition", {}, RuntimeError("transient"))
        return self._delegate()


@asynccontextmanager
async def quota_identity(
    *, request_limit: int | None = None, token_limit: int | None = None
) -> AsyncIterator[QuotaIdentity]:
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()

    async with session_factory() as session:
        organization = Organization(
            id=org_id,
            name="Quota Test Org",
            slug=f"quota-test-{org_id}",
            quota_requests=request_limit,
            quota_tokens=token_limit,
        )
        session.add(organization)
        session.add(
            User(
                id=user_id,
                keycloak_id=uuid.uuid4(),
                email=f"quota-{user_id}@example.com",
                org_id=org_id,
            )
        )
        await session.flush()
        organization.quota_requests = request_limit
        organization.quota_tokens = token_limit
        await session.commit()

    try:
        yield QuotaIdentity(
            org_id=org_id,
            user_id=user_id,
            service=QuotaService(session_factory),
            session_factory=session_factory,
        )
    finally:
        async with session_factory() as session:
            await session.execute(delete(LLMUsageEvent).where(LLMUsageEvent.org_id == org_id))
            await session.execute(
                delete(OrganizationUsageMonthly).where(
                    OrganizationUsageMonthly.org_id == org_id
                )
            )
            await session.execute(delete(Organization).where(Organization.id == org_id))
            await session.commit()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("now", "period_start", "period_end"),
    [
        (
            datetime(2026, 8, 31, 23, 59, 59, tzinfo=timezone.utc),
            "2026-08-01",
            "2026-09-01",
        ),
        (
            datetime(2026, 12, 15, 12, 0, tzinfo=timezone.utc),
            "2026-12-01",
            "2027-01-01",
        ),
    ],
)
async def test_empty_snapshot_uses_utc_calendar_month(
    now: datetime, period_start: str, period_end: str
) -> None:
    async with quota_identity() as identity:
        snapshot = await identity.service.snapshot(identity.org_id, identity.user_id, now=now)

    assert snapshot.period_start.isoformat() == period_start
    assert snapshot.period_end.isoformat() == period_end
    assert snapshot.org_requests_used == 0
    assert snapshot.org_tokens_used == 0
    assert snapshot.user_requests_used == 0
    assert snapshot.user_tokens_used == 0
    assert snapshot.request_limit is None
    assert snapshot.token_limit is None


@pytest.mark.asyncio
async def test_reserve_increments_request_once_for_an_unlimited_org() -> None:
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    request_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    async with quota_identity() as identity:
        reservation = await identity.service.reserve(
            identity.org_id,
            identity.user_id,
            conversation_id,
            "gpt-test",
            request_id,
            now=now,
        )
        snapshot = await identity.service.snapshot(
            identity.org_id, identity.user_id, now=now
        )
        async with identity.session_factory() as session:
            event = await session.scalar(
                select(LLMUsageEvent).where(LLMUsageEvent.request_id == request_id)
            )

    assert reservation.request_id == request_id
    assert reservation.period_start.isoformat() == "2026-08-01"
    assert snapshot.org_requests_used == 1
    assert snapshot.user_requests_used == 1
    assert event is not None
    assert event.conversation_id == conversation_id
    assert event.model == "gpt-test"
    assert event.status == "reserved"
    assert event.workflow_run_id is None
    assert event.workflow_phase is None
    assert event.workflow_node_key is None


@pytest.mark.asyncio
async def test_reserve_persists_exact_workflow_correlation() -> None:
    request_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    async with quota_identity() as identity:
        async with identity.session_factory() as session:
            session.info["org_id"] = str(identity.org_id)
            agent = AgentDefinition(
                org_id=identity.org_id,
                name=f"Quota workflow agent-{uuid.uuid4()}",
                model_id="gpt-test",
                system_prompt="Answer",
            )
            session.add(agent)
            await session.flush()
            definition = WorkflowDefinition(
                org_id=identity.org_id,
                name=f"Quota workflow-{uuid.uuid4()}",
                supervisor_agent_definition_id=agent.id,
            )
            conversation = Conversation(
                id=conversation_id,
                org_id=identity.org_id,
                user_id=identity.user_id,
                model="gpt-test",
                title="Quota workflow",
            )
            session.add_all([definition, conversation])
            await session.flush()
            turn_id = uuid.uuid4()
            message = Message(
                org_id=identity.org_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                user_id=identity.user_id,
                role="user",
                content="Run workflow",
            )
            session.add(message)
            await session.flush()
            run = WorkflowRun(
                org_id=identity.org_id,
                workflow_definition_id=definition.id,
                workflow_definition_version=1,
                initiating_user_id=identity.user_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                source_message_id=message.id,
                temporal_workflow_id=f"quota-workflow-{uuid.uuid4()}",
                execution_snapshot={"supervisor": {}, "nodes": []},
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        await identity.service.reserve(
            identity.org_id,
            identity.user_id,
            conversation_id,
            "gpt-test",
            request_id,
            workflow_run_id=run_id,
            workflow_phase="node",
            workflow_node_key="research",
        )
        await identity.service.reserve(
            identity.org_id,
            identity.user_id,
            conversation_id,
            "gpt-test",
            request_id,
            workflow_run_id=run_id,
            workflow_phase="node",
            workflow_node_key="research",
        )
        with pytest.raises(QuotaInvariantError):
            await identity.service.reserve(
                identity.org_id,
                identity.user_id,
                conversation_id,
                "gpt-test",
                request_id,
                workflow_run_id=run_id,
                workflow_phase="node",
                workflow_node_key="review",
            )
        async with identity.session_factory() as session:
            session.info["org_id"] = str(identity.org_id)
            event = await session.scalar(
                select(LLMUsageEvent).where(LLMUsageEvent.request_id == request_id)
            )

    assert event is not None
    assert (event.workflow_run_id, event.workflow_phase, event.workflow_node_key) == (
        run_id,
        "node",
        "research",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_limit", "token_limit", "resource"),
    [(0, None, "requests"), (None, 0, "tokens")],
)
async def test_reserve_rejects_an_exhausted_zero_limit(
    request_limit: int | None, token_limit: int | None, resource: str
) -> None:
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    async with quota_identity(
        request_limit=request_limit, token_limit=token_limit
    ) as identity:
        with pytest.raises(QuotaExceededError) as exc_info:
            await identity.service.reserve(
                identity.org_id,
                identity.user_id,
                uuid.uuid4(),
                "gpt-test",
                uuid.uuid4(),
                now=now,
            )
        snapshot = await identity.service.snapshot(
            identity.org_id, identity.user_id, now=now
        )

    assert exc_info.value.resource == resource
    assert exc_info.value.used == 0
    assert exc_info.value.limit == 0
    assert exc_info.value.period_end.isoformat() == "2026-09-01"
    assert snapshot.org_requests_used == 0


@pytest.mark.asyncio
async def test_reserve_is_idempotent_but_rejects_conflicting_attribution() -> None:
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    request_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    async with quota_identity(request_limit=1) as identity:
        first = await identity.service.reserve(
            identity.org_id,
            identity.user_id,
            conversation_id,
            "gpt-test",
            request_id,
            now=now,
        )
        repeated = await identity.service.reserve(
            identity.org_id,
            identity.user_id,
            conversation_id,
            "gpt-test",
            request_id,
            now=now,
        )
        with pytest.raises(QuotaInvariantError):
            await identity.service.reserve(
                identity.org_id,
                identity.user_id,
                conversation_id,
                "different-model",
                request_id,
                now=now,
            )
        # An image call never reuses a chat completion's attribution.
        with pytest.raises(QuotaInvariantError):
            await identity.service.reserve(
                identity.org_id,
                identity.user_id,
                conversation_id,
                "gpt-test",
                request_id,
                kind=UsageEventKind.IMAGE_GENERATE,
                now=now,
            )
        snapshot = await identity.service.snapshot(
            identity.org_id, identity.user_id, now=now
        )

    assert repeated == first
    assert snapshot.org_requests_used == 1


@pytest.mark.asyncio
async def test_reservations_are_independent_across_utc_months() -> None:
    august = datetime(2026, 8, 31, 23, 59, tzinfo=timezone.utc)
    september = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    async with quota_identity(request_limit=1) as identity:
        await identity.service.reserve(
            identity.org_id,
            identity.user_id,
            uuid.uuid4(),
            "gpt-test",
            uuid.uuid4(),
            now=august,
        )
        await identity.service.reserve(
            identity.org_id,
            identity.user_id,
            uuid.uuid4(),
            "gpt-test",
            uuid.uuid4(),
            now=september,
        )
        august_snapshot = await identity.service.snapshot(
            identity.org_id, identity.user_id, now=august
        )
        september_snapshot = await identity.service.snapshot(
            identity.org_id, identity.user_id, now=september
        )

    assert august_snapshot.org_requests_used == 1
    assert september_snapshot.org_requests_used == 1


@pytest.mark.asyncio
async def test_concurrent_reservations_cannot_overspend_one_remaining_request() -> None:
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    async with quota_identity(request_limit=1) as identity:
        results = await asyncio.gather(
            *(
                identity.service.reserve(
                    identity.org_id,
                    identity.user_id,
                    uuid.uuid4(),
                    "gpt-test",
                    uuid.uuid4(),
                    now=now,
                )
                for _ in range(2)
            ),
            return_exceptions=True,
        )
        snapshot = await identity.service.snapshot(
            identity.org_id, identity.user_id, now=now
        )
        async with identity.session_factory() as session:
            event_count = await session.scalar(
                select(func.count(LLMUsageEvent.id)).where(
                    LLMUsageEvent.org_id == identity.org_id
                )
            )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    errors = [result for result in results if isinstance(result, Exception)]
    assert len(errors) == 1
    assert isinstance(errors[0], QuotaExceededError)
    assert errors[0].resource == "requests"
    assert snapshot.org_requests_used == 1
    assert event_count == 1


@pytest.mark.asyncio
async def test_cancel_refunds_only_a_reserved_request_and_is_idempotent() -> None:
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    request_id = uuid.uuid4()
    async with quota_identity() as identity:
        await identity.service.reserve(
            identity.org_id,
            identity.user_id,
            uuid.uuid4(),
            "gpt-test",
            request_id,
            now=now,
        )
        await identity.service.cancel(identity.org_id, request_id)
        await identity.service.cancel(identity.org_id, request_id)
        snapshot = await identity.service.snapshot(
            identity.org_id, identity.user_id, now=now
        )
        async with identity.session_factory() as session:
            event = await session.scalar(
                select(LLMUsageEvent).where(LLMUsageEvent.request_id == request_id)
            )

    assert snapshot.org_requests_used == 0
    assert snapshot.user_requests_used == 0
    assert event is None


@pytest.mark.asyncio
async def test_accept_is_idempotent_and_prevents_refund() -> None:
    now = datetime(2026, 8, 15, 12, 30, tzinfo=timezone.utc)
    request_id = uuid.uuid4()
    async with quota_identity() as identity:
        await identity.service.reserve(
            identity.org_id,
            identity.user_id,
            uuid.uuid4(),
            "gpt-test",
            request_id,
            now=now,
        )
        await identity.service.accept(identity.org_id, request_id, now=now)
        await identity.service.accept(identity.org_id, request_id, now=now)
        await identity.service.cancel(identity.org_id, request_id)
        snapshot = await identity.service.snapshot(
            identity.org_id, identity.user_id, now=now
        )
        async with identity.session_factory() as session:
            event = await session.scalar(
                select(LLMUsageEvent).where(LLMUsageEvent.request_id == request_id)
            )

    assert event is not None
    assert event.status == "accepted"
    assert event.accepted_at == now
    assert snapshot.org_requests_used == 1
    assert snapshot.org_tokens_used == 0


@pytest.mark.asyncio
async def test_complete_adds_provider_usage_once_and_rejects_a_conflicting_retry() -> None:
    now = datetime(2026, 8, 15, 12, 30, tzinfo=timezone.utc)
    request_id = uuid.uuid4()
    async with quota_identity() as identity:
        await identity.service.reserve(
            identity.org_id,
            identity.user_id,
            uuid.uuid4(),
            "gpt-test",
            request_id,
            now=now,
        )
        await identity.service.accept(identity.org_id, request_id, now=now)
        await identity.service.complete(
            identity.org_id, request_id, 3, 4, 9, now=now
        )
        await identity.service.complete(
            identity.org_id, request_id, 3, 4, 9, now=now
        )
        with pytest.raises(QuotaInvariantError):
            await identity.service.complete(
                identity.org_id, request_id, 3, 4, 10, now=now
            )
        await identity.service.cancel(identity.org_id, request_id)
        snapshot = await identity.service.snapshot(
            identity.org_id, identity.user_id, now=now
        )
        async with identity.session_factory() as session:
            event = await session.scalar(
                select(LLMUsageEvent).where(LLMUsageEvent.request_id == request_id)
            )

    assert event is not None
    assert event.status == "completed"
    assert event.prompt_tokens == 3
    assert event.completion_tokens == 4
    assert event.total_tokens == 9
    assert event.completed_at == now
    assert snapshot.org_requests_used == 1
    assert snapshot.org_tokens_used == 9
    assert snapshot.user_tokens_used == 9


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt_tokens", "completion_tokens", "total_tokens"),
    [(-1, 0, 0), (0, -1, 0), (0, 0, -1), (True, 0, 1)],
)
async def test_complete_rejects_invalid_usage_without_mutating_counters(
    prompt_tokens: int, completion_tokens: int, total_tokens: int
) -> None:
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    request_id = uuid.uuid4()
    async with quota_identity() as identity:
        await identity.service.reserve(
            identity.org_id,
            identity.user_id,
            uuid.uuid4(),
            "gpt-test",
            request_id,
            now=now,
        )
        await identity.service.accept(identity.org_id, request_id, now=now)
        with pytest.raises(QuotaInvariantError):
            await identity.service.complete(
                identity.org_id,
                request_id,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                now=now,
            )
        snapshot = await identity.service.snapshot(
            identity.org_id, identity.user_id, now=now
        )

    assert snapshot.org_requests_used == 1
    assert snapshot.org_tokens_used == 0


@pytest.mark.asyncio
async def test_complete_requires_an_existing_accepted_event() -> None:
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    async with quota_identity() as identity:
        reserved_id = uuid.uuid4()
        await identity.service.reserve(
            identity.org_id,
            identity.user_id,
            uuid.uuid4(),
            "gpt-test",
            reserved_id,
            now=now,
        )
        with pytest.raises(QuotaInvariantError):
            await identity.service.complete(
                identity.org_id, reserved_id, 1, 1, 2, now=now
            )
        with pytest.raises(QuotaInvariantError):
            await identity.service.complete(
                identity.org_id, uuid.uuid4(), 1, 1, 2, now=now
            )


@pytest.mark.asyncio
async def test_token_limit_allows_one_crossing_then_blocks_later_calls() -> None:
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    request_id = uuid.uuid4()
    async with quota_identity(token_limit=5) as identity:
        await identity.service.reserve(
            identity.org_id,
            identity.user_id,
            uuid.uuid4(),
            "gpt-test",
            request_id,
            now=now,
        )
        await identity.service.accept(identity.org_id, request_id, now=now)
        await identity.service.complete(
            identity.org_id, request_id, 4, 4, 8, now=now
        )
        with pytest.raises(QuotaExceededError) as exc_info:
            await identity.service.preflight(identity.org_id, now=now)
        with pytest.raises(QuotaExceededError):
            await identity.service.reserve(
                identity.org_id,
                identity.user_id,
                uuid.uuid4(),
                "gpt-test",
                uuid.uuid4(),
                now=now,
            )

    assert exc_info.value.resource == "tokens"
    assert exc_info.value.used == 8
    assert exc_info.value.limit == 5


@pytest.mark.asyncio
async def test_snapshot_separates_personal_usage_from_organization_totals() -> None:
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    async with quota_identity() as identity:
        second_user_id = uuid.uuid4()
        async with identity.session_factory() as session:
            session.add(
                User(
                    id=second_user_id,
                    keycloak_id=uuid.uuid4(),
                    email=f"quota-second-{second_user_id}@example.com",
                    org_id=identity.org_id,
                )
            )
            await session.commit()

        for user_id, total_tokens in (
            (identity.user_id, 7),
            (second_user_id, 11),
        ):
            request_id = uuid.uuid4()
            await identity.service.reserve(
                identity.org_id,
                user_id,
                uuid.uuid4(),
                "gpt-test",
                request_id,
                now=now,
            )
            await identity.service.accept(identity.org_id, request_id, now=now)
            await identity.service.complete(
                identity.org_id,
                request_id,
                total_tokens,
                0,
                total_tokens,
                now=now,
            )

        accepted_without_usage = uuid.uuid4()
        await identity.service.reserve(
            identity.org_id,
            identity.user_id,
            uuid.uuid4(),
            "gpt-test",
            accepted_without_usage,
            now=now,
        )
        await identity.service.accept(
            identity.org_id, accepted_without_usage, now=now
        )

        first_snapshot = await identity.service.snapshot(
            identity.org_id, identity.user_id, now=now
        )
        second_snapshot = await identity.service.snapshot(
            identity.org_id, second_user_id, now=now
        )

    assert first_snapshot.org_requests_used == 3
    assert first_snapshot.org_tokens_used == 18
    assert first_snapshot.user_requests_used == 2
    assert first_snapshot.user_tokens_used == 7
    assert second_snapshot.user_requests_used == 1
    assert second_snapshot.user_tokens_used == 11


@pytest.mark.asyncio
async def test_accept_retries_transient_database_failures_with_the_same_event() -> None:
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    request_id = uuid.uuid4()
    async with quota_identity() as identity:
        await identity.service.reserve(
            identity.org_id,
            identity.user_id,
            uuid.uuid4(),
            "gpt-test",
            request_id,
            now=now,
        )
        failing_factory = TransientlyFailingSessionFactory(
            identity.session_factory, failures=2
        )
        retrying_service = QuotaService(cast(Any, failing_factory), retry_attempts=3)

        await retrying_service.accept(identity.org_id, request_id, now=now)
        async with identity.session_factory() as session:
            event = await session.scalar(
                select(LLMUsageEvent).where(LLMUsageEvent.request_id == request_id)
            )

    assert failing_factory.attempts == 3
    assert event is not None
    assert event.status == "accepted"


@pytest.mark.asyncio
async def test_complete_retries_transient_database_failures_with_the_same_usage() -> None:
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    request_id = uuid.uuid4()
    async with quota_identity() as identity:
        await identity.service.reserve(
            identity.org_id,
            identity.user_id,
            uuid.uuid4(),
            "gpt-test",
            request_id,
            now=now,
        )
        await identity.service.accept(identity.org_id, request_id, now=now)
        failing_factory = TransientlyFailingSessionFactory(
            identity.session_factory, failures=2
        )
        retrying_service = QuotaService(cast(Any, failing_factory), retry_attempts=3)

        await retrying_service.complete(
            identity.org_id, request_id, 2, 3, 5, now=now
        )
        snapshot = await identity.service.snapshot(
            identity.org_id, identity.user_id, now=now
        )

    assert failing_factory.attempts == 3
    assert snapshot.org_tokens_used == 5


@pytest.mark.asyncio
async def test_accept_stops_after_the_configured_transient_retry_budget() -> None:
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    request_id = uuid.uuid4()
    async with quota_identity() as identity:
        await identity.service.reserve(
            identity.org_id,
            identity.user_id,
            uuid.uuid4(),
            "gpt-test",
            request_id,
            now=now,
        )
        failing_factory = TransientlyFailingSessionFactory(
            identity.session_factory, failures=10
        )
        retrying_service = QuotaService(cast(Any, failing_factory), retry_attempts=3)

        with pytest.raises(OperationalError):
            await retrying_service.accept(identity.org_id, request_id, now=now)

    assert failing_factory.attempts == 3


@pytest.mark.asyncio
async def test_quota_service_never_mixes_two_organizations_monthly_totals() -> None:
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    async with quota_identity() as first, quota_identity() as second:
        for identity, total_tokens in ((first, 5), (first, 7), (second, 11)):
            request_id = uuid.uuid4()
            await identity.service.reserve(
                identity.org_id,
                identity.user_id,
                uuid.uuid4(),
                "same-model",
                request_id,
                now=now,
            )
            await identity.service.accept(identity.org_id, request_id, now=now)
            await identity.service.complete(
                identity.org_id,
                request_id,
                total_tokens,
                0,
                total_tokens,
                now=now,
            )

        first_snapshot = await first.service.snapshot(
            first.org_id, first.user_id, now=now
        )
        second_snapshot = await second.service.snapshot(
            second.org_id, second.user_id, now=now
        )

    assert first_snapshot.org_requests_used == 2
    assert first_snapshot.org_tokens_used == 12
    assert second_snapshot.org_requests_used == 1
    assert second_snapshot.org_tokens_used == 11


async def _backdate_reservation(
    identity: QuotaIdentity, request_id: uuid.UUID, created_at: datetime
) -> None:
    """created_at carries a server-side `now()` default, so a test cannot
    reserve "in the past" — it has to move the row afterwards."""
    async with identity.session_factory() as session:
        await session.execute(
            update(LLMUsageEvent)
            .where(LLMUsageEvent.request_id == request_id)
            .values(created_at=created_at)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_preflight_reclaims_a_stale_reservation_that_locked_the_org_out() -> None:
    """reserve() raises requests_used before the provider is contacted and only
    the stream generator's `finally` puts it back. A backend killed in between
    leaves the counter raised for the rest of the month — and because
    preflight() gates /v1/chat/stream and raises before reserve() is ever
    reached, an org on its last request stays locked out until the month
    rolls over."""
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    request_id = uuid.uuid4()
    async with quota_identity(request_limit=1) as identity:
        await identity.service.reserve(
            identity.org_id,
            identity.user_id,
            uuid.uuid4(),
            "gpt-test",
            request_id,
            now=now,
        )
        await _backdate_reservation(identity, request_id, now - timedelta(hours=1))

        snapshot = await identity.service.preflight(identity.org_id, now=now)

        async with identity.session_factory() as session:
            event = await session.scalar(
                select(LLMUsageEvent).where(LLMUsageEvent.request_id == request_id)
            )

    assert snapshot.org_requests_used == 0
    assert event is None


@pytest.mark.asyncio
async def test_reserve_reclaims_a_stale_reservation_before_counting_a_new_one() -> None:
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    leaked_id = uuid.uuid4()
    fresh_id = uuid.uuid4()
    async with quota_identity(request_limit=1) as identity:
        await identity.service.reserve(
            identity.org_id,
            identity.user_id,
            uuid.uuid4(),
            "gpt-test",
            leaked_id,
            now=now,
        )
        await _backdate_reservation(identity, leaked_id, now - timedelta(hours=1))

        await identity.service.reserve(
            identity.org_id,
            identity.user_id,
            uuid.uuid4(),
            "gpt-test",
            fresh_id,
            now=now,
        )
        snapshot = await identity.service.snapshot(
            identity.org_id, identity.user_id, now=now
        )

    assert snapshot.org_requests_used == 1


@pytest.mark.asyncio
async def test_a_reservation_inside_its_ttl_is_never_reclaimed() -> None:
    """The window between reserve() and on_accepted() is a gateway handshake,
    not a generation, so nothing legitimate ever sits in `reserved` for long —
    but a sweep that reclaimed a live one would refund a request that is about
    to be billed."""
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    request_id = uuid.uuid4()
    async with quota_identity() as identity:
        await identity.service.reserve(
            identity.org_id,
            identity.user_id,
            uuid.uuid4(),
            "gpt-test",
            request_id,
            now=now,
        )

        snapshot = await identity.service.preflight(identity.org_id, now=now)

        async with identity.session_factory() as session:
            event = await session.scalar(
                select(LLMUsageEvent).where(LLMUsageEvent.request_id == request_id)
            )

    assert snapshot.org_requests_used == 1
    assert event is not None
    assert event.status == "reserved"


@pytest.mark.asyncio
async def test_accepted_events_are_never_reclaimed_however_old() -> None:
    """An accepted request is billable and must never be refunded, even if the
    backend died before it could be completed."""
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    request_id = uuid.uuid4()
    async with quota_identity() as identity:
        await identity.service.reserve(
            identity.org_id,
            identity.user_id,
            uuid.uuid4(),
            "gpt-test",
            request_id,
            now=now,
        )
        await identity.service.accept(identity.org_id, request_id, now=now)
        await _backdate_reservation(identity, request_id, now - timedelta(days=7))

        snapshot = await identity.service.preflight(identity.org_id, now=now)

        async with identity.session_factory() as session:
            event = await session.scalar(
                select(LLMUsageEvent).where(LLMUsageEvent.request_id == request_id)
            )

    assert snapshot.org_requests_used == 1
    assert event is not None
    assert event.status == "accepted"


@pytest.mark.asyncio
async def test_cancel_retries_transient_database_failures() -> None:
    """cancel() runs in the stream generator's `finally` and is the only path
    that gives a reservation back. Unretried, one transient database error
    leaked the counter for the rest of the month behind a lone warning."""
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    request_id = uuid.uuid4()
    async with quota_identity() as identity:
        await identity.service.reserve(
            identity.org_id,
            identity.user_id,
            uuid.uuid4(),
            "gpt-test",
            request_id,
            now=now,
        )
        failing_factory = TransientlyFailingSessionFactory(
            identity.session_factory, failures=2
        )
        retrying_service = QuotaService(cast(Any, failing_factory), retry_attempts=3)

        await retrying_service.cancel(identity.org_id, request_id)
        snapshot = await identity.service.snapshot(
            identity.org_id, identity.user_id, now=now
        )

    assert failing_factory.attempts == 3
    assert snapshot.org_requests_used == 0
