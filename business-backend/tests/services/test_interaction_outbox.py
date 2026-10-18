"""Committed outbox delivery: exact targets, leases, retries and crash duplication."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import delete, event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from src.core.config import settings
from src.models.conversation import Conversation
from src.models.interaction import (
    InteractionExecution,
    InteractionOutbox,
    InteractionTurn,
    UserQuestion,
)
from src.models.organization import Organization
from src.models.user import User
from src.schemas.interaction import AnswerRequest, QuestionRequest, TurnLimits


@dataclass(frozen=True)
class OutboxFixture:
    sessions: async_sessionmaker[AsyncSession]
    org_id: UUID
    user_id: UUID
    conversation_id: UUID
    turn_id: UUID
    execution_id: UUID
    question_id: UUID
    outbox_id: UUID
    workflow_id: str
    run_id: UUID


@pytest.fixture
async def outbox_fixture() -> OutboxFixture:
    from src.repositories.interaction_repository import InteractionRepository

    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    org_id, user_id, conversation_id, turn_id, execution_id = (uuid4() for _ in range(5))
    workflow_id, run_id = f"conversation-{conversation_id}", uuid4()
    try:
        async with sessions.begin() as session:
            session.add(Organization(id=org_id, name="Outbox test", slug=f"outbox-{org_id}"))
            await session.flush()
            session.add(
                User(
                    id=user_id,
                    org_id=org_id,
                    keycloak_id=uuid4(),
                    email=f"{user_id}@example.test",
                )
            )
            await session.flush()
            session.add(
                Conversation(
                    id=conversation_id,
                    org_id=org_id,
                    user_id=user_id,
                    model="provider/model",
                )
            )
            await session.flush()
            session.add(
                InteractionTurn(
                    id=turn_id,
                    org_id=org_id,
                    conversation_id=conversation_id,
                    owner_id=user_id,
                    limits=TurnLimits().model_dump(),
                )
            )
            await session.flush()
            session.add(
                InteractionExecution(
                    id=execution_id,
                    org_id=org_id,
                    turn_id=turn_id,
                    workflow_id=workflow_id,
                    run_id=run_id,
                    task_queue=f"session-{conversation_id}",
                    phase="ordinary",
                )
            )
        repository = InteractionRepository(org_id)
        invocation_id = uuid4()
        question_request = QuestionRequest(text="Which city?")
        from src.services.interaction_rules import question_arguments_hash

        async with sessions.begin() as session:
            operation = await repository.reserve_operation(
                session,
                execution_id,
                invocation_id,
                "ask_user",
                question_arguments_hash(question_request),
            )
            question = await repository.create_question(session, operation.id, question_request)
        async with sessions.begin() as session:
            await repository.answer_question(
                session,
                question.id,
                user_id,
                uuid4(),
                AnswerRequest(text="Bangkok"),
            )
        async with sessions() as session:
            outbox = await session.scalar(
                select(InteractionOutbox).where(
                    InteractionOutbox.question_id == question.id,
                    InteractionOutbox.event_kind == "question_available",
                )
            )
            assert outbox is not None
        yield OutboxFixture(
            sessions,
            org_id,
            user_id,
            conversation_id,
            turn_id,
            execution_id,
            question.id,
            outbox.id,
            workflow_id,
            run_id,
        )
    finally:
        async with sessions.begin() as cleanup:
            await cleanup.execute(delete(InteractionTurn).where(InteractionTurn.id == turn_id))
            await cleanup.execute(delete(Conversation).where(Conversation.id == conversation_id))
            await cleanup.execute(delete(User).where(User.id == user_id))
            await cleanup.execute(delete(Organization).where(Organization.id == org_id))
        await engine.dispose()


def temporal_client(fixture: OutboxFixture, *, description=None, signal=None):  # noqa: ANN001
    handle = MagicMock()
    handle.describe = AsyncMock(
        return_value=description
        or SimpleNamespace(
            id=fixture.workflow_id,
            run_id=str(fixture.run_id),
            status=WorkflowExecutionStatus.RUNNING,
        )
    )
    handle.signal = AsyncMock(side_effect=signal)
    client = MagicMock()
    client.get_workflow_handle.return_value = handle
    return client, handle


async def outbox_row(fixture: OutboxFixture) -> InteractionOutbox:
    async with fixture.sessions() as session:
        row = await session.get(InteractionOutbox, fixture.outbox_id)
        assert row is not None
        return row


def test_signal_contains_no_answer() -> None:
    from src.services.interaction_outbox import question_signal_payload

    assert question_signal_payload("q-id", 2) == ["q-id", 2]


async def test_success_signals_exact_run_then_acks_without_consuming(
    outbox_fixture: OutboxFixture,
) -> None:
    from src.services.interaction_outbox import dispatch_batch

    before = REGISTRY.get_sample_value(
        "interaction_outbox_delivery_total",
        {"outcome": "delivered"},
    ) or 0
    client, handle = temporal_client(outbox_fixture)
    assert await dispatch_batch(outbox_fixture.sessions, client) == 1

    client.get_workflow_handle.assert_called_once_with(
        outbox_fixture.workflow_id,
        run_id=str(outbox_fixture.run_id),
    )
    handle.signal.assert_awaited_once_with(
        "question_available",
        args=[str(outbox_fixture.question_id), 2],
    )
    row = await outbox_row(outbox_fixture)
    assert row.status == "delivered" and row.attempts == 1 and row.delivered_at is not None
    assert REGISTRY.get_sample_value(
        "interaction_outbox_delivery_total",
        {"outcome": "delivered"},
    ) == before + 1
    async with outbox_fixture.sessions() as session:
        question = await session.get(UserQuestion, outbox_fixture.question_id)
        assert question is not None and question.consumed_at is None


async def test_answer_waiting_while_dispatcher_stopped_is_consumed_once_after_start(
    outbox_fixture: OutboxFixture,
) -> None:
    from src.repositories.interaction_repository import InteractionRepository
    from src.services.interaction_outbox import dispatch_batch

    consume_count = 0

    async def consume_on_signal(*args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal consume_count
        async with outbox_fixture.sessions.begin() as session:
            question = await InteractionRepository(outbox_fixture.org_id).consume_question(
                session,
                outbox_fixture.question_id,
            )
            if question.consumed_at is not None:
                consume_count += 1

    client, _ = temporal_client(outbox_fixture, signal=consume_on_signal)
    assert (await outbox_row(outbox_fixture)).status == "pending"
    assert await dispatch_batch(outbox_fixture.sessions, client) == 1
    assert await dispatch_batch(outbox_fixture.sessions, client) == 0
    assert consume_count == 1
    async with outbox_fixture.sessions() as session:
        question = await session.get(UserQuestion, outbox_fixture.question_id)
        assert question is not None and question.consumed_at is not None


async def test_crash_after_send_before_ack_delivers_twice(
    outbox_fixture: OutboxFixture, monkeypatch
) -> None:
    from src.services import interaction_outbox

    client, handle = temporal_client(outbox_fixture)
    original_ack = interaction_outbox._ack_claim
    monkeypatch.setattr(
        interaction_outbox,
        "_ack_claim",
        AsyncMock(side_effect=RuntimeError("simulated process crash")),
    )
    with pytest.raises(RuntimeError, match="simulated process crash"):
        await interaction_outbox.dispatch_batch(outbox_fixture.sessions, client)
    assert handle.signal.await_count == 1
    async with outbox_fixture.sessions.begin() as session:
        await session.execute(
            text(
                "UPDATE interaction_outbox SET lease_until=clock_timestamp()-interval '1 second' "
                "WHERE id=:id"
            ),
            {"id": outbox_fixture.outbox_id},
        )
    monkeypatch.setattr(interaction_outbox, "_ack_claim", original_ack)

    assert await interaction_outbox.dispatch_batch(outbox_fixture.sessions, client) == 1
    assert handle.signal.await_count == 2
    assert (await outbox_row(outbox_fixture)).status == "delivered"


@pytest.mark.parametrize("fault", ["closed", "mismatched"])
async def test_closed_or_mismatched_exact_run_fails_without_retarget(
    outbox_fixture: OutboxFixture, fault: str
) -> None:
    from src.services.interaction_outbox import dispatch_batch

    description = SimpleNamespace(
        id="other" if fault == "mismatched" else outbox_fixture.workflow_id,
        run_id=str(outbox_fixture.run_id),
        status=(
            WorkflowExecutionStatus.RUNNING
            if fault == "mismatched"
            else WorkflowExecutionStatus.COMPLETED
        ),
    )
    client, handle = temporal_client(outbox_fixture, description=description)
    assert await dispatch_batch(outbox_fixture.sessions, client) == 0
    client.get_workflow_handle.assert_called_once_with(
        outbox_fixture.workflow_id,
        run_id=str(outbox_fixture.run_id),
    )
    handle.signal.assert_not_awaited()
    row = await outbox_row(outbox_fixture)
    assert row.status == "failed" and row.error_code == "interaction_delivery_failed"


async def test_network_call_holds_no_outbox_row_lock(outbox_fixture: OutboxFixture) -> None:
    from src.services.interaction_outbox import dispatch_batch

    async def signal_without_db_lock(*args, **kwargs):  # noqa: ANN002, ANN003
        async with outbox_fixture.sessions.begin() as observer:
            await observer.execute(
                select(Conversation)
                .where(Conversation.id == outbox_fixture.conversation_id)
                .with_for_update(nowait=True)
            )
            await observer.execute(
                text("SELECT 1 FROM conversation_identities WHERE id=:id FOR UPDATE NOWAIT"),
                {"id": outbox_fixture.conversation_id},
            )
            await observer.execute(
                select(InteractionOutbox)
                .where(InteractionOutbox.id == outbox_fixture.outbox_id)
                .with_for_update(nowait=True)
            )

    client, handle = temporal_client(outbox_fixture, signal=signal_without_db_lock)
    assert await dispatch_batch(outbox_fixture.sessions, client) == 1
    handle.signal.assert_awaited_once()
    assert (await outbox_row(outbox_fixture)).status == "delivered"


async def test_two_dispatchers_claim_event_once(outbox_fixture: OutboxFixture) -> None:
    from src.services.interaction_outbox import dispatch_batch

    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_signal(*args, **kwargs):  # noqa: ANN002, ANN003
        entered.set()
        await release.wait()

    client, handle = temporal_client(outbox_fixture, signal=slow_signal)
    first = asyncio.create_task(dispatch_batch(outbox_fixture.sessions, client))
    await asyncio.wait_for(entered.wait(), timeout=5)
    second = asyncio.create_task(dispatch_batch(outbox_fixture.sessions, client))
    assert await asyncio.wait_for(second, timeout=5) == 0
    release.set()
    assert await asyncio.wait_for(first, timeout=5) == 1
    assert handle.signal.await_count == 1


async def test_transient_failure_releases_lease_with_capped_backoff(
    outbox_fixture: OutboxFixture,
) -> None:
    from src.services.interaction_outbox import dispatch_batch

    client, _ = temporal_client(
        outbox_fixture,
        signal=RuntimeError("temporal unavailable"),
    )
    before = datetime.now(UTC)
    assert await dispatch_batch(outbox_fixture.sessions, client) == 0
    row = await outbox_row(outbox_fixture)
    assert row.status == "pending" and row.lease_id is None and row.lease_until is None
    assert before < row.available_at <= datetime.now(UTC) + timedelta(seconds=31)


async def test_missing_exact_run_fails_closed_without_retry(
    outbox_fixture: OutboxFixture,
) -> None:
    from src.services.interaction_outbox import dispatch_batch

    client, handle = temporal_client(outbox_fixture)
    handle.describe.side_effect = RPCError(
        "not found",
        RPCStatusCode.NOT_FOUND,
        b"",
    )
    assert await dispatch_batch(outbox_fixture.sessions, client) == 0
    row = await outbox_row(outbox_fixture)
    assert row.status == "failed" and row.error_code == "interaction_delivery_failed"
    handle.signal.assert_not_awaited()


async def test_delivery_stops_after_bounded_settlement_window(
    outbox_fixture: OutboxFixture,
) -> None:
    from src.services.interaction_outbox import dispatch_batch

    async with outbox_fixture.sessions.begin() as session:
        await session.execute(
            text(
                "UPDATE user_questions "
                "SET created_at=clock_timestamp()-interval '2 hours', "
                "expires_at=clock_timestamp()-interval '1 hour' WHERE id=:id"
            ),
            {"id": outbox_fixture.question_id},
        )
    client, handle = temporal_client(outbox_fixture)
    assert await dispatch_batch(outbox_fixture.sessions, client) == 0
    assert (await outbox_row(outbox_fixture)).status == "failed"
    client.get_workflow_handle.assert_not_called()
    handle.signal.assert_not_awaited()


async def test_dispatcher_works_under_forced_rls_application_role(
    outbox_fixture: OutboxFixture,
) -> None:
    from src.services.interaction_outbox import dispatch_batch

    class AppRoleSession(Session):
        pass

    @event.listens_for(AppRoleSession, "after_begin")
    def set_app_role(session, transaction, connection):  # noqa: ANN001
        connection.execute(text("SET LOCAL ROLE aurum_ai_app"))

    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    restricted_sessions = async_sessionmaker(
        engine,
        class_=AsyncSession,
        sync_session_class=AppRoleSession,
        expire_on_commit=False,
    )
    try:
        client, handle = temporal_client(outbox_fixture)
        assert await dispatch_batch(restricted_sessions, client) == 1
        handle.signal.assert_awaited_once()
        assert (await outbox_row(outbox_fixture)).status == "delivered"
    finally:
        await engine.dispose()
