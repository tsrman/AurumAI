"""Committed PostgreSQL transactions: budgets, retries and terminal-state races."""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.config import settings
from src.models.conversation import Conversation
from src.models.interaction import (
    InteractionExecution,
    InteractionOutbox,
    InteractionTurn,
    ToolOperation,
    UserQuestion,
)
from src.models.organization import Organization
from src.models.user import User
from src.schemas.interaction import AnswerRequest, QuestionRequest, TurnLimits


@dataclass
class Fixture:
    sessions: async_sessionmaker[AsyncSession]
    org: UUID
    owner: UUID
    turn: UUID
    executions: list[UUID]


@pytest.fixture
async def owned():
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    org, owner, conv, turn = (uuid4() for _ in range(4))
    executions = [uuid4() for _ in range(6)]
    try:
        async with sessions.begin() as db:
            db.add(Organization(id=org, name="Question repository test", slug=f"qrepo-{org}"))
            await db.flush()
            db.add(User(id=owner, org_id=org, keycloak_id=uuid4(), email=f"{owner}@example.test"))
            await db.flush()
            db.add(Conversation(id=conv, org_id=org, user_id=owner, model="fixture"))
            await db.flush()
            db.add(
                InteractionTurn(
                    id=turn,
                    org_id=org,
                    conversation_id=conv,
                    owner_id=owner,
                    limits=TurnLimits().model_dump(),
                )
            )
            await db.flush()
            for index, execution in enumerate(executions):
                db.add(
                    InteractionExecution(
                        id=execution,
                        org_id=org,
                        turn_id=turn,
                        workflow_id="repeated-child-id",
                        run_id=uuid4(),
                        task_queue="fixture",
                        phase="node",
                        node_key="alpha",
                        iteration=index + 1,
                    )
                )
        yield Fixture(sessions, org, owner, turn, executions)
    finally:
        async with sessions.begin() as db:
            # Only this committed test fixture; no production cleanup helper.
            await db.execute(delete(InteractionTurn).where(InteractionTurn.id == turn))
            await db.execute(delete(Conversation).where(Conversation.id == conv))
            await db.execute(delete(User).where(User.id == owner))
            await db.execute(delete(Organization).where(Organization.id == org))
        await engine.dispose()


def repository(owned):
    from src.repositories.interaction_repository import InteractionRepository

    return InteractionRepository(owned.org)


def fingerprint(request):
    # Public canonical JSON contract, independent of production helper.
    return hashlib.sha256(
        json.dumps(
            request.model_dump(by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


async def create(owned, execution=0, invocation=None, request=None):
    repo = repository(owned)
    request = request or QuestionRequest(text="Which city?")
    async with owned.sessions.begin() as db:
        op = await repo.reserve_operation(
            db, owned.executions[execution], invocation or uuid4(), "ask_user", fingerprint(request)
        )
        q = await repo.create_question(db, op.id, request)
        return q


async def rows(owned, model):
    async with owned.sessions() as db:
        return list((await db.scalars(select(model).where(model.org_id == owned.org))).all())


async def test_creation_retry_keeps_identity_deadline_and_counter(owned):
    invocation = uuid4()
    first = await create(owned, invocation=invocation)
    second = await create(owned, invocation=invocation)
    assert second.id == first.id and second.expires_at == first.expires_at
    assert (second.expires_at - second.created_at).total_seconds() == 3600
    assert (await rows(owned, InteractionTurn))[0].question_count == 1
    assert len(await rows(owned, InteractionOutbox)) == 1
    assert len(await rows(owned, ToolOperation)) == 1


async def test_feature_off_blocks_only_new_question_reservations(owned):
    repo = repository(owned)
    request = QuestionRequest(text="Which city?")
    invocation = uuid4()
    async with owned.sessions.begin() as db:
        first = await repo.reserve_operation(
            db,
            owned.executions[0],
            invocation,
            "ask_user",
            fingerprint(request),
        )
        retry = await repo.reserve_operation(
            db,
            owned.executions[0],
            invocation,
            "ask_user",
            fingerprint(request),
            allow_new=False,
        )
        assert retry.id == first.id
        with pytest.raises(ValueError, match="user_input_disabled"):
            await repo.reserve_operation(
                db,
                owned.executions[1],
                uuid4(),
                "ask_user",
                fingerprint(request),
                allow_new=False,
            )

    assert (await rows(owned, InteractionTurn))[0].question_count == 1
    assert len(await rows(owned, ToolOperation)) == 1


async def test_changed_duplicate_arguments_are_conflict_without_side_effect(owned):
    invocation = uuid4()
    await create(owned, invocation=invocation)
    with pytest.raises(ValueError, match="interaction_conflict"):
        await create(owned, invocation=invocation, request=QuestionRequest(text="Different?"))
    assert (await rows(owned, InteractionTurn))[0].question_count == 1


async def test_one_open_per_execution_and_four_per_turn(owned):
    await create(owned)
    with pytest.raises(ValueError, match="user_input_limit"):
        await create(owned)
    for index in (1, 2, 3):
        await create(owned, execution=index)
    with pytest.raises(ValueError, match="user_input_limit"):
        await create(owned, execution=4)
    assert len(await rows(owned, UserQuestion)) == 4
    assert (await rows(owned, InteractionTurn))[0].question_count == 4


async def test_total_budget_survives_closed_questions_and_new_execution(owned):
    repo = repository(owned)
    for index in range(10):
        q = await create(owned, execution=index % 6)
        async with owned.sessions.begin() as db:
            await repo.answer_question(db, q.id, owned.owner, uuid4(), AnswerRequest(text="A"))
    with pytest.raises(ValueError, match="user_input_limit"):
        await create(owned, execution=5)
    assert (await rows(owned, InteractionTurn))[0].question_count == 10


@pytest.mark.parametrize(
    "kind,limit,counter",
    [
        ("web_search", 10, "search_count"),
        ("web_read", 20, "read_count"),
    ],
)
async def test_generic_operation_budget_cannot_reset_on_retry_or_sibling(
    owned, kind, limit, counter
):
    repo = repository(owned)
    for index in range(limit):
        invocation = uuid4()
        async with owned.sessions.begin() as db:
            first = await repo.reserve_operation(
                db, owned.executions[index % 6], invocation, kind, "a" * 64
            )
            retry = await repo.reserve_operation(
                db, owned.executions[index % 6], invocation, kind, "a" * 64
            )
            assert first.id == retry.id
    with pytest.raises(ValueError, match="tool_limit"):
        async with owned.sessions.begin() as db:
            await repo.reserve_operation(db, owned.executions[0], uuid4(), kind, "b" * 64)
    assert getattr((await rows(owned, InteractionTurn))[0], counter) == limit


async def test_answer_and_delivery_are_atomic_idempotent_and_owner_only(owned):
    repo = repository(owned)
    q = await create(
        owned, request=QuestionRequest(text="Where?", options=[{"id": "a", "label": "North"}])
    )
    key = uuid4()
    with pytest.raises(ValueError, match="interaction_not_found"):
        async with owned.sessions.begin() as db:
            await repo.answer_question(db, q.id, uuid4(), key, AnswerRequest(text="intruder"))
    with pytest.raises(ValueError, match="invalid_answer"):
        async with owned.sessions.begin() as db:
            await repo.answer_question(
                db, q.id, owned.owner, key, AnswerRequest(option_id="missing")
            )
    async with owned.sessions.begin() as db:
        answer = await repo.answer_question(
            db, q.id, owned.owner, key, AnswerRequest(option_id="a")
        )
    async with owned.sessions.begin() as db:
        retry = await repo.answer_question(db, q.id, owned.owner, key, AnswerRequest(option_id="a"))
        assert retry.answered_at == answer.answered_at and retry.version == 2
    assert retry.answer == {"optionId": "a"}
    events = await rows(owned, InteractionOutbox)
    assert sorted((e.event_kind, e.question_version) for e in events) == [
        ("question_available", 2),
        ("question_status", 1),
        ("question_status", 2),
    ]
    for changed_key, payload in [
        (uuid4(), AnswerRequest(option_id="a")),
        (key, AnswerRequest(text="changed")),
    ]:
        with pytest.raises(ValueError, match="interaction_conflict"):
            async with owned.sessions.begin() as db:
                await repo.answer_question(db, q.id, owned.owner, changed_key, payload)


async def test_rollback_does_not_publish_an_answer(owned):
    repo = repository(owned)
    q = await create(owned)
    async with owned.sessions() as db:
        await repo.answer_question(db, q.id, owned.owner, uuid4(), AnswerRequest(text="discard"))
        await db.rollback()
    assert (await rows(owned, UserQuestion))[0].status == "pending"
    assert len(await rows(owned, InteractionOutbox)) == 1


@pytest.mark.parametrize("clock_delta", [-1, 0, 1])
async def test_consume_preserves_causal_timestamp_and_retry_identity(owned, monkeypatch, clock_delta):
    repo = repository(owned)
    q = await create(owned)
    async with owned.sessions.begin() as db:
        answered = await repo.answer_question(
            db, q.id, owned.owner, uuid4(), AnswerRequest(text="accepted")
        )
    answered_at = answered.answered_at
    observed = answered_at + timedelta(seconds=clock_delta)

    async def clock(_session):
        return observed

    # Only the clock boundary is substituted; real row locks, UPDATE, commit
    # and ck_user_question_consumed must accept the causal timestamp.
    monkeypatch.setattr(repo, "_now", clock)
    async with owned.sessions.begin() as db:
        await repo.consume_question(db, q.id)
    saved = (await rows(owned, UserQuestion))[0]
    expected = answered_at if clock_delta <= 0 else answered_at + timedelta(seconds=1)
    assert saved.consumed_at == expected
    assert saved.answered_at == answered_at
    assert saved.answer == {"text": "accepted"}
    assert saved.expires_at == q.expires_at
    observed = answered_at + timedelta(seconds=60)
    async with owned.sessions.begin() as db:
        await repo.consume_question(db, q.id)
    assert (await rows(owned, UserQuestion))[0].consumed_at == expected
    assert len(await rows(owned, InteractionOutbox)) == 3


async def overdue(owned, question):
    async with owned.sessions.begin() as db:
        await db.execute(
            text(
                "UPDATE user_questions SET created_at=clock_timestamp()-interval '2 hours', "
                "expires_at=clock_timestamp()-interval '1 hour' WHERE id=:id"
            ),
            {"id": question.id},
        )


async def test_timeout_never_overwrites_already_accepted_answer(owned):
    repo = repository(owned)
    q = await create(owned)
    async with owned.sessions.begin() as db:
        await repo.answer_question(db, q.id, owned.owner, uuid4(), AnswerRequest(text="accepted"))
    await overdue(owned, q)
    async with owned.sessions.begin() as db:
        result = await repo.expire_question(db, q.id)
        assert result.status == "answered" and result.answer == {"text": "accepted"}
    assert len(await rows(owned, InteractionOutbox)) == 3


async def test_expiry_is_idempotent_and_blocks_late_answer(owned):
    repo = repository(owned)
    q = await create(owned)
    async with owned.sessions.begin() as db:
        assert (await repo.expire_question(db, q.id)).status == "pending"
    await overdue(owned, q)
    with pytest.raises(ValueError, match="user_input_timeout"):
        async with owned.sessions.begin() as db:
            await repo.answer_question(db, q.id, owned.owner, uuid4(), AnswerRequest(text="late"))
    for _ in range(2):
        async with owned.sessions.begin() as db:
            assert (await repo.expire_question(db, q.id)).status == "expired"
    assert len(await rows(owned, InteractionOutbox)) == 3
    op = (await rows(owned, ToolOperation))[0]
    assert op.status == "failed" and op.error_code == "user_input_timeout"


async def test_cancel_only_own_turn_preserves_accepted_answer_and_stops_pending(owned):
    repo = repository(owned)
    answered = await create(owned)
    pending = await create(owned, execution=1)
    async with owned.sessions.begin() as db:
        await repo.answer_question(
            db, answered.id, owned.owner, uuid4(), AnswerRequest(text="keep")
        )
    for _ in range(2):
        async with owned.sessions.begin() as db:
            await repo.cancel_turn(db, owned.turn)
    states = {q.id: q.status for q in await rows(owned, UserQuestion)}
    assert states == {answered.id: "answered", pending.id: "cancelled"}
    assert (await rows(owned, InteractionTurn))[0].status == "cancelled"
    with pytest.raises(ValueError, match="user_input_cancelled"):
        async with owned.sessions.begin() as db:
            await repo.answer_question(
                db, pending.id, owned.owner, uuid4(), AnswerRequest(text="late")
            )
    with pytest.raises(ValueError, match="user_input_cancelled"):
        await create(owned, execution=2)


async def test_explicit_org_scope_even_when_test_session_bypasses_rls(owned):
    from src.repositories.interaction_repository import InteractionRepository

    foreign = InteractionRepository(uuid4())
    q = await create(owned)
    async with owned.sessions.begin() as db:
        for action in [
            foreign.reserve_operation(db, owned.executions[0], uuid4(), "ask_user", "a" * 64),
            foreign.create_question(db, q.operation_id, QuestionRequest(text="forged")),
            foreign.answer_question(db, q.id, owned.owner, uuid4(), AnswerRequest(text="forged")),
            foreign.expire_question(db, q.id),
            foreign.cancel_turn(db, owned.turn),
        ]:
            with pytest.raises(ValueError, match="interaction_not_found"):
                await action
    assert (await rows(owned, UserQuestion))[0].status == "pending"


@pytest.mark.parametrize("repeat", range(20))
async def test_concurrent_duplicate_creation_is_one_question(owned, repeat):
    invocation = uuid4()
    results = await asyncio.wait_for(
        asyncio.gather(
            create(owned, invocation=invocation),
            create(owned, invocation=invocation),
        ),
        timeout=10,
    )
    assert results[0].id == results[1].id
    assert (await rows(owned, InteractionTurn))[0].question_count == 1
    assert len(await rows(owned, InteractionOutbox)) == 1


async def test_concurrent_first_turns_are_serialized_on_parent_conversation(owned) -> None:
    from src.core.authorization_types import AuthorizationSubject
    from src.services.interaction import InteractionConflict, InteractionService

    conversation_id, first_turn_id, second_turn_id = uuid4(), uuid4(), uuid4()
    subject = AuthorizationSubject(owned.owner, owned.org)
    service = InteractionService()
    try:
        async with owned.sessions.begin() as setup:
            setup.add(
                Conversation(
                    id=conversation_id,
                    org_id=owned.org,
                    user_id=owned.owner,
                    model="fixture",
                )
            )

        first = owned.sessions()
        await first.begin()
        await service.begin_turn(first, subject, conversation_id, first_turn_id, None)

        async def contend() -> str:
            try:
                async with owned.sessions.begin() as second:
                    await service.begin_turn(
                        second,
                        subject,
                        conversation_id,
                        second_turn_id,
                        None,
                    )
                return "created"
            except InteractionConflict:
                return "conflict"

        competing = asyncio.create_task(contend())
        await asyncio.sleep(0.1)
        assert not competing.done()
        await first.commit()
        await first.close()

        assert await asyncio.wait_for(competing, timeout=10) == "conflict"
        async with owned.sessions() as observer:
            turns = list(
                (
                    await observer.scalars(
                        select(InteractionTurn).where(
                            InteractionTurn.conversation_id == conversation_id
                        )
                    )
                ).all()
            )
        assert [turn.id for turn in turns] == [first_turn_id]
    finally:
        async with owned.sessions.begin() as cleanup:
            await cleanup.execute(
                delete(InteractionTurn).where(InteractionTurn.conversation_id == conversation_id)
            )
            await cleanup.execute(delete(Conversation).where(Conversation.id == conversation_id))


@pytest.mark.parametrize("repeat", range(20))
async def test_competing_answer_and_cancel_never_leave_pending(owned, repeat):
    repo = repository(owned)
    q = await create(owned)

    async def answer():
        try:
            async with owned.sessions.begin() as db:
                await repo.answer_question(db, q.id, owned.owner, uuid4(), AnswerRequest(text="A"))
            return "answered"
        except ValueError as exc:
            assert str(exc) == "user_input_cancelled"
            return "cancelled"

    async def cancel():
        async with owned.sessions.begin() as db:
            await repo.cancel_turn(db, owned.turn)

    result, _ = await asyncio.wait_for(asyncio.gather(answer(), cancel()), timeout=10)
    saved = (await rows(owned, UserQuestion))[0]
    assert saved.status == result
    assert (await rows(owned, InteractionTurn))[0].status == "cancelled"


async def test_transaction_started_before_deadline_cannot_answer_after_lock_wait(owned):
    repo = repository(owned)
    q = await create(owned)
    async with owned.sessions() as waiter:
        # Start this transaction before expiry is moved: transaction_timestamp is now stale.
        await waiter.execute(select(func.now()))
        pid = await waiter.scalar(select(func.pg_backend_pid()))
        # The identity map must not hide the newly committed deadline either.
        stale = await waiter.get(UserQuestion, q.id)
        assert stale.expires_at == q.expires_at
        async with owned.sessions.begin() as locker:
            # Lifecycle admission requires the conversation lock before a turn
            # lock. The competing answer still starts before the deadline update
            # and must observe its committed value after waiting.
            from src.services.conversation_lifecycle import lock_active_conversation
            conversation_id = await locker.scalar(select(InteractionTurn.conversation_id).where(InteractionTurn.id == owned.turn))
            await lock_active_conversation(locker, owned.org, conversation_id)
            await locker.execute(
                select(InteractionTurn).where(InteractionTurn.id == owned.turn).with_for_update()
            )
            task = asyncio.create_task(
                repo.answer_question(waiter, q.id, owned.owner, uuid4(), AnswerRequest(text="late"))
            )

            async def wait_until_blocked():
                async with owned.sessions() as monitor:
                    while True:
                        blocked = await monitor.scalar(
                            text(
                                "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid=:pid"
                            ),
                            {"pid": pid},
                        )
                        await monitor.rollback()  # refresh PostgreSQL statistics snapshot
                        if blocked:
                            return

            await asyncio.wait_for(wait_until_blocked(), timeout=10)
            # A committed deadline change must be seen after acquiring the turn lock.
            await locker.execute(
                text(
                    "UPDATE user_questions SET created_at=clock_timestamp()-interval '2 hours', "
                    "expires_at=clock_timestamp() WHERE id=:id"
                ),
                {"id": q.id},
            )
        with pytest.raises(ValueError, match="user_input_timeout"):
            await asyncio.wait_for(task, timeout=10)
        await waiter.rollback()


@pytest.mark.parametrize("budget", ["execution", "open", "total"])
async def test_competing_creations_cannot_take_the_same_last_slot(owned, budget):
    repo = repository(owned)
    if budget == "open":
        for index in range(3):
            await create(owned, execution=index)
        targets, expected = [3, 4], 4
    elif budget == "total":
        for _ in range(9):
            q = await create(owned)
            async with owned.sessions.begin() as db:
                await repo.answer_question(db, q.id, owned.owner, uuid4(), AnswerRequest(text="A"))
        targets, expected = [1, 2], 10
    else:
        targets, expected = [0, 0], 1

    async def attempt(index):
        try:
            await create(owned, execution=index)
            return "created"
        except ValueError as exc:
            assert str(exc) == "user_input_limit"
            return "limited"

    outcomes = await asyncio.wait_for(
        asyncio.gather(*(attempt(index) for index in targets)), timeout=10
    )
    assert sorted(outcomes) == ["created", "limited"]
    assert (await rows(owned, InteractionTurn))[0].question_count == expected
    assert len(await rows(owned, UserQuestion)) == expected
    assert len(await rows(owned, ToolOperation)) == expected


async def test_competing_distinct_answers_persist_one_winner_and_one_notification(owned):
    repo = repository(owned)
    q = await create(owned)

    async def answer(value):
        try:
            async with owned.sessions.begin() as db:
                await repo.answer_question(
                    db, q.id, owned.owner, uuid4(), AnswerRequest(text=value)
                )
            return value
        except ValueError as exc:
            assert str(exc) == "interaction_conflict"
            return None

    outcomes = await asyncio.wait_for(asyncio.gather(answer("A"), answer("B")), timeout=10)
    winners = [value for value in outcomes if value is not None]
    assert len(winners) == 1
    saved = (await rows(owned, UserQuestion))[0]
    assert saved.answer == {"text": winners[0]} and saved.version == 2
    events = await rows(owned, InteractionOutbox)
    assert len(events) == 3
    assert sum(event.event_kind == "question_available" for event in events) == 1


@pytest.mark.parametrize("expired", [False, True])
async def test_competing_answer_and_expiry_preserve_terminal_winner(owned, expired):
    repo = repository(owned)
    q = await create(owned)
    if expired:
        await overdue(owned, q)

    async def answer():
        try:
            async with owned.sessions.begin() as db:
                await repo.answer_question(db, q.id, owned.owner, uuid4(), AnswerRequest(text="A"))
            return "answered"
        except ValueError as exc:
            assert str(exc) == "user_input_timeout"
            return "expired"

    async def expire():
        async with owned.sessions.begin() as db:
            await repo.expire_question(db, q.id)

    result, _ = await asyncio.wait_for(asyncio.gather(answer(), expire()), timeout=10)
    saved = (await rows(owned, UserQuestion))[0]
    assert saved.status == result == ("expired" if expired else "answered")
    assert saved.version == 2
    assert saved.answer == (None if expired else {"text": "A"})
    assert len(await rows(owned, InteractionOutbox)) == 3


async def test_rollback_creation_removes_question_operation_counter_and_outbox(owned):
    repo = repository(owned)
    request = QuestionRequest(text="Which city?")
    async with owned.sessions() as db:
        op = await repo.reserve_operation(
            db, owned.executions[0], uuid4(), "ask_user", fingerprint(request)
        )
        await repo.create_question(db, op.id, request)
        await db.rollback()
    assert (await rows(owned, InteractionTurn))[0].question_count == 0
    for model in (UserQuestion, ToolOperation, InteractionOutbox):
        assert await rows(owned, model) == []


async def test_cancel_does_not_touch_another_active_turn_in_same_organization(owned):
    repo = repository(owned)
    first = await create(owned)
    conv, turn, execution = uuid4(), uuid4(), uuid4()
    try:
        async with owned.sessions.begin() as db:
            db.add(Conversation(id=conv, org_id=owned.org, user_id=owned.owner, model="fixture"))
            await db.flush()
            db.add(
                InteractionTurn(
                    id=turn,
                    org_id=owned.org,
                    conversation_id=conv,
                    owner_id=owned.owner,
                    limits=TurnLimits().model_dump(),
                )
            )
            await db.flush()
            db.add(
                InteractionExecution(
                    id=execution,
                    org_id=owned.org,
                    turn_id=turn,
                    workflow_id="neighbor",
                    run_id=uuid4(),
                    task_queue="fixture",
                    phase="node",
                    node_key="alpha",
                )
            )
        neighbor = Fixture(owned.sessions, owned.org, owned.owner, turn, [execution])
        second = await create(neighbor)
        async with owned.sessions.begin() as db:
            await repo.cancel_turn(db, owned.turn)
        states = {q.id: q.status for q in await rows(owned, UserQuestion)}
        assert states == {first.id: "cancelled", second.id: "pending"}
        turns = {t.id: t.status for t in await rows(owned, InteractionTurn)}
        assert turns == {owned.turn: "cancelled", turn: "active"}
        async with owned.sessions.begin() as db:
            assert (
                await repo.answer_question(
                    db, second.id, owned.owner, uuid4(), AnswerRequest(text="still active")
                )
            ).status == "answered"
    finally:
        async with owned.sessions.begin() as db:
            await db.execute(delete(InteractionTurn).where(InteractionTurn.id == turn))
            await db.execute(delete(Conversation).where(Conversation.id == conv))


async def test_repository_lifecycle_works_under_forced_rls_application_role(owned):
    repo = repository(owned)
    request = QuestionRequest(text="Which city?")
    async with owned.sessions.begin() as db:
        await db.execute(text("SET LOCAL ROLE aurum_ai_app"))
        await db.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(owned.org)}
        )
        op = await repo.reserve_operation(
            db, owned.executions[0], uuid4(), "ask_user", fingerprint(request)
        )
        q = await repo.create_question(db, op.id, request)
        await repo.answer_question(db, q.id, owned.owner, uuid4(), AnswerRequest(text="Bangkok"))
        await repo.cancel_turn(db, owned.turn)
    saved = (await rows(owned, UserQuestion))[0]
    assert saved.status == "answered" and saved.answer == {"text": "Bangkok"}
    assert (await rows(owned, InteractionTurn))[0].status == "cancelled"
    assert len(await rows(owned, InteractionOutbox)) == 3
